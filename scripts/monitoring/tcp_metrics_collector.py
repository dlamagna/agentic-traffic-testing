#!/usr/bin/env python3
"""
tcp_metrics_collector.py
------------------------
Collect TCP-level network metrics and expose them to Prometheus.

This script:
1. Captures TCP traffic using tcpdump (or reads from pcap)
2. Calculates TCP-level metrics (RTT estimates, flows, packet sizes)
3. Exposes metrics on a Prometheus-compatible /metrics endpoint

Run with sudo (required for packet capture):
    sudo python scripts/monitoring/tcp_metrics_collector.py --interface br-<network_id>

Metrics exposed:
    - tcp_packets_total: Total packets by direction and service
    - tcp_bytes_total: Total bytes by direction and service
    - tcp_flows_active: Currently active TCP flows
    - tcp_flow_duration_seconds: Flow duration histogram
    - tcp_packet_size_bytes: Packet size histogram
    - tcp_syn_total: SYN packets (new connections)
    - tcp_fin_total: FIN packets (closed connections)
    - tcp_rst_total: RST packets (reset connections)
"""

import argparse
import subprocess
import threading
import time
import re
import signal
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Optional, Set, Tuple

# Service IP mapping for distributed mode (inter_agent_network IPs)
SERVICE_IPS = {
    "172.23.0.10": "agent_a",
    "172.23.0.20": "agent_b_1",
    "172.23.0.21": "agent_b_2",
    "172.23.0.22": "agent_b_3",
    "172.23.0.23": "agent_b_4",
    "172.23.0.24": "agent_b_5",
    "172.23.0.30": "llm_backend",
    "172.23.0.40": "mcp_tool_db",
    "172.23.0.50": "chat_ui",
    "172.23.0.60": "jaeger",
    # Tools network IPs (for traffic within tools_network)
    "172.24.0.10": "mcp_tool_db",
}


def ip_to_service(ip: str) -> str:
    """Convert IP to service name, or return 'external'."""
    return SERVICE_IPS.get(ip, "external")


@dataclass
class FlowState:
    """Track state of a TCP flow."""
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    start_time: float
    last_seen: float
    packets: int = 0
    bytes: int = 0
    syn_seen: bool = False
    fin_seen: bool = False


@dataclass
class TCPMetrics:
    """Collected TCP metrics."""
    # Counters by (src_service, dst_service)
    packets: Dict[Tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    bytes: Dict[Tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    
    # Connection counters
    syn_count: Dict[Tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    fin_count: Dict[Tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    rst_count: Dict[Tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    
    # Active flows
    flows: Dict[tuple, FlowState] = field(default_factory=dict)
    
    # Histograms (buckets)
    packet_size_buckets: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    flow_duration_buckets: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Metadata
    start_time: float = field(default_factory=time.time)
    packets_processed: int = 0
    
    lock: threading.Lock = field(default_factory=threading.Lock)


# Global metrics instance
metrics = TCPMetrics()


def bucket_for_size(size: int) -> str:
    """Get histogram bucket for packet size."""
    buckets = [64, 128, 256, 512, 1024, 1500, 4096, 9000]
    for b in buckets:
        if size <= b:
            return str(b)
    return "inf"


def bucket_for_duration(duration: float) -> str:
    """Get histogram bucket for flow duration (seconds)."""
    buckets = [0.001, 0.01, 0.1, 0.5, 1.0, 5.0, 30.0, 60.0, 300.0]
    for b in buckets:
        if duration <= b:
            return str(b)
    return "inf"


def parse_tcpdump_line(line: str) -> Optional[dict]:
    """
    Parse a tcpdump output line.
    
    Example formats:
    18:30:45.123456 IP 172.23.0.10.8101 > 172.23.0.30.8000: Flags [S], seq 123, length 0
    18:30:45.123456 IP 172.23.0.10.8101 > 172.23.0.30.8000: Flags [.], ack 1, length 1024
    """
    # Pattern for tcpdump verbose output
    pattern = r'(\d+:\d+:\d+\.\d+)\s+IP\s+(\d+\.\d+\.\d+\.\d+)\.(\d+)\s+>\s+(\d+\.\d+\.\d+\.\d+)\.(\d+):\s+Flags\s+\[([^\]]+)\].*?length\s+(\d+)'
    
    match = re.search(pattern, line)
    if not match:
        return None
    
    timestamp_str, src_ip, src_port, dst_ip, dst_port, flags, length = match.groups()
    
    return {
        "timestamp": timestamp_str,
        "src_ip": src_ip,
        "src_port": int(src_port),
        "dst_ip": dst_ip,
        "dst_port": int(dst_port),
        "flags": flags,
        "length": int(length),
    }


def process_packet(pkt: dict) -> None:
    """Process a parsed packet and update metrics."""
    global metrics
    
    src_service = ip_to_service(pkt["src_ip"])
    dst_service = ip_to_service(pkt["dst_ip"])
    service_pair = (src_service, dst_service)
    
    # Flow key (bidirectional)
    flow_key = tuple(sorted([
        (pkt["src_ip"], pkt["src_port"]),
        (pkt["dst_ip"], pkt["dst_port"])
    ]))
    
    now = time.time()
    
    with metrics.lock:
        metrics.packets_processed += 1
        
        # Update counters
        metrics.packets[service_pair] += 1
        metrics.bytes[service_pair] += pkt["length"]
        
        # Update packet size histogram
        size_bucket = bucket_for_size(pkt["length"])
        metrics.packet_size_buckets[size_bucket] += 1
        
        # Check flags
        flags = pkt["flags"]
        if "S" in flags and "." not in flags:  # SYN (not SYN-ACK)
            metrics.syn_count[service_pair] += 1
        if "F" in flags:
            metrics.fin_count[service_pair] += 1
        if "R" in flags:
            metrics.rst_count[service_pair] += 1
        
        # Track flows
        if flow_key not in metrics.flows:
            metrics.flows[flow_key] = FlowState(
                src_ip=pkt["src_ip"],
                dst_ip=pkt["dst_ip"],
                src_port=pkt["src_port"],
                dst_port=pkt["dst_port"],
                start_time=now,
                last_seen=now,
            )
        
        flow = metrics.flows[flow_key]
        flow.last_seen = now
        flow.packets += 1
        flow.bytes += pkt["length"]
        
        if "S" in flags:
            flow.syn_seen = True
        if "F" in flags:
            flow.fin_seen = True
            # Flow is closing, record duration
            duration = now - flow.start_time
            bucket = bucket_for_duration(duration)
            metrics.flow_duration_buckets[bucket] += 1


def cleanup_old_flows(max_idle: float = 60.0) -> None:
    """Remove flows that haven't been seen recently."""
    global metrics
    now = time.time()
    
    with metrics.lock:
        to_remove = []
        for key, flow in metrics.flows.items():
            if now - flow.last_seen > max_idle:
                to_remove.append(key)
                # Record duration for closed flows
                duration = flow.last_seen - flow.start_time
                bucket = bucket_for_duration(duration)
                metrics.flow_duration_buckets[bucket] += 1
        
        for key in to_remove:
            del metrics.flows[key]


def generate_prometheus_metrics() -> str:
    """Generate Prometheus-format metrics."""
    global metrics
    lines = []
    
    with metrics.lock:
        uptime = time.time() - metrics.start_time
        
        # Metadata
        lines.append(f"# HELP tcp_collector_uptime_seconds Time since collector started")
        lines.append(f"# TYPE tcp_collector_uptime_seconds gauge")
        lines.append(f"tcp_collector_uptime_seconds {uptime:.2f}")
        
        lines.append(f"# HELP tcp_collector_packets_processed Total packets processed")
        lines.append(f"# TYPE tcp_collector_packets_processed counter")
        lines.append(f"tcp_collector_packets_processed {metrics.packets_processed}")
        
        # Packets by service pair
        lines.append(f"# HELP tcp_packets_total Total TCP packets")
        lines.append(f"# TYPE tcp_packets_total counter")
        for (src, dst), count in metrics.packets.items():
            lines.append(f'tcp_packets_total{{src_service="{src}",dst_service="{dst}"}} {count}')
        
        # Bytes by service pair
        lines.append(f"# HELP tcp_bytes_total Total TCP bytes")
        lines.append(f"# TYPE tcp_bytes_total counter")
        for (src, dst), count in metrics.bytes.items():
            lines.append(f'tcp_bytes_total{{src_service="{src}",dst_service="{dst}"}} {count}')
        
        # Connection events
        lines.append(f"# HELP tcp_syn_total TCP SYN packets (new connections)")
        lines.append(f"# TYPE tcp_syn_total counter")
        for (src, dst), count in metrics.syn_count.items():
            lines.append(f'tcp_syn_total{{src_service="{src}",dst_service="{dst}"}} {count}')
        
        lines.append(f"# HELP tcp_fin_total TCP FIN packets (closed connections)")
        lines.append(f"# TYPE tcp_fin_total counter")
        for (src, dst), count in metrics.fin_count.items():
            lines.append(f'tcp_fin_total{{src_service="{src}",dst_service="{dst}"}} {count}')
        
        lines.append(f"# HELP tcp_rst_total TCP RST packets (reset connections)")
        lines.append(f"# TYPE tcp_rst_total counter")
        for (src, dst), count in metrics.rst_count.items():
            lines.append(f'tcp_rst_total{{src_service="{src}",dst_service="{dst}"}} {count}')
        
        # Active flows
        lines.append(f"# HELP tcp_flows_active Currently active TCP flows")
        lines.append(f"# TYPE tcp_flows_active gauge")
        lines.append(f"tcp_flows_active {len(metrics.flows)}")
        
        # Packet size histogram
        lines.append(f"# HELP tcp_packet_size_bytes_bucket TCP packet size distribution")
        lines.append(f"# TYPE tcp_packet_size_bytes_bucket counter")
        cumulative = 0
        for bucket in ["64", "128", "256", "512", "1024", "1500", "4096", "9000", "inf"]:
            cumulative += metrics.packet_size_buckets.get(bucket, 0)
            lines.append(f'tcp_packet_size_bytes_bucket{{le="{bucket}"}} {cumulative}')
        
        # Flow duration histogram
        lines.append(f"# HELP tcp_flow_duration_seconds_bucket TCP flow duration distribution")
        lines.append(f"# TYPE tcp_flow_duration_seconds_bucket counter")
        cumulative = 0
        for bucket in ["0.001", "0.01", "0.1", "0.5", "1.0", "5.0", "30.0", "60.0", "300.0", "inf"]:
            cumulative += metrics.flow_duration_buckets.get(bucket, 0)
            lines.append(f'tcp_flow_duration_seconds_bucket{{le="{bucket}"}} {cumulative}')
    
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics endpoint."""
    
    def do_GET(self):
        if self.path == "/metrics":
            content = generate_prometheus_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content.encode())
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Suppress logging


def run_tcpdump(interface: str, filter_expr: str) -> None:
    """Run tcpdump and process output."""
    cmd = [
        "tcpdump",
        "-i", interface,
        "-l",  # Line-buffered
        "-n",  # Don't resolve hostnames
        "-tt",  # Unix timestamp
        filter_expr
    ]
    
    print(f"[*] Starting tcpdump: {' '.join(cmd)}")
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    try:
        for line in proc.stdout:
            pkt = parse_tcpdump_line(line)
            if pkt:
                process_packet(pkt)
    except Exception as e:
        print(f"[!] Error processing tcpdump output: {e}")
    finally:
        proc.terminate()


def find_docker_bridge() -> Optional[str]:
    """Find the Docker bridge interface for inter_agent_network."""
    try:
        result = subprocess.run(
            ["docker", "network", "ls", "--filter", "name=inter_agent", "--format", "{{.ID}}"],
            capture_output=True,
            text=True
        )
        network_id = result.stdout.strip()
        if network_id:
            bridge = f"br-{network_id[:12]}"
            # Verify it exists
            result = subprocess.run(["ip", "link", "show", bridge], capture_output=True)
            if result.returncode == 0:
                return bridge
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Collect TCP metrics and expose to Prometheus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--interface", "-i",
        help="Network interface to capture on (default: auto-detect Docker bridge)"
    )
    parser.add_argument(
        "--filter", "-f",
        default="tcp and net 172.23.0.0/24",
        help="tcpdump filter expression (default: tcp and net 172.23.0.0/24)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=9100,
        help="Port for Prometheus metrics endpoint (default: 9100)"
    )
    parser.add_argument(
        "--cleanup-interval",
        type=int,
        default=30,
        help="Interval for cleaning up old flows (seconds, default: 30)"
    )
    
    args = parser.parse_args()
    
    # Find interface
    interface = args.interface
    if not interface:
        interface = find_docker_bridge()
        if interface:
            print(f"[*] Auto-detected Docker bridge: {interface}")
        else:
            print("[!] Could not auto-detect Docker bridge. Falling back to 'any'.")
            interface = "any"
    
    print(f"[*] Interface: {interface}")
    print(f"[*] Filter: {args.filter}")
    print(f"[*] Metrics port: {args.port}")
    
    # Start HTTP server for metrics
    server = HTTPServer(("0.0.0.0", args.port), MetricsHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[*] Metrics endpoint: http://localhost:{args.port}/metrics")
    
    # Start cleanup thread
    def cleanup_loop():
        while True:
            time.sleep(args.cleanup_interval)
            cleanup_old_flows()
    
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    
    # Handle shutdown
    def shutdown(signum, frame):
        print("\n[*] Shutting down...")
        server.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    # Run tcpdump (blocks)
    print("[*] Starting packet capture...")
    run_tcpdump(interface, args.filter)


if __name__ == "__main__":
    main()
