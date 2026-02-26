"""
Run MCP-Universe benchmarks from the agentic traffic testbed.

This script assumes you have cloned the MCP-Universe repository and set
MCP_UNIVERSE_DIR to point at it. It invokes MCP-Universe's benchmark runners
with the appropriate environment (including OPENAI_BASE_URL if using the
local LLM proxy).

Usage:
    # Set path to MCP-Universe repo (required)
    export MCP_UNIVERSE_DIR=/path/to/MCP-Universe

    # Optional: use local LLM via OpenAI proxy (default: http://localhost:8110/v1)
    export OPENAI_API_KEY=dummy
    export OPENAI_BASE_URL=http://localhost:8110/v1

    # Run a specific benchmark domain (e.g. dummy, location_navigation, etc.)
    python scripts/experiment/run_mcp_universe.py dummy

    # Run all available domains (runs each test file found under tests/benchmark/)
    python scripts/experiment/run_mcp_universe.py --all

    # List available benchmark domains
    python scripts/experiment/run_mcp_universe.py --list

See docs/mcp_universe_integration.md for full setup instructions.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Known MCP-Universe benchmark test modules (from their tests/benchmark/mcpuniverse/)
# Map short names to the test module path
MCP_UNIVERSE_BENCHMARKS = {
    "dummy": "tests/benchmark/mcpuniverse/test_benchmark_dummy.py",
    "location_navigation": "tests/benchmark/mcpuniverse/test_benchmark_location_navigation.py",
    "browser_automation": "tests/benchmark/mcpuniverse/test_benchmark_browser_automation.py",
    "financial_analysis": "tests/benchmark/mcpuniverse/test_benchmark_financial_analysis.py",
    "repository_management": "tests/benchmark/mcpuniverse/test_benchmark_repository_management.py",
    "web_search": "tests/benchmark/mcpuniverse/test_benchmark_web_search.py",
    "3d_design": "tests/benchmark/mcpuniverse/test_benchmark_3d_design.py",
}


def get_mcp_universe_dir() -> Path | None:
    """Return MCP_UNIVERSE_DIR if set and valid, else None."""
    raw = os.environ.get("MCP_UNIVERSE_DIR", "")
    if not raw:
        return None
    path = Path(raw).resolve()
    if not path.is_dir():
        return None
    return path


def discover_benchmarks(mcp_dir: Path) -> dict[str, Path]:
    """Find benchmark test files under MCP-Universe's tests/benchmark/mcpuniverse/."""
    benchmark_dir = mcp_dir / "tests" / "benchmark" / "mcpuniverse"
    found: dict[str, Path] = {}
    if not benchmark_dir.is_dir():
        return found
    for f in benchmark_dir.glob("test_benchmark_*.py"):
        name = f.stem.replace("test_benchmark_", "")
        found[name] = f
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run MCP-Universe benchmarks from the agentic traffic testbed"
    )
    parser.add_argument(
        "domain",
        nargs="?",
        help="Benchmark domain to run (e.g. dummy, location_navigation)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all discovered benchmark domains",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available benchmark domains and exit",
    )
    parser.add_argument(
        "--mcp-universe-dir",
        default=os.environ.get("MCP_UNIVERSE_DIR", ""),
        help="Path to MCP-Universe repo (default: MCP_UNIVERSE_DIR env)",
    )
    args = parser.parse_args()

    mcp_dir = Path(args.mcp_universe_dir).resolve() if args.mcp_universe_dir else get_mcp_universe_dir()

    if not mcp_dir or not mcp_dir.is_dir():
        print(
            "[ERROR] MCP-Universe directory not found. Set MCP_UNIVERSE_DIR or pass --mcp-universe-dir.",
            file=sys.stderr,
        )
        print("  Example: export MCP_UNIVERSE_DIR=/path/to/MCP-Universe", file=sys.stderr)
        return 1

    benchmarks = discover_benchmarks(mcp_dir)
    if not benchmarks:
        benchmarks = dict(MCP_UNIVERSE_BENCHMARKS)  # fallback to known names
        # Resolve paths relative to mcp_dir
        for name, rel_path in list(benchmarks.items()):
            full = mcp_dir / rel_path
            if full.exists():
                benchmarks[name] = full
            else:
                del benchmarks[name]

    if args.list:
        print("Available MCP-Universe benchmark domains:")
        for name in sorted(benchmarks.keys()):
            print(f"  {name}")
        return 0

    if args.all:
        domains_to_run = sorted(benchmarks.keys())
    elif args.domain:
        if args.domain not in benchmarks:
            print(f"[ERROR] Unknown domain: {args.domain}", file=sys.stderr)
            print(f"  Available: {', '.join(sorted(benchmarks.keys()))}", file=sys.stderr)
            return 1
        domains_to_run = [args.domain]
    else:
        parser.print_help()
        return 0

    env = os.environ.copy()
    env["PYTHONPATH"] = str(mcp_dir) + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")

    failed = []
    for domain in domains_to_run:
        test_path = benchmarks.get(domain)
        if not test_path or not Path(test_path).exists():
            print(f"[SKIP] {domain}: test file not found")
            continue
        print(f"\n[*] Running MCP-Universe benchmark: {domain}")
        ret = subprocess.run(
            [sys.executable, str(test_path)],
            cwd=str(mcp_dir),
            env=env,
        )
        if ret.returncode != 0:
            failed.append(domain)

    if failed:
        print(f"\n[FAILED] {len(failed)} benchmark(s): {', '.join(failed)}", file=sys.stderr)
        return 1
    print("\n[OK] All benchmarks completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
