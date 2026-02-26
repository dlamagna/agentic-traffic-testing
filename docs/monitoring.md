## Monitoring Overview

The testbed includes an optional monitoring stack that covers three layers:

- **Network layer**: inter-container / inter-service traffic
- **Application / request layer**: end‑to‑end latency for LLM calls
- **AI performance layer**: LLM tokens, latency, and time‑to‑first‑token

When `ENABLE_MONITORING=1` in `infra/.env`, deployment will:

- Start **Prometheus**, **Grafana**, and **cAdvisor** via the monitoring compose files.
- Attempt to start the **TCP metrics collector** on the host for service‑level network metrics.

---

## Components

- **Prometheus**
  - Config: `infra/monitoring/prometheus.yml`
  - Scrapes:
    - `prometheus` itself
    - `cadvisor:8080` (container / host metrics)
    - `llm-backend:8000/metrics` (LLM performance metrics)
    - `tcp_metrics_collector.py` on port `9100` (TCP service‑level metrics)

- **Grafana**
  - Config / dashboards:
    - Datasource: `infra/monitoring/grafana/provisioning/datasources/datasources.yml`
    - Dashboard: `infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json`
  - UI: `http://localhost:3001` (admin/admin by default)
  - The **Agentic Traffic Testbed** dashboard is provisioned on startup.

- **cAdvisor**
  - Runs as a container via `infra/docker-compose.monitoring*.yml`
  - Exposes `/metrics` on `http://localhost:8080/metrics`.
  - Provides `container_*` CPU, memory, and network metrics used in the **Resource Usage** and **Docker Network** panels.
  - On some hosts cAdvisor may initially only expose a single `id="/"` series (host‑level aggregate). To get **per‑container** metrics (one time series per container with labels such as `name` or `container_label_com_docker_compose_service`), cAdvisor must have working read‑only access to:
    - `/sys/fs/cgroup`
    - `/var/lib/docker`
    - `/var/run/docker.sock`
    and Docker must be using cgroups in a way that cAdvisor understands (including cgroups v2). See "Enabling per‑container CPU / memory metrics" below.

- **TCP Metrics Collector**
  - Script: `scripts/monitoring/tcp_metrics_collector.py`
  - Runs on the **host**, not in Docker.
  - Captures TCP traffic on the `inter_agent_network` bridge using `tcpdump`.
  - Exposes a Prometheus `/metrics` endpoint on `http://localhost:9100/metrics` with:
    - `tcp_packets_total{src_service, dst_service}`
    - `tcp_bytes_total{src_service, dst_service}`
    - `tcp_flow_duration_seconds_bucket{src_service, dst_service, le}`
    - `tcp_packet_size_bytes_bucket{le}`
    - `tcp_syn_total`, `tcp_fin_total`, `tcp_rst_total`

---

## How the TCP metrics collector is started

When `ENABLE_MONITORING=1`, `scripts/deploy/deploy.sh` will:

- Deploy Prometheus, Grafana, and cAdvisor for the selected mode (`single` or `distributed`).
- Then ensure the TCP metrics collector is running on the host:

  - It checks for an existing process:

    ```bash
    pgrep -f "tcp_metrics_collector.py"
    ```

  - If not found, it starts the collector in the background from the repo root:

    ```bash
    python3 scripts/monitoring/tcp_metrics_collector.py --sudo-tcpdump \
      >> logs/tcp_metrics_collector.log 2>&1 &
    ```

  - You **may be prompted for your sudo password for `tcpdump`**. The Python process itself is not run with sudo; only `tcpdump` is.

If automatic start fails (for example, if `sudo tcpdump` is not allowed), the deploy script will print a warning and you can start the collector manually (see below).

---

## Running the TCP metrics collector manually

From the repo root:

```bash
cd /home/dlamagna/projects/agentic-traffic-testing
python3 scripts/monitoring/tcp_metrics_collector.py --sudo-tcpdump
```

Notes:

- The script will:
  - Auto‑detect the Docker bridge for `inter_agent_network` (e.g. `br-df4088ff2909`).
  - Apply the default filter `tcp and net 172.23.0.0/24`.
  - Expose metrics on `http://localhost:9100/metrics`.
- You need to have permission to run **`sudo tcpdump`**, since packet capture requires elevated privileges.

If `sudo tcpdump` works but running `--sudo-tcpdump` inside Python does not (due to sudoers/TTY quirks), you can instead pipe `tcpdump` output into the collector:

```bash
cd /home/dlamagna/projects/agentic-traffic-testing
sudo tcpdump -i br-df4088ff2909 -l -n -tt tcp and net 172.23.0.0/24 \
  | python3 scripts/monitoring/tcp_metrics_collector.py --read-stdin
```

In this mode:

- `sudo` is only used for `tcpdump`.
- The collector reads line-oriented tcpdump output from `stdin` and still exposes metrics at `http://localhost:9100/metrics`.

To run without sudo at all (only if an admin has granted the necessary capabilities to `tcpdump` or your user), omit both sudo and `--sudo-tcpdump`:

```bash
python3 scripts/monitoring/tcp_metrics_collector.py
```

---

## Dashboard Layout (Agentic Traffic Testbed)

The provisioned Grafana dashboard (`agentic-traffic-testbed`) is structured into three main layers:

1. **Overview**
   - Active Docker containers (from `container_cpu_usage_seconds_total`).
   - Docker network TX/RX rate (by Docker service).
   - LLM request rate (from `llm_requests_total`).

2. **Network Traffic**
   - Docker network transmit/receive rates and packets per second, broken down by Docker service / container label (`container_label_com_docker_compose_service`).

3. **Resource Usage**
   - CPU usage (`container_cpu_usage_seconds_total`).
   - Memory usage (`container_memory_usage_bytes`).
   - When cAdvisor exposes per‑container metrics (not just `id="/"`), these panels can be switched to show **per‑container** or **per‑service** usage (e.g. grouped by `container_label_com_docker_compose_service` and filtered to agent containers only).

4. **Service‑level Network (TCP)**
   - `TCP Bytes/s by Service Pair`:
     - Uses `rate(tcp_bytes_total{src_service!="external",dst_service!="external"}[1m])`.
     - Legend: `src_service → dst_service` (e.g. `agent_a → llm_backend`).
   - `TCP Flow Duration (Agent A → LLM)`:
     - `histogram_quantile` over `tcp_flow_duration_seconds_bucket` for p50/p95 flow duration between `agent_a` and `llm_backend`.

5. **AI Performance (LLM)**
   - `LLM End-to-end Latency (p50/p95)`:
     - Uses `llm_request_latency_seconds_bucket`.
   - `LLM Time-to-First-Token (TTFT p50/p95)`:
     - Uses `llm_queue_wait_seconds_bucket`.
   - `Prompt Tokens / s`:
     - `rate(llm_prompt_tokens_total[1m])`.
   - `Completion Tokens / s`:
     - `rate(llm_completion_tokens_total[1m])`.
   - `In-flight LLM Requests`:
     - `llm_inflight_requests`.

---

## Enabling Monitoring

1. Copy the example env file and enable monitoring:

```bash
cd infra
cp .env.example .env
```

In `infra/.env`:

```dotenv
ENABLE_MONITORING=1
```

2. Deploy from the repo root:

```bash
./scripts/deploy/deploy.sh
```

3. After deployment:

- Grafana: `http://localhost:3001` (admin/admin)
- Prometheus: `http://localhost:9090`
- cAdvisor: `http://localhost:8080`
- TCP metrics (if collector is running): `http://localhost:9100/metrics`

---

## Enabling per‑container CPU / memory metrics

By default, on some hosts cAdvisor (running in Docker) may only see a single root cgroup and export `container_*` metrics with `id="/"`. In that case Prometheus and Grafana only have **host‑level aggregates**, so panels cannot distinguish individual containers or "agent" vs non‑agent workloads.

To enable **per‑container** metrics:

1. Ensure the `cadvisor` container has read‑only access to host resources (already configured in `docker-compose.monitoring.yml`):

   - `/sys:/sys:ro`
   - `/var/lib/docker/:/var/lib/docker:ro`
   - `/var/run:/var/run:ro` (for `docker.sock`)
   - `/dev/disk/:/dev/disk:ro`
   - `privileged: true`

2. Confirm that your Docker engine is using cgroups in a way cAdvisor supports (including cgroups v2). On the host you can check:

   ```bash
   docker info | grep -i cgroup
   ```

3. Browse raw cAdvisor metrics:

   - Open `http://localhost:8080/metrics` in a browser.
   - Search for `container_cpu_usage_seconds_total` or `container_memory_usage_bytes`.
   - You should ideally see **multiple series** with labels such as `name`, `container`, or `container_label_com_docker_compose_service` rather than only `id="/"`.

4. Once per‑container series are visible in cAdvisor, Prometheus will ingest them and the Grafana **Resource Usage** panels can be updated to:

   - Group by service label, e.g. `sum by (container_label_com_docker_compose_service) (...)`.
   - Filter to only **agent containers** (e.g. `container_label_com_docker_compose_service=~"agent-a|agent-b.*"`), keeping the dashboard generic across different agent workflows.

---

## Why some panels show raw IDs instead of service names

### 1. Where the unreadable IDs come from

Some Grafana panels are built on top of **low-level cAdvisor metrics** that only expose:

- For host-level network metrics:
  - `id="/"` (the host / root cgroup)
  - `interface="br-<network_id_prefix>"` (Linux bridge names created by Docker)
- For host/systemd metrics:
  - `id="/system.slice/docker-<container_id>.scope"`

Examples:

- `br-df4088ff2909` → Linux bridge backing the `infra_inter_agent_network` Docker network.
- `/system.slice/docker-a86eb27ddbcc...scope` → systemd cgroup for a container (e.g. `agent-a`), but cAdvisor only exposes the **cgroup path**, not the friendly container or compose service name.

These IDs are meaningful to the kernel and Docker, but not to humans.

### 2. Why Grafana can’t turn them into names on its own

Grafana visualizations here are driven by **Prometheus metrics only**:

- PromQL can:
  - Filter / aggregate by labels that exist on the series (`id`, `interface`, `job`, `instance`, etc.).
- PromQL **cannot**:
  - Call out to Docker, systemd, or a shell script to “look up” `br-df4088ff2909` or `docker-a86e...scope`.
  - Join arbitrary external data (like the output of `docker ps` or a bash mapping script) at query time.

Because the cAdvisor series backing the "Network Traffic" and "Resource Usage" panels **lack service-level labels**, Prometheus and Grafana have no way to render those as `agent-a`, `agent-b-3`, or `llm-backend`.

In contrast, the **TCP collector** is designed to export metrics with logical labels:

- `tcp_bytes_total{src_service="agent_a", dst_service="llm_backend"}`
- `tcp_flow_duration_seconds_bucket{src_service="agent_a", dst_service="llm_backend", le="0.5"}`

Those labels are available directly in Prometheus, so Grafana can use them to show meaningful service names.

### 3. What cAdvisor can and cannot do

cAdvisor can be configured (depending on host setup and cgroups) to expose **container labels**:

- e.g. `container_cpu_usage_seconds_total{container_label_com_docker_compose_service="agent-a", ...}`

However:

- Those labels appear on **per-container metrics** (where `id="/system.slice/docker-...scope"`), not on the **host-level** `id="/"` metrics used for per-bridge traffic.
- The `br-...` network metrics are inherently per-interface aggregates:
  - `container_network_transmit_bytes_total{id="/", interface="br-df4088ff2909"}`
  - There is no place in that label set to attach a single "service name" because multiple containers share the same bridge.

So even with better cAdvisor labeling, the specific panels that aggregate by `interface="br-*"` will still show **bridge names**, not container names.

### 4. Why we added the TCP collector and service-level panels

To get truly human-readable, service-oriented metrics, we introduced a separate **TCP metrics collector** (`tcp_metrics_collector.py`) which:

- Watches packets on the inter-agent network bridge (e.g. `infra_inter_agent_network`).
- Uses a static IP → service mapping (`SERVICE_IPS`) to assign:
  - `src_service` and `dst_service` labels like `agent_a`, `agent_b_1`, `llm_backend`, `mcp_tool_db`, etc.
- Exposes metrics such as:
  - `tcp_packets_total{src_service="agent_a", dst_service="llm_backend"}`
  - `tcp_bytes_total{src_service="agent_a", dst_service="llm_backend"}`
  - `tcp_flow_duration_seconds_bucket{src_service="agent_a", dst_service="llm_backend", le="..."}`

This is why the **Service-level Network (TCP)** row in the dashboard can show:

- `agent_a → llm_backend` instead of raw IPs or `br-*` names.
- Latency distributions (flow duration) for specific service pairs (e.g. Agent A → LLM).

### 5. Possible future improvements to get friendlier names everywhere

If you want friendlier names on more panels without changing Grafana/Prometheus themselves, some options to explore are:

- **Enhanced exporters**
  - Write a small "metadata exporter" that periodically:
    - Calls `docker ps`, `docker network ls`, and systemd APIs.
    - Exposes metrics like `network_info{interface="br-df4088ff2909", network="infra_inter_agent_network"}` and `container_info{id="/system.slice/docker-a86e...", service="agent-a"}`.
  - Grafana panels could then at least show friendlier names by plotting these metadata metrics or combining them in annotations/tooltips.

- **Alternative observability stacks**
  - Kubernetes + CNI observability (Cilium, Hubble, Pixie) inherently track flows at the **pod/service** level and expose richer labels.
  - Service meshes (e.g., Istio/Linkerd) can add HTTP/gRPC metrics with service names and richer routing metadata.

This repo deliberately stays "vanilla" Docker + cAdvisor + Prometheus + Grafana, so some of those richer mappings are intentionally left out to avoid adding heavyweight dependencies—but the TCP collector and this document are structured so you can plug in other technologies that provide first-class service naming. 
