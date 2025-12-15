# Agentic Traffic Testbed

This repository contains an **initial testbed** to study how **agentic software** (LLM-powered agents with tools) generates traffic patterns that differ from **non-agentic**, traditional microservice-based applications.

The long-term goal is to characterise, at the **network level (L3/L4)**, how agentic workloads behave: burstiness, RTT distributions, retransmissions, traffic fan-out, and the relationship between semantic workflow (AgentID, TaskID, ToolCallID) and packet/flow-level behaviour.

This MVP runs entirely on a **single GPU server**, using a **virtual multi-node setup** (multiple VMs or lightweight “nodes” on the same host).

---

## 1. High-level architecture (MVP)

The MVP architecture looks like this:

```mermaid
flowchart LR
    subgraph Host["Physical Server (GPU)"]
        
        subgraph Node1["VM / Node 1 - Agent A"]
            AgentA["Agent A (MCP host + LLM client)"]
            AgentALogger["Agent A Telemetry Hooks\n(TaskID / AgentID / ToolCallID)"]
        end

        subgraph Node2["VM / Node 2 - Agent B + Tools"]
            AgentB["Agent B (MCP host + LLM client)"]
            Tool1["MCP Tool Server 1\n(e.g. DB / HTTP API)"]
            Tool2["MCP Tool Server 2\n(e.g. Synthetic microservice)"]
            BaselineSvc["Baseline Non-agentic Service\n(e.g. fixed microservice chain)"]
            AgentBLogger["Agent B Telemetry Hooks\n(TaskID / AgentID / ToolCallID)"]
        end

        subgraph Node3["VM / Node 3 - LLM / SLM Server"]
            LLM["Local LLM / SLM Server\n(vLLM or similar)"]
        end

        subgraph Obs1["Node 1 eBPF"]
            BCC1["BCC / bpftrace tools\n(tcplife, tcpconnect, tcprtt, tcpretrans)"]
        end

        subgraph Obs2["Node 2 eBPF"]
            BCC2["BCC / bpftrace tools\n(tcplife, tcpconnect, tcprtt, tcpretrans)"]
        end

        subgraph Obs3["Node 3 eBPF"]
            BCC3["BCC / bpftrace tools\n(tcplife, tcpconnect, tcprtt, tcpretrans)"]
        end

        MetricsDB[(Optional Metrics Store\n(e.g. Prometheus / log directory)]
    end

    User((User / Benchmark Driver)) -->|User task / intent| AgentA
    AgentA -->|Agent message / subtask| AgentB
    AgentA -->|MCP tool calls| Tool1
    AgentB -->|MCP tool calls| Tool2
    AgentA -->|Service calls| BaselineSvc
    AgentB -->|Service calls| BaselineSvc

    AgentA -->|LLM queries| LLM
    AgentB -->|LLM queries| LLM

    AgentA --- BCC1
    AgentB --- BCC2
    Tool1 --- BCC2
    Tool2 --- BCC2
    BaselineSvc --- BCC2
    LLM --- BCC3

    BCC1 -->|export logs / metrics| MetricsDB
    BCC2 -->|export logs / metrics| MetricsDB
    BCC3 -->|export logs / metrics| MetricsDB
````

### Components

* **Node 1 – Agent A**

  * Agent A: LLM-based agent (MCP host + LLM client).
  * Emits application-level telemetry: `TaskID`, `AgentID`, `ToolCallID`.

* **Node 2 – Agent B + Tools**

  * Agent B: second agent (e.g. planner, tool specialist, summariser).
  * `Tool1` / `Tool2`: MCP tool servers (e.g. DB, HTTP API, synthetic microservice).
  * `BaselineSvc`: non-agentic baseline microservice chain (fixed call graph, no LLM).

* **Node 3 – Local LLM / SLM**

  * Local LLM server (e.g. vLLM or similar) serving requests from Agent A and Agent B.

* **Observability**

  * On each node, **eBPF-based tools** (BCC / bpftrace) export:

    * TCP connection lifetimes (`tcplife`)
    * Connection events (`tcpconnect`, `tcpaccept`)
    * RTT distributions (`tcprtt`)
    * Retransmissions (`tcpretrans`)
  * Optional metrics/log store on the host (Prometheus or even just log files).

---

## 2. What is eBPF and why we use it here

**eBPF (extended Berkeley Packet Filter)** is a Linux kernel mechanism that lets us attach safe, sandboxed programs to events in the kernel (e.g. network, syscalls) without changing kernel code.

In this project it is used to observe:

* **L3/L4 metrics per flow**:

  * RTT distributions
  * Retransmissions
  * Connection lifetimes
  * Flow creation and teardown
* Without modifying the agents or the tools.

We rely on **BCC** and **bpftrace** utilities to avoid writing raw eBPF code.

---

## 3. Installing eBPF tools (Debian/Ubuntu)

On each node (or inside each VM), run the following script to install the basic toolchain.

Save this as `scripts/install_ebpf_tools.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[*] Installing eBPF tools (BCC and bpftrace)..."

if ! command -v apt &>/dev/null; then
  echo "This script currently supports apt-based systems (Debian/Ubuntu)."
  exit 1
fi

sudo apt update

# Kernel headers for building eBPF probes
sudo apt install -y "linux-headers-$(uname -r)" || true

# BCC (bpfcc-tools) and Python bindings
sudo apt install -y bpfcc-tools python3-bpfcc

# bpftrace for quick custom scripts
sudo apt install -y bpftrace

echo "[*] Installed packages:"
dpkg -l | grep -E "bpfcc|bpftrace" || true

echo "[*] Quick sanity checks (these may print usage and exit):"
if command -v tcplife >/dev/null 2>&1; then
  tcplife -h | head -n 1 || true
fi
if command -v tcpconnect >/dev/null 2>&1; then
  tcpconnect -h | head -n 1 || true
fi

echo "[*] eBPF tools installation complete."
```

Make it executable:

```bash
chmod +x scripts/install_ebpf_tools.sh
./scripts/install_ebpf_tools.sh
```

---

## 4. Example commands to collect L3/L4 metrics

Once the tools are installed, you can run these on each node:

### Watch new TCP connections (who talks to whom)

```bash
sudo tcpconnect
```

### Measure TCP RTT in real time

```bash
sudo tcprtt
```

This gives per-socket RTT, which you can sample while running agentic workloads.

### See connection lifetimes

```bash
sudo tcplife
```

Helps compare agentic workflows vs baseline microservices (depth and chattiness).

### Monitor retransmissions

```bash
sudo tcpretrans
```

Useful when you introduce synthetic congestion or run heavy agentic traffic.

You can redirect output to logs for later analysis:

```bash
sudo tcprtt > logs/tcprtt_node1_agentA.log
```

---

## 5. Experimental idea (MVP)

For a single GPU server with 3 local nodes:

1. **Deploy:**

   * Node 1: Agent A.
   * Node 2: Agent B, MCP tool servers, and the non-agentic baseline service.
   * Node 3: Local LLM / SLM server.

2. **Define scenarios:**

   * Baseline: client → BaselineSvc (no LLM, fixed microservice path).
   * Agentic (simple): Agent A → Tool1 → LLM → response.
   * Agentic (multi-hop): Agent A → Agent B → Tool1/Tool2 → LLM → response.

3. **Run workload:**

   * For each scenario, run a fixed request set (e.g. same user profiles / tasks).
   * Log:

     * Agent-level telemetry: TaskID, AgentID, ToolCallID.
     * eBPF metrics from each node.

4. **Compare:**

   * Flow counts per task.
   * RTT distributions and tails.
   * Retransmission rates under load.
   * Traffic burst patterns (number and size of packets per task).

This gives you the first “traffic-shape” insight for agentic vs non-agentic workloads.

---

## 6. Roadmap / Next Phases

This repo initially targets the **single-host, multi-node MVP** described above.
Future phases build on the same core idea but add realism and complexity.

### Phase 1 – Current MVP (this repo)

* Single GPU host with multiple virtual nodes (VMs / containers).
* Two agents (Agent A, Agent B) using MCP tools.
* Local LLM server (vLLM or similar).
* eBPF-based L3/L4 observability with BCC / bpftrace.
* Non-agentic baseline service for comparison.

### Phase 2 – Kubernetes-based deployment

* Replace VM-based nodes with a **Kubernetes cluster** (kind, k3s, or full K8s).
* Agents and tools become **pods**; LLM server becomes a **service**.
* Introduce:

  * **Cilium** as CNI for eBPF-powered flow logs.
  * **Pixie** for cluster-wide TCP RTT, retransmissions, and L7 telemetry.
* Keep the same agent vs baseline comparison, but now across pods and services.

### Phase 3 – Programmable underlay / network emulation

* Introduce a **programmable data plane**:

  * Mininet for controllable topologies, or
  * P4 / INT for per-hop latency and queue measurements.
* Connect K8s nodes through this emulated underlay.
* Study how:

  * Agentic fan-out, branching, and tool chains interact with congestion.
  * Different routing / scheduling policies impact agentic QoS.

### Phase 4 – Multi-cluster and cross-domain scenarios

* Deploy agents across **multiple clusters** or “domains”.
* Use cluster-mesh / VPN / overlay networks.
* Explore:

  * Cross-cluster agent coordination.
  * Inter-domain policy, latency, and reliability impact on agent workflows.
  * How semantic routing or intent-based networking could prioritise agentic flows.

---

## 7. Repository layout (suggested)

You can adopt a structure like:

```text
.
├── agents/
│   ├── agent_a/
│   ├── agent_b/
│   └── common/
├── tools/
│   ├── mcp_tool_db/
│   └── mcp_tool_synthetic/
├── baseline/
│   └── service_chain/
├── llm/
│   ├── serve_llm.py
│   ├── config/
│   │   └── llama-3.1-8b.yaml
│   └── Dockerfile
├── llm_server/
│   └── server.py
├── infra/
│   └── docker-compose.yml
├── scripts/
│   ├── install_ebpf_tools.sh
│   ├── run_mvp_experiment.sh
│   └── collect_metrics.sh
├── logs/
│   └── ...
├── requirements.txt
└── README.md
```

The `llm/` directory contains a vLLM-based backend targeting:

```text
meta-llama/Llama-3.1-8B-Instruct
```

You can run it directly:

```bash
pip install -r requirements.txt
python -m llm.serve_llm --model meta-llama/Llama-3.1-8B-Instruct --port 8000
```

Or via Docker / Docker Compose:

```bash
cd infra
docker compose up --build llm-backend
```

Agents can point at this backend by setting `LLM_SERVER_URL`, e.g.:

```bash
export LLM_SERVER_URL="http://llm-backend:8000/chat"
```

### Changing the model served by llm-backend

The model and device used by the `llm-backend` container are set in `infra/docker-compose.yml` under the `llm-backend` service `command`. We currently default to a lighter, ungated model for compatibility:

- Model: `TinyLlama/TinyLlama-1.1B-Chat-v1.0`
- Device flag: `--device cpu` (vLLM will use CPU; switch to `cuda` if you have a compatible GPU)

To serve another model (e.g., `meta-llama/Llama-3.1-8B-Instruct`), edit the `command` array for `llm-backend` in `infra/docker-compose.yml` and change the `--model` value (and `--device` if you want GPU). After editing, rebuild/restart:

```bash
cd infra
docker compose up -d --build llm-backend
```

If the model is gated on Hugging Face, set `HF_TOKEN` (and optionally `HUGGINGFACE_HUB_TOKEN`) in your shell so Compose passes it through to the container.

You can evolve this as you move into Kubernetes and programmable networks.

---

## 8. Next steps

Immediate next steps to make this repo useful:

1. Implement minimal Agent A and Agent B (even dumb prompts) that:

   * Call MCP tools.
   * Call the local LLM server.
   * Emit `TaskID`, `AgentID`, and `ToolCallID` in logs.

2. Implement a simple non-agentic baseline service.

3. Add scripts to:

   * Run one scenario at a time.
   * Start eBPF probes.
   * Dump all logs into `logs/` with timestamps.

From there, you can start collecting and analysing traffic-shape data for your first write-up.


