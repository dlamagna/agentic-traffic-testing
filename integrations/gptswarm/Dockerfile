# Dockerfile for GPTSwarm experiments
# Extends the base Python image and adds GPTSwarm dependencies
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install base dependencies (same as agents/Dockerfile)
COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && rm -rf /var/lib/apt/lists/*

# Install GPTSwarm optional dependencies
COPY integrations/gptswarm/requirements.txt /app/integrations/gptswarm/requirements.txt
RUN pip install --no-cache-dir -r /app/integrations/gptswarm/requirements.txt

# Copy the entire repo (needed for scripts and llm module)
COPY . /app

ENV PYTHONPATH=/app

# Default: run the Docker-optimized demo script, but can be overridden
CMD ["python", "-m", "integrations.gptswarm.scripts.docker"]
