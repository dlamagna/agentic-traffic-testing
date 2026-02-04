# GPTSwarm Integration

This directory contains the GPTSwarm integration for the agentic traffic testing testbed.

## Structure

- `proxy.py` - LM Studio-compatible Chat Completions proxy that bridges GPTSwarm to the local LLM backend
- `Dockerfile` - Docker image for running GPTSwarm experiments
- `requirements.txt` - GPTSwarm-specific Python dependencies
- `scripts/` - Demo scripts for running GPTSwarm
  - `demo.py` - Local development script
  - `docker.py` - Docker-optimized script with URL patching

## Quick Start

See the main documentation: `docs/gptswarm_implementation.md`

## Files

- **Proxy**: `integrations/gptswarm/proxy.py` - Runs the OpenAI-compatible API server
- **Docker**: `integrations/gptswarm/Dockerfile` - Container image for GPTSwarm
- **Scripts**: `integrations/gptswarm/scripts/` - Experiment scripts
