# Architecture

```mermaid
flowchart TB

%% STACK
WORKFLOW["Workflow (AgentVerse)"]
AGENTS["Agents + MCP (python)"]
CONTAINERS["Docker Containers"]
LLM_BACKEND["LLM Backend (vLLM + Llama)"]
NETWORK["Docker Networks"]
CAPTURE["Traffic Capture (tcpdump)"]

%% MAIN STACK
WORKFLOW --> AGENTS
AGENTS --> CONTAINERS
CONTAINERS --> LLM_BACKEND
NETWORK --> CAPTURE

%% NETWORK PATHS
CONTAINERS --> NETWORK
LLM_BACKEND --> NETWORK

%% MONITORING
PROM["Prometheus"]
GRAF["Grafana"]

PROM --> GRAF

%% METRICS
WORKFLOW -. workflow latency .-> PROM
AGENTS -. token usage .-> PROM
CONTAINERS -. cpu / memory .-> PROM
LLM_BACKEND -. model latency / TTFT .-> PROM
NETWORK -. connection rates .-> PROM
CAPTURE -. packet timing .-> PROM

%% STYLING
style WORKFLOW fill:#e3f2fd,stroke:#1e88e5,stroke-width:2px
style AGENTS fill:#e8f5e9,stroke:#43a047,stroke-width:2px
style CONTAINERS fill:#fff8e1,stroke:#f9a825,stroke-width:2px
style LLM_BACKEND fill:#fce4ec,stroke:#d81b60,stroke-width:2px
style NETWORK fill:#ede7f6,stroke:#5e35b1,stroke-width:2px
style CAPTURE fill:#eceff1,stroke:#546e7a,stroke-width:2px

```


# Experiment setup

```mermaid
flowchart LR

%% EXPERIMENT CONFIGURATION
WF["Workflow Variant"]
LLM["LLM Backend Variant"]

%% TESTBED
RUN["Agentic Traffic Testbed"]

%% METRICS
METRICS["Traffic + System Metrics"]

%% ANALYSIS
ANALYSIS["Interarrival Analysis"]
COMPARE["Cross-layer Comparison"]

%% FLOW
WF --> RUN
LLM --> RUN

RUN --> METRICS

METRICS --> ANALYSIS
ANALYSIS --> COMPARE

%% STYLING
style WF fill:#e3f2fd,stroke:#1e88e5,stroke-width:2px
style LLM fill:#fce4ec,stroke:#d81b60,stroke-width:2px
style RUN fill:#e8f5e9,stroke:#43a047,stroke-width:2px
style METRICS fill:#fff8e1,stroke:#f9a825,stroke-width:2px
style ANALYSIS fill:#ede7f6,stroke:#5e35b1,stroke-width:2px
style COMPARE fill:#eceff1,stroke:#546e7a,stroke-width:2px
```

# Networking

```mermaid
graph LR

%% ==============================
%% CENTRAL BACKBONE
%% ==============================

INTER((inter_agent_network))

%% ==============================
%% HOME NETWORKS
%% ==============================

subgraph A_NET["agent_a_network"]
agentA["agent-a"]
end

subgraph B_NET["agent_b_network"]
agentB["agent-b replicas"]
end

subgraph LLM_NET["llm_network"]
llm["llm-backend"]
end

subgraph TOOLS_NET["tools_network"]
tools["mcp-tool-db"]
end

subgraph OBS["observability"]
chat["chat-ui"]
jaeger["jaeger"]
prom["prometheus"]
graf["grafana"]
cad["cadvisor"]
dockexp["docker-mapping-exporter"]
end

%% ==============================
%% INTER-AGENT NETWORK LINKS
%% ==============================

INTER --- agentA
INTER --- agentB
INTER --- llm
INTER --- tools
INTER --- chat
INTER --- jaeger
INTER --- prom
INTER --- graf
INTER --- cad
INTER --- dockexp

%% ==============================
%% LINK COLORS (INTER NETWORK)
%% ==============================

linkStyle 0 stroke:#1e88e5,stroke-width:3px
linkStyle 1 stroke:#1e88e5,stroke-width:3px
linkStyle 2 stroke:#1e88e5,stroke-width:3px
linkStyle 3 stroke:#1e88e5,stroke-width:3px
linkStyle 4 stroke:#1e88e5,stroke-width:3px
linkStyle 5 stroke:#1e88e5,stroke-width:3px
linkStyle 6 stroke:#1e88e5,stroke-width:3px
linkStyle 7 stroke:#1e88e5,stroke-width:3px
linkStyle 8 stroke:#1e88e5,stroke-width:3px
linkStyle 9 stroke:#1e88e5,stroke-width:3px

%% ==============================
%% STYLING
%% ==============================

style INTER fill:#e3f2fd,stroke:#1e88e5,stroke-width:3px

style A_NET fill:#e8f5e9,stroke:#43a047,stroke-width:2px
style B_NET fill:#e8f5e9,stroke:#43a047,stroke-width:2px
style LLM_NET fill:#fff8e1,stroke:#f9a825,stroke-width:2px
style TOOLS_NET fill:#fce4ec,stroke:#d81b60,stroke-width:2px
style OBS fill:#ede7f6,stroke:#5e35b1,stroke-width:2px
```


# Agent communication flows

```mermaid
flowchart LR

%% Agents & services
agentA["agent-a"]
agentB["agent-b replicas"]
llm["llm-backend"]
tools["mcp-tool-db"]
prom["prometheus"]

%% Agent flows
agentA -->|subtasks| agentB
agentA -->|inference| llm
agentA -->|tool calls| tools

agentB -->|inference| llm

%% Telemetry
prom -->|scrape| agentA
prom -->|scrape| agentB
prom -->|scrape| llm
prom -->|scrape| tools

%% STYLING
style agentA fill:#e8f5e9,stroke:#43a047,stroke-width:2px
style agentB fill:#e8f5e9,stroke:#43a047,stroke-width:2px
style llm fill:#fff8e1,stroke:#f9a825,stroke-width:2px
style tools fill:#fce4ec,stroke:#d81b60,stroke-width:2px
style prom fill:#e3f2fd,stroke:#1e88e5,stroke-width:2px
```

# Fixed IP address reference

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

# Metrics

| Component             | Metric                              | Description / Meaning                                  |
| --------------------- | ----------------------------------- | ------------------------------------------------------ |
| **Workflow**          | Workflow latency                    | Time between workflow steps or agent interactions      |
|                       | Inter-agent timing                  | Delays between agents sending/receiving messages       |
| **Agents + MCP**      | Token usage                         | Number of tokens consumed per agent action             |
|                       | Request latency                     | Time taken to execute agent request or tool invocation |
|                       | Agent actions                       | Count / type of agent operations executed              |
|                       | Tool invocation rate (MCP)          | How frequently MCP tools are called by agents          |
| **Containers**        | CPU usage                           | Container CPU consumption                              |
|                       | Memory usage                        | Container memory footprint                             |
|                       | Network I/O                         | Bytes sent/received by container                       |
| **LLM Backend**       | Token throughput                    | Tokens processed per second by model                   |
|                       | Model latency                       | Time to generate responses from LLM                    |
|                       | Request rate                        | Number of requests handled per second                  |
|                       | API latency / errors (external LLM) | Response time and failure count for external servers   |
| **Networking**        | Connection counts                   | Number of active TCP/UDP connections                   |
|                       | Packet rates                        | Packets per second transmitted on docker networks      |
|                       | Interarrival time                   | Time between consecutive packets                       |
| **Traffic Capture**   | Packet timestamps                   | Raw timestamps of captured packets                     |
|                       | Flow distribution                   | Packet flows between agents / containers               |
|                       | Traffic burstiness                  | Measure of traffic clustering / variability            |
