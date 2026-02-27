# k3s + Cilium + Hubble Deployment (Experimental)

This directory contains **experimental Kubernetes manifests** for running the
Agentic Traffic Testbed on a **single-node k3s cluster** with **Cilium + Hubble**
and **kube-prometheus-stack**.

The goal is to replace the Docker Compose–based deployment with:

- k3s (orchestration)
- Cilium (CNI)
- Hubble (network observability)
- kube-prometheus-stack (Prometheus + Grafana + kube-state-metrics)

## 1. Prerequisites

- A Linux host with:
  - k3s installed (server role)
  - Cilium installed via Helm, using `infra/k8s/cluster/cilium-values.yaml`
- `kubectl` and `helm` configured to talk to the k3s cluster.

> These manifests are **not** wired into the existing `scripts/deploy` flow yet;
> apply them manually while iterating.

## 2. Namespaces and Cluster Components

1. Create the application namespace:

```bash
kubectl apply -f infra/k8s/base/namespace.yaml
```

2. Install Cilium (from repo root, adjust version as needed):

```bash
helm repo add cilium https://helm.cilium.io/
helm repo update

helm install cilium cilium/cilium \
  --namespace kube-system \
  -f infra/k8s/cluster/cilium-values.yaml
```

3. Install kube-prometheus-stack:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f infra/k8s/monitoring/kube-prometheus-values.yaml

kubectl apply -f infra/k8s/monitoring/hubble-servicemonitor.yaml
```

## 3. Workloads

Build and load container images for the in-cluster components so they are visible to k3s (for example, by
pushing to a registry or loading into containerd). The manifests assume local
image names:

- `agent-a:local`
- `agent-b:local`
- `mcp-tool-db:local`

Then deploy the core testbed services into the `agentic-testbed` namespace:

```bash
kubectl apply -f infra/k8s/workloads/jaeger.yaml
kubectl apply -f infra/k8s/workloads/agent-b.yaml
kubectl apply -f infra/k8s/workloads/agent-a.yaml
kubectl apply -f infra/k8s/workloads/mcp-tool-db.yaml
```

## 4. Accessing Services

The following services are exposed as NodePorts on the k3s node IP:

- Agent A: `http://<node-ip>:30101` (`/task`, `/agentverse`)
- Agent B: `http://<node-ip>:30102` (`/subtask`)
- MCP db tool: `http://<node-ip>:30201`
- Jaeger UI: `http://<node-ip>:31686`

The LLM backend runs externally on `saturn.cba.upc.edu:8000` and is called via `LLM_SERVER_URL` from the agents.

You can reuse the existing smoke tests from `README.md` by pointing them at
these NodePort URLs instead of the Docker ports.

## 5. Observability

- Grafana (from kube-prometheus-stack) is exposed as a NodePort on `3001`:

  ```bash
  http://<node-ip>:3001
  ```

- Jaeger is available via the `jaeger` service:

  - UI: `http://jaeger:16686` (inside the cluster)
  - OTLP HTTP: `http://jaeger:4318/v1/traces`

The application pods are configured to send OTLP traces to `http://jaeger:4318/v1/traces`.

