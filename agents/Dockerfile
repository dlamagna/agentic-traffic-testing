FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# Install Python dependencies and networking tools for tc/netem
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

ENV PYTHONPATH=/app

EXPOSE 8101 8102

# The specific command is provided per-service in docker-compose.yml
CMD ["sleep", "infinity"]


