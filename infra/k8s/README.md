# Kind-based Kubernetes Deployment (Experimental)

This directory contains **experimental Kubernetes manifests** for running the
Agentic Traffic Testbed on a **local Kind cluster** with
**kube-prometheus-stack** for observability.

The goal is to replace the Docker Compose–based deployment with:

- Kind (local Kubernetes cluster, using Docker)
- kube-prometheus-stack (Prometheus + Grafana + kube-state-metrics)

## Architecture: Kind observability cluster + external LLM backend

At a high level, these manifests assume:

- A **local Kind cluster** running the agents, MCP tools, Prometheus, Grafana, and Jaeger.
- An **external LLM backend** (vLLM) running on a separate host (or the same host) referenced by
  `SATURN_LLM_HOST` / `SATURN_LLM_PORT` in `infra/.env`.

```mermaid
flowchart LR
    subgraph KCL["Kind node (Docker host)"]
        subgraph NS["agentic-testbed namespace"]
            AgentA["Agent A (Deployment + Service)"]
            AgentB["Agent B (Deployment + Service)"]
            MCPDB["MCP Tool DB (Deployment + Service)"]
            Jaeger["Jaeger all-in-one (Deployment + Service)"]
        end

        subgraph Monitoring["monitoring namespace"]
            Prom["Prometheus (kube-prometheus-stack)"]
            Graf["Grafana"]
            Hubble["Hubble relay / metrics"]
        end
    end

    LLM[("LLM backend (vLLM)\nSATURN_LLM_HOST:SATURN_LLM_PORT")]

    AgentA -->|"HTTP (LLM_SERVER_URL)"| LLM
    AgentB -->|"HTTP (LLM_SERVER_URL)"| LLM

    Hubble -->|"flow metrics"| Prom
    Prom -->|"scrape /metrics"| LLM
    Prom -->|"metrics"| Graf
    Jaeger -->|"traces"| Graf
```

Key points:

- Agent pods talk to the LLM via `LLM_SERVER_URL=http://${SATURN_LLM_HOST}:${SATURN_LLM_PORT}/chat`.
- Prometheus (inside the cluster) scrapes LLM performance metrics from `http://${SATURN_LLM_HOST}:${SATURN_LLM_PORT}/metrics`.
- Cilium + Hubble provide L3/L4 flow visibility for **in-cluster** traffic; agent → LLM calls appear as egress flows to the external LLM host.

## 1. Prerequisites

- A Linux host with:
  - Docker installed
  - Kind installed (`kind` CLI)
- `kubectl` and `helm` configured to talk to the Kind cluster.

The `scripts/deploy/deploy_cluster.sh` script automates the Kind cluster
creation and observability stack installation, but you can also apply these
manifests manually while iterating.

## 2. Namespaces and Cluster Components

1. Create the application namespace:

```bash
kubectl apply -f infra/k8s/base/namespace.yaml
```

2. Install kube-prometheus-stack:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f infra/k8s/monitoring/kube-prometheus-values.yaml

kubectl apply -f infra/k8s/monitoring/hubble-servicemonitor.yaml
```

## 3. Workloads

Build and load container images for the in-cluster components so they are visible to the Kind
cluster (for example, by using `kind load docker-image` or pushing to a registry). The manifests assume local
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

The Kubernetes Services are exposed as NodePorts inside the Kind cluster. Since
we are not binding those ports directly on the Docker host, the simplest way to
access them from your machine is via `kubectl port-forward`. For example:

```bash
# Grafana (kube-prometheus-stack)
kubectl -n monitoring port-forward svc/kube-prometheus-grafana 3000:80
# then open http://localhost:3000

# Jaeger UI
kubectl -n agentic-testbed port-forward svc/jaeger 16686:16686
# then open http://localhost:16686

# Agent A
kubectl -n agentic-testbed port-forward svc/agent-a 30101:30101

# Agent B
kubectl -n agentic-testbed port-forward svc/agent-b 30102:30102

# MCP DB
kubectl -n agentic-testbed port-forward svc/mcp-tool-db 30201:30201
```

The LLM backend runs externally on `$SATURN_LLM_HOST:$SATURN_LLM_PORT` and is
called via `LLM_SERVER_URL` from the agents.

## 5. Observability

- Grafana (from kube-prometheus-stack) is available via the `kube-prometheus-grafana`
  Service in the `monitoring` namespace. Use `kubectl port-forward` as shown above.

- Jaeger is available via the `jaeger` Service in the `agentic-testbed` namespace:

  - UI: `http://jaeger:16686` (inside the cluster)
  - OTLP HTTP: `http://jaeger:4318/v1/traces`

The application pods are configured to send OTLP traces to `http://jaeger:4318/v1/traces`.

