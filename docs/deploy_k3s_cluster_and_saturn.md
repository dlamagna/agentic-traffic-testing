## Split-host deployment: k3s observability node + Saturn LLM backend

This document describes how to deploy the testbed in a **two‑server setup**:

- **Saturn (`SATURN_LLM_HOST` in `infra/.env`)**: runs the **LLM backend** with GPU.
- **k3s server (`K3S_NODE_HOST` in `infra/.env`)**: runs **agents, tools, Prometheus, Grafana, Hubble, and Jaeger** on a single‑node k3s cluster with Cilium.

The agents call the LLM over the university network via:

```text
LLM_SERVER_URL=http://${SATURN_LLM_HOST}:${SATURN_LLM_PORT}/chat
```

Prometheus in the k3s cluster scrapes LLM metrics from Saturn at:

```text
http://${SATURN_LLM_HOST}:${SATURN_LLM_PORT}/metrics
```

This gives you real inter‑host L3/L4 traffic for LLM calls, while keeping the GPU‑heavy backend on Saturn.

---

## 1. Saturn server (LLM backend)

### 1.1 Prerequisites

- NVIDIA GPU with drivers installed.
- Docker + Docker Compose v2.
- This repository cloned, e.g.:

```bash
git clone https://github.com/.../agentic-traffic-testing.git
cd agentic-traffic-testing
```

### 1.2 Configure infra/.env

From the repo root:

```bash
cd infra
cp .env.example .env
```

Edit `infra/.env` and set at least:

- **HF_TOKEN**: your Hugging Face token for the Llama model.
- Optional: adjust `LLM_MODEL`, `LLM_MAX_MODEL_LEN`, `LLM_GPU_MEMORY_UTILIZATION`, etc.

You do **not** need to change `LLM_SERVER_URL` here; the agents on the k3s node will point at Saturn explicitly.

### 1.3 Deploy the LLM backend

On Saturn, from the repo root:

```bash
chmod +x scripts/deploy/deploy_llm.sh
./scripts/deploy/deploy_llm.sh
```

What this does:

- Uses `infra/docker-compose.yml` to **build and start** the `llm-backend` service only.
- Keeps all other Docker‑based services stopped (agents, tools, monitoring).

After a short while, verify:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/metrics | head
```

From another machine (e.g. the k3s node):

```bash
curl http://$SATURN_LLM_HOST:$SATURN_LLM_PORT/health
```

You should see `200 OK`. If this fails, fix routing / firewall rules between the k3s node and Saturn **before** continuing.

---

## 2. k3s observability server (agents + Cilium + Hubble)

All steps below run on the **k3s server**, not on Saturn.

### 2.1 Prerequisites

- Linux host (Ubuntu/Debian is fine).
- Root / sudo access.
- **curl**, **Docker**, **python3**.
- Outbound internet access for:
  - `https://get.k3s.io` (k3s install script)
  - `https://helm.sh` (Helm install script)
  - Helm chart registries (Cilium, kube‑prometheus‑stack)
- This repository cloned to the same path as on Saturn, e.g.:

```bash
git clone https://github.com/.../agentic-traffic-testing.git
cd agentic-traffic-testing
```

### 2.2 (Optional but recommended) Test connectivity to Saturn

From the repo root on the k3s node:

```bash
chmod +x scripts/monitoring/test_llm_connectivity.sh
./scripts/monitoring/test_llm_connectivity.sh \
  --llm-url http://$SATURN_LLM_HOST:$SATURN_LLM_PORT
```

This checks:

- `http://saturn.cba.upc.edu:8000/health` is reachable.
- `http://saturn.cba.upc.edu:8000/metrics` is reachable and exposes LLM metrics.

Do not proceed until `/health` succeeds; agents and Prometheus depend on this.

### 2.3 One‑shot cluster deployment (recommended)

From the repo root on the k3s node:

```bash
chmod +x scripts/deploy/deploy_cluster.sh
./scripts/deploy/deploy_cluster.sh
```

This script runs the full sequence:

1. **LLM connectivity check** to Saturn (using `test_llm_connectivity.sh`).
2. `install_k3s_cilium.sh`:
   - Installs k3s with Flannel disabled.
   - Installs Cilium with Hubble relay + UI using `infra/k8s/cluster/cilium-values.yaml`.
   - Installs `kube-prometheus-stack` (Prometheus + Grafana + kube‑state‑metrics).
   - Applies `infra/k8s/monitoring/hubble-servicemonitor.yaml`.
3. `build_and_load_k8s_images.sh`:
   - Builds images:
     - `agent-a:local`
     - `agent-b:local`
     - `mcp-tool-db:local`
   - Loads them into k3s’s containerd via `k3s ctr images import`.
4. `deploy_k8s_workloads.sh`:
   - Ensures namespace `agentic-testbed` exists.
   - Applies:
     - `infra/k8s/workloads/jaeger.yaml`
     - `infra/k8s/workloads/agent-b.yaml`
     - `infra/k8s/workloads/agent-a.yaml`
     - `infra/k8s/workloads/mcp-tool-db.yaml`
   - Waits for all deployments to become Ready.

When it finishes, you will have:

- **Agent A** (NodePort): `http://<k3s-node-ip>:30101`
- **Agent B** (NodePort): `http://<k3s-node-ip>:30102`
- **MCP DB tool** (NodePort): `http://<k3s-node-ip>:30201`
- **Jaeger UI** (NodePort): `http://<k3s-node-ip>:31686`
- **Grafana** (NodePort): `http://<k3s-node-ip>:30001` (admin/admin)

Prometheus is configured (via `infra/k8s/monitoring/kube-prometheus-values.yaml`) to:

- Scrape Kubernetes components and Hubble.
- Scrape LLM metrics from Saturn:

  ```yaml
  prometheus:
    prometheusSpec:
      additionalScrapeConfigs: |
        - job_name: "llm-backend-saturn"
          metrics_path: "/metrics"
          static_configs:
            - targets: ["saturn.cba.upc.edu:8000"]
              labels:
                app: "llm-backend"
                location: "saturn"
  ```

### 2.4 Manual step‑by‑step (if you don’t want the wrapper script)

If you prefer to run each step manually on the k3s node:

```bash
# 1) Install k3s + Cilium + kube-prometheus-stack + Hubble
./scripts/deploy/install_k3s_cilium.sh

# 2) Build and load agent/tool images into k3s
./scripts/deploy/build_and_load_k8s_images.sh

# 3) Deploy workloads into the agentic-testbed namespace
./scripts/deploy/deploy_k8s_workloads.sh
```

---

## 3. Health checks for the split‑host setup

The existing `scripts/monitoring/health_check.py` script supports both
Docker Compose and k3s deployments.

### 3.1 Docker (original) mode

On a Docker‑based setup (everything on one host), usage remains:

```bash
python scripts/monitoring/health_check.py
```

### 3.2 k3s mode (agents on k3s, LLM on Saturn)

On a machine that can reach the k3s NodePorts and Saturn:

```bash
python scripts/monitoring/health_check.py \
  --mode k8s \
  --llm-url http://$SATURN_LLM_HOST:$SATURN_LLM_PORT/chat \
  --agent-a-url http://$K3S_NODE_HOST:30101/task \
  --agent-b-url http://$K3S_NODE_HOST:30102/subtask \
  --ui-url http://$K3S_NODE_HOST:30001 \
  --skip-monitoring
```

Key points:

- `--mode k8s`:
  - Skips Docker Compose service checks.
  - Can adapt defaults for NodePort‑style URLs (see script help).
- `--llm-url` should point at Saturn’s `/chat` endpoint.
- `--agent-a-url` / `--agent-b-url` should point at the k3s NodePort services.
- `--ui-url` is set to Grafana on NodePort 30001 (if you don’t run the chat UI in this setup).

You can also set `K8S_NODE_IP=$K3S_NODE_HOST` in your environment and rely on defaults where appropriate.

---

## 4. Quick smoke tests

Once both servers are up:

### 4.1 Test LLM from k3s node

```bash
./scripts/monitoring/test_llm_connectivity.sh \
  --llm-url http://saturn.cba.upc.edu:8000
```

### 4.2 Test Agent A and Agent B via NodePorts

From any host that can reach the k3s node:

```bash
# Agent A - simple task
curl -X POST http://$K3S_NODE_HOST:30101/task \
  -H "Content-Type: application/json" \
  -d '{"task":"Summarise what this testbed is for."}'

# Agent A - multi-hop scenario
curl -X POST http://$K3S_NODE_HOST:30101/task \
  -H "Content-Type: application/json" \
  -d '{"task":"Produce a 3-step plan for RTT metrics.","scenario":"agentic_multi_hop"}'

# Agent B directly
curl -X POST http://$K3S_NODE_HOST:30102/subtask \
  -H "Content-Type: application/json" \
  -d '{"subtask":"List two example MCP tool calls."}'
```

If these succeed, you have a working split‑host deployment: agents and observability on the k3s node, and the LLM backend on Saturn.

