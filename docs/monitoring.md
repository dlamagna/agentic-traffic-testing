## Monitoring Overview

The testbed includes an optional monitoring stack that covers three layers:

- **Network layer**: inter-container / inter-service traffic
- **Application / request layer**: end‑to‑end latency for LLM calls
- **AI performance layer**: LLM tokens, latency, and time‑to‑first‑token

When `ENABLE_MONITORING=1` in `infra/.env`, deployment will:

- Start **Prometheus**, **Grafana**, and **cAdvisor** via the monitoring compose files.
- Attempt to start the **TCP metrics collector** on the host for service‑level network metrics.

---

## Components

- **Prometheus**
  - Config: `infra/monitoring/prometheus.yml`
  - Scrapes:
    - `prometheus` itself
    - `cadvisor:8080` (container / host metrics)
    - `llm-backend:8000/metrics` (LLM performance metrics)
    - `docker-mapping-exporter:9101` (Docker ID → name mappings)
    - `tcp_metrics_collector.py` on port `9100` (TCP service‑level metrics)

- **Grafana**
  - Config / dashboards:
    - Datasource: `infra/monitoring/grafana/provisioning/datasources/datasources.yml`
    - Dashboard: `infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json`
  - UI: `http://localhost:3001` (admin/admin by default)
  - The **Agentic Traffic Testbed** dashboard is provisioned on startup.

- **cAdvisor**
  - Runs as a container via `infra/docker-compose.monitoring*.yml`
  - Exposes `/metrics` on `http://localhost:8080/metrics`.
  - Provides `container_*` CPU, memory, and network metrics used in the **Resource Usage** and **Docker Network** panels.
  - On some hosts cAdvisor may initially only expose a single `id="/"` series (host‑level aggregate). To get **per‑container** metrics (one time series per container with labels such as `name` or `container_label_com_docker_compose_service`), cAdvisor must have working read‑only access to:
    - `/sys/fs/cgroup`
    - `/var/lib/docker`
    - `/var/run/docker.sock`
    and Docker must be using cgroups in a way that cAdvisor understands (including cgroups v2). See "Enabling per‑container CPU / memory metrics" below.

- **Docker Mapping Exporter**
  - Script: `scripts/monitoring/docker_mapping_exporter.py`
  - Runs as a container, exposes metrics on `http://localhost:9101/metrics`.
  - Queries the Docker Engine API via the Unix socket (`/var/run/docker.sock`, mounted read-only).
  - Produces three mapping metric families (gauge, always value `1`):

    | Metric | Labels | Description |
    |---|---|---|
    | `docker_network_mapping` | `interface`, `network_name` | Maps `br-xxxx` bridge IDs → Docker network name |
    | `docker_container_mapping` | `id`, `container_name`, `service_name` | Maps `/system.slice/docker-<id>.scope` cgroup paths → container/service name |
    | `docker_ip_mapping` | `ip_address`, `container_name`, `service_name` | Maps container IPs on `infra_inter_agent_network` → container/service name |

  - Results are cached for 10 seconds (configurable via `CACHE_TTL` env var).
  - **Performance**: ~5 ms per Docker query; exporter uses ~30–50 MB memory (Python 3.11 slim + Docker CLI).
  - These mappings are used by Grafana panels via PromQL vector joins (e.g. `* on(interface) group_left(network_name) docker_network_mapping`) to replace raw bridge IDs and cgroup paths with human-readable names.

### Grafana “nicknames” (human‑readable labels)

The dashboard is configured so panels show **Docker network / service names** instead of raw bridge IDs (`br-…`) and systemd cgroup scopes (`/system.slice/docker-….scope`).

#### Quick start

From the repo root:

```bash
docker compose -f infra/docker-compose.yml -f infra/docker-compose.monitoring.yml down
docker compose -f infra/docker-compose.yml -f infra/docker-compose.monitoring.yml up -d
```

#### Verify

- Exporter metrics:

  ```bash
  curl http://localhost:9101/metrics
  ```

- Prometheus has the mapping series:

  ```bash
  curl 'http://localhost:9090/api/v1/query?query=docker_network_mapping'
  ```

#### Example query (bridge ID → network name)

```promql
rate(container_network_transmit_bytes_total{id="/",interface=~"br-.*"}[30s])
  * on(interface) group_left(network_name) docker_network_mapping
```

Set the panel legend to `{{network_name}} TX` (or `RX`) to display the friendly name.

#### Troubleshooting

- **Nicknames not showing**
  - Ensure the exporter is running: `docker ps | grep docker-mapping-exporter`
  - Check logs: `docker logs docker-mapping-exporter` and `docker logs prometheus`
  - Force Grafana to reload: `docker restart grafana`

#### Performance impact

- **Exporter overhead**: typically tens of milliseconds per scrape (results cached)
- **Prometheus**: one additional scrape job, small metrics payload
- **Grafana**: PromQL joins add modest query overhead (usually negligible for this dashboard)

#### Advanced customization

- **Change scrape interval**: update the `docker-mapping` job in `infra/monitoring/prometheus.yml`.
- **Add nickname support to another bridge-based panel**: join with:

  ```promql
  <your_metric> * on(interface) group_left(network_name) docker_network_mapping
  ```

- **TCP Metrics Collector**
  - Script: `scripts/monitoring/tcp_metrics_collector.py`
  - Runs on the **host**, not in Docker.
  - Captures TCP traffic on the `inter_agent_network` bridge using `tcpdump`.
  - Exposes a Prometheus `/metrics` endpoint on `http://localhost:9100/metrics` with:
    - `tcp_packets_total{src_service, dst_service}`
    - `tcp_bytes_total{src_service, dst_service}`
    - `tcp_flow_duration_seconds_bucket{src_service, dst_service, le}`
    - `tcp_rtt_handshake_seconds_bucket{src_service, dst_service, le}`
    - `tcp_packet_size_bytes_bucket{le}`
    - `tcp_syn_total`, `tcp_fin_total`, `tcp_rst_total`

---

## How the TCP metrics collector is started

When `ENABLE_MONITORING=1`, `scripts/deploy/deploy.sh` will:

- Deploy Prometheus, Grafana, and cAdvisor for the selected mode (`single` or `distributed`).
- Then ensure the TCP metrics collector is running on the host:

  - It checks for an existing process:

    ```bash
    pgrep -f "tcp_metrics_collector.py"
    ```

  - If not found, it starts the collector in the background from the repo root:

    ```bash
    python3 scripts/monitoring/tcp_metrics_collector.py --sudo-tcpdump \
      >> logs/tcp_metrics_collector.log 2>&1 &
    ```

  - You **may be prompted for your sudo password for `tcpdump`**. The Python process itself is not run with sudo; only `tcpdump` is.

If automatic start fails (for example, if `sudo tcpdump` is not allowed), the deploy script will print a warning and you can start the collector manually (see below).

---

## Running the TCP metrics collector manually

From the repo root:

```bash
cd /home/dlamagna/projects/agentic-traffic-testing
python3 scripts/monitoring/tcp_metrics_collector.py --sudo-tcpdump
```

Notes:

- The script will:
  - Auto‑detect the Docker bridge for `inter_agent_network` (e.g. `br-df4088ff2909`).
  - Apply the default filter `tcp and net 172.23.0.0/24`.
  - Expose metrics on `http://localhost:9100/metrics`.
- You need to have permission to run **`sudo tcpdump`**, since packet capture requires elevated privileges.

If `sudo tcpdump` works but running `--sudo-tcpdump` inside Python does not (due to sudoers/TTY quirks), you can instead pipe `tcpdump` output into the collector:

```bash
cd /home/dlamagna/projects/agentic-traffic-testing
sudo tcpdump -i br-df4088ff2909 -l -n -tt tcp and net 172.23.0.0/24 \
  | python3 scripts/monitoring/tcp_metrics_collector.py --read-stdin
```

In this mode:

- `sudo` is only used for `tcpdump`.
- The collector reads line-oriented tcpdump output from `stdin` and still exposes metrics at `http://localhost:9100/metrics`.

To run without sudo at all (only if an admin has granted the necessary capabilities to `tcpdump` or your user), omit both sudo and `--sudo-tcpdump`:

```bash
python3 scripts/monitoring/tcp_metrics_collector.py
```

---

## Dashboard Layout (Agentic Traffic Testbed)

The provisioned Grafana dashboard (`agentic-traffic-testbed`) is structured into the following rows. All panels use the `${datasource}` variable pointing at Prometheus.

### 1. Overview

| Panel | PromQL | Unit |
|---|---|---|
| Active Containers (Docker) | `count(container_cpu_usage_seconds_total{cpu="total",id=~"/system.slice/docker-.*\\.scope"})` | count |
| Docker Network TX Rate | `sum(rate(container_network_transmit_bytes_total{id="/",interface=~"br-.*"}[1m]))` | Bps |
| Docker Network RX Rate | `sum(rate(container_network_receive_bytes_total{id="/",interface=~"br-.*"}[1m]))` | Bps |
| LLM Request Rate — success vs error | `rate(llm_requests_total{status="success"}[30s])` / `rate(llm_requests_total{status="error"}[30s])` | req/s |

### 2. Network Traffic

Network panels use `docker_network_mapping` PromQL joins so bridge IDs become human-readable Docker network names.

| Panel | PromQL | Unit |
|---|---|---|
| Network Transmit Rate by Interface | `rate(container_network_transmit_bytes_total{id="/",interface=~"br-.*"}[30s]) * on(interface) group_left(network_name) docker_network_mapping` | Bps |
| Network Receive Rate by Interface | `rate(container_network_receive_bytes_total{id="/",interface=~"br-.*"}[30s]) * on(interface) group_left(network_name) docker_network_mapping` | Bps |
| Packets Transmitted (by Interface) | `rate(container_network_transmit_packets_total{id="/",interface=~"br-.*"}[30s]) * on(interface) group_left(network_name) docker_network_mapping` | pps |
| Packets per Minute (by Interface) | `increase(container_network_transmit_packets_total{id="/",interface=~"br-.*"}[1m]) * on(interface) group_left(network_name) docker_network_mapping` | pkts |

### 3. Resource Usage

Resource panels use `docker_container_mapping` joins so cgroup scope IDs become compose service names.

| Panel | PromQL | Unit |
|---|---|---|
| CPU (core equivalents per container) | `sum by (id) (rate(container_cpu_usage_seconds_total{cpu="total",id=~"/system.slice/docker-.*\\.scope"}[1m])) * on(id) group_left(service_name) docker_container_mapping` | cores |
| Memory Usage per container | `container_memory_usage_bytes{id=~"/system.slice/docker-.*\\.scope"} * on(id) group_left(service_name) docker_container_mapping` | bytes |

### 4. Service‑level Network (TCP)

| Panel | PromQL | Unit |
|---|---|---|
| TCP Bytes/s by Service Pair | `rate(tcp_bytes_total{src_service!="external",dst_service!="external",src_service!="jaeger",dst_service!="jaeger"}[1m])` — legend `src → dst` | Bps |
| TCP Bytes/s from LLM Backend | `sum(rate(tcp_bytes_total{src_service="llm_backend",dst_service!="external"}[1m]))` | Bps |
| TCP RTT (SYN/SYN-ACK Agent A → LLM) | `histogram_quantile(0.5/0.95, sum by (le) (rate(tcp_rtt_handshake_seconds_bucket{src_service="agent_a",dst_service="llm_backend"}[5m])))` | s |
| TCP Flow Duration (Agent A → LLM) | `histogram_quantile(0.5/0.95, sum by (le) (rate(tcp_flow_duration_seconds_bucket{src_service="agent_a",dst_service="llm_backend"}[5m])))` | s |

### 5. AI Performance (LLM)

| Panel | PromQL | Unit |
|---|---|---|
| LLM End-to-end Latency (p50/p95) | `histogram_quantile(0.5/0.95, sum by (le) (rate(llm_request_latency_seconds_bucket[5m])))` | s |
| LLM Time-to-First-Token (TTFT p50/p95) | `histogram_quantile(0.5/0.95, sum by (le) (rate(llm_queue_wait_seconds_bucket[5m])))` | s |
| Prompt Tokens / s | `rate(llm_prompt_tokens_total[1m])` | tok/s |
| Completion Tokens / s | `rate(llm_completion_tokens_total[1m])` | tok/s |
| In-flight LLM Requests | `llm_inflight_requests` | count |
| LLM Tokens & In-flight Requests (overlay) | `rate(llm_prompt_tokens_total[1m])`, `rate(llm_completion_tokens_total[1m])`, `llm_inflight_requests` | mixed |

### 6. Interarrival Interpretation

Panels focused on understanding the inter-request arrival pattern (burstiness, clustering, queue effects).

| Panel | PromQL | Unit |
|---|---|---|
| LLM Interarrival Time (30s rolling avg) | `1 / sum(rate(llm_requests_total[30s]))` | s |
| Request arrivals in last 4s (by status) | `increase(llm_requests_total{status="success"}[4s])`, `increase(llm_requests_total{status="error"}[4s])` | count |
| LLM Request Rate — success vs error (30s window) | `rate(llm_requests_total{status="success/error"}[30s])` | req/s |
| LLM End-to-end Latency (p50/p95) | *(repeat of AI Performance panel — for side-by-side context)* | s |
| LLM Time-to-First-Token (TTFT p50/p95) | *(repeat of AI Performance panel — for side-by-side context)* | s |
| Concurrent In-flight Requests (burst signature) | `llm_inflight_requests` | count |

### 7. LLM Configuration

Static/config panels showing how vLLM is configured for the current run. These are useful for cross-referencing performance results against configuration.

| Panel | PromQL | Unit |
|---|---|---|
| vLLM KV-cache max concurrency | `llm_computed_max_concurrency` | count |
| vLLM max_num_batched_tokens | `llm_config_max_num_batched_tokens` | count |
| Max tokens per generation (LLM_MAX_TOKENS) | `llm_config_max_tokens` | count |
| GPU memory utilization target | `llm_config_gpu_memory_utilization` | % |
| LLM Errors — total (since restart) | `llm_requests_total{status="error"}` | count |
| LLM Errors — last 1h | `increase(llm_requests_total{status="error"}[1h])` | count |
| Free Concurrent Slots (KV-cache − in-flight) | `clamp_min(llm_computed_max_concurrency - llm_inflight_requests, 0)` | count |

### 8. Traffic Characterization

Deep-dive panels for classifying the traffic regime (Poisson, bursty, periodic, etc.).

| Panel | PromQL | Unit |
|---|---|---|
| Interarrival Jitter (p95 − p50) | `histogram_quantile(0.5/0.95, ...(rate(llm_interarrival_seconds_bucket[5m])))` — also shows `p95-p50` spread | s |
| Queue Wait Distribution (p50/p95/p99) + In-flight | `histogram_quantile(0.5/0.95/0.99, ...(rate(llm_queue_wait_seconds_bucket[5m])))` + `llm_inflight_requests` | s |
| Burstiness Coefficient (peak 10s / avg 5m) | `max_over_time(rate(llm_requests_total[10s])[5m:10s]) / rate(llm_requests_total[5m])` — also shows avg and peak throughput | ratio |

#### Key metrics for traffic characterization

- `llm_interarrival_seconds_bucket` — histogram of wall-clock gaps between consecutive LLM requests. A narrow distribution centred near `1/λ` indicates Poisson-like arrivals; a wide or multi-modal distribution indicates bursts or periodic patterns.
- `llm_queue_wait_seconds_bucket` — histogram of how long each request spent waiting before the LLM backend started processing it. Rising p99 relative to p50 is the first sign of queue saturation.
- Burstiness coefficient > 1 means there are short-term spikes above the average rate; > 5 is considered highly bursty.

---

## Enabling Monitoring

1. Copy the example env file and enable monitoring:

```bash
cd infra
cp .env.example .env
```

In `infra/.env`:

```dotenv
ENABLE_MONITORING=1
```

2. Deploy from the repo root:

```bash
./scripts/deploy/deploy.sh
```

3. After deployment:

- Grafana: `http://localhost:3001` (admin/admin)
- Prometheus: `http://localhost:9090`
- cAdvisor: `http://localhost:8080`
- TCP metrics (if collector is running): `http://localhost:9100/metrics`

---

## Enabling per‑container CPU / memory metrics

By default, on some hosts cAdvisor (running in Docker) may only see a single root cgroup and export `container_*` metrics with `id="/"`. In that case Prometheus and Grafana only have **host‑level aggregates**, so panels cannot distinguish individual containers or "agent" vs non‑agent workloads.

To enable **per‑container** metrics:

1. Ensure the `cadvisor` container has read‑only access to host resources (already configured in `docker-compose.monitoring.yml`):

   - `/sys:/sys:ro`
   - `/var/lib/docker/:/var/lib/docker:ro`
   - `/var/run:/var/run:ro` (for `docker.sock`)
   - `/dev/disk/:/dev/disk:ro`
   - `privileged: true`

2. Confirm that your Docker engine is using cgroups in a way cAdvisor supports (including cgroups v2). On the host you can check:

   ```bash
   docker info | grep -i cgroup
   ```

3. Browse raw cAdvisor metrics:

   - Open `http://localhost:8080/metrics` in a browser.
   - Search for `container_cpu_usage_seconds_total` or `container_memory_usage_bytes`.
   - You should ideally see **multiple series** with labels such as `name`, `container`, or `container_label_com_docker_compose_service` rather than only `id="/"`.

4. Once per‑container series are visible in cAdvisor, Prometheus will ingest them and the Grafana **Resource Usage** panels can be updated to:

   - Group by service label, e.g. `sum by (container_label_com_docker_compose_service) (...)`.
   - Filter to only **agent containers** (e.g. `container_label_com_docker_compose_service=~"agent-a|agent-b.*"`), keeping the dashboard generic across different agent workflows.

---

## Why some panels show raw IDs instead of service names

### 1. Where the unreadable IDs come from

Some Grafana panels are built on top of **low-level cAdvisor metrics** that only expose:

- For host-level network metrics:
  - `id="/"` (the host / root cgroup)
  - `interface="br-<network_id_prefix>"` (Linux bridge names created by Docker)
- For host/systemd metrics:
  - `id="/system.slice/docker-<container_id>.scope"`

Examples:

- `br-df4088ff2909` → Linux bridge backing the `infra_inter_agent_network` Docker network.
- `/system.slice/docker-a86eb27ddbcc...scope` → systemd cgroup for a container (e.g. `agent-a`), but cAdvisor only exposes the **cgroup path**, not the friendly container or compose service name.

These IDs are meaningful to the kernel and Docker, but not to humans.

### 2. Why Grafana can’t turn them into names on its own

Grafana visualizations here are driven by **Prometheus metrics only**:

- PromQL can:
  - Filter / aggregate by labels that exist on the series (`id`, `interface`, `job`, `instance`, etc.).
- PromQL **cannot**:
  - Call out to Docker, systemd, or a shell script to “look up” `br-df4088ff2909` or `docker-a86e...scope`.
  - Join arbitrary external data (like the output of `docker ps` or a bash mapping script) at query time.

Because the cAdvisor series backing the "Network Traffic" and "Resource Usage" panels **lack service-level labels**, Prometheus and Grafana have no way to render those as `agent-a`, `agent-b-3`, or `llm-backend`.

In contrast, the **TCP collector** is designed to export metrics with logical labels:

- `tcp_bytes_total{src_service="agent_a", dst_service="llm_backend"}`
- `tcp_flow_duration_seconds_bucket{src_service="agent_a", dst_service="llm_backend", le="0.5"}`

Those labels are available directly in Prometheus, so Grafana can use them to show meaningful service names.

### 3. What cAdvisor can and cannot do

cAdvisor can be configured (depending on host setup and cgroups) to expose **container labels**:

- e.g. `container_cpu_usage_seconds_total{container_label_com_docker_compose_service="agent-a", ...}`

However:

- Those labels appear on **per-container metrics** (where `id="/system.slice/docker-...scope"`), not on the **host-level** `id="/"` metrics used for per-bridge traffic.
- The `br-...` network metrics are inherently per-interface aggregates:
  - `container_network_transmit_bytes_total{id="/", interface="br-df4088ff2909"}`
  - There is no place in that label set to attach a single "service name" because multiple containers share the same bridge.

So even with better cAdvisor labeling, the specific panels that aggregate by `interface="br-*"` will still show **bridge names**, not container names.

### 4. Why we added the TCP collector and service-level panels

To get truly human-readable, service-oriented metrics, we introduced a separate **TCP metrics collector** (`tcp_metrics_collector.py`) which:

- Watches packets on the inter-agent network bridge (e.g. `infra_inter_agent_network`).
- Uses a static IP → service mapping (`SERVICE_IPS`) to assign:
  - `src_service` and `dst_service` labels like `agent_a`, `agent_b_1`, `llm_backend`, `mcp_tool_db`, etc.
- Exposes metrics such as:
  - `tcp_packets_total{src_service="agent_a", dst_service="llm_backend"}`
  - `tcp_bytes_total{src_service="agent_a", dst_service="llm_backend"}`
  - `tcp_flow_duration_seconds_bucket{src_service="agent_a", dst_service="llm_backend", le="..."}`

This is why the **Service-level Network (TCP)** row in the dashboard can show:

- `agent_a → llm_backend` instead of raw IPs or `br-*` names.
- Latency distributions (flow duration) for specific service pairs (e.g. Agent A → LLM).

### 5. How we solved it: the Docker Mapping Exporter

The **Docker Mapping Exporter** (`scripts/monitoring/docker_mapping_exporter.py`) provides the missing link. It queries the Docker Engine API at scrape time and emits three gauge families that act as **lookup tables** in PromQL:

```
docker_network_mapping{interface="br-df4088ff2909", network_name="infra_inter_agent_network"} 1
docker_container_mapping{id="/system.slice/docker-a86e...scope", container_name="infra-agent-a-1", service_name="agent-a"} 1
docker_ip_mapping{ip_address="172.23.0.5", container_name="infra-agent-a-1", service_name="agent-a"} 1
```

Grafana panels then join these with cAdvisor metrics using PromQL vector matching:

```promql
# Interface → network name
rate(container_network_transmit_bytes_total{id="/",interface=~"br-.*"}[30s])
  * on(interface) group_left(network_name) docker_network_mapping

# Cgroup scope → service name
sum by (id) (rate(container_cpu_usage_seconds_total{cpu="total",id=~"/system.slice/docker-.*\\.scope"}[1m]))
  * on(id) group_left(service_name) docker_container_mapping
```

This gives every Network Traffic and Resource Usage panel human-readable labels without requiring Kubernetes or a service mesh.

### 6. Possible future improvements

- **Alternative observability stacks**
  - Kubernetes + CNI observability (Cilium, Hubble, Pixie) inherently track flows at the **pod/service** level and expose richer labels.
  - Service meshes (e.g., Istio/Linkerd) can add HTTP/gRPC metrics with service names and richer routing metadata.

This repo deliberately stays "vanilla" Docker + cAdvisor + Prometheus + Grafana. The Docker Mapping Exporter and TCP collector are structured so you can plug in other technologies that provide first-class service naming.

#### Planned exporter improvements

- [ ] Add more detailed service mapping (src/dst service pairs from `tcp_metrics_collector`)
- [ ] Cache raw Docker API responses to reduce overhead
- [ ] Expose container label information as Prometheus labels
- [ ] Support for Kubernetes/multi-host deployments

---

## Correlating Discussion Structure with Network Metrics

`scripts/experiment/correlate_structure_metrics.py` correlates the AgentVerse
discussion structure label (`horizontal` / `vertical`, from
`stages.recruitment.communication_structure` in each `response.json`) with
lower-level network and LLM metrics stored in the per-run `metrics.csv` files.
No live Prometheus instance is needed — all data is read from the experiment
output directories on disk.

### What it does

For each run directory the script extracts:

| Source | Fields extracted |
|---|---|
| `response.json` (Stage-2 discussion requests only) | Request count, discussion duration, total tokens, mean per-request LLM latency, mean IAT |
| `metrics.csv` (Prometheus time-series aggregated to per-run scalars) | LLM latency p50/p95, TTFT p50/p95, in-flight mean/peak, queue wait p50/p95, IAT mean, burstiness mean/max, IAT jitter p50/p95, TCP RTT p50/p95, TCP flow duration p50/p95, TCP bytes/s from LLM (mean/peak), TCP bytes/s A→LLM, B→LLM, A→B |

Aggregation per run: each panel's ~50 time-series data points are reduced to
a single scalar (`mean` or `max` depending on the metric).

### Outputs

All files are written to `<experiment-dir>/plots/` by default.

| File | Description |
|---|---|
| `structure_metrics_llm_performance.png` | Violin / box plots: LLM latency, TTFT, in-flight requests, queue wait — split by structure |
| `structure_metrics_traffic_characterisation.png` | IAT mean, burstiness coefficient (mean/max), IAT jitter — split by structure |
| `structure_metrics_tcp_service-level.png` | TCP RTT, flow duration, bytes/s from LLM, bytes A→LLM / B→LLM / A→B — split by structure |
| `structure_metrics_application_(discussion_stage).png` | Discussion request count, duration, total tokens, mean LLM latency, mean IAT — Stage 2 only |
| `correlation_heatmap.png` | Spearman ρ heatmap across all per-run numeric metrics |
| `per_run_metrics.csv` | Raw dataframe (one row per run) for further analysis |

Each violin/box panel shows the **Mann-Whitney U p-value** for the horizontal
vs vertical comparison (requires `scipy`).

### Quick start

```bash
# Against the most recent 100_RUNS_* dataset (auto-detected)
.venv/bin/python scripts/experiment/correlate_structure_metrics.py

# Explicit directory + custom output
.venv/bin/python scripts/experiment/correlate_structure_metrics.py \
    data/runs/100_RUNS_experiment_2026-03-14_11-32-14 \
    --output-dir data/runs/100_RUNS_experiment_2026-03-14_11-32-14/plots/
```

### Key findings from 100-run dataset

From 388 runs (17 horizontal, 371 vertical):

| Metric | Horizontal median | Vertical median | p-value | Interpretation |
|---|---|---|---|---|
| **IAT Jitter p50** | 3.33 s | 0.04 s | <0.001 | Vertical's parallel reviewer calls arrive near-simultaneously → near-zero jitter |
| **Discussion Duration** | 117 s | 69.5 s | <0.001 | Horizontal sequential rounds take ~70% longer than vertical parallel critique |
| **Discussion Tokens** | 57,300 | 35,800 | <0.001 | Horizontal accumulates more tokens across multiple rounds |
| **In-flight LLM Req (mean)** | 0.93 | 1.38 | <0.001 | Vertical keeps more concurrent requests in-flight |
| **TCP Bytes/s from LLM (mean)** | 539 B/s | 751 B/s | <0.001 | Vertical drives ~40% more LLM throughput |
| **Burstiness (max)** | 6.9 | 5.66 | 0.005 | Horizontal has higher peak bursts (large round-robin prompts) |
| **TCP Flow Duration p95** | 232 s | 260 s | 0.02 | Vertical's longer aggregate flows due to parallel fan-out |
| **Discussion IAT Mean** | 7.83 s | 5.15 s | <0.001 | Vertical requests arrive closer together on average |

### Limitations

- Metrics are aggregated over the **full run window** (all four AgentVerse stages),
  not the discussion stage alone. LLM and TCP metrics therefore include contributions
  from recruitment, execution, and evaluation phases. Per-stage isolation would
  require querying Prometheus with the discussion-stage timestamp window derived
  from `response.json` (not yet implemented).
- With only 17 horizontal runs vs 371 vertical, the horizontal group is small.
  Statistical results should be treated as exploratory.
- Concurrent runs (if any) cause TCP metric overlap between tasks.

---

## Correlating Application Logs with TCP Telemetry

`scripts/experiment/correlate_metrics.py` joins the per-call LLM log
(`logs/llm_calls.jsonl`, written by `MetricsLogger`) with Prometheus TCP
telemetry to produce a **per-task CSV dataset** — one row per task, with both
application-level and network-level columns.

### Quick start

```bash
# After running experiments (so llm_calls.jsonl has data):
python scripts/experiment/correlate_metrics.py \
    --call-log   logs/llm_calls.jsonl \
    --agentverse-dir logs/agentverse \
    --prometheus http://localhost:9090 \
    --output     data/correlated.csv
```

### Output schema

| Column | Source | Description |
|---|---|---|
| `task_id` | app log | UUID identifying the task |
| `scenario` | app log / agentverse | `agentic_simple`, `agentic_multi_hop`, `agentic_parallel` |
| `task_start` / `task_end` | app log | ISO 8601 timestamps derived from call records |
| `window_s` | derived | Duration of the Prometheus lookback window (seconds) |
| `total_llm_calls` | app log | Number of LLM calls across all agents for this task |
| `agent_a_calls` / `agent_b_calls` | app log | Breakdown by agent |
| `total_prompt_tokens` / `total_completion_tokens` / `total_tokens` | app log | Summed across all calls |
| `total_llm_latency_ms` | app log | Sum of per-call LLM latency (not wall-clock) |
| `cost_estimate_usd` | app log | `prompt_tokens × COST_PER_INPUT_TOKEN_USD + completion_tokens × COST_PER_OUTPUT_TOKEN_USD` |
| `model_name` | app log | Model serving the calls (`MODEL_NAME` env var) |
| `tcp_bytes_to_llm` | Prometheus | Total bytes sent from any agent to `llm_backend` over task window |
| `tcp_bytes_from_llm` | Prometheus | Total bytes sent from `llm_backend` to any agent |
| `tcp_packets_to_llm` | Prometheus | Packet count: agents → LLM |
| `tcp_bytes_a_to_b` / `tcp_packets_a_to_b` | Prometheus | Agent A → Agent B fan-out traffic |
| `tcp_syn_count` | Prometheus | New TCP connections opened ≈ flow count during task |
| `tcp_flow_duration_p50_s` / `tcp_flow_duration_p95_s` | Prometheus | Flow duration histogram quantiles (agent_a → llm_backend) |
| `tcp_rtt_p50_s` / `tcp_rtt_p95_s` | Prometheus | SYN/SYN-ACK RTT quantiles (agent_a → llm_backend) |

### Correlation methodology

1. `MetricsLogger` writes one JSONL record per LLM call to `logs/llm_calls.jsonl`,
   including `task_id`, `timestamp_start`, and `timestamp_end`.
2. The script groups records by `task_id` and derives the task time window as
   `[min(timestamp_start), max(timestamp_end)]` across all calls.
3. Prometheus is queried using `increase()` instant queries at `task_end` with a
   lookback of `window_s` (minimum 15 s to match the scrape interval). This captures
   counter increments on the inter-agent bridge during the task.
4. The `X-Task-ID` header propagated by all agents links application log records
   to the same `task_id`, ensuring consistent grouping across agents.

### Known limitations

- **Scrape interval resolution**: tasks shorter than ~15 s may have no Prometheus
  samples within their window. The script prints a warning and still writes the
  row; TCP columns will be `null`.
- **Concurrent task overlap**: Prometheus TCP metrics are not per-task-id — they
  are time-windowed counters. If multiple tasks run concurrently, their TCP
  windows overlap and attribution becomes ambiguous. Run experiments serially for
  the cleanest per-task signal.
- **Histogram precision**: `increase()` on histogram buckets works correctly only
  when the counter has been scraped at least once during the window. For very
  short tasks, quantile results may be `null` or inaccurate.

---

## Interarrival Time: Theory and Interpretation

### What interarrival time means

At the LLM backend, a stream of HTTP requests arrives from Agent A, Agent B workers, and AgentVerse multi-stage workflows. Each completed request increments `llm_requests_total`. If requests arrive at times T₁, T₂, T₃, …, the **interarrival time** Aₙ is:

```
Aₙ = Tₙ − Tₙ₋₁
```

Smaller Aₙ means requests are arriving closer together (higher instantaneous load); larger Aₙ means they are more spaced out (lower load). Because this testbed studies *agentic* workloads with structured dynamic workflows (recruitment → decision → execution → evaluation), the interarrival pattern directly encodes:

- Bursty phases (planning bursts, tool-use bursts, summarisation phases)
- Differences between **agentic** and **non-agentic** (baseline) scenarios
- How orchestration choices translate into actual LLM traffic patterns

### Closed-loop arrivals: what interarrival really measures

The agent workloads here are not an open-loop Poisson process where arrivals are independent of system state. They form a **closed-loop** system:

1. An agent issues an LLM request.
2. The LLM backend processes it and returns a response.
3. The agent performs local work (reasoning, routing, tool calls).
4. Only then does it decide whether and when to issue the **next** LLM request.

For a single logical agent, the gap between successive LLM calls decomposes as:

```
t_interarrival = t_LLM_latency + t_agent_processing + t_tool_calls
```

- `t_LLM_latency` is exposed as `llm_request_latency_seconds`.
- `t_agent_processing + t_tool_calls` is the **agent-side gap** — not yet separately instrumented (see "Missing metrics" below).

This means the observed interarrival time is **partly determined by the backend itself** (via LLM latency and batching config) and partly by agent behaviour. It is therefore a valid traffic characterisation metric for a *specific system configuration*, not an intrinsic property of the abstract workflow. For comparing agentic vs non-agentic traffic under controlled server settings, this is both acceptable and desirable — it makes the feedback loop between LLM capacity and agent traffic explicitly visible.

### Deriving interarrival time from existing metrics

No separate Prometheus metric is needed. From queueing theory, the mean interarrival time over a window is approximately `1/λ(t)`, where λ is the instantaneous arrival rate. The testbed derives this in PromQL as:

```promql
-- arrival rate
sum(rate(llm_requests_total[30s]))

-- mean interarrival time (seconds)
1 / sum(rate(llm_requests_total[30s]))
```

This is the quantity shown in the **"LLM Interarrival Time (avg)"** panel (row 6 of the dashboard, "Interarrival Interpretation"). The 30-second window balances noise and responsiveness.

If the full **distribution** of interarrival times is ever needed (e.g. to test for Poisson vs bursty arrivals), a histogram can be added to `llm/serve_llm.py`: track a global `last_arrival_ts`; on each accepted request, compute `now − last_arrival_ts` and observe it into `llm_interarrival_seconds_bucket`. For now, the derived mean via PromQL is sufficient for scenario comparisons.

### Interpreting interarrival alongside request duration

Because the system is closed-loop, interarrival time must always be read together with:

- **End-to-end latency**: `llm_request_latency_seconds_bucket`
- **Queue wait / TTFT**: `llm_queue_wait_seconds_bucket`
- **In-flight requests**: `llm_inflight_requests`

Common patterns:

| Interarrival | Latency | Queue wait | What it means |
|---|---|---|---|
| Short | Short | Low | High rate, backend keeping up. vLLM batching effective; little queueing. Typical of efficient parallel execution phases. |
| Short | Long | High | **Queueing regime**: requests arrive faster than served. `llm_inflight_requests` rises in parallel. Most informative for agentic vs non-agentic comparisons. |
| Long | Long | Low | Low rate but expensive calls. Latency is model-driven (long generations, heavy tool chains), not load-driven. Optimise at the workflow/prompt level. |
| Long | Short | Low | System mostly idle from the LLM's perspective. Burstiness is agent-logic-driven, not resource-driven. |

**Little's Law** provides a sanity check: in steady state, `L ≈ λW`, where L = in-flight requests (`llm_inflight_requests`), λ = arrival rate, W = latency. Watching all three together shows when the backend is being pushed into high-load or overloaded regimes and how orchestration decisions drive those regimes.

### Relationship to TCP-level metrics

The TCP metrics collector exposes:

- `tcp_packets_total{src_service, dst_service}`
- `tcp_bytes_total{src_service, dst_service}`
- `tcp_flow_duration_seconds_bucket{src_service, dst_service, le}`
- `tcp_rtt_handshake_seconds_bucket{src_service, dst_service, le}`

TCP connection events are **not always 1:1 with LLM RPCs** (due to connection reuse / keep-alive). Use:

- **LLM interarrival time** (from `llm_requests_total`) as the primary signal for **application-level arrivals**.
- **TCP-level metrics** as a complementary view for **network-layer behaviour** (RTT distributions, flow durations, retransmissions) associated with those same agentic workloads.

Together these metrics characterise how semantic workflows (AgentID, TaskID, ToolCallID) translate into both application-level load and network-level behaviour between agents, tools, and the LLM.

### Missing metrics / future instrumentation

The following would make the interarrival picture more complete. None are currently implemented.

#### Agent-side gap

**Goal:** separate `t_LLM_latency` (already measured) from `t_agent_gap = t_agent_processing + t_tool_calls`.

**What to add:**
- `agent_llm_gap_seconds_bucket{agent_id, scenario}` — histogram of time from "LLM response received" to "next LLM request sent", tracked per agent.
- Dashboard location: new panel "Agent-side Gap (p50/p95)" in a **"Agent Workflow Metrics"** row, alongside LLM latency and TTFT.

#### Task-level workflow metrics

**What to add:**
- `agent_task_total{scenario, status}` — completed tasks.
- `agent_task_latency_seconds_bucket{scenario}` — end-to-end task latency.
- `agent_llm_calls_total{scenario, role}` — LLM calls per scenario and agent role.

**Derived PromQL once metrics exist:**
```promql
-- LLM calls per task
sum(rate(agent_llm_calls_total[window])) / sum(rate(agent_task_total[window]))

-- Fan-out ratio (orchestrator vs worker)
rate(agent_llm_calls_total{role="orchestrator"}[window])
  / rate(agent_llm_calls_total{role!="orchestrator"}[window])
```

**Dashboard location:** new **"Agent Workflow Metrics"** row with panels: Tasks Completed/s, Task Latency (p50/p95), LLM Calls per Task, LLM Calls by Role (stacked).
