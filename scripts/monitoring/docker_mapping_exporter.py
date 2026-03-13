#!/usr/bin/env python3
"""
Docker Network & Container Mapping Exporter
Exposes Prometheus metrics that map Docker bridge IDs, container IDs, and IPs
to human-readable names for dashboard labeling.

Uses the Docker Engine API directly via the Unix socket (no Docker CLI needed).
"""

import os
import json
import http.client
import socket
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import sys

DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
INTER_AGENT_NETWORK = os.getenv("INTER_AGENT_NETWORK", "infra_inter_agent_network")

# Metrics cache
_cache = {}
_cache_time = 0
_cache_ttl = int(os.getenv("CACHE_TTL", "10"))


class DockerSocketConnection(http.client.HTTPConnection):
    """HTTP connection over a Unix socket."""

    def __init__(self, socket_path):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def docker_api_get(path):
    """Make a GET request to the Docker Engine API via Unix socket."""
    conn = DockerSocketConnection(DOCKER_SOCKET)
    conn.request("GET", path)
    response = conn.getresponse()
    data = response.read().decode("utf-8")
    conn.close()
    if response.status != 200:
        print(f"Docker API error {response.status} for {path}: {data[:200]}", file=sys.stderr)
        return None
    return json.loads(data)


def get_docker_mappings():
    """Get current Docker network and container mappings."""
    global _cache, _cache_time

    if time.time() - _cache_time < _cache_ttl and _cache:
        return _cache

    mappings = {
        "networks": {},      # br-xxx -> network_name
        "containers": {},    # full_cgroup_id -> (container_name, service_name)
        "ips": {},           # ip -> (container_name, service_name)
    }

    try:
        # --- Network bridge mappings ---
        networks = docker_api_get("/networks")
        if networks:
            for net in networks:
                net_id = net.get("Id", "")
                net_name = net.get("Name", "")
                driver = net.get("Driver", "")
                # Only bridge networks have br-xxx interfaces
                if driver == "bridge" and net_id and net_name:
                    br_id = f"br-{net_id[:12]}"
                    mappings["networks"][br_id] = net_name

        # --- Container mappings ---
        containers = docker_api_get("/containers/json")
        if containers:
            for c in containers:
                container_id = c.get("Id", "")
                # Container name (strip leading /)
                names = c.get("Names", [])
                container_name = names[0].lstrip("/") if names else container_id[:12]

                # Compose service name from labels
                labels = c.get("Labels", {})
                service_name = labels.get("com.docker.compose.service", container_name)

                # Build the full cgroup scope path that cAdvisor uses
                full_scope = f"/system.slice/docker-{container_id}.scope"
                mappings["containers"][full_scope] = (container_name, service_name)

                # Also map short prefix (first 12 chars, matching docker ps)
                short_scope = f"docker-{container_id[:12]}"
                mappings["containers"][short_scope] = (container_name, service_name)

                # IP on the inter-agent network
                net_settings = c.get("NetworkSettings", {}).get("Networks", {})
                if INTER_AGENT_NETWORK in net_settings:
                    ip_addr = net_settings[INTER_AGENT_NETWORK].get("IPAddress", "")
                    if ip_addr:
                        mappings["ips"][ip_addr] = (container_name, service_name)

    except Exception as e:
        print(f"Error fetching Docker mappings: {e}", file=sys.stderr, flush=True)

    _cache = mappings
    _cache_time = time.time()

    n_nets = len(mappings["networks"])
    n_containers = len([k for k in mappings["containers"] if k.startswith("/system")])
    n_ips = len(mappings["ips"])
    print(f"Refreshed mappings: {n_nets} networks, {n_containers} containers, {n_ips} IPs",
          file=sys.stderr, flush=True)

    return mappings


def generate_metrics():
    """Generate Prometheus metrics from Docker mappings."""
    mappings = get_docker_mappings()

    lines = [
        "# HELP docker_network_mapping Mapping of Docker bridge interfaces to network names",
        "# TYPE docker_network_mapping gauge",
    ]

    for br_id, net_name in mappings["networks"].items():
        lines.append(
            f'docker_network_mapping{{interface="{br_id}",network_name="{net_name}"}} 1'
        )

    lines.extend([
        "# HELP docker_container_mapping Mapping of Docker cgroup scopes to container/service names",
        "# TYPE docker_container_mapping gauge",
    ])

    for scope_id, (container_name, service_name) in mappings["containers"].items():
        # Only emit the full cgroup path (what cAdvisor uses as `id` label)
        if scope_id.startswith("/system.slice/"):
            lines.append(
                f'docker_container_mapping{{id="{scope_id}",container_name="{container_name}",service_name="{service_name}"}} 1'
            )

    lines.extend([
        "# HELP docker_ip_mapping Mapping of Docker container IPs to names on inter_agent_network",
        "# TYPE docker_ip_mapping gauge",
    ])

    for ip_addr, (container_name, service_name) in mappings["ips"].items():
        lines.append(
            f'docker_ip_mapping{{ip_address="{ip_addr}",container_name="{container_name}",service_name="{service_name}"}} 1'
        )

    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            metrics = generate_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(metrics))
            self.end_headers()
            self.wfile.write(metrics.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run_server(port=9101):
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    print(f"Docker mapping exporter listening on port {port}", flush=True)
    # Test connection on startup
    try:
        result = docker_api_get("/version")
        if result:
            print(f"Connected to Docker {result.get('Version', '?')}", flush=True)
    except Exception as e:
        print(f"WARNING: Cannot connect to Docker: {e}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.getenv("EXPORTER_PORT", "9101"))
    run_server(port)
