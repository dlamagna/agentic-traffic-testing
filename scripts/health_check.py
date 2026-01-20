#!/usr/bin/env python3
"""
Health check script for the agentic traffic testbed.

This script verifies that all components are running and can communicate correctly.
It checks:
- Docker Compose services (if using Docker)
- LLM server connectivity
- Agent A and Agent B endpoints
- Agent-to-LLM connectivity (the critical path)
- UI endpoint (if available)
- DNS resolution for container names

Usage:
    python scripts/health_check.py
    python scripts/health_check.py --llm-url http://localhost:8000/chat
    python scripts/health_check.py --docker-compose-dir infra
"""

import argparse
import json
import os
import socket
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx


class Colors:
    """ANSI color codes for terminal output."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def print_check(name: str, status: bool, message: str = "") -> None:
    """Print a health check result."""
    status_str = f"{Colors.GREEN}✓{Colors.RESET}" if status else f"{Colors.RED}✗{Colors.RESET}"
    msg = f" {message}" if message else ""
    print(f"{status_str} {name}{msg}")


def print_section(title: str) -> None:
    """Print a section header."""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{title}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.RESET}\n")


def check_docker_compose_services(compose_dir: str) -> Dict[str, bool]:
    """Check if Docker Compose services are running."""
    results: Dict[str, bool] = {}
    
    if not os.path.exists(os.path.join(compose_dir, "docker-compose.yml")):
        print(f"{Colors.YELLOW}⚠{Colors.RESET} Docker Compose file not found at {compose_dir}/docker-compose.yml")
        return results
    
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", os.path.join(compose_dir, "docker-compose.yml"), "ps", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        if result.returncode != 0:
            print(f"{Colors.YELLOW}⚠{Colors.RESET} Docker Compose not available or services not running")
            return results
        
        services = {}
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                name = data.get("Name", "")
                state = data.get("State", "")
                services[name] = state == "running"
            except json.JSONDecodeError:
                continue
        
        expected_services = ["llm-backend", "agent-a", "agent-b", "chat-ui"]
        for svc in expected_services:
            if svc in services:
                results[svc] = services[svc]
                print_check(f"Docker service: {svc}", services[svc], f"({services[svc]})")
            else:
                results[svc] = False
                print_check(f"Docker service: {svc}", False, "(not found)")
        
    except FileNotFoundError:
        print(f"{Colors.YELLOW}⚠{Colors.RESET} Docker not found in PATH")
    except subprocess.TimeoutExpired:
        print(f"{Colors.YELLOW}⚠{Colors.RESET} Docker Compose check timed out")
    except Exception as e:
        print(f"{Colors.YELLOW}⚠{Colors.RESET} Docker Compose check failed: {e}")
    
    return results


def check_dns_resolution(hostname: str) -> Tuple[bool, Optional[str]]:
    """Check if a hostname resolves to an IP address."""
    try:
        ip = socket.gethostbyname(hostname)
        return True, ip
    except socket.gaierror:
        return False, None


def check_http_endpoint(url: str, method: str = "GET", json_data: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Tuple[bool, Optional[str]]:
    """
    Check if an HTTP endpoint is reachable and responds.
    
    Returns:
        (success: bool, error_message: Optional[str])
    """
    try:
        if method == "GET":
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        elif method == "POST":
            resp = httpx.post(url, json=json_data, timeout=timeout, follow_redirects=True)
        else:
            return False, f"Unsupported method: {method}"
        
        # Consider 2xx and 3xx as success, 4xx/5xx as failure
        if resp.is_success or resp.is_redirect:
            return True, None
        else:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.ConnectError as e:
        return False, f"Connection error: {str(e)}"
    except httpx.TimeoutException:
        return False, "Request timed out"
    except Exception as e:
        return False, f"Error: {str(e)}"


def check_llm_server(llm_url: str) -> Tuple[bool, Optional[str]]:
    """Check LLM server connectivity and functionality."""
    # Parse URL to check DNS resolution
    parsed = urlparse(llm_url)
    hostname = parsed.hostname
    
    if hostname and hostname not in ("localhost", "127.0.0.1", "0.0.0.0"):
        dns_ok, ip = check_dns_resolution(hostname)
        if not dns_ok:
            return False, f"DNS resolution failed for {hostname}"
        print(f"  → {hostname} resolves to {ip}")
    
    # Try a simple POST request
    success, error = check_http_endpoint(
        llm_url,
        method="POST",
        json_data={"prompt": "test"},
        timeout=30.0,
    )
    
    if success:
        return True, None
    else:
        return False, error


def check_agent_endpoint(agent_url: str, agent_name: str, field: str) -> Tuple[bool, Optional[str]]:
    """Check agent endpoint connectivity and functionality."""
    parsed = urlparse(agent_url)
    hostname = parsed.hostname
    
    if hostname and hostname not in ("localhost", "127.0.0.1", "0.0.0.0"):
        dns_ok, ip = check_dns_resolution(hostname)
        if not dns_ok:
            return False, f"DNS resolution failed for {hostname}"
        print(f"  → {hostname} resolves to {ip}")
    
    # Try a simple POST request
    success, error = check_http_endpoint(
        agent_url,
        method="POST",
        json_data={field: "health check test"},
        timeout=30.0,
    )
    
    if success:
        return True, None
    else:
        return False, error


def check_agent_to_llm_connectivity(agent_url: str, agent_name: str, llm_url: str, field: str) -> Tuple[bool, Optional[str]]:
    """
    Check if an agent can successfully call the LLM server.
    This is the critical path that was failing in the original error.
    """
    try:
        # Send a request to the agent that will trigger an LLM call
        resp = httpx.post(
            agent_url,
            json={field: "Say hello"},
            timeout=60.0,
        )
        
        if resp.status_code == 502:
            # This is the error we're looking for - agent can't reach LLM
            error_text = resp.text
            if "name resolution" in error_text.lower() or "temporary failure" in error_text.lower():
                return False, f"Agent cannot resolve LLM hostname. Error: {error_text[:200]}"
            return False, f"LLM call failed (502). Error: {error_text[:200]}"
        
        if resp.status_code >= 500:
            return False, f"Server error: HTTP {resp.status_code}: {resp.text[:200]}"
        
        if resp.is_success:
            # Agent responded successfully, which means it could reach the LLM
            return True, None
        else:
            return False, f"Unexpected status: HTTP {resp.status_code}"
            
    except httpx.ConnectError as e:
        return False, f"Cannot connect to agent: {str(e)}"
    except httpx.TimeoutException:
        return False, "Request to agent timed out"
    except Exception as e:
        return False, f"Error: {str(e)}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Health check for agentic traffic testbed")
    parser.add_argument(
        "--llm-url",
        default=os.environ.get("LLM_SERVER_URL", "http://localhost:8000/chat"),
        help="LLM server URL (default: LLM_SERVER_URL env var or http://localhost:8000/chat)",
    )
    parser.add_argument(
        "--agent-a-url",
        default="http://localhost:8101/task",
        help="Agent A endpoint URL (default: http://localhost:8101/task)",
    )
    parser.add_argument(
        "--agent-b-url",
        default="http://localhost:8102/subtask",
        help="Agent B endpoint URL (default: http://localhost:8102/subtask)",
    )
    parser.add_argument(
        "--ui-url",
        default="http://localhost:3000",
        help="UI endpoint URL (default: http://localhost:3000)",
    )
    parser.add_argument(
        "--docker-compose-dir",
        default="infra",
        help="Directory containing docker-compose.yml (default: infra)",
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="Skip Docker Compose service checks",
    )
    
    args = parser.parse_args()
    
    all_checks_passed = True
    
    # Docker Compose services check
    if not args.skip_docker:
        print_section("Docker Compose Services")
        docker_results = check_docker_compose_services(args.docker_compose_dir)
        if docker_results:
            all_checks_passed = all_checks_passed and all(docker_results.values())
    
    # LLM Server check
    print_section("LLM Server")
    llm_ok, llm_error = check_llm_server(args.llm_url)
    print_check("LLM Server", llm_ok, llm_error or f"({args.llm_url})")
    if not llm_ok:
        all_checks_passed = False
        if llm_error:
            print(f"  {Colors.RED}Error: {llm_error}{Colors.RESET}")
    
    # Agent A check
    print_section("Agent A")
    agent_a_ok, agent_a_error = check_agent_endpoint(args.agent_a_url, "Agent A", "task")
    print_check("Agent A endpoint", agent_a_ok, agent_a_error or f"({args.agent_a_url})")
    if not agent_a_ok:
        all_checks_passed = False
        if agent_a_error:
            print(f"  {Colors.RED}Error: {agent_a_error}{Colors.RESET}")
    
    # Agent A → LLM connectivity (critical path)
    if llm_ok and agent_a_ok:
        print_section("Agent A → LLM Connectivity (Critical Path)")
        agent_a_llm_ok, agent_a_llm_error = check_agent_to_llm_connectivity(
            args.agent_a_url, "Agent A", args.llm_url, "task"
        )
        print_check(
            "Agent A can reach LLM",
            agent_a_llm_ok,
            agent_a_llm_error or "(successful end-to-end test)",
        )
        if not agent_a_llm_ok:
            all_checks_passed = False
            if agent_a_llm_error:
                print(f"  {Colors.RED}Error: {agent_a_llm_error}{Colors.RESET}")
                print(f"  {Colors.YELLOW}This is likely the issue causing your 502 error!{Colors.RESET}")
                print(f"  {Colors.YELLOW}Check that LLM_SERVER_URL in Agent A's environment matches a reachable URL.{Colors.RESET}")
    
    # Agent B check
    print_section("Agent B")
    agent_b_ok, agent_b_error = check_agent_endpoint(args.agent_b_url, "Agent B", "subtask")
    print_check("Agent B endpoint", agent_b_ok, agent_b_error or f"({args.agent_b_url})")
    if not agent_b_ok:
        all_checks_passed = False
        if agent_b_error:
            print(f"  {Colors.RED}Error: {agent_b_error}{Colors.RESET}")
    
    # Agent B → LLM connectivity
    if llm_ok and agent_b_ok:
        print_section("Agent B → LLM Connectivity")
        agent_b_llm_ok, agent_b_llm_error = check_agent_to_llm_connectivity(
            args.agent_b_url, "Agent B", args.llm_url, "subtask"
        )
        print_check(
            "Agent B can reach LLM",
            agent_b_llm_ok,
            agent_b_llm_error or "(successful end-to-end test)",
        )
        if not agent_b_llm_ok:
            all_checks_passed = False
            if agent_b_llm_error:
                print(f"  {Colors.RED}Error: {agent_b_llm_error}{Colors.RESET}")
    
    # UI check
    print_section("UI (Chat Console)")
    ui_ok, ui_error = check_http_endpoint(args.ui_url, method="GET", timeout=10.0)
    print_check("UI endpoint", ui_ok, ui_error or f"({args.ui_url})")
    if not ui_ok:
        # UI is optional, so don't fail overall check
        if ui_error:
            print(f"  {Colors.YELLOW}Warning: {ui_error}{Colors.RESET}")
    
    # Summary
    print_section("Summary")
    if all_checks_passed:
        print(f"{Colors.GREEN}{Colors.BOLD}✓ All critical checks passed!{Colors.RESET}")
        sys.exit(0)
    else:
        print(f"{Colors.RED}{Colors.BOLD}✗ Some checks failed. Please review the errors above.{Colors.RESET}")
        print(f"\n{Colors.YELLOW}Common issues:{Colors.RESET}")
        print("  1. LLM server not running or not reachable")
        print("  2. Agent containers cannot resolve LLM hostname (check LLM_SERVER_URL)")
        print("  3. Services not started: cd infra && docker compose up -d")
        print("  4. Port conflicts: check if ports 8000, 8101, 8102 are already in use")
        sys.exit(1)


if __name__ == "__main__":
    main()
