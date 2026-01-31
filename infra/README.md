# Infrastructure Configuration

This directory contains Docker Compose configurations and environment settings for the Agentic Traffic Testbed.

## Quick Start

```bash
# 1. Copy the example environment file
cp .env.example .env

# 2. Edit .env and set your HF_TOKEN (required for Llama models)
#    Also choose your DEPLOYMENT_MODE

# 3. Deploy from the repo root
cd ..
./scripts/deploy.sh
```

## Deployment Modes

The testbed supports three deployment modes, controlled by `DEPLOYMENT_MODE` in `.env`:

| Mode | Description | Use Case |
|------|-------------|----------|
| `single` | All containers on one Docker bridge network | Development, quick testing |
| `distributed` | Separate Docker networks per logical node | Traffic pattern analysis |
| `multi-vm` | Services deployed to separate VMs via SSH | Production-like experiments |

### Single Mode (Default)

```
DEPLOYMENT_MODE=single
```

All services run on a single `agent-net` bridge network. Services discover each other via Docker DNS (e.g., `http://llm-backend:8000`).

```
┌─────────────────────────────────────────────────┐
│                  agent-net                      │
│                                                 │
│  llm-backend  agent-a  agent-b  mcp-tool-db    │
│                                                 │
└─────────────────────────────────────────────────┘
```

**Pros:** Simple, fast startup, easy debugging  
**Cons:** Can't observe inter-node traffic patterns

---

### Distributed Mode

```
DEPLOYMENT_MODE=distributed
```

Services are placed on isolated networks and communicate through a shared `inter_agent_network`. This creates observable cross-network traffic patterns.

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│                        inter_agent_network (172.23.0.0/24)                        │
│                                                                                   │
│   Agent A       Agent B (x5)      LLM Backend      MCP Tools       Jaeger        │
│  172.23.0.10    172.23.0.20-24    172.23.0.30     172.23.0.40+    172.23.0.60    │
│      │               │                 │               │              │          │
└──────┼───────────────┼─────────────────┼───────────────┼──────────────┼──────────┘
       │               │                 │               │              │
       ▼               ▼                 ▼               ▼              │
 agent_a_network  agent_b_network   llm_network    mcp_network         │
 (172.20.0.0/24)  (172.21.0.0/24)  (172.22.0.0/24) (172.24.0.0/24)     │
```

**Network Configuration:**

| Network | Subnet | Services |
|---------|--------|----------|
| `agent_a_network` | 172.20.0.0/24 | Agent A |
| `agent_b_network` | 172.21.0.0/24 | Agent B instances |
| `llm_network` | 172.22.0.0/24 | LLM backend |
| `mcp_network` | 172.24.0.0/24 | MCP Tool Servers (future) |
| `inter_agent_network` | 172.23.0.0/24 | All services (cross-communication) |

> **Note:** MCP Tool Servers are on a separate network from agents. This allows for:
> - Isolated traffic analysis of agent ↔ tool communication
> - Independent scaling and deployment of tools
> - Clear separation of concerns between agent logic and tool implementations

**Pros:** Observable traffic patterns, network isolation  
**Cons:** Latency is still minimal (same host)

#### Adding Realistic Network Conditions

Enable network emulation to add artificial latency:

```bash
# In .env
ENABLE_NETWORK_EMULATION=1
NETWORK_DELAY_MS=10        # Base delay
NETWORK_JITTER_MS=2        # Variation
NETWORK_LOSS_PERCENT=0     # Packet loss (0-100)
```

Or apply manually:

```bash
# Apply/remove/check network emulation
./scripts/apply_network_emulation.sh apply
./scripts/apply_network_emulation.sh remove
./scripts/apply_network_emulation.sh status
```

---

### Multi-VM Mode

```
DEPLOYMENT_MODE=multi-vm
NODE1_HOST=192.168.56.10    # Agent A + Jaeger + Chat UI
NODE2_HOST=192.168.56.11    # Agent B instances
NODE3_HOST=192.168.56.12    # LLM backend (GPU node)
NODE4_HOST=192.168.56.13    # MCP Tool Servers (future)
REMOTE_REPO_DIR=/home/user/projects/agentic-traffic-testing
```

Services are deployed to separate VMs via SSH. This provides true network isolation with realistic latency and the ability to run eBPF tools on each node.

**Node Layout:**

| Node | Services | Notes |
|------|----------|-------|
| NODE1 | Agent A, Jaeger, Chat UI | Orchestrator agent |
| NODE2 | Agent B instances | Worker agents |
| NODE3 | LLM backend | GPU required |
| NODE4 | MCP Tool Servers | Future - isolated tools |

**Prerequisites:**
- Passwordless SSH access to all hosts
- This repository cloned at `REMOTE_REPO_DIR` on each host
- Docker installed on each host
- GPU drivers on NODE3_HOST for LLM backend

**Pros:** Realistic network measurements, eBPF compatible  
**Cons:** More setup, requires multiple VMs

---

## Files

| File | Description |
|------|-------------|
| `.env.example` | Template configuration - copy to `.env` |
| `.env` | Your local configuration (gitignored) |
| `docker-compose.yml` | Single-network deployment |
| `docker-compose.distributed.yml` | Distributed-network deployment |

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `HF_TOKEN` | Hugging Face token for gated models (e.g., Llama) |

### Deployment

| Variable | Default | Description |
|----------|---------|-------------|
| `DEPLOYMENT_MODE` | `single` | `single`, `distributed`, or `multi-vm` |
| `NODE1_HOST` | - | (multi-vm) SSH host for Agent A |
| `NODE2_HOST` | - | (multi-vm) SSH host for Agent B |
| `NODE3_HOST` | - | (multi-vm) SSH host for LLM backend |
| `NODE4_HOST` | - | (multi-vm) SSH host for MCP Tools (future) |
| `REMOTE_REPO_DIR` | `/home/$USER/projects/testbed` | (multi-vm) Repo path on remote hosts |

### Network (Distributed Mode)

| Variable | Default | Description |
|----------|---------|-------------|
| `NETWORK_AGENT_A_SUBNET` | `172.20.0.0/24` | Agent A network |
| `NETWORK_AGENT_B_SUBNET` | `172.21.0.0/24` | Agent B network |
| `NETWORK_LLM_SUBNET` | `172.22.0.0/24` | LLM network |
| `NETWORK_MCP_SUBNET` | `172.24.0.0/24` | MCP Tools network (future) |
| `NETWORK_INTER_AGENT_SUBNET` | `172.23.0.0/24` | Inter-agent network |

### Network Emulation

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_NETWORK_EMULATION` | `0` | Enable tc netem rules |
| `NETWORK_DELAY_MS` | `10` | Base latency (ms) |
| `NETWORK_JITTER_MS` | `2` | Latency variation (ms) |
| `NETWORK_LOSS_PERCENT` | `0` | Packet loss (%) |

### LLM Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL` | `meta-llama/Llama-3.1-8B-Instruct` | Model to serve |
| `LLM_MAX_MODEL_LEN` | `4096` | Max context length |
| `LLM_DTYPE` | `float16` | Model precision |
| `LLM_GPU_MEMORY_UTILIZATION` | `0.90` | GPU memory fraction |
| `LLM_MAX_NUM_SEQS` | `12` | Max concurrent sequences |
| `LLM_TIMEOUT_SECONDS` | `120` | Request timeout |

### Agents

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_B_TIMEOUT_SECONDS` | `120` | Agent B call timeout |
| `MAX_AGENT_B_TURNS` | `3` | Max turns in multi-hop |
| `MAX_PARALLEL_WORKERS` | `5` | Max parallel Agent B workers |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LLM_REQUESTS` | `0` | Log LLM request content |
| `LLM_LOG_MAX_CHARS` | `500` | Max chars to log |

---

## Scripts

From the repository root:

```bash
# Deploy the testbed
./scripts/deploy.sh

# Stop the testbed  
./scripts/stop.sh

# Stop and remove volumes
./scripts/stop.sh --volumes

# Apply network emulation (distributed mode)
./scripts/apply_network_emulation.sh apply

# Check health
python scripts/health_check.py
```

---

## Troubleshooting

### Containers can't reach each other (distributed mode)

Check that all services are on the `inter_agent_network`:

```bash
docker network inspect infra_inter_agent_network
```

### LLM backend fails to start

- Check GPU availability: `nvidia-smi`
- Verify HF_TOKEN is set correctly
- Check logs: `docker logs llm-backend`

### Network emulation doesn't work

Containers need `NET_ADMIN` capability. The current compose files don't grant this by default for security. To enable:

1. Add to the service in docker-compose:
   ```yaml
   cap_add:
     - NET_ADMIN
   ```

2. Rebuild: `./scripts/deploy.sh`

### Port conflicts

If ports are already in use, either stop the conflicting service or modify the port mappings in the compose file.

Default ports:
- 8000: LLM backend
- 8101: Agent A
- 8102-8106: Agent B instances
- 8201: MCP Tool DB
- 3000: Chat UI
- 16686: Jaeger UI
