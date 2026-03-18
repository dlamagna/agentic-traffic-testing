# MCP-Universe Integration

Integrates the [MCP-Universe](https://github.com/SalesforceAIResearch/MCP-Universe) benchmark framework for execution-based MCP tool evaluation across 6 domains. All LLM calls are routed to the local vLLM backend via an OpenAI-compatible proxy, so the full testbed telemetry stack (TCP metrics, token usage, Prometheus) captures every benchmark request.

- **Upstream repo**: `https://github.com/SalesforceAIResearch/MCP-Universe`
- **Paper**: [MCP-Universe: Benchmarking Large Language Models with Real-World Model Context Protocol Servers](https://arxiv.org/abs/2508.14704)

---

## 1. Domains and metrics

| Domain | External dependency | Metric |
|--------|--------------------|----|
| Location Navigation | Google Maps API | SR / AE / AS |
| Repository Management | GitHub PAT | SR / AE / AS |
| Financial Analysis | Yahoo Finance (often keyless) | SR / AE / AS |
| 3D Design | Blender executable | SR / AE / AS |
| Browser Automation | Playwright MCP | SR / AE / AS |
| Web Search | SerpAPI | SR / AE / AS |
| `dummy` | None | SR / AE / AS |

**Metrics:**
- **SR** (Success Rate) — fraction of tasks fully passed
- **AE** (Average Evaluator score) — fraction of individual evaluators passed
- **AS** (Average Steps) — mean agent steps for successful tasks

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│           MCP-Universe Benchmark Runner                       │
│       (scripts/experiment/run_mcp_universe.py)                │
└─────────────────────────┬────────────────────────────────────┘
                          │ PYTHONPATH=$MCP_UNIVERSE_DIR
                          ▼
┌──────────────────────────────────────────────────────────────┐
│               MCP-Universe Framework                          │
│  (BenchmarkRunner, ReAct Agent, MCP servers, evaluators)      │
└─────────────────────────┬────────────────────────────────────┘
                          │ OpenAI client
                          │ OPENAI_BASE_URL=http://...:8110/v1
                          ▼
┌──────────────────────────────────────────────────────────────┐
│         OpenAI Proxy  (tools/mcp-universe/openai_proxy)       │
│         POST /v1/chat/completions → local LLM backend         │
└─────────────────────────┬────────────────────────────────────┘
                          │ HTTP
                          ▼
┌──────────────────────────────────────────────────────────────┐
│             Local LLM Backend (llm/serve_llm)                 │
│             POST /chat (vLLM + Llama)                         │
└──────────────────────────────────────────────────────────────┘
```

MCP-Universe is **not vendored** — clone and install it separately, then point the testbed at it via `MCP_UNIVERSE_DIR`.

---

## 3. Setup

### 3.1 Clone MCP-Universe

```bash
git clone https://github.com/SalesforceAIResearch/MCP-Universe.git
cd MCP-Universe
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r dev-requirements.txt
cd ..
export MCP_UNIVERSE_DIR=/path/to/MCP-Universe
```

### 3.2 Start the local LLM backend

```bash
# Direct
python -m llm.serve_llm --port 8000

# Or via Docker Compose
cd infra && docker compose up -d llm-backend
```

### 3.3 Start the OpenAI proxy

MCP-Universe expects an OpenAI Chat Completions endpoint. The proxy translates to the testbed's `/chat` format:

```bash
python -m tools.mcp_universe.openai_proxy \
  --port 8110 \
  --backend-url http://localhost:8000/chat
```

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_SERVER_URL` | `http://llm-backend:8000/chat` | Local LLM `/chat` endpoint |

### 3.4 Point MCP-Universe at the proxy

```bash
export OPENAI_API_KEY=dummy
export OPENAI_BASE_URL=http://localhost:8110/v1
```

---

## 4. Running benchmarks

```bash
export MCP_UNIVERSE_DIR=/path/to/MCP-Universe

# List available domains
python scripts/experiment/run_mcp_universe.py --list

# Smoke test (no external keys needed)
python scripts/experiment/run_mcp_universe.py dummy

# Individual domains
python scripts/experiment/run_mcp_universe.py location_navigation
python scripts/experiment/run_mcp_universe.py financial_analysis

# All discovered benchmarks
python scripts/experiment/run_mcp_universe.py --all
```

The runner adds `MCP_UNIVERSE_DIR` to `PYTHONPATH` and invokes MCP-Universe's test modules under `tests/benchmark/mcpuniverse/`.

---

## 5. Domain API keys

Configure in MCP-Universe's `.env`:

```bash
cp $MCP_UNIVERSE_DIR/.env.example $MCP_UNIVERSE_DIR/.env
# edit .env with your keys
```

| Domain | Variables |
|--------|-----------|
| Web Search | `SERP_API_KEY` |
| Location Navigation | `GOOGLE_MAPS_API_KEY` |
| Repository Management | `GITHUB_PERSONAL_ACCESS_TOKEN`, `GITHUB_PERSONAL_ACCOUNT_NAME` |
| Financial Analysis | Yahoo Finance MCP — often keyless |
| 3D Design | `BLENDER_APP_PATH` |
| Browser Automation | Playwright MCP (installed via `playwright install`) |

---

## 6. Testbed telemetry

LLM traffic flows through the local backend and proxy, so every benchmark request appears in:

- `logs/llm_calls.jsonl` — per-call token counts, latency, `task_id`
- Prometheus `llm_*` metrics — TTFT, throughput, in-flight
- TCP metrics collector — `tcp_bytes_total`, flow duration, RTT by service pair
- `scripts/experiment/correlate_metrics.py` — joins application logs with Prometheus over the task time window

MCP-Universe's ReAct agent loop generates multi-turn tool-call sequences (agent → MCP server → agent → LLM), producing the iterative bursty traffic patterns that are the primary research subject of this testbed.

---

## 7. File layout

```
agentic-traffic-testing/
├── tools/
│   └── mcp_universe/
│       ├── __init__.py
│       └── openai_proxy.py       # OpenAI-compatible proxy for local LLM
├── scripts/
│   └── experiment/
│       └── run_mcp_universe.py   # Benchmark runner
└── docs/
    └── benchmarks/
        └── mcp_universe.md       # This file
```

---

## 8. Troubleshooting

**"MCP-Universe directory not found"**
- Ensure `MCP_UNIVERSE_DIR` is set and points to the repo root, or pass `--mcp-universe-dir /path`.

**"Backend LLM request failed" (502 from proxy)**
- Confirm the LLM backend is running at `--backend-url`. Check Docker networking (`host.docker.internal` on Mac/Windows).

**Benchmark fails with missing API keys**
- Use `dummy` first to verify the proxy chain. Configure `.env` for domains requiring external APIs.

**Import errors**
- Ensure MCP-Universe dependencies are installed in the same Python environment, or that `PYTHONPATH` includes `$MCP_UNIVERSE_DIR` (the runner sets this automatically).
