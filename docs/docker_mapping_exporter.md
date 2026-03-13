# Docker Mapping Exporter

The Docker Mapping Exporter automatically maps Docker infrastructure IDs (bridge interfaces, container cgroup scopes, IP addresses) to human-readable names in Grafana dashboards.

## What It Does

The exporter runs as a container and queries the Docker daemon to generate three categories of mappings:

1. **Network Bridge Mappings** (`docker_network_mapping`)
   - Maps `br-xxxx` bridge interface IDs → Docker network names
   - Example: `br-1163d8f109d1` → `inter_agent_network`

2. **Container Mappings** (`docker_container_mapping`)
   - Maps `docker-xxxx` cgroup scope IDs → container names and compose service names
   - Example: `docker-04524d6485` → `infra-agent-a-1`

3. **IP Address Mappings** (`docker_ip_mapping`)
   - Maps container IP addresses → container names and service names
   - Only includes containers on the `infra_inter_agent_network`

## How to Use

### 1. Start the Monitoring Stack

```bash
cd /path/to/agentic-traffic-testing
docker compose -f infra/docker-compose.yml -f infra/docker-compose.monitoring.yml up -d
```

The exporter will start automatically and expose metrics on `http://localhost:9101/metrics`.

### 2. Verify Metrics Are Being Collected

Check Prometheus targets:
```bash
curl http://localhost:9090/api/v1/targets
```

Look for the `docker-mapping` job with a "UP" state.

### 3. View the Metrics

Visit Prometheus and query:
```promql
docker_network_mapping
docker_container_mapping
docker_ip_mapping
```

### 4. Dashboard Integration

The Grafana dashboard automatically uses these mappings in several panels:

#### Network Panels
- **Network Transmit/Receive Rate by Interface**: Uses `docker_network_mapping` with PromQL joins to show network names instead of bridge IDs
  ```promql
  rate(container_network_transmit_bytes_total{id="/",interface=~"br-.*"}[30s])
    * on(interface) group_left(network_name) docker_network_mapping
  ```

#### Resource Panels
- **CPU (core equivalents per container)**: Uses cAdvisor's `name` label directly
  ```promql
  sum by (name) (rate(container_cpu_usage_seconds_total{cpu="total",name=~".+"}[1m]))
  ```

- **Memory Usage per container**: Uses cAdvisor's `name` label directly
  ```promql
  container_memory_usage_bytes{name=~".+"}
  ```

## How It Works

### Metrics Refresh Cycle

1. Prometheus scrapes the exporter every 10 seconds (configurable in `prometheus.yml`)
2. The exporter queries Docker daemon and caches results for 10 seconds
3. Metrics are returned in Prometheus text format
4. Grafana retrieves metrics via Prometheus and applies the mappings

### Docker Socket Access

The exporter requires access to the Docker socket:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

This is mounted as read-only. The container queries:
- `docker network ls` - for bridge ID → network name mappings
- `docker ps` - for container ID → name/service mappings
- `docker inspect` - for IP address on `infra_inter_agent_network`

## Troubleshooting

### Metrics Not Appearing in Dashboard

1. **Check exporter is running:**
   ```bash
   docker ps | grep docker-mapping-exporter
   docker logs docker-mapping-exporter
   ```

2. **Check Prometheus scrape target:**
   ```bash
   curl http://localhost:9090/api/v1/targets
   # Look for docker-mapping job
   ```

3. **Verify metrics endpoint:**
   ```bash
   curl http://localhost:9101/metrics
   ```

4. **Check Prometheus has scraped the metrics:**
   ```bash
   curl 'http://localhost:9090/api/v1/query?query=docker_network_mapping'
   ```

### Container Names Not Showing

If you see `<no value>` or no results:

1. **Verify cAdvisor has the `name` label:**
   ```bash
   curl 'http://localhost:9090/api/v1/query?query=container_cpu_usage_seconds_total' | jq '.data.result[0].metric'
   # Should include "name": "container-name"
   ```

2. **Rebuild cAdvisor** - ensure it's built with `--store_container_labels=true`:
   ```bash
   docker inspect cadvisor | jq '.[0].Config.Cmd'
   ```

3. **Rebuild dashboard** - restart Grafana to reload dashboard JSON:
   ```bash
   docker restart grafana
   ```

## Architecture

```
Docker Host
    ↓
docker_mapping_exporter (queries /var/run/docker.sock)
    ↓
Generates Prometheus metrics:
  - docker_network_mapping{interface="br-xxx", network_name="yyy"}
  - docker_container_mapping{scope_id="...", container_name="...", service_name="..."}
  - docker_ip_mapping{ip_address="...", container_name="...", service_name="..."}
    ↓
Prometheus (scrapes every 10s)
    ↓
Grafana Dashboard
    ↓
Uses PromQL joins/label_replace to show human-readable names
```

## Performance Notes

- **Exporter:** ~5ms per Docker query, results cached for 10 seconds
- **Prometheus:** Scrapes every 10 seconds (configurable)
- **Grafana:** Dashboard queries typically execute in <100ms with joins
- **Memory:** Exporter uses ~30-50MB (Python 3.11 slim + Docker CLI)

## Future Enhancements

- [ ] Add more detailed service mapping (src/dst service pairs from tcp_metrics_collector)
- [ ] Cache raw Docker API responses to reduce overhead
- [ ] Expose container label information as Prometheus labels
- [ ] Support for Kubernetes/multi-host deployments
