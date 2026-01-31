#!/usr/bin/env python3
"""
analyze_traffic.py
------------------
Analyze captured network traffic and correlate with application telemetry.

This script processes:
1. Packet captures (.pcap) from tcpdump
2. Application telemetry logs (.log/.jsonl) from agents
3. Docker stats logs (.jsonl) if available

It produces metrics and visualizations for comparing agentic vs non-agentic
traffic patterns.

USAGE:
    python scripts/analyze_traffic.py --pcap logs/traffic/packets_*.pcap
    python scripts/analyze_traffic.py --pcap capture.pcap --telemetry logs/agent_telemetry/
    python scripts/analyze_traffic.py --help

PREREQUISITES:
    pip install scapy pandas matplotlib

"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Try to import optional dependencies
try:
    from scapy.all import rdpcap, TCP, IP
    HAS_SCAPY = True
except ImportError:
    HAS_SCAPY = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


# IP address to service mapping for distributed mode
SERVICE_IPS = {
    # inter_agent_network IPs (172.23.0.0/24)
    "172.23.0.10": "agent-a",
    "172.23.0.20": "agent-b-1",
    "172.23.0.21": "agent-b-2",
    "172.23.0.22": "agent-b-3",
    "172.23.0.23": "agent-b-4",
    "172.23.0.24": "agent-b-5",
    "172.23.0.30": "llm-backend",
    "172.23.0.40": "mcp-tool-db",
    "172.23.0.50": "chat-ui",
    "172.23.0.60": "jaeger",
    # tools_network IPs (172.24.0.0/24)
    "172.24.0.10": "mcp-tool-db",
}


@dataclass
class FlowStats:
    """Statistics for a single TCP flow."""
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    src_service: str
    dst_service: str
    packet_count: int = 0
    bytes_total: int = 0
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    syn_count: int = 0
    fin_count: int = 0
    rst_count: int = 0


def ip_to_service(ip: str) -> str:
    """Map IP address to service name."""
    return SERVICE_IPS.get(ip, ip)


def analyze_pcap(pcap_path: Path) -> Dict[str, Any]:
    """Analyze a packet capture file."""
    if not HAS_SCAPY:
        print("[!] scapy not installed. Install with: pip install scapy")
        return {}
    
    print(f"[*] Reading pcap: {pcap_path}")
    packets = rdpcap(str(pcap_path))
    print(f"[*] Loaded {len(packets)} packets")
    
    # Track flows (5-tuple)
    flows: Dict[tuple, FlowStats] = {}
    
    # Track connections over time
    connections_per_second: Dict[int, int] = defaultdict(int)
    bytes_per_second: Dict[int, int] = defaultdict(int)
    
    first_timestamp = None
    last_timestamp = None
    
    for pkt in packets:
        if not pkt.haslayer(IP) or not pkt.haslayer(TCP):
            continue
        
        ip_layer = pkt[IP]
        tcp_layer = pkt[TCP]
        
        timestamp = float(pkt.time)
        if first_timestamp is None:
            first_timestamp = timestamp
        last_timestamp = timestamp
        
        # Create flow key (sorted to treat both directions as same flow)
        endpoints = tuple(sorted([
            (ip_layer.src, tcp_layer.sport),
            (ip_layer.dst, tcp_layer.dport)
        ]))
        flow_key = endpoints
        
        # Initialize flow if new
        if flow_key not in flows:
            src_ip, src_port = endpoints[0]
            dst_ip, dst_port = endpoints[1]
            flows[flow_key] = FlowStats(
                src_ip=src_ip,
                dst_ip=dst_ip,
                src_port=src_port,
                dst_port=dst_port,
                src_service=ip_to_service(src_ip),
                dst_service=ip_to_service(dst_ip),
                start_time=timestamp,
            )
        
        flow = flows[flow_key]
        flow.packet_count += 1
        flow.bytes_total += len(pkt)
        flow.end_time = timestamp
        
        # Track TCP flags
        flags = tcp_layer.flags
        if flags & 0x02:  # SYN
            flow.syn_count += 1
            second = int(timestamp - first_timestamp)
            connections_per_second[second] += 1
        if flags & 0x01:  # FIN
            flow.fin_count += 1
        if flags & 0x04:  # RST
            flow.rst_count += 1
        
        # Track bytes per second
        second = int(timestamp - first_timestamp)
        bytes_per_second[second] += len(pkt)
    
    # Compute summary statistics
    duration = (last_timestamp - first_timestamp) if first_timestamp and last_timestamp else 0
    total_packets = sum(f.packet_count for f in flows.values())
    total_bytes = sum(f.bytes_total for f in flows.values())
    
    # Group flows by service pair
    service_pairs: Dict[tuple, List[FlowStats]] = defaultdict(list)
    for flow in flows.values():
        pair = tuple(sorted([flow.src_service, flow.dst_service]))
        service_pairs[pair].append(flow)
    
    return {
        "pcap_file": str(pcap_path),
        "duration_seconds": duration,
        "total_packets": total_packets,
        "total_bytes": total_bytes,
        "total_flows": len(flows),
        "flows": list(flows.values()),
        "service_pairs": dict(service_pairs),
        "connections_per_second": dict(connections_per_second),
        "bytes_per_second": dict(bytes_per_second),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
    }


def analyze_telemetry(telemetry_dir: Path) -> Dict[str, Any]:
    """Analyze application telemetry logs."""
    events = []
    
    # Find all telemetry log files
    log_files = list(telemetry_dir.glob("*.log")) + list(telemetry_dir.glob("*.jsonl"))
    
    if not log_files:
        print(f"[!] No telemetry files found in {telemetry_dir}")
        return {"events": [], "tasks": {}}
    
    print(f"[*] Found {len(log_files)} telemetry files")
    
    for log_file in log_files:
        print(f"    - {log_file.name}")
        try:
            with open(log_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"[!] Error reading {log_file}: {e}")
    
    # Group events by task_id
    tasks: Dict[str, List[Dict]] = defaultdict(list)
    for event in events:
        task_id = event.get("task_id")
        if task_id:
            tasks[task_id].append(event)
    
    # Sort events within each task by timestamp
    for task_id in tasks:
        tasks[task_id].sort(key=lambda e: e.get("timestamp_ms", 0))
    
    return {
        "total_events": len(events),
        "total_tasks": len(tasks),
        "events": events,
        "tasks": dict(tasks),
    }


def print_flow_summary(analysis: Dict[str, Any]) -> None:
    """Print a summary of traffic flows."""
    print("\n" + "=" * 70)
    print("TRAFFIC FLOW SUMMARY")
    print("=" * 70)
    
    print(f"\nCapture Duration: {analysis['duration_seconds']:.1f} seconds")
    print(f"Total Packets:    {analysis['total_packets']}")
    print(f"Total Bytes:      {analysis['total_bytes']} ({analysis['total_bytes'] / 1024:.1f} KB)")
    print(f"Total Flows:      {analysis['total_flows']}")
    
    print("\n--- Traffic by Service Pair ---")
    print(f"{'Service Pair':<40} {'Flows':>8} {'Packets':>10} {'Bytes':>12}")
    print("-" * 70)
    
    service_pairs = analysis.get("service_pairs", {})
    for pair, flows in sorted(service_pairs.items(), key=lambda x: -sum(f.bytes_total for f in x[1])):
        pair_name = f"{pair[0]} <-> {pair[1]}"
        total_packets = sum(f.packet_count for f in flows)
        total_bytes = sum(f.bytes_total for f in flows)
        print(f"{pair_name:<40} {len(flows):>8} {total_packets:>10} {total_bytes:>12}")
    
    # Show top flows
    print("\n--- Top 10 Flows by Bytes ---")
    print(f"{'Source':<20} {'Dest':<20} {'Packets':>10} {'Bytes':>12} {'Duration':>10}")
    print("-" * 70)
    
    flows = sorted(analysis.get("flows", []), key=lambda f: -f.bytes_total)[:10]
    for f in flows:
        duration = (f.end_time - f.start_time) if f.start_time and f.end_time else 0
        src = f"{f.src_service}:{f.src_port}"
        dst = f"{f.dst_service}:{f.dst_port}"
        print(f"{src:<20} {dst:<20} {f.packet_count:>10} {f.bytes_total:>12} {duration:>10.2f}s")


def print_telemetry_summary(telemetry: Dict[str, Any]) -> None:
    """Print a summary of application telemetry."""
    print("\n" + "=" * 70)
    print("APPLICATION TELEMETRY SUMMARY")
    print("=" * 70)
    
    print(f"\nTotal Events: {telemetry['total_events']}")
    print(f"Total Tasks:  {telemetry['total_tasks']}")
    
    # Count events by type
    event_types: Dict[str, int] = defaultdict(int)
    for event in telemetry.get("events", []):
        event_type = event.get("event_type", "unknown")
        event_types[event_type] += 1
    
    print("\n--- Events by Type ---")
    for event_type, count in sorted(event_types.items(), key=lambda x: -x[1]):
        print(f"  {event_type:<30} {count:>6}")
    
    # Show sample tasks
    tasks = telemetry.get("tasks", {})
    if tasks:
        print("\n--- Sample Task Traces ---")
        for task_id, events in list(tasks.items())[:3]:
            print(f"\nTask: {task_id[:8]}...")
            for event in events[:5]:
                event_type = event.get("event_type", "?")
                agent = event.get("agent_id", "?")
                ts = event.get("timestamp_ms", 0)
                print(f"  [{ts}] {agent}: {event_type}")
            if len(events) > 5:
                print(f"  ... and {len(events) - 5} more events")


def export_to_csv(analysis: Dict[str, Any], output_dir: Path) -> None:
    """Export analysis results to CSV files."""
    if not HAS_PANDAS:
        print("[!] pandas not installed. Install with: pip install pandas")
        return
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Export flows
    flows = analysis.get("flows", [])
    if flows:
        flow_data = []
        for f in flows:
            duration = (f.end_time - f.start_time) if f.start_time and f.end_time else 0
            flow_data.append({
                "src_service": f.src_service,
                "dst_service": f.dst_service,
                "src_ip": f.src_ip,
                "dst_ip": f.dst_ip,
                "src_port": f.src_port,
                "dst_port": f.dst_port,
                "packets": f.packet_count,
                "bytes": f.bytes_total,
                "duration_s": duration,
                "syn_count": f.syn_count,
                "fin_count": f.fin_count,
                "rst_count": f.rst_count,
            })
        
        df = pd.DataFrame(flow_data)
        csv_path = output_dir / "flows.csv"
        df.to_csv(csv_path, index=False)
        print(f"[*] Exported flows to {csv_path}")
    
    # Export time series
    cps = analysis.get("connections_per_second", {})
    bps = analysis.get("bytes_per_second", {})
    if cps or bps:
        all_seconds = set(cps.keys()) | set(bps.keys())
        ts_data = []
        for s in sorted(all_seconds):
            ts_data.append({
                "second": s,
                "new_connections": cps.get(s, 0),
                "bytes": bps.get(s, 0),
            })
        
        df = pd.DataFrame(ts_data)
        csv_path = output_dir / "timeseries.csv"
        df.to_csv(csv_path, index=False)
        print(f"[*] Exported timeseries to {csv_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze captured network traffic and application telemetry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--pcap", "-p",
        type=Path,
        help="Path to packet capture file (.pcap)"
    )
    parser.add_argument(
        "--telemetry", "-t",
        type=Path,
        help="Path to telemetry logs directory"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("logs/analysis"),
        help="Output directory for analysis results (default: logs/analysis)"
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Export results to CSV files"
    )
    
    args = parser.parse_args()
    
    if not args.pcap and not args.telemetry:
        parser.print_help()
        print("\n[!] Please specify --pcap and/or --telemetry")
        sys.exit(1)
    
    # Analyze packet capture
    pcap_analysis = {}
    if args.pcap:
        if not args.pcap.exists():
            print(f"[!] Pcap file not found: {args.pcap}")
            sys.exit(1)
        pcap_analysis = analyze_pcap(args.pcap)
        print_flow_summary(pcap_analysis)
    
    # Analyze telemetry
    telemetry_analysis = {}
    if args.telemetry:
        if not args.telemetry.exists():
            print(f"[!] Telemetry directory not found: {args.telemetry}")
            sys.exit(1)
        telemetry_analysis = analyze_telemetry(args.telemetry)
        print_telemetry_summary(telemetry_analysis)
    
    # Export to CSV if requested
    if args.csv and pcap_analysis:
        export_to_csv(pcap_analysis, args.output)
    
    print("\n" + "=" * 70)
    print("ANALYSIS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
