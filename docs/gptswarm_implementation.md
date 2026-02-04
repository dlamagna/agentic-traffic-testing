GPTSwarm Integration
=====================

This document describes how the [GPTSwarm](https://github.com/metauto-ai/gptswarm) framework
is integrated with the agentic traffic testing testbed so that **all GPTSwarm agents run
on the same local LLaMA 3.1 8B backend** used by Agent A / Agent B (no OpenAI keys required).

References
----------

- **Paper**: [GPTSwarm: Language Agents as Optimizable Graphs](https://arxiv.org/pdf/2402.16823)  
- **Code**: [metauto-ai/GPTSwarm](https://github.com/metauto-ai/gptswarm)

Overview
--------

GPTSwarm exposes a high-level `Swarm` class:

- `swarm.graph.swarm.Swarm(agent_names, domain, model_name=..., ...)`
- Example from the official README:

```python
from swarm.graph.swarm import Swarm

swarm = Swarm(["IO", "IO", "IO"], "gaia")
task = "What is the capital of Jordan?"
inputs = {"task": task}
answer = swarm.run(inputs)
```

Under the hood, GPTSwarm routes all LLM calls through `swarm.llm.GPTChat`. That module
supports:

- **OpenAI** (default): uses your `OPENAI_API_KEY` values.
- **LM Studio mode**: when `model_name="lmstudio"`, it sends OpenAI-style Chat
  Completions requests to:

  ```python
  LM_STUDIO_URL = "http://localhost:1234/v1"
  ```

Our integration hooks into this LM Studio path by providing a **local proxy** that
implements `/v1/chat/completions` and forwards to the existing `llm-backend` service.

Architecture Mapping
--------------------

| GPTSwarm Concept         | Our Testbed Implementation                           |
|--------------------------|------------------------------------------------------|
| LLM backend              | `llm-backend` (vLLM serving LLaMA 3.1 8B)            |
| OpenAI / LM Studio API   | `llm.lmstudio_proxy` (Chat Completions proxy)       |
| Swarm model_name         | `model_name="lmstudio"` (uses the proxy)            |
| Task input               | Python script or future HTTP wrapper (TBD)          |

The key idea is:

- **Do not modify GPTSwarm internals.**  
- Instead, provide an **OpenAI-compatible Chat Completions endpoint** that speaks the
  same protocol GPTSwarm expects for LM Studio, and bridge that to the existing
  `/chat` endpoint of `llm-backend`.

LM Studio-Compatible Proxy
--------------------------

File: `llm/lmstudio_proxy.py`

This module exposes:

- `POST /v1/chat/completions`
- `GET  /health`, `/ready`, `/live` (for basic health checks)

Behavior:

1. **Accepts** OpenAI Chat Completions-style requests:

   - `model` (e.g. `"lmstudio"`)
   - `messages`: list of `{role, content}` items
   - `max_tokens` (optional)
   - `temperature` (optional, currently ignored by the backend)

2. **Transforms** them into the existing `/chat` payload:

   - Maps all `system` messages into a `system_prompt` string.
   - Concatenates the remaining messages (user/assistant/etc.) into a plain `prompt`.
   - Sends `{"prompt": prompt, "system_prompt": system_prompt, "max_tokens": ...}` to
     `LLM_SERVER_URL` (default `http://localhost:8000/chat`).

3. **Wraps** the vLLM response back into an OpenAI-style Chat Completions object:

   - `choices[0].message.content` contains the generated text.
   - `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens` are filled
     from the `meta` block produced by `llm.serve_llm`.

This keeps GPTSwarm’s `cost_count` and other helpers working unchanged, while reusing
the same LLaMA 3.1 backend as AgentVerse.

Configuration
-------------

### Environment Variables (Proxy)

For `llm.lmstudio_proxy`:

| Variable              | Default                     | Description                                |
|-----------------------|-----------------------------|--------------------------------------------|
| `LLM_SERVER_URL`      | `http://localhost:8000/chat`| URL of vLLM backend (`llm-backend` service)|
| `LMSTUDIO_PROXY_HOST` | `0.0.0.0`                   | Bind host for proxy                        |
| `LMSTUDIO_PROXY_PORT` | `1234`                      | Bind port for proxy                        |

GPTSwarm expects LM Studio at `http://localhost:1234/v1`, so by default we bind:

- `host=0.0.0.0`, `port=1234` and serve under `/v1/chat/completions`.

### Python Requirements

GPTSwarm is **optional** and kept separate from the core testbed dependencies to avoid
slowing down Docker builds for AgentVerse.

**Installation (optional):**

```bash
# Install core dependencies first (for AgentVerse, etc.)
pip install -r requirements.txt

# Then install GPTSwarm on top (only when needed)
pip install -r requirements-gptswarm.txt
```

**Note:** `requirements-gptswarm.txt` includes a compatible `httpx` version override
(`httpx>=0.25.2,<0.26.0`) because GPTSwarm requires `httpx<0.26.0`, while the core
testbed uses `httpx>=0.27.0`. This is safe because GPTSwarm is only used in optional
experiment scripts, not in the core AgentVerse deployment.

**For Docker builds:**

If you want GPTSwarm available in your containers, you can either:

1. **Install manually** after the container starts (for experimentation):
   ```bash
   docker exec -it <container> pip install -r /app/requirements-gptswarm.txt
   ```

2. **Create a separate Dockerfile** that extends the base image and adds GPTSwarm:
   ```dockerfile
   FROM <your-base-image>
   COPY requirements-gptswarm.txt /app/
   RUN pip install -r /app/requirements-gptswarm.txt
   ```

3. **Use a multi-stage build** or conditional install based on a build arg (more advanced).

Running GPTSwarm Against the Local LLM
--------------------------------------

### Option 1: Docker Compose (Recommended)

1. **Start the core services** (if not already running):
   ```bash
   cd infra
   docker compose up -d llm-backend
   ```

2. **Start GPTSwarm services**:
   ```bash
   docker compose --profile gptswarm up -d lmstudio-proxy
   ```

3. **Run a GPTSwarm experiment**:
   ```bash
   docker compose run --rm gptswarm python -m scripts.experiment.run_gptswarm_docker \
     "Compare two strategies for shaping network traffic in a microservices environment."
   ```

   The `gptswarm` container will:
   - Connect to `lmstudio-proxy:1234/v1` (patched automatically)
   - Which forwards to `llm-backend:8000/chat`
   - All using the same LLaMA 3.1 model as AgentVerse

### Option 2: Local Development (Host)

1. **Start the vLLM backend** (as you already do), e.g. via `infra/docker-compose.yml`:

   - Service: `llm-backend`
   - Exposes: `http://localhost:8000/chat`

2. **Install GPTSwarm dependencies**:
   ```bash
   pip install -r requirements-gptswarm.txt
   ```

3. **Start the LM Studio-compatible proxy** on the host:

   ```bash
   cd /home/dlamagna/projects/agentic-traffic-testing

   # Optionally override backend URL if different
   export LLM_SERVER_URL=http://localhost:8000/chat

   python -m integrations.gptswarm.proxy
   # Now listening on http://0.0.0.0:1234, GPTSwarm will see http://localhost:1234/v1
   ```

4. **Run a minimal GPTSwarm demo**:

   ```bash
   python -m integrations.gptswarm.scripts.demo "Your task here"
   ```

   Or from Python:
   ```python
   from swarm.graph.swarm import Swarm

   swarm = Swarm(["IO", "IO", "IO"], "gaia", model_name="lmstudio")
   result = swarm.run({"task": "Compare two network traffic shaping strategies."})
   print(result)
   ```

   All LLM calls inside GPTSwarm will:

   - Hit `/v1/chat/completions` on `llm.lmstudio_proxy`
   - Be forwarded to `/chat` on `llm-backend`
   - Be served by the same LLaMA 3.1 model used by AgentVerse.

Relationship to AgentVerse
--------------------------

AgentVerse in this repo already uses:

- A **single shared LLM backend** (`llm-backend`).
- HTTP-based orchestration via Agent A / Agent B that talks to `/chat`.

The GPTSwarm integration mirrors that design:

- GPTSwarm’s **graph-based agents** are still pure Python (no HTTP changes needed).
- All heavy lifting is centralized in the same vLLM service.
- A thin **protocol adapter** (`llm.lmstudio_proxy`) bridges GPTSwarm’s OpenAI-style
  expectations to the existing `/chat` interface.

Next Steps
----------

The current integration focuses on wiring GPTSwarm to your local LLM.
Possible follow-ups:

1. **Experiment scripts**: add dedicated `scripts/experiment/run_gptswarm_experiment.py`
   that:
   - Starts a Swarm (e.g. `["IO", "TOT"]`, domain `"gaia"`, `model_name="lmstudio"`).
   - Logs traffic and results similarly to the existing MVP scripts.

2. **HTTP wrapper for Swarm**: expose a simple `/gptswarm` endpoint (similar to
   `/agentverse`) that accepts a `task` and runs a configurable Swarm.

3. **UI integration**: add a GPTSwarm tab in the UI to visualize graph executions and
   compare them side by side with AgentVerse workflows.

