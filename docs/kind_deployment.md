## Kind-based deployment (local Kubernetes)

This document describes how to run the Agentic Traffic Testbed on a **local Kind
cluster** with **kube-prometheus-stack** (Prometheus + Grafana) and optional
**Cilium + Hubble** for network observability.

The agents and tools run in Kubernetes; the LLM backend still runs externally
on Saturn (`SATURN_LLM_HOST` / `SATURN_LLM_PORT` in `infra/.env`).

---

## 1. Prerequisites

On the machine where you will run the Kind cluster:

- **Docker** installed and working (you can run `docker ps`).
- **Kind** CLI installed (`kind` on `PATH`).
- **kubectl** installed (`kubectl` on `PATH`).
- **Helm 3** installed (`helm` on `PATH`).
- A **Docker Hub account** and credentials (`docker login` works).

Clone the repo if you haven’t already:

```bash
git clone https://github.com/.../agentic-traffic-testing.git
cd agentic-traffic-testing
```

---

## 2. Build and publish images to Docker Hub

Rather than relying on `*:local` images inside Kind, we push the Kubernetes
images to Docker Hub so the cluster can pull them like any other registry
images.

### 2.1 Log in to Docker Hub

```bash
docker login
```

Enter your Docker Hub username and password when prompted.

### 2.2 Publish images

From the repo root:

```bash
cd ~/projects/agentic-traffic-testing

DOCKERHUB_USER=<your-dockerhub-username> \
  ./scripts/deploy/publish_k8s_images_to_dockerhub.sh
```

This script:

- Builds the images:
  - `agent-a:local` (from `agents/Dockerfile`)
  - `agent-b:local` (from `agents/Dockerfile`)
  - `mcp-tool-db:local` (from `tools/mcp_tool_db/Dockerfile`)
- Tags and pushes:
  - `docker.io/$DOCKERHUB_USER/agent-a:latest`
  - `docker.io/$DOCKERHUB_USER/agent-b:latest`
  - `docker.io/$DOCKERHUB_USER/mcp-tool-db:latest`

By default, the manifests in `infra/k8s/workloads/*.yaml` are wired to use:

```yaml
image: docker.io/dlamagna/agent-a:latest
image: docker.io/dlamagna/agent-b:latest
image: docker.io/dlamagna/mcp-tool-db:latest
```

If your Docker Hub username is not `dlamagna`, update these `image:` fields to
match your actual Docker Hub namespace.

---

## 3. Create Kind cluster + monitoring + Cilium (one shot)

The main entrypoint for the Kind deployment is:

```bash
./scripts/deploy/deploy_cluster.sh
```

This script performs:

1. **LLM connectivity check**  
   Uses `scripts/monitoring/test_llm_connectivity.sh` to verify that:
   - `http://saturn.cba.upc.edu:8000/health` is reachable
   - `http://saturn.cba.upc.edu:8000/metrics` exposes LLM metrics

2. **Kind cluster + kube-prometheus-stack install**  
   Via `scripts/deploy/install_kind_cluster.sh`:
   - Creates (or reuses) a Kind cluster named `agentic-testbed` using
     `infra/k8s/kind-config.yaml` (one control-plane node + one worker).
   - Installs `kube-prometheus-stack` into the `monitoring` namespace using
     `infra/k8s/monitoring/kube-prometheus-values.yaml`.

3. **Cilium + Hubble install on Kind**  
   Via `scripts/deploy/install_cilium_on_kind.sh`:
   - Installs Cilium into the Kind cluster using
     `infra/k8s/cluster/cilium-values-kind.yaml` (with `kubeProxyReplacement: false`).
   - Enables Hubble relay, UI, and metrics.

4. **(Optional) Image publish reminder**  
   Prints how to (re)publish images to Docker Hub via:
   `scripts/deploy/publish_k8s_images_to_dockerhub.sh`.  
   The script does **not** auto-publish; it assumes images are already in
   Docker Hub.

5. **Workload deployment**  
   Via `scripts/deploy/deploy_k8s_workloads.sh`:
   - Ensures the `agentic-testbed` namespace exists.
   - Applies:
     - `infra/k8s/workloads/jaeger.yaml`
     - `infra/k8s/workloads/agent-b.yaml`
     - `infra/k8s/workloads/agent-a.yaml`
     - `infra/k8s/workloads/mcp-tool-db.yaml`
   - Waits for `jaeger`, `agent-b`, `agent-a`, and `mcp-tool-db` deployments
     to roll out.

After `deploy_cluster.sh` completes, you should see:

```bash
kubectl get nodes -o wide
kubectl get pods -n monitoring
kubectl get pods -n agentic-testbed
```

with:

- One control-plane and one worker node Ready.
- Monitoring pods (Grafana, Prometheus, kube-state-metrics, node-exporter) Running.
- Jaeger and the agent / MCP DB deployments Running (assuming images are
  available in Docker Hub).

---

## 4. Accessing services via port-forward

In this setup we do **not** bind NodePorts directly on the Docker host. Instead
we use `kubectl port-forward` to reach services from your machine.

Typical commands:

### 4.1 Grafana (kube-prometheus-stack)

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-grafana 3000:80
```

Then open `http://localhost:3000` in your browser. The default credentials are
`admin` / `admin` unless you changed them in
`infra/k8s/monitoring/kube-prometheus-values.yaml`.

### 4.2 Jaeger UI

```bash
kubectl -n agentic-testbed port-forward svc/jaeger 16686:16686
```

Then open `http://localhost:16686`.

### 4.3 Agent A / Agent B / MCP DB

```bash
# Agent A
kubectl -n agentic-testbed port-forward svc/agent-a 30101:30101

# Agent B
kubectl -n agentic-testbed port-forward svc/agent-b 30102:30102

# MCP DB
kubectl -n agentic-testbed port-forward svc/mcp-tool-db 30201:30201
```

You can then hit:

- `http://localhost:30101/task` (Agent A)
- `http://localhost:30102/subtask` (Agent B)
- `http://localhost:30201` (MCP DB tool)

from your local machine.

---

## 5. Health check in Kind mode

Once the cluster and workloads are up, you can use the existing health check
script in **k8s mode**, pointing it at the forwarded ports.

For example:

```bash
python scripts/monitoring/health_check.py \
  --mode k8s \
  --llm-url http://saturn.cba.upc.edu:8000/chat \
  --agent-a-url http://localhost:30101/task \
  --agent-b-url http://localhost:30102/subtask \
  --ui-url http://localhost:3000 \
  --skip-monitoring
```

This will:

- Verify the LLM endpoint is reachable.
- Check Agent A and Agent B HTTP endpoints.
- Optionally exercise the full agent → LLM path.
- Check the UI (Grafana) endpoint.

---

## 6. Reset flows

Two useful reset scripts:

- **Logical reset (namespaces + monitoring + workloads only)**:

  ```bash
  ./scripts/reset_k8_cluster.sh
  ```

  This:
  - Runs `uninstall_testbed.sh` (if present) to stop any Docker-based services.
  - Deletes the `agentic-testbed` and `monitoring` namespaces.
  - Reinstalls `kube-prometheus-stack`.
  - Re-deploys the k8s workloads.

- **Full Kind reset (delete + recreate cluster)**:

  ```bash
  ./scripts/reset_k8_cluster_full.sh
  ```

  This:
  - Runs `uninstall_testbed.sh` to stop Docker-based services.
  - Deletes the Kind cluster (`kind delete cluster --name agentic-testbed`).
  - Re-runs `deploy_cluster.sh` to recreate Kind + monitoring + workloads.

If you still have a legacy k3s installation on the host, you can remove it
entirely via:

```bash
./scripts/deploy/uninstall_k3s.sh
```

which wraps the official `k3s-uninstall.sh` script.

