# Network topology (distributed mode)

This document describes the Docker network layout used when `DEPLOYMENT_MODE=distributed`.
Defined in [infra/docker-compose.distributed.yml](../infra/docker-compose.distributed.yml).

---

## Overview

In distributed mode each logical "node" gets its own isolated **home network**, plus every
service also joins a shared **inter-agent network** that carries all cross-service traffic.
This two-NIC design lets you observe and measure inter-node traffic independently of
intra-node traffic.

```
                        172.23.0.0/24  (inter_agent_network)
                        ┌──────────────────────────────────────────────────────┐
                        │                                                      │
  agent_a_network       │  .10 agent-a ──────────────────────────── .30 llm   │
  172.20.0.0/24  ───── agent-a                                                 │
                        │  .20-.24 agent-b (x5) ─────────────────── .30 llm   │
  agent_b_network       │                                                      │
  172.21.0.0/24  ───── agent-b (x5)            .50 chat-ui                    │
                        │                       .60 jaeger                    │
  llm_network           │                       .70 prometheus                │
  172.22.0.0/24  ───── llm-backend              .71 grafana                   │
                        │                       .72 cadvisor                  │
  tools_network         │                       .73 docker-mapping-exporter   │
  172.24.0.0/24  ───── mcp-tool-db (.40)        .40 mcp-tool-db               │
                        │                                                      │
                        └──────────────────────────────────────────────────────┘
```

---

## Networks

### `infra_inter_agent_network` — `172.23.0.0/24`

The **shared communication backbone**. Every service has a second NIC on this network.
All cross-service traffic flows here:

| Traffic path | Src IP | Dst IP |
|---|---|---|
| Agent A → Agent B (subtask) | 172.23.0.10 | 172.23.0.20–.24 |
| Agent A → LLM | 172.23.0.10 | 172.23.0.30 |
| Agent B → LLM | 172.23.0.20–.24 | 172.23.0.30 |
| Agent A → MCP tools | 172.23.0.10 | 172.23.0.40 |
| Agents → Jaeger (traces) | 172.23.0.10–.24 | 172.23.0.60 |
| Prometheus → all /metrics | 172.23.0.70 | all |

This is the primary network to monitor for agentic traffic analysis.

### `infra_agent_a_network` — `172.20.0.0/24`

Agent A's isolated home network (simulates Node 1 being on its own segment).
Only `agent-a` has a home NIC here (`172.20.0.10`).

**Why you see packets here:** Docker port-forwarding routes host-originated traffic
(e.g. `curl localhost:8101`, the experiment runner) through the container's **primary**
interface, which is `agent_a_network` because it is listed first in agent-a's `networks:`
config. This traffic does not traverse `inter_agent_network`.

### `infra_agent_b_network` — `172.21.0.0/24`

Agent B instances' isolated home network (simulates Node 2).
All five `agent-b` replicas share this network (`.10`–`.14`).
Same port-forwarding behaviour applies for traffic reaching `localhost:8102`–`8106`.

### `infra_llm_network` — `172.22.0.0/24`

LLM backend's isolated home network (simulates Node 3).
Only `llm-backend` has a home NIC here (`172.22.0.10`).
LLM inference traffic from agents arrives via `inter_agent_network` (`.30`), not here.

### `infra_tools_network` — `172.24.0.0/24`

MCP tool servers' isolated home network (simulates Node 4).
Only `mcp-tool-db` has a home NIC here (`172.24.0.10`).
Tool calls from agents arrive via `inter_agent_network` (`.40`), not here.

### `infra_agent-net` (simple mode only)

A single flat bridge network used by [infra/docker-compose.yml](../infra/docker-compose.yml)
(i.e. `DEPLOYMENT_MODE=simple`). No isolation — all services share one network and
communicate by hostname. Not used in distributed mode.

---

## Fixed IP address reference

| Service | agent_a_network | agent_b_network | llm_network | tools_network | inter_agent_network |
|---|---|---|---|---|---|
| agent-a | 172.20.0.10 | — | — | — | 172.23.0.10 |
| agent-b | — | 172.21.0.10 | — | — | 172.23.0.20 |
| agent-b-2 | — | 172.21.0.11 | — | — | 172.23.0.21 |
| agent-b-3 | — | 172.21.0.12 | — | — | 172.23.0.22 |
| agent-b-4 | — | 172.21.0.13 | — | — | 172.23.0.23 |
| agent-b-5 | — | 172.21.0.14 | — | — | 172.23.0.24 |
| llm-backend | — | — | 172.22.0.10 | — | 172.23.0.30 |
| mcp-tool-db | — | — | — | 172.24.0.10 | 172.23.0.40 |
| chat-ui | — | — | — | — | 172.23.0.50 |
| jaeger | — | — | — | — | 172.23.0.60 |
| prometheus | — | — | — | — | 172.23.0.70 |
| grafana | — | — | — | — | 172.23.0.71 |
| cadvisor | — | — | — | — | 172.23.0.72 |
| docker-mapping-exporter | — | — | — | — | 172.23.0.73 |

All IPs are overridable via environment variables in `infra/.env`
(e.g. `AGENT_A_IP`, `LLM_BACKEND_INTER_IP`, etc.).

---

## Traffic monitoring

To capture traffic on the inter-agent network from the host:

```bash
# Find the bridge interface for infra_inter_agent_network
docker network inspect infra_inter_agent_network | grep -i "bridge.name\|com.docker.network.bridge.name"

# Then capture on that interface (e.g. br-<id>)
sudo tcpdump -i br-<id> -n
sudo tcplife -i br-<id>
```

To capture host→agent-a port-forwarded traffic (appears on agent_a_network):

```bash
docker network inspect infra_agent_a_network | grep -i "bridge.name\|com.docker.network.bridge.name"
sudo tcpdump -i br-<id> -n port 8101
```
