FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3 python3-dev python3-pip python3-venv ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy repo-level requirements (build context is repo root via docker-compose)
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Copy llm package
COPY llm /app/llm

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["python3", "-m", "llm.serve_llm", "--model", "meta-llama/Llama-3.1-8B-Instruct", "--host", "0.0.0.0", "--port", "8000"]



