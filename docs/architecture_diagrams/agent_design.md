```mermaid
flowchart LR

    %% User entrypoint
    User["User / Task Request"] --> Orchestrator["AutoGen Orchestrator<br>(Multi-Agent Workflow Engine)"]

    %% AutoGen agents (inter-agent layer)
    subgraph AutoGenAgents["AutoGen Agents (Inter-Agent Coordination Layer)"]
        Planner["Planner Agent<br>(AutoGen)"]
        Worker["Worker Agent<br>(AutoGen)"]
        Critic["Critic Agent<br>(AutoGen)"]
    end

    Orchestrator --> Planner
    Orchestrator --> Worker
    Orchestrator --> Critic

    %% Semantic Kernel inside each agent (intra-agent layer)
    subgraph SemanticKernel["Semantic Kernel (Intra-Agent Cognition Layer)"]
        SKMemory["SK Memory<br>(vectors, embeddings, recall)"]
        SKPlanner["SK Reasoning / Planning Engine"]
        SKSkills["SK Skills / Tool Interfaces"]
    end

    SKMemory --> SKPlanner
    SKPlanner --> SKSkills

    %% Each AutoGen agent uses SK internally
    Planner --> SKPlanner
    Worker --> SKPlanner
    Critic --> SKPlanner

    %% LLM backend used by SK (separate node/network)
    subgraph LLMNode["LLM Node (Separate Network)"]
        LLM["LLM Backend<br>(Llama via vLLM, TGI, Ollama, etc.)"]
    end
    SKPlanner -->|LLM queries| LLM

    %% Tooling layer - on separate node/network from agents
    subgraph ToolsNode["MCP Tools Node (Separate Network)"]
        MCPTools["MCP Tool Servers"]
        HTTPAPI["REST / HTTP APIs"]
        DB["Databases / Internal Services"]
    end

    SKSkills -->|MCP tool calls<br>(cross-network)| MCPTools
    SKSkills --> HTTPAPI
    SKSkills --> DB

    %% MCP is a protocol between tools and agent frameworks
    MCPTools -. "MCP Protocol" .- Orchestrator

```

## Network Isolation

The architecture separates agents, LLM backend, and MCP tools onto different networks/nodes:

| Component | Network | Purpose |
|-----------|---------|---------|
| Agents (Planner, Worker, Critic) | `agent_network` | Inter-agent coordination |
| LLM Backend | `llm_network` | Model inference |
| MCP Tool Servers | `mcp_network` | External tool access |

This separation enables:
- **Traffic analysis**: Observable patterns between agents and tools
- **Independent scaling**: Tools can scale independently of agents
- **Security isolation**: Tool access can be controlled at the network level
- **Latency measurement**: Real network hops between components