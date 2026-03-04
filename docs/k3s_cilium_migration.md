**Agentic Traffic Testbed**

**Observability Migration Plan**

Docker Compose + Prometheus/Grafana/cAdvisor → k3s + Cilium + Hubble

**Document purpose**

This document describes why we are migrating the testbed observability
stack, what the target architecture looks like, and a phased plan for
getting there. It is written for engineers familiar with the existing
repo and serves as both a rationale document and a practical checklist.

**1. Why We Are Migrating**

The current stack is functional but has several pain points that grow
worse as the testbed matures. The core issues are described below.

**1.1 The Raw-ID Problem**

cAdvisor exposes container-level network metrics using kernel and Docker
internal identifiers rather than human-readable names. In practice this
means:

-   **Docker bridge names** like **br-df4088ff2909** appear in every
    network panel instead of **inter_agent_network**.

-   **Systemd cgroup paths** like
    **/system.slice/docker-a86eb27d...scope** appear instead of
    **agent-a** or **llm-backend**.

-   PromQL cannot resolve these to service names at query time --- there
    is no join mechanism between Prometheus metrics and Docker metadata.

-   Result: dashboards are unreadable without manually cross-referencing
    bridge IDs and container IDs in a separate shell.

**1.2 The TCP Collector is Fragile**

To work around the labelling problem, the repo ships a custom
tcp_metrics_collector.py that:

-   Runs tcpdump on the host bridge interface (requires sudo).

-   Maintains a static SERVICE_IPS mapping from container IP to service
    name.

-   Exposes a Prometheus endpoint on port 9100.

**The fundamental problem:** container IPs change on every restart. The
static mapping silently becomes stale, producing incorrect or missing
labels with no error surfaced in Grafana. The collector is also a single
point of failure that lives outside Docker Compose and has to be started
separately, sometimes interactively.

**1.3 No Per-Container Network Breakdown**

Even when cAdvisor does expose per-container metrics (which requires
cgroups v2 and correct host volume mounts), the network metrics are
per-bridge aggregates, not per-container. Multiple containers share the
same bridge, so it is structurally impossible to get a per-service
network view from cAdvisor alone.

**1.4 The Research Goal Demands Service-Level Fidelity**

The central question this testbed is answering --- how do agentic
workloads differ from non-agentic workloads at L3/L4 --- requires
precise, per-service-pair metrics: bytes and packets between agent-a and
llm-backend, RTT distributions for agent-b-3 to mcp-tool-db, and so on.
The current stack can only approximate this through the fragile TCP
collector.

Kubernetes with Cilium solves all of these problems natively. Pod
identity is first-class; every metric carries pod, namespace, and
service labels automatically. This is already called out as Phase 2 in
the repo roadmap.

**2. Target Architecture**

**2.1 Component Summary**

  ----------------- --------------------------- ---------------------------
  **Layer**         **Current**                 **Target**

  Orchestration     Docker Compose (single      k3s on K3S_NODE_HOST
                    host)                       (value in infra/.env)

  LLM backend       Same host as agents         saturn.cba.upc.edu
                                                (unchanged, remote)

  Networking / CNI  Default bridge + iptables   Cilium (eBPF-native CNI)

  Network           cAdvisor +                  Hubble (built into Cilium)
  observability     tcp_metrics_collector.py    

  Metrics storage   Prometheus                  Prometheus (unchanged)

  Dashboards        Grafana                     Grafana (unchanged)

  Distributed       Jaeger (existing)           Jaeger (existing, added as
  tracing                                       Grafana datasource)

  OTel              Existing in agents          Existing (no changes
  instrumentation                               needed)

  Container metrics cAdvisor                    kube-state-metrics (via
                                                Helm)

  LLM metrics       llm-backend /metrics        llm-backend /metrics
                                                scraped remotely by
                                                Prometheus
  ----------------- --------------------------- ---------------------------

**2.2 Split-Host Architecture**

Rather than running everything on one machine, the target deployment
splits responsibilities across two servers:

  ------------------------ ----------------------------------------------
  **Server**               **Role**

  **K3S_NODE_HOST**        k3s cluster --- agents, MCP tools, baseline
                           service, Prometheus, Grafana, Hubble, Jaeger

  **saturn.cba.upc.edu**   LLM backend --- vLLM serving the local model,
                           exposed on port 8000
  ------------------------ ----------------------------------------------

This is a natural split. The LLM backend is GPU-bound and lives on
Saturn where the GPU is. The agent and observability workloads are
CPU-bound and live on the k3s server. The agents reach Saturn over the
university network via a single environment variable:

> LLM_SERVER_URL=http://saturn.cba.upc.edu:8000/chat

+-----------------------------------------------------------------------+
| **Research implication**                                              |
|                                                                       |
| With this split, agent-to-LLM calls cross a real network hop rather   |
| than a loopback. Hubble will record these as egress flows leaving the |
| cluster to an external IP. This is more representative of a real      |
| distributed deployment and adds an interesting dimension to the       |
| traffic analysis --- you can observe the actual network cost of LLM   |
| calls, not just the logical cost.                                     |
+-----------------------------------------------------------------------+

**Hubble visibility boundary**

It is worth being explicit about what Hubble can and cannot see in this
architecture:

-   Intra-cluster flows (agent-a to agent-b, agent-a to mcp-tool-db,
    etc.) --- full service-pair labelling, RTT, flags, drop reasons.

-   Egress to Saturn (any agent to llm-backend) --- Hubble sees the
    packet leave the node but labels the destination as an external IP
    (the Saturn address), not as a named service. You will see bytes and
    flow counts for this traffic but not the friendly llm-backend label.

-   LLM performance metrics (latency, TTFT, tokens/s) --- still fully
    available via Prometheus scraping Saturn\'s /metrics endpoint
    directly. These are application-layer metrics and are unaffected by
    the network boundary.

If full Hubble visibility of agent-to-LLM flows becomes important later,
the LLM backend can be moved into the k3s cluster if the GPU is
accessible from that node. For now the remote setup is the right
pragmatic choice.

**2.3 What the k3s Observability Node Gives Us**

k3s is a fully-conformant, CNCF-certified Kubernetes distribution
packaged as a single binary. It is designed for resource-constrained
single-node or small-cluster deployments --- exactly the single GPU
server scenario here. Key properties:

-   Runs as a single process; default install takes roughly 512 MB RAM
    at idle.

-   Ships with containerd, CoreDNS, Traefik (ingress), and local-path
    provisioner out of the box.

-   Full kubectl / Helm compatibility --- anything that works on full
    K8s works on k3s.

-   GPU workloads are supported via the NVIDIA device plugin, the same
    as standard Kubernetes.

-   Flannel is the default CNI but can be replaced with Cilium during
    install, which is exactly what we will do.

**2.4 What Cilium Gives Us**

Cilium replaces the default Linux bridge + iptables networking with eBPF
programs compiled directly into the kernel. This is significant for this
project because:

-   Every packet passing between pods is processed by a Cilium eBPF
    program that already knows the source and destination pod identity.

-   There is no need for IP-to-service mapping --- the identity is baked
    in at the kernel level.

-   Network policies, load balancing, and service discovery all run in
    eBPF, removing iptables entirely and reducing per-packet overhead.

**2.5 What Hubble Gives Us**

Hubble is the observability layer built into Cilium. It taps the same
eBPF programs and exports flow-level metrics and logs with service-level
labels. What this means in practice:

-   Metrics like tcp_bytes_total are emitted with source and destination
    labelled by Kubernetes service name, not IP address.

-   Flow duration histograms, packet counts, dropped packets, and
    connection state (SYN/FIN/RST) are all available natively.

-   Labels survive pod restarts because they are derived from Kubernetes
    service identity, not container IP.

-   A Prometheus metrics endpoint is built in --- it plugs directly into
    the existing Prometheus scrape config.

-   The Hubble UI and Hubble CLI give an interactive flow explorer that
    replaces tcpdump for ad-hoc investigation.

+-----------------------------------------------------------------------+
| **Key distinction from the current TCP collector**                    |
|                                                                       |
| tcp_metrics_collector.py captures packets on a bridge and guesses     |
| service identity from a static IP map. Hubble knows service identity  |
| before the packet is even processed because Cilium assigned that      |
| identity when the pod started. There is no map to maintain and no     |
| staleness to worry about.                                             |
+-----------------------------------------------------------------------+

**2.6 Jaeger in Grafana**

Jaeger already exists in the testbed and the agents already emit
OpenTelemetry traces with TaskID, AgentID, and ToolCallID as span
attributes. The only missing piece is wiring Jaeger as a Grafana
datasource. Once done:

-   Grafana can correlate a network metric spike (from
    Hubble/Prometheus) with the traces active at that timestamp.

-   Clicking a time window in a Grafana panel can jump directly to
    Jaeger traces from that period.

-   The full end-to-end story --- task arrives, agents fan out, LLM is
    called, tools are invoked, response is synthesised --- becomes
    visible as a single trace alongside its network footprint.

**3. Migration Plan**

The migration is structured into four phases. Phases 1 and 2 can be done
in parallel with the existing Docker Compose stack on the same machine,
since k3s runs independently. The existing stack can remain running
until Phase 3.

**Phase 1 --- Pre-flight Checks and k3s Install with Cilium**

+-----------------------------------------------------------------------+
| **Goal**                                                              |
|                                                                       |
| A running single-node k3s cluster with Cilium as the CNI. No          |
| workloads yet.                                                        |
+-----------------------------------------------------------------------+

**Step 1.0 --- Verify network reachability to Saturn**

Before installing anything, confirm that the k3s server can reach the
LLM backend on Saturn. Run this from the k3s node (`K3S_NODE_HOST` in
`infra/.env`):

> curl -v http://saturn.cba.upc.edu:8000/health

If this fails, the LLM backend port may be firewalled. Options to
resolve:

-   Ask the Saturn admin to open port 8000 to the k3s node
    (`K3S_NODE_HOST`).

-   Use an SSH tunnel as a temporary workaround: ssh -L
    8000:localhost:8000 user@saturn.cba.upc.edu

-   If both servers are on the UPC internal network, a firewall rule may
    just need to be added between subnets.

Do not proceed past this step until the curl succeeds --- everything
downstream depends on it.

**Step 1.1 --- Disable the default Flannel CNI and install k3s**

k3s must be installed with Flannel disabled so Cilium can take its
place. Run on the host:

> curl -sfL https://get.k3s.io \|
> INSTALL_K3S_EXEC=\'\--flannel-backend=none \--disable-network-policy
> \--disable=traefik\' sh -

The flags disable Flannel and the built-in network policy controller,
both of which Cilium will replace. Traefik is optional to keep or
disable depending on whether you want k3s ingress.

**Step 1.2 --- Export kubeconfig**

> mkdir -p \~/.kube
>
> sudo cp /etc/rancher/k3s/k3s.yaml \~/.kube/config
>
> sudo chown \$(id -u):\$(id -g) \~/.kube/config
>
> kubectl get nodes \# should show one node in NotReady state (no CNI
> yet)

**Step 1.3 --- Install Cilium via Helm**

> helm repo add cilium https://helm.cilium.io/
>
> helm repo update
>
> helm install cilium cilium/cilium \--version 1.16.0 \\
>
> \--namespace kube-system \\
>
> \--set kubeProxyReplacement=true \\
>
> \--set k8sServiceHost=127.0.0.1 \\
>
> \--set k8sServicePort=6443 \\
>
> \--set hubble.relay.enabled=true \\
>
> \--set hubble.ui.enabled=true \\
>
> \--set hubble.metrics.enableOpenMetrics=true \\
>
> \--set
> hubble.metrics.enabled=\'{dns,drop,tcp,flow,port-distribution,icmp,httpV2}\'

**Note:** Check the latest stable Cilium version compatible with your
kernel before running. Cilium 1.15+ requires Linux kernel 4.19+. Run
**uname -r** to check.

**Step 1.4 --- Verify Cilium and Hubble**

> cilium status \--wait
>
> cilium connectivity test \# optional but recommended first time
>
> hubble status

The node should now show Ready in kubectl get nodes.

**Step 1.5 --- Install NVIDIA device plugin (GPU access in pods)**

> kubectl create -f
> https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.16.0/deployments/static/nvidia-device-plugin.yml

**Why:** The LLM backend (vLLM) needs GPU access. The device plugin
exposes NVIDIA GPUs as a Kubernetes resource (**nvidia.com/gpu**) that
pods can request.

**Phase 2 --- Deploy the Observability Stack**

+-----------------------------------------------------------------------+
| **Goal**                                                              |
|                                                                       |
| Prometheus, Grafana, and Hubble metrics scraping running in k3s.      |
| Jaeger connected as a Grafana datasource.                             |
+-----------------------------------------------------------------------+

**Step 2.1 --- Deploy Prometheus via kube-prometheus-stack**

The kube-prometheus-stack Helm chart installs Prometheus, Grafana, and a
set of default Kubernetes dashboards in one step. It also installs
kube-state-metrics, which gives pod and service-level resource metrics
with proper labels --- replacing cAdvisor for that purpose.

> helm repo add prometheus-community
> https://prometheus-community.github.io/helm-charts
>
> helm repo update
>
> helm install kube-prometheus
> prometheus-community/kube-prometheus-stack \\
>
> \--namespace monitoring \--create-namespace \\
>
> \--set grafana.adminPassword=admin \\
>
> \--set grafana.service.type=NodePort \\
>
> \--set grafana.service.nodePort=30001

Grafana will be accessible on NodePort 30001.

**Step 2.2 --- Add a Hubble scrape target to Prometheus**

Hubble\'s metrics endpoint runs inside the cluster. Add a ServiceMonitor
so Prometheus scrapes it:

> \# hubble-servicemonitor.yaml
>
> apiVersion: monitoring.coreos.com/v1
>
> kind: ServiceMonitor
>
> metadata:
>
> name: hubble
>
> namespace: monitoring
>
> spec:
>
> namespaceSelector:
>
> matchNames: \[kube-system\]
>
> selector:
>
> matchLabels:
>
> app.kubernetes.io/name: hubble-relay
>
> endpoints:
>
> \- port: metrics
>
> interval: 15s
>
> kubectl apply -f hubble-servicemonitor.yaml

**Step 2.3 --- Add Jaeger as a Grafana datasource**

The agents already push traces to Jaeger. We just need to tell Grafana
where Jaeger is. In the Grafana UI:

1.  Go to Configuration → Data Sources → Add data source.

2.  Select Jaeger.

3.  Set the URL to your Jaeger query endpoint (e.g.
    http://jaeger-query:16686 if Jaeger is in the cluster, or
    http://\<host-ip\>:16686 if it is still running on Docker).

4.  Click Save & Test.

Alternatively, add it as a provisioned datasource in values.yaml when
installing kube-prometheus-stack:

> grafana:
>
> additionalDataSources:
>
> \- name: Jaeger
>
> type: jaeger
>
> url: http://\<jaeger-host\>:16686
>
> access: proxy

**Step 2.4 --- Verify metrics in Grafana**

At this point, confirm the following metric families are present in
Prometheus:

-   hubble_flows_processed_total --- total flows seen by Hubble,
    labelled by source and destination

-   hubble_tcp_flags_total --- TCP flag counts per service pair

-   hubble_drop_total --- dropped packets (useful for detecting policy
    or connectivity issues)

-   container_cpu_usage_seconds_total (from cAdvisor, now with proper
    pod labels via kube-state-metrics)

-   kube_pod_info --- pod metadata including labels, namespace, node

**Phase 3 --- Migrate Workloads to k3s**

+-----------------------------------------------------------------------+
| **Goal**                                                              |
|                                                                       |
| All testbed services (agent-a, agent-b instances, llm-backend, mcp    |
| tools, baseline service) running as Kubernetes pods. Docker Compose   |
| stack can be shut down.                                               |
+-----------------------------------------------------------------------+

**Step 3.1 --- Convert Docker Compose services to Kubernetes manifests**

Each service in docker-compose.yml needs a corresponding Kubernetes
Deployment and Service. The mapping is straightforward:

  ------------------------ ----------------------------------------------
  **Docker Compose         **Kubernetes equivalent**
  concept**                

  **service:**             Deployment + Service

  **ports:**               Service (NodePort or ClusterIP)

  **environment:**         ConfigMap or Secret + envFrom

  **volumes:**             PersistentVolumeClaim or hostPath

  **networks:**            Kubernetes namespace + NetworkPolicy

  **deploy.replicas:**     Deployment.spec.replicas

  **healthcheck:**         livenessProbe / readinessProbe
  ------------------------ ----------------------------------------------

For the LLM backend, add a GPU resource request:

> resources:
>
> limits:
>
> nvidia.com/gpu: 1

For the multiple agent-b instances (currently ports 8102--8106), use a
single Deployment with replicas: 5 and a ClusterIP Service. Kubernetes
will load-balance across the replicas automatically, removing the need
for the manual port-per-instance approach.

**Step 3.2 --- Handle the inter_agent_network equivalent**

In Docker Compose, services communicate over the inter_agent_network
bridge. In Kubernetes, all pods in the same namespace can communicate by
default via ClusterIP services. The equivalent of your
service-name-based DNS (e.g. http://llm-backend:8000) works identically
--- Kubernetes CoreDNS resolves service names within the namespace.

If you want network isolation between groups of services (e.g. agents vs
tools), use Kubernetes NetworkPolicy resources with Cilium as the
enforcement engine.

**Step 3.3 --- Update environment variables**

Replace Docker Compose service hostnames with their Kubernetes service
names. The critical change for this deployment is the LLM backend URL,
which now points to Saturn rather than a local container:

> \# ConfigMap for agent environment variables
>
> apiVersion: v1
>
> kind: ConfigMap
>
> metadata:
>
> name: agent-config
>
> data:
>
> LLM_SERVER_URL: http://saturn.cba.upc.edu:8000/chat
>
> LLM_TIMEOUT_SECONDS: \'120\'

Reference this ConfigMap in your agent Deployment specs via envFrom so
all agent pods pick it up without individual env blocks:

> envFrom:
>
> \- configMapRef:
>
> name: agent-config

**Step 3.4 --- Add Saturn as an external Prometheus scrape target**

Prometheus running in the k3s cluster needs to scrape the LLM metrics
endpoint on Saturn. Add a static scrape job to the Prometheus
configuration (via a PrometheusRule or additionalScrapeConfigs in the
Helm values):

> additionalScrapeConfigs:
>
> \- job_name: llm-backend-saturn
>
> static_configs:
>
> \- targets:
>
> \- saturn.cba.upc.edu:8000
>
> metrics_path: /metrics
>
> scrape_interval: 15s

This preserves all existing LLM performance panels in Grafana (latency,
TTFT, token rates) unchanged. The metrics travel over the network from
Saturn to Prometheus but this is a lightweight polling operation.

If Jaeger is currently running in Docker, you can move it to k3s using
the Jaeger Operator or a simple Deployment. The agents\'
OTEL_EXPORTER_OTLP_ENDPOINT environment variable just needs to point to
the new Jaeger collector address.

**Step 3.5 --- Run health checks and smoke tests**

The existing health_check.py script can be run against the new
endpoints:

> python scripts/monitoring/health_check.py \\
>
> \--llm-url http://\<node-ip\>:8000/chat \\
>
> \--agent-a-url http://\<node-ip\>:8101/task \\
>
> \--agent-b-url http://\<node-ip\>:8102/subtask

Then run the existing curl smoke tests from the README against the
NodePort addresses.

**Phase 4 --- Build Hubble Dashboards and Retire Legacy Components**

+-----------------------------------------------------------------------+
| **Goal**                                                              |
|                                                                       |
| Purpose-built Grafana dashboards using Hubble metrics.                |
| tcp_metrics_collector.py retired. Traces and metrics correlated in    |
| Grafana.                                                              |
+-----------------------------------------------------------------------+

**Step 4.1 --- Build the Hubble network dashboard**

Create a new Grafana dashboard (or update the existing
agentic-traffic-testbed dashboard) using Hubble metrics. Key panels to
build:

  ------------------------ ----------------------------------------------
  **Panel**                **PromQL**

  **TCP bytes/s by service rate(hubble_tcp_flags_total\[1m\]) grouped by
  pair**                   source, destination

  **Flow rate by service   rate(hubble_flows_processed_total\[1m\]) by
  pair**                   (source, destination, verdict)

  **Dropped flows**        rate(hubble_drop_total\[1m\]) by (reason,
                           source, destination)

  **Active flows**         hubble_flows_processed_total by (type)

  **LLM request latency    histogram_quantile over
  p50/p95**                llm_request_latency_seconds_bucket (unchanged)

  **TTFT p50/p95**         histogram_quantile over
                           llm_queue_wait_seconds_bucket (unchanged)

  **Trace link panel**     Link to Jaeger filtered by time range
  ------------------------ ----------------------------------------------

**Step 4.2 --- Enable trace-to-metrics correlation**

In Grafana, configure the Jaeger datasource with a Derived Fields rule
that extracts the TraceID from trace spans and links to the Jaeger UI.
Then in the Prometheus datasource, set the exemplar linking so that
metric data points with embedded trace IDs link to Jaeger traces. This
is the built-in Grafana exemplar feature and requires no code changes
--- the agents already emit TraceIDs in their spans.

**Step 4.3 --- Retire legacy components**

Once the Hubble dashboards are validated and producing equivalent or
better data than the previous stack, the following can be removed:

-   scripts/monitoring/tcp_metrics_collector.py and its log file

-   The tcpdump-based scrape target in infra/monitoring/prometheus.yml

-   The ENABLE_MONITORING=1 Docker Compose monitoring files
    (docker-compose.monitoring\*.yml)

-   cAdvisor container (metrics are now served by Kubelet and
    kube-state-metrics)

Keep the eBPF tools (tcplife, tcprtt, tcpconnect, tcpretrans) --- they
remain useful for ad-hoc host-level investigation and Cilium does not
replace them for that use case.

**4. Risks and Mitigations**

  ------------------ --------------------- ------------------------------
  **Risk**           **Likelihood**        **Mitigation**

  Saturn port 8000   Medium                Verify with curl before
  firewalled from                          starting migration (Step 1.0).
  k3s server                               If blocked, request a firewall
                                           rule between the two UPC
                                           servers or use an SSH tunnel
                                           temporarily.

  Saturn LLM latency Certain               This is expected and actually
  higher than                              desirable for research --- it
  loopback                                 represents real distributed
                                           deployment. Establish a
                                           latency baseline early so you
                                           can distinguish network
                                           variance from model variance.

  GPU access in k3s  Medium                NVIDIA device plugin is
  pods more complex                        well-documented and widely
  than Docker                              used. Test with a simple GPU
                                           pod before migrating
                                           llm-backend.

  Cilium kernel      Low--Medium           Check kernel version (uname
  version                                  -r) against Cilium
  incompatibility                          compatibility matrix before
                                           installing. k3s and Cilium
                                           both work on Ubuntu 20.04+
                                           with kernel 5.4+.

  Docker Compose     Low                   k3s runs completely
  workflow                                 independently of Docker on the
  disruption during                        same host. Both stacks can run
  migration                                in parallel until Phase 3.

  Hubble metrics     Medium                Plan dashboard updates in
  schema differs                           Phase 4. The metric names
  from tcp_collector                       change but the underlying data
  metrics                                  (bytes, flows, flags) is
                                           equivalent or richer.

  Shared GPU         Low                   Coordinate GPU usage on Saturn
  contention on                            the same way as currently ---
  Saturn                                   nvidia-smi checks before
                                           running workloads.
  ------------------ --------------------- ------------------------------

**5. What Does Not Change**

To be clear about scope --- the following require zero or minimal
changes:

-   Agent code (agent_a/, agent_b/) --- no changes needed.

-   LLM backend (llm/, llm_server/) --- same Docker image, same
    environment variables, same GPU config.

-   MCP tool servers (tools/) --- same Docker images, just redeployed as
    pods.

-   OpenTelemetry instrumentation --- already in place, just update the
    OTEL_EXPORTER_OTLP_ENDPOINT if Jaeger moves.

-   Prometheus scrape config for llm-backend /metrics --- same endpoint,
    just a new ServiceMonitor resource instead of a static scrape.

-   Experiment scripts and health checks --- same scripts, updated
    endpoint URLs.

-   eBPF tools (tcplife, tcprtt, etc.) --- host-level tools, unaffected
    by the container orchestration layer.

**6. Summary**

+-----------------------------------------------------------------------+
| **The one-line version**                                              |
|                                                                       |
| We are replacing Docker Compose with k3s so that we can use Cilium as |
| the CNI, which gives us Hubble --- an eBPF-native flow observability  |
| layer that knows service names natively and eliminates both the       |
| raw-ID problem and the fragile TCP collector.                         |
+-----------------------------------------------------------------------+

The migration is designed to be low-risk and incremental. The existing
Docker Compose stack continues running throughout Phases 1 and 2.
Workloads are migrated in Phase 3, and the legacy observability
components are retired only once the replacement dashboards are
validated.

The end state is a stack where every Grafana panel --- network traffic,
resource usage, LLM performance, and distributed traces --- shows
service names, not raw IDs, and where traces and network metrics are
correlated on the same timeline without any custom glue code.