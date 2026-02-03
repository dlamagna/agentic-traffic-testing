"""
AgentVerse Orchestrator Module

Implements the 4-stage AgentVerse workflow:
1. Expert Recruitment - Dynamically determine agent composition
2. Collaborative Decision-Making - Horizontal or vertical communication
3. Action Execution - Execute collaboratively-decided actions
4. Evaluation - Assess results and provide feedback for iteration

Based on: https://arxiv.org/pdf/2308.10848 (AgentVerse paper)
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple
from enum import Enum

import httpx

from opentelemetry import context as otel_context
from opentelemetry import propagate
from opentelemetry.trace import SpanKind

from agents.common.telemetry import TelemetryLogger
from agents.common.tracing import get_tracer, span_to_metadata
from agents.agent_a.prompts import (
    EXPERT_RECRUITMENT_PROMPT,
    HORIZONTAL_DISCUSSION_PROMPT,
    VERTICAL_SOLVER_PROMPT,
    VERTICAL_REVIEWER_PROMPT,
    EXECUTION_PROMPT,
    EVALUATION_PROMPT,
    FINAL_SYNTHESIS_PROMPT,
    SYNTHESIZE_DISCUSSION_PROMPT,
)


# Configuration
LLM_SERVER_URL = os.environ.get("LLM_SERVER_URL", "http://localhost:8000/chat")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "120"))
AGENT_B_TIMEOUT_SECONDS = float(os.environ.get("AGENT_B_TIMEOUT_SECONDS", "120"))
MAX_PARALLEL_WORKERS = int(os.environ.get("MAX_PARALLEL_WORKERS", "5"))
DEFAULT_AGENT_B_URL = os.environ.get("AGENT_B_URL", "http://agent-b:8102/subtask")
AGENT_B_URLS = [
    url.strip()
    for url in os.environ.get("AGENT_B_URLS", "").split(",")
    if url.strip()
]
if not AGENT_B_URLS:
    AGENT_B_URLS = [DEFAULT_AGENT_B_URL]


class CommunicationStructure(Enum):
    HORIZONTAL = "horizontal"  # Democratic - all agents discuss
    VERTICAL = "vertical"      # Solver + reviewers


@dataclass
class Expert:
    """Represents a recruited expert agent."""
    role: str
    responsibilities: str
    contract: str
    endpoint: Optional[str] = None
    index: int = 0


@dataclass
class RecruitmentResult:
    """Result of the expert recruitment stage."""
    experts: List[Expert]
    communication_structure: CommunicationStructure
    execution_order: List[str]
    reasoning: str


@dataclass
class DecisionResult:
    """Result of the collaborative decision-making stage."""
    final_decision: str
    discussion_rounds: List[Dict[str, Any]]
    consensus_reached: bool
    structure_used: str
    solver_role: Optional[str] = None
    reviewer_roles: List[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    """Result of the action execution stage."""
    outputs: List[Dict[str, Any]]
    success_count: int
    failure_count: int


@dataclass
class EvaluationResult:
    """Result of the evaluation stage."""
    goal_achieved: bool
    score: int
    criteria: Optional[Dict[str, int]] = None  # Breakdown: completeness, correctness, clarity, relevance, actionability
    rationale: Optional[str] = None  # Explanation of how the score was calculated
    feedback: str = ""
    missing_aspects: List[str] = field(default_factory=list)
    should_iterate: bool = False


@dataclass
class AgentVerseState:
    """Complete state of an AgentVerse workflow execution."""
    task_id: str
    original_task: str
    iteration: int = 0
    max_iterations: int = 3
    success_threshold: int = 70  # Score (0-100) required to accept and stop iterating
    
    # Stage results
    recruitment: Optional[RecruitmentResult] = None
    decision: Optional[DecisionResult] = None
    execution: Optional[ExecutionResult] = None
    evaluation: Optional[EvaluationResult] = None
    
    # History across iterations
    iteration_history: List[Dict[str, Any]] = field(default_factory=list)
    
    # Detailed LLM request/response log for each call
    llm_requests: List[Dict[str, Any]] = field(default_factory=list)
    
    # Final output
    final_output: Optional[str] = None
    completed: bool = False


class AgentVerseOrchestrator:
    """
    Orchestrator implementing the AgentVerse 4-stage workflow.
    
    Stages:
    1. Expert Recruitment - Dynamically determine agent composition
    2. Collaborative Decision-Making - Horizontal or vertical communication  
    3. Action Execution - Execute collaboratively-decided actions
    4. Evaluation - Assess results and provide feedback for iteration
    """
    
    def __init__(self, logger: TelemetryLogger, tracer=None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.logger = logger
        self.tracer = tracer or get_tracer("agent-a-orchestrator")
        self.http_client = httpx.Client(timeout=LLM_TIMEOUT_SECONDS)
        self.progress_callback = progress_callback
    
    def _call_llm(
        self,
        prompt: str,
        headers: Optional[Dict[str, str]] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Call the LLM server and return (output, metadata)."""
        payload: Dict[str, Any] = {"prompt": prompt}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        # Create a dedicated client span for each LLM HTTP request so we can
        # surface trace/span IDs in the raw request JSON (no UI changes needed).
        with self.tracer.start_as_current_span("agent_a.call_llm", kind=SpanKind.CLIENT) as span_llm:
            start_time_utc = datetime.now(timezone.utc).isoformat()
            span_llm.set_attribute("app.request_start_time_utc", start_time_utc)
            span_llm.set_attribute("app.llm.url", LLM_SERVER_URL)
            if max_tokens is not None:
                span_llm.set_attribute("llm.max_tokens", int(max_tokens))

            merged_headers: Dict[str, str] = dict(headers or {})
            propagate.inject(merged_headers)

            resp = self.http_client.post(
                LLM_SERVER_URL,
                json=payload,
                headers=merged_headers,
                timeout=LLM_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data: Dict[str, Any] = resp.json()

            output = str(data.get("output", ""))
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            meta_out: Dict[str, Any] = {
                "llm_backend": meta,
                "otel": {
                    "agent_a": span_to_metadata(span_llm),
                    "llm_backend": meta.get("otel") if isinstance(meta, dict) else {},
                },
            }
            return output, meta_out
    
    def _call_agent_b(
        self,
        subtask: str,
        scenario: str = "agentic_verse",
        headers: Optional[Dict[str, str]] = None,
        agent_b_role: Optional[str] = None,
        agent_b_contract: Optional[str] = None,
        agent_b_url: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call an Agent B instance."""
        payload: Dict[str, Any] = {"subtask": subtask, "scenario": scenario}
        if agent_b_role:
            payload["agent_b_role"] = agent_b_role
        if agent_b_contract:
            payload["agent_b_contract"] = agent_b_contract
        
        target_url = agent_b_url or DEFAULT_AGENT_B_URL
        
        # Validate URL
        if not target_url or not isinstance(target_url, str) or not target_url.strip():
            raise ValueError(f"Invalid Agent B URL: {target_url!r}")
        
        # Log the attempt for debugging
        self.logger.log(
            task_id=task_id or 'unknown',
            event_type="agent_b_call_attempt",
            message=f"Calling Agent B at {target_url}",
            extra={
                "url": target_url,
                "role": agent_b_role,
                "scenario": scenario,
            },
        )
        
        try:
            resp = self.http_client.post(
                target_url,
                json=payload,
                headers=headers,
                timeout=AGENT_B_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data: Dict[str, Any] = resp.json()
            return {
                "output": str(data.get("output", "")),
                "llm_prompt": data.get("llm_prompt"),
                "llm_response": data.get("llm_response"),
                "llm_endpoint": data.get("llm_endpoint"),
                "llm_meta": data.get("llm_meta") if isinstance(data.get("llm_meta"), dict) else None,
                "otel": data.get("otel") if isinstance(data.get("otel"), dict) else None,
            }
        except httpx.ConnectError as e:
            error_msg = (
                f"Failed to connect to Agent B at {target_url}. "
                f"Error: {e}. "
                f"Please verify that the Agent B service is running and reachable. "
                f"Available URLs: {AGENT_B_URLS}"
            )
            self.logger.log(
                task_id=task_id or 'unknown',
                event_type="agent_b_connection_error",
                message=error_msg,
                extra={
                    "url": target_url,
                    "role": agent_b_role,
                    "available_urls": AGENT_B_URLS,
                },
            )
            raise ConnectionError(error_msg) from e
        except httpx.TimeoutException as e:
            error_msg = (
                f"Timeout connecting to Agent B at {target_url} "
                f"(timeout: {AGENT_B_TIMEOUT_SECONDS}s)"
            )
            self.logger.log(
                task_id=task_id or 'unknown',
                event_type="agent_b_timeout_error",
                message=error_msg,
                extra={
                    "url": target_url,
                    "role": agent_b_role,
                    "timeout": AGENT_B_TIMEOUT_SECONDS,
                },
            )
            raise TimeoutError(error_msg) from e
        except httpx.HTTPStatusError as e:
            error_msg = (
                f"Agent B returned error status {e.response.status_code} "
                f"for URL {target_url}: {e.response.text[:200]}"
            )
            self.logger.log(
                task_id=task_id or 'unknown',
                event_type="agent_b_http_error",
                message=error_msg,
                extra={
                    "url": target_url,
                    "role": agent_b_role,
                    "status_code": e.response.status_code,
                },
            )
            raise
    
    def _send_progress(self, event_type: str, data: Dict[str, Any]) -> None:
        """Send progress update via callback if available."""
        if self.progress_callback:
            self.progress_callback({
                "event": event_type,
                "data": data
            })
    
    def _record_llm_request(
        self,
        state: AgentVerseState,
        *,
        stage: str,
        label: str,
        prompt: str,
        response: str,
        source: str = "Agent A",
        agent_role: Optional[str] = None,
        endpoint: Optional[str] = None,
        round_num: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        request_id: Optional[str] = None,
        otel: Optional[Dict[str, Any]] = None,
        llm_meta: Optional[Dict[str, Any]] = None,
        start_time_utc: Optional[str] = None,
    ) -> None:
        """Record an LLM request/response for the detailed flow.

        duration_seconds: end-to-end task duration (LLM call for Agent A direct calls,
        or full Agent B round-trip including network, Agent B processing, and LLM call).
        start_time_utc: ISO 8601 UTC timestamp when the request started (for tracing and UI).
        """
        seq = len(state.llm_requests) + 1
        role = agent_role
        if role is None and source == "Agent A":
            role = "orchestrator"
        entry: Dict[str, Any] = {
            "seq": seq,
            "iteration": state.iteration,
            "stage": stage,
            "label": label,
            "source": source,
            "prompt": prompt,
            "response": response,
            "endpoint": endpoint or LLM_SERVER_URL,
        }
        if start_time_utc is not None:
            entry["start_time_utc"] = start_time_utc
        if request_id is not None:
            entry["request_id"] = request_id
        if otel is not None:
            entry["otel"] = otel
        if llm_meta is not None:
            entry["llm_meta"] = llm_meta
        if role:
            entry["agent_role"] = role
        if round_num is not None:
            entry["round"] = round_num
        if duration_seconds is not None:
            entry["duration_seconds"] = round(duration_seconds, 2)
        state.llm_requests.append(entry)
        
        # Send progress update with new LLM request
        self._send_progress("llm_request", entry)
    
    def _new_llm_request_id(self, state: AgentVerseState) -> str:
        """Generate a short, human-friendly ID for an LLM call.

        This ID is propagated to the LLM backend (via X-Request-ID) so that
        Docker logs can be correlated with the UI's LLM request table/graph.
        """
        return str(uuid.uuid4())[:8]
    
    def _parse_json_response(self, response: str, default: Any = None) -> Any:
        """Parse JSON from LLM response, handling common issues."""
        # Try to extract JSON from the response
        response = response.strip()
        
        # If response starts with ```json, extract the content
        if response.startswith("```json"):
            response = response[7:]
        if response.startswith("```"):
            response = response[3:]
        if response.endswith("```"):
            response = response[:-3]
        
        response = response.strip()
        
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            # Try to find JSON object in the response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    return json.loads(response[start:end])
                except json.JSONDecodeError:
                    pass
            return default
    
    # ========================================================================
    # Stage 1: Expert Recruitment
    # ========================================================================
    
    def recruit_experts(
        self,
        state: AgentVerseState,
        feedback: Optional[str] = None
    ) -> RecruitmentResult:
        """
        Stage 1: Analyze the task and recruit appropriate expert agents.
        """
        with self.tracer.start_as_current_span(
            "orchestrator.recruit_experts",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("app.task_id", state.task_id)
            span.set_attribute("app.iteration", state.iteration)
            
            # Send progress update: starting recruitment
            self._send_progress("stage_start", {
                "stage": "recruitment",
                "stage_number": 1,
                "iteration": state.iteration,
                "message": "Analyzing task and recruiting expert agents..."
            })
            
            feedback_context = ""
            if feedback:
                feedback_context = f"\nFeedback from previous iteration:\n{feedback}\n"
            
            prompt = EXPERT_RECRUITMENT_PROMPT.format(
                task=state.original_task,
                feedback_context=feedback_context,
            )
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_recruitment_start",
                message="Starting expert recruitment",
                extra={"iteration": state.iteration},
            )
            
            headers: Dict[str, str] = {}
            propagate.inject(headers)
            request_id = self._new_llm_request_id(state)
            headers["X-Request-ID"] = request_id
            t0 = time.time()
            response, llm_trace_meta = self._call_llm(prompt, headers=headers)
            duration = time.time() - t0
            start_time_utc = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()

            self._record_llm_request(
                state,
                stage="recruitment",
                label="expert_recruitment",
                prompt=prompt,
                response=response,
                source="Agent A",
                agent_role="orchestrator",
                duration_seconds=duration,
                request_id=request_id,
                otel=llm_trace_meta.get("otel"),
                llm_meta=llm_trace_meta.get("llm_backend"),
                start_time_utc=start_time_utc,
            )
            
            parsed = self._parse_json_response(response, {})
            
            # Parse experts
            experts = []
            raw_experts = parsed.get("experts", [])
            
            # Validate AGENT_B_URLS is not empty
            if not AGENT_B_URLS:
                raise ValueError(
                    f"No Agent B URLs configured. Please set AGENT_B_URLS environment variable. "
                    f"Default URL would be: {DEFAULT_AGENT_B_URL}"
                )
            
            for idx, expert_data in enumerate(raw_experts[:MAX_PARALLEL_WORKERS]):
                endpoint = AGENT_B_URLS[idx % len(AGENT_B_URLS)]
                
                # Validate endpoint URL
                if not endpoint or not isinstance(endpoint, str) or not endpoint.strip():
                    self.logger.log(
                        task_id=state.task_id,
                        event_type="agentverse_invalid_endpoint",
                        message=f"Invalid endpoint for expert {idx}: {endpoint!r}",
                        extra={
                            "expert_index": idx,
                            "endpoint": endpoint,
                            "available_urls": AGENT_B_URLS,
                        },
                    )
                    # Fall back to first available URL
                    endpoint = AGENT_B_URLS[0] if AGENT_B_URLS else DEFAULT_AGENT_B_URL
                
                experts.append(Expert(
                    role=expert_data.get("role", "executor"),
                    responsibilities=expert_data.get("responsibilities", ""),
                    contract=expert_data.get("contract", ""),
                    endpoint=endpoint,
                    index=idx,
                ))
            
            # Default experts if none parsed
            if not experts:
                if not AGENT_B_URLS:
                    raise ValueError(
                        f"No Agent B URLs available for default expert. "
                        f"Please set AGENT_B_URLS environment variable."
                    )
                experts = [
                    Expert(
                        role="executor",
                        responsibilities="Execute the given task",
                        contract="You are an executor agent. Complete the assigned task thoroughly.",
                        endpoint=AGENT_B_URLS[0],
                        index=0,
                    )
                ]
            
            # Parse communication structure
            structure_str = parsed.get("communication_structure", "horizontal")
            try:
                structure = CommunicationStructure(structure_str.lower())
            except ValueError:
                structure = CommunicationStructure.HORIZONTAL
            
            # Use LLM reasoning if provided, else generate fallback from structure
            raw_reasoning = parsed.get("reasoning", "").strip()
            if raw_reasoning:
                reasoning = raw_reasoning
            else:
                structure_desc = (
                    "democratic discussion among all experts"
                    if structure == CommunicationStructure.HORIZONTAL
                    else "solver proposes, reviewers critique, solver refines"
                )
                reasoning = (
                    f"Selected {structure.value} communication structure ({structure_desc}) "
                    f"with {len(experts)} expert(s): {', '.join(e.role for e in experts)}."
                )
            
            result = RecruitmentResult(
                experts=experts,
                communication_structure=structure,
                execution_order=parsed.get("execution_order", [e.role for e in experts]),
                reasoning=reasoning,
            )
            
            # Log expert endpoints for debugging
            expert_endpoints = {e.role: e.endpoint for e in experts}
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_recruitment_complete",
                message=f"Recruited {len(experts)} experts",
                extra={
                    "experts": [e.role for e in experts],
                    "expert_endpoints": expert_endpoints,
                    "structure": structure.value,
                    "reasoning": result.reasoning,
                    "available_agent_b_urls": AGENT_B_URLS,
                },
            )
            
            span.set_attribute("app.expert_count", len(experts))
            span.set_attribute("app.communication_structure", structure.value)
            
            # Send progress update: recruitment complete
            self._send_progress("stage_complete", {
                "stage": "recruitment",
                "stage_number": 1,
                "iteration": state.iteration,
                "experts": [{"role": e.role, "responsibilities": e.responsibilities} for e in experts],
                "communication_structure": structure.value,
                "reasoning": reasoning
            })
            
            return result
    
    # ========================================================================
    # Stage 2: Collaborative Decision-Making
    # ========================================================================
    
    def collaborative_decision(
        self,
        state: AgentVerseState,
        recruitment: RecruitmentResult,
    ) -> DecisionResult:
        """
        Stage 2: Agents engage in collaborative discussion to decide on approach.
        """
        with self.tracer.start_as_current_span(
            "orchestrator.collaborative_decision",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("app.task_id", state.task_id)
            span.set_attribute("app.structure", recruitment.communication_structure.value)
            
            # Send progress update: starting decision-making
            self._send_progress("stage_start", {
                "stage": "decision",
                "stage_number": 2,
                "iteration": state.iteration,
                "message": f"Starting {recruitment.communication_structure.value} decision-making...",
                "structure": recruitment.communication_structure.value
            })
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_decision_start",
                message=f"Starting {recruitment.communication_structure.value} decision-making",
            )
            
            if recruitment.communication_structure == CommunicationStructure.HORIZONTAL:
                result = self._horizontal_discussion(state, recruitment)
            else:
                result = self._vertical_decision(state, recruitment)
            
            # Send progress update: decision complete
            self._send_progress("stage_complete", {
                "stage": "decision",
                "stage_number": 2,
                "iteration": state.iteration,
                "consensus_reached": result.consensus_reached,
                "structure": result.structure_used,
                "rounds": len(result.discussion_rounds)
            })
            
            return result
    
    def _horizontal_discussion(
        self,
        state: AgentVerseState,
        recruitment: RecruitmentResult,
        max_rounds: int = 3,
    ) -> DecisionResult:
        """Horizontal (democratic) discussion among all agents."""
        discussion_rounds: List[Dict[str, Any]] = []
        discussion_history = ""
        consensus_reached = False
        
        for round_num in range(1, max_rounds + 1):
            round_responses = []
            all_consensus = True
            
            for expert in recruitment.experts:
                prompt = HORIZONTAL_DISCUSSION_PROMPT.format(
                    role=expert.role,
                    contract=expert.contract,
                    task=state.original_task,
                    discussion_history=discussion_history or "(No discussion yet)",
                    round_num=round_num,
                )
                
                try:
                    headers: Dict[str, str] = {}
                    propagate.inject(headers)
                    request_id = self._new_llm_request_id(state)
                    headers["X-Request-ID"] = request_id
                    t0 = time.time()
                    response = self._call_agent_b(
                        subtask=prompt,
                        agent_b_role=expert.role,
                        agent_b_contract=expert.contract,
                        agent_b_url=expert.endpoint,
                        headers=headers,
                        task_id=state.task_id,
                    )
                    duration = time.time() - t0
                    start_time_utc = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()
                    output = response.get("output", "")
                    llm_prompt = response.get("llm_prompt") or prompt
                    llm_response = response.get("llm_response") or output
                    self._record_llm_request(
                        state,
                        stage="decision",
                        label=f"horizontal_discussion_round{round_num}",
                        prompt=llm_prompt,
                        response=llm_response,
                        source=f"agent-b-{expert.index + 1}",
                        agent_role=expert.role,
                        endpoint=response.get("llm_endpoint"),
                        round_num=round_num,
                        duration_seconds=duration,
                        request_id=request_id,
                        otel=response.get("otel"),
                        llm_meta=response.get("llm_meta"),
                        start_time_utc=start_time_utc,
                    )
                except Exception as exc:
                    output = f"[Agent error: {exc}]"
                
                round_responses.append({
                    "expert": expert.role,
                    "index": expert.index,
                    "response": output,
                    "consensus": "[CONSENSUS]" in output,
                })
                
                if "[CONSENSUS]" not in output:
                    all_consensus = False
            
            # Build history for next round
            round_summary = f"\n--- Round {round_num} ---\n"
            for resp in round_responses:
                round_summary += f"{resp['expert'].upper()}: {resp['response']}\n"
            discussion_history += round_summary
            
            discussion_rounds.append({
                "round": round_num,
                "responses": round_responses,
            })
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_discussion_round",
                message=f"Completed discussion round {round_num}",
                extra={"round": round_num, "all_consensus": all_consensus},
            )
            
            # Send progress update: round complete
            self._send_progress("discussion_round", {
                "stage": "decision",
                "round": round_num,
                "iteration": state.iteration,
                "responses": round_responses,
                "consensus": all_consensus
            })
            
            if all_consensus:
                consensus_reached = True
                break
        
        # Synthesize final decision from discussion
        final_decision = self._synthesize_discussion(state, discussion_history)
        
        return DecisionResult(
            final_decision=final_decision,
            discussion_rounds=discussion_rounds,
            consensus_reached=consensus_reached,
            structure_used="horizontal",
            solver_role=None,
            reviewer_roles=[e.role for e in recruitment.experts],
        )
    
    def _vertical_decision(
        self,
        state: AgentVerseState,
        recruitment: RecruitmentResult,
        max_iterations: int = 3,
    ) -> DecisionResult:
        """Vertical (solver + reviewers) decision-making."""
        discussion_rounds: List[Dict[str, Any]] = []
        
        # Find solver and reviewers.
        # Prefer a planner as solver; if none, fall back to first expert.
        solver: Optional[Expert] = None
        reviewers: List[Expert] = []

        if recruitment.experts:
            planner_solver = next(
                (e for e in recruitment.experts if e.role == "planner"),
                None,
            )
            if planner_solver is not None:
                solver = planner_solver
                reviewers = [e for e in recruitment.experts if e is not planner_solver]
            else:
                solver = recruitment.experts[0]
                reviewers = recruitment.experts[1:] if len(recruitment.experts) > 1 else []
        
        if not solver:
            return DecisionResult(
                final_decision="No solver agent available",
                discussion_rounds=[],
                consensus_reached=False,
                structure_used="vertical",
                solver_role=None,
                reviewer_roles=[],
            )
        
        proposal = ""
        critiques = ""
        
        for iteration in range(1, max_iterations + 1):
            # Solver proposes
            previous_context = ""
            if proposal:
                previous_context = f"\nYour previous proposal:\n{proposal}\n"
            critique_context = ""
            if critiques:
                critique_context = f"\nReviewer critiques:\n{critiques}\n"
            
            solver_prompt = VERTICAL_SOLVER_PROMPT.format(
                contract=solver.contract,
                task=state.original_task,
                previous_proposal=previous_context,
                critiques=critique_context,
            )
            
            try:
                headers: Dict[str, str] = {}
                propagate.inject(headers)
                request_id = self._new_llm_request_id(state)
                headers["X-Request-ID"] = request_id
                t0 = time.time()
                response = self._call_agent_b(
                    subtask=solver_prompt,
                    agent_b_role=solver.role,
                    agent_b_contract=solver.contract,
                    agent_b_url=solver.endpoint,
                    headers=headers,
                    task_id=state.task_id,
                )
                duration = time.time() - t0
                start_time_utc = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()
                proposal = response.get("output", "")
                llm_prompt = response.get("llm_prompt") or solver_prompt
                llm_response = response.get("llm_response") or proposal
                self._record_llm_request(
                    state,
                    stage="decision",
                    label=f"vertical_solver_iter{iteration}",
                    prompt=llm_prompt,
                    response=llm_response,
                    source=f"agent-b-{solver.index + 1}",
                    agent_role=solver.role,
                    endpoint=response.get("llm_endpoint"),
                    round_num=iteration,
                    duration_seconds=duration,
                    request_id=request_id,
                    otel=response.get("otel"),
                    llm_meta=response.get("llm_meta"),
                    start_time_utc=start_time_utc,
                )
            except Exception as exc:
                proposal = f"[Solver error: {exc}]"
            
            # Reviewers critique (in parallel)
            reviewer_responses: List[Dict[str, Any]] = []
            all_approved = True

            if reviewers:
                request_ctx = otel_context.get_current()

                def _reviewer_task(
                    ctx: otel_context.Context,
                    reviewer: Expert,
                ) -> Dict[str, Any]:
                    token = otel_context.attach(ctx)
                    try:
                        reviewer_prompt = VERTICAL_REVIEWER_PROMPT.format(
                            role=reviewer.role,
                            contract=reviewer.contract,
                            task=state.original_task,
                            proposal=proposal,
                        )

                        headers: Dict[str, str] = {}
                        propagate.inject(headers)
                        request_id = self._new_llm_request_id(state)
                        headers["X-Request-ID"] = request_id
                        t0 = time.time()
                        response = self._call_agent_b(
                            subtask=reviewer_prompt,
                            agent_b_role=reviewer.role,
                            agent_b_contract=reviewer.contract,
                            agent_b_url=reviewer.endpoint,
                            headers=headers,
                            task_id=state.task_id,
                        )
                        duration = time.time() - t0
                        start_time_utc = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()
                        critique = response.get("output", "")
                        llm_prompt = response.get("llm_prompt") or reviewer_prompt
                        llm_response = response.get("llm_response") or critique
                        self._record_llm_request(
                            state,
                            stage="decision",
                            label=f"vertical_reviewer_{reviewer.role}_iter{iteration}",
                            prompt=llm_prompt,
                            response=llm_response,
                            source=f"agent-b-{reviewer.index + 1}",
                            agent_role=reviewer.role,
                            endpoint=response.get("llm_endpoint"),
                            round_num=iteration,
                            duration_seconds=duration,
                            request_id=request_id,
                            otel=response.get("otel"),
                            llm_meta=response.get("llm_meta"),
                            start_time_utc=start_time_utc,
                        )
                    except Exception as exc:
                        critique = f"[Reviewer error: {exc}]"
                    finally:
                        otel_context.detach(token)

                    return {
                        "reviewer": reviewer.role,
                        "critique": critique,
                        "approved": "[APPROVED]" in critique,
                    }

                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=len(reviewers)) as executor:
                    futures = [
                        executor.submit(_reviewer_task, request_ctx, reviewer)
                        for reviewer in reviewers
                    ]
                    for future in futures:
                        reviewer_responses.append(future.result())

                all_approved = all(r.get("approved", False) for r in reviewer_responses)
            
            critiques = "\n".join([
                f"{r['reviewer']}: {r['critique']}"
                for r in reviewer_responses
            ])
            
            discussion_rounds.append({
                "iteration": iteration,
                "proposal": proposal,
                "reviewer_responses": reviewer_responses,
                "all_approved": all_approved,
            })
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_vertical_iteration",
                message=f"Completed vertical iteration {iteration}",
                extra={"iteration": iteration, "all_approved": all_approved},
            )
            
            # Send progress update: vertical iteration complete
            self._send_progress("vertical_iteration", {
                "stage": "decision",
                "iteration": state.iteration,
                "solver_iteration": iteration,
                "proposal": proposal[:200] + "..." if len(proposal) > 200 else proposal,
                "reviewer_responses": reviewer_responses,
                "all_approved": all_approved
            })
            
            if all_approved:
                break
        
        return DecisionResult(
            final_decision=proposal,
            discussion_rounds=discussion_rounds,
            consensus_reached=all_approved if reviewers else True,
            structure_used="vertical",
            solver_role=solver.role if solver else None,
            reviewer_roles=[r.role for r in reviewers],
        )
    
    def _synthesize_discussion(
        self,
        state: AgentVerseState,
        discussion_history: str,
    ) -> str:
        """Synthesize a final decision from discussion history."""
        prompt = SYNTHESIZE_DISCUSSION_PROMPT.format(
            task=state.original_task,
            discussion_history=discussion_history,
        )
        
        headers: Dict[str, str] = {}
        propagate.inject(headers)
        request_id = self._new_llm_request_id(state)
        headers["X-Request-ID"] = request_id
        t0 = time.time()
        # Allow a larger completion for the final synthesized answer
        response, llm_trace_meta = self._call_llm(prompt, headers=headers, max_tokens=2048)
        duration = time.time() - t0
        self._record_llm_request(
            state,
            stage="decision",
            label="synthesize_discussion",
            prompt=prompt,
            response=response,
            source="Agent A",
            agent_role="orchestrator",
            duration_seconds=duration,
            request_id=request_id,
            otel=llm_trace_meta.get("otel"),
            llm_meta=llm_trace_meta.get("llm_backend"),
        )
        return response
    
    # ========================================================================
    # Stage 3: Action Execution
    # ========================================================================
    
    def execute_actions(
        self,
        state: AgentVerseState,
        recruitment: RecruitmentResult,
        decision: DecisionResult,
    ) -> ExecutionResult:
        """
        Stage 3: Execute the collaboratively-decided actions.
        """
        with self.tracer.start_as_current_span(
            "orchestrator.execute_actions",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("app.task_id", state.task_id)
            
            # Send progress update: starting execution
            self._send_progress("stage_start", {
                "stage": "execution",
                "stage_number": 3,
                "iteration": state.iteration,
                "message": f"Executing tasks with {len(recruitment.experts)} agents...",
                "expert_count": len(recruitment.experts)
            })
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_execution_start",
                message="Starting action execution",
            )
            
            # Create subtasks based on decision
            subtasks = self._create_subtasks(state, recruitment, decision)
            
            outputs: List[Dict[str, Any]] = []
            success_count = 0
            failure_count = 0
            
            # Execute subtasks in parallel
            request_ctx = otel_context.get_current()
            
            def execute_subtask(
                ctx: otel_context.Context,
                expert: Expert,
                subtask: str,
            ) -> Dict[str, Any]:
                token = otel_context.attach(ctx)
                try:
                    with self.tracer.start_as_current_span(
                        f"orchestrator.execute_subtask.{expert.role}",
                        kind=SpanKind.CLIENT,
                    ):
                        # Validate expert endpoint
                        if not expert.endpoint or not isinstance(expert.endpoint, str) or not expert.endpoint.strip():
                            error_msg = (
                                f"Expert {expert.role} (index {expert.index}) has invalid endpoint: {expert.endpoint!r}. "
                                f"Available URLs: {AGENT_B_URLS}"
                            )
                            self.logger.log(
                                task_id=state.task_id,
                                event_type="agentverse_execution_error",
                                message=error_msg,
                                extra={
                                    "expert_role": expert.role,
                                    "expert_index": expert.index,
                                    "endpoint": expert.endpoint,
                                    "available_urls": AGENT_B_URLS,
                                },
                            )
                            raise ValueError(error_msg)
                        
                        prompt = EXECUTION_PROMPT.format(
                            role=expert.role,
                            contract=expert.contract,
                            task=state.original_task,
                            subtask=subtask,
                            decision_context=decision.final_decision[:500],
                        )
                        
                        headers: Dict[str, str] = {}
                        propagate.inject(headers)
                        request_id = self._new_llm_request_id(state)
                        headers["X-Request-ID"] = request_id
                        t0 = time.time()
                        response = self._call_agent_b(
                            subtask=prompt,
                            agent_b_role=expert.role,
                            agent_b_contract=expert.contract,
                            agent_b_url=expert.endpoint,
                            headers=headers,
                            task_id=state.task_id,
                        )
                        duration = time.time() - t0
                        start_time_utc = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()
                        llm_prompt = response.get("llm_prompt") or prompt
                        llm_response = response.get("llm_response") or response.get("output", "")
                        self._record_llm_request(
                            state,
                            stage="execution",
                            label=f"execute_{expert.role}",
                            prompt=llm_prompt,
                            response=llm_response,
                            source=f"agent-b-{expert.index + 1}",
                            agent_role=expert.role,
                            endpoint=response.get("llm_endpoint"),
                            duration_seconds=duration,
                            request_id=request_id,
                            otel=response.get("otel"),
                            llm_meta=response.get("llm_meta"),
                            start_time_utc=start_time_utc,
                        )
                        return {
                            "expert": expert.role,
                            "index": expert.index,
                            "subtask": subtask,
                            "output": response.get("output", ""),
                            "success": True,
                        }
                except Exception as exc:
                    return {
                        "expert": expert.role,
                        "index": expert.index,
                        "subtask": subtask,
                        "output": f"Execution failed: {exc}",
                        "success": False,
                    }
                finally:
                    otel_context.detach(token)
            
            with ThreadPoolExecutor(max_workers=len(recruitment.experts)) as executor:
                futures = []
                for expert, subtask in zip(recruitment.experts, subtasks):
                    future = executor.submit(
                        execute_subtask,
                        request_ctx,
                        expert,
                        subtask,
                    )
                    futures.append(future)
                
                for future in as_completed(futures):
                    result = future.result()
                    outputs.append(result)
                    if result.get("success"):
                        success_count += 1
                    else:
                        failure_count += 1
                    
                    # Send progress update: execution result
                    self._send_progress("execution_result", {
                        "stage": "execution",
                        "iteration": state.iteration,
                        "expert": result.get("expert"),
                        "success": result.get("success"),
                        "output_preview": result.get("output", "")[:200],
                        "completed": len(outputs),
                        "total": len(recruitment.experts)
                    })
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_execution_complete",
                message=f"Execution complete: {success_count} success, {failure_count} failures",
                extra={"success": success_count, "failures": failure_count},
            )
            
            result = ExecutionResult(
                outputs=outputs,
                success_count=success_count,
                failure_count=failure_count,
            )
            
            # Send progress update: execution complete
            self._send_progress("stage_complete", {
                "stage": "execution",
                "stage_number": 3,
                "iteration": state.iteration,
                "success_count": success_count,
                "failure_count": failure_count,
                "total": len(outputs)
            })
            
            return result
    
    def _create_subtasks(
        self,
        state: AgentVerseState,
        recruitment: RecruitmentResult,
        decision: DecisionResult,
    ) -> List[str]:
        """Create subtasks for each expert based on the decision."""
        subtasks = []
        for expert in recruitment.experts:
            subtask = f"""Based on your role as {expert.role}:

Responsibilities: {expert.responsibilities}

Execute your part of the plan:
{decision.final_decision}

Focus on what is relevant to your expertise.
"""
            subtasks.append(subtask)
        return subtasks
    
    # ========================================================================
    # Stage 4: Evaluation
    # ========================================================================
    
    def evaluate_results(
        self,
        state: AgentVerseState,
        execution: ExecutionResult,
    ) -> EvaluationResult:
        """
        Stage 4: Evaluate if the goal has been achieved.
        """
        with self.tracer.start_as_current_span(
            "orchestrator.evaluate_results",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("app.task_id", state.task_id)
            span.set_attribute("app.iteration", state.iteration)
            
            # Send progress update: starting evaluation
            self._send_progress("stage_start", {
                "stage": "evaluation",
                "stage_number": 4,
                "iteration": state.iteration,
                "message": "Evaluating results and determining if iteration is needed..."
            })
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_evaluation_start",
                message="Starting evaluation",
            )
            
            # Format results for evaluation
            results_text = "\n\n".join([
                f"[{output['expert']}]:\n{output['output']}"
                for output in execution.outputs
            ])
            
            prompt = EVALUATION_PROMPT.format(
                task=state.original_task,
                results=results_text,
                iteration=state.iteration + 1,
                max_iterations=state.max_iterations,
            )
            
            headers: Dict[str, str] = {}
            propagate.inject(headers)
            request_id = self._new_llm_request_id(state)
            headers["X-Request-ID"] = request_id
            t0 = time.time()
            response, llm_trace_meta = self._call_llm(prompt, headers=headers)
            duration = time.time() - t0
            start_time_utc = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()

            self._record_llm_request(
                state,
                stage="evaluation",
                label="evaluate_results",
                prompt=prompt,
                response=response,
                source="Agent A",
                agent_role="orchestrator",
                duration_seconds=duration,
                request_id=request_id,
                otel=llm_trace_meta.get("otel"),
                llm_meta=llm_trace_meta.get("llm_backend"),
                start_time_utc=start_time_utc,
            )
            
            parsed = self._parse_json_response(response, {})
            
            # Determine if we should iterate
            goal_achieved = parsed.get("goal_achieved", False)
            score = parsed.get("score", 50)
            should_iterate = parsed.get("should_iterate", False)
            
            # Extract structured criteria breakdown and rationale (before overriding)
            criteria = parsed.get("criteria")
            rationale = parsed.get("rationale")
            
            # Apply user's success threshold as source of truth:
            # - Score >= threshold: accept and stop.
            # - Score < threshold: do not accept; force another iteration (ignore LLM's goal_achieved).
            if state.success_threshold > 0:
                if score >= state.success_threshold:
                    goal_achieved = True
                    should_iterate = False
                else:
                    goal_achieved = False
                    should_iterate = True  # Always try again when below threshold (until max iterations)
            
            # Don't iterate if we've reached max iterations
            if state.iteration + 1 >= state.max_iterations:
                should_iterate = False
            if goal_achieved:
                should_iterate = False
            
            feedback = parsed.get("feedback", "") or ""
            missing_aspects = parsed.get("missing_aspects", [])
            # Fallback: if LLM returns empty feedback but we should iterate, synthesize from rationale/missing_aspects
            if not feedback.strip() and should_iterate and (rationale or missing_aspects):
                parts = []
                if rationale:
                    parts.append(f"Previous rationale: {rationale}")
                if missing_aspects:
                    parts.append(f"Missing or weak aspects: {', '.join(str(x) for x in missing_aspects)}.")
                feedback = " ".join(parts).strip() or (
                    f"Score {score}/100 is below threshold. Consider adjusting the expert team or approach."
                )
            
            result = EvaluationResult(
                goal_achieved=goal_achieved,
                score=score,
                criteria=criteria,
                rationale=rationale,
                feedback=feedback,
                missing_aspects=missing_aspects,
                should_iterate=should_iterate,
            )
            
            self.logger.log(
                task_id=state.task_id,
                event_type="agentverse_evaluation_complete",
                message=f"Evaluation: goal_achieved={goal_achieved}, score={score}",
                extra={
                    "goal_achieved": goal_achieved,
                    "score": score,
                    "should_iterate": should_iterate,
                },
            )
            
            span.set_attribute("app.goal_achieved", goal_achieved)
            span.set_attribute("app.score", score)
            
            # Send progress update: evaluation complete
            self._send_progress("stage_complete", {
                "stage": "evaluation",
                "stage_number": 4,
                "iteration": state.iteration,
                "goal_achieved": goal_achieved,
                "score": score,
                "should_iterate": should_iterate,
                "feedback": result.feedback
            })
            
            return result
    
    # ========================================================================
    # Main Workflow
    # ========================================================================
    
    def run_workflow(
        self,
        task: str,
        task_id: str,
        max_iterations: int = 3,
        success_threshold: int = 70,
    ) -> Dict[str, Any]:
        """
        Run the complete AgentVerse 4-stage workflow.
        success_threshold: score (0-100) required to accept and stop iterating.
        """
        with self.tracer.start_as_current_span(
            "orchestrator.run_workflow",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("app.task_id", task_id)
            span.set_attribute("app.success_threshold", success_threshold)
            
            state = AgentVerseState(
                task_id=task_id,
                original_task=task,
                max_iterations=max_iterations,
                success_threshold=min(100, max(0, success_threshold)),
            )
            
            self.logger.log(
                task_id=task_id,
                event_type="agentverse_workflow_start",
                message="Starting AgentVerse workflow",
                extra={"max_iterations": max_iterations},
            )
            
            feedback: Optional[str] = None
            
            while state.iteration < state.max_iterations:
                iteration_start = time.time()
                
                # Send progress update: starting iteration
                self._send_progress("iteration_start", {
                    "iteration": state.iteration,
                    "max_iterations": state.max_iterations,
                    "message": f"Starting iteration {state.iteration + 1} of {state.max_iterations}..."
                })
                
                # Stage 1: Expert Recruitment
                state.recruitment = self.recruit_experts(state, feedback)
                
                # Stage 2: Collaborative Decision-Making
                state.decision = self.collaborative_decision(state, state.recruitment)
                
                # Stage 3: Action Execution
                state.execution = self.execute_actions(
                    state, state.recruitment, state.decision
                )
                
                # Stage 4: Evaluation
                state.evaluation = self.evaluate_results(state, state.execution)
                
                # Record iteration
                iteration_time = time.time() - iteration_start
                iteration_entry = {
                    "iteration": state.iteration,
                    "duration_seconds": round(iteration_time, 2),
                    "recruitment": {
                        "experts": [e.role for e in state.recruitment.experts],
                        "structure": state.recruitment.communication_structure.value,
                    },
                    "decision": {
                        "consensus": state.decision.consensus_reached,
                        "rounds": len(state.decision.discussion_rounds),
                    },
                    "execution": {
                        "success": state.execution.success_count,
                        "failures": state.execution.failure_count,
                    },
                    "evaluation": {
                        "goal_achieved": state.evaluation.goal_achieved,
                        "score": state.evaluation.score,
                        "criteria": state.evaluation.criteria,
                        "rationale": state.evaluation.rationale,
                        "feedback": state.evaluation.feedback or "",
                    },
                }
                state.iteration_history.append(iteration_entry)
                
                # Stream iteration_complete so UI can update iteration history live
                self._send_progress("iteration_complete", {
                    "iteration_history": state.iteration_history,
                })
                
                # Check if we should continue
                if not state.evaluation.should_iterate:
                    break
                
                feedback = state.evaluation.feedback
                state.iteration += 1
            
            # Generate final output
            self._send_progress("stage_start", {
                "stage": "synthesis",
                "stage_number": 5,
                "iteration": state.iteration,
                "message": "Generating final synthesized output..."
            })
            
            state.final_output = self._generate_final_output(state)
            state.completed = True
            
            # Send progress update: synthesis complete
            self._send_progress("stage_complete", {
                "stage": "synthesis",
                "stage_number": 5,
                "iteration": state.iteration,
                "final_output": state.final_output
            })
            
            self.logger.log(
                task_id=task_id,
                event_type="agentverse_workflow_complete",
                message="AgentVerse workflow complete",
                extra={
                    "iterations": state.iteration + 1,
                    "final_score": state.evaluation.score if state.evaluation else 0,
                },
            )
            
            return self._state_to_response(state)
    
    def _generate_final_output(self, state: AgentVerseState) -> str:
        """Generate the final synthesized output."""
        if not state.execution:
            return "No execution results available."
        
        results_text = "\n\n".join([
            f"[{output['expert']}]:\n{output['output']}"
            for output in state.execution.outputs
        ])
        
        iteration_summary = "\n".join([
            f"Iteration {h['iteration'] + 1}: score={h['evaluation']['score']}, "
            f"experts={h['recruitment']['experts']}"
            for h in state.iteration_history
        ])
        
        evaluation_text = ""
        if state.evaluation:
            evaluation_text = f"""
Score: {state.evaluation.score}/100
Goal Achieved: {state.evaluation.goal_achieved}
Feedback: {state.evaluation.feedback}
"""
        
        prompt = FINAL_SYNTHESIS_PROMPT.format(
            task=state.original_task,
            iteration_summary=iteration_summary or "(Single iteration)",
            results=results_text,
            evaluation=evaluation_text,
        )
        
        headers: Dict[str, str] = {}
        propagate.inject(headers)
        request_id = self._new_llm_request_id(state)
        headers["X-Request-ID"] = request_id
        t0 = time.time()
        response, llm_trace_meta = self._call_llm(prompt, headers=headers, max_tokens=4096)
        duration = time.time() - t0
        start_time_utc = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()
        self._record_llm_request(
            state,
            stage="synthesis",
            label="final_output",
            prompt=prompt,
            response=response,
            source="Agent A",
            agent_role="orchestrator",
            duration_seconds=duration,
            request_id=request_id,
            otel=llm_trace_meta.get("otel"),
            llm_meta=llm_trace_meta.get("llm_backend"),
            start_time_utc=start_time_utc,
        )
        return response
    
    def _state_to_response(self, state: AgentVerseState) -> Dict[str, Any]:
        """Convert state to API response format."""
        # Calculate total duration from iteration history
        total_duration = sum(h.get("duration_seconds", 0) for h in state.iteration_history)
        
        return {
            "task_id": state.task_id,
            "original_task": state.original_task,
            "completed": state.completed,
            "iterations": state.iteration + 1,
            "duration_seconds": total_duration,
            "final_output": state.final_output,
            
            # Detailed stage results
            "stages": {
                "recruitment": {
                    "experts": [
                        {
                            "role": e.role,
                            "responsibilities": e.responsibilities,
                            "endpoint": e.endpoint,
                        }
                        for e in (state.recruitment.experts if state.recruitment else [])
                    ],
                    "communication_structure": (
                        state.recruitment.communication_structure.value
                        if state.recruitment else None
                    ),
                    "reasoning": state.recruitment.reasoning if state.recruitment else "",
                },
                "decision": {
                    "final_decision": state.decision.final_decision if state.decision else "",
                    "consensus_reached": state.decision.consensus_reached if state.decision else False,
                    "structure_used": state.decision.structure_used if state.decision else "",
                    "discussion_rounds": state.decision.discussion_rounds if state.decision else [],
                    "solver_role": state.decision.solver_role if state.decision else None,
                    "reviewer_roles": state.decision.reviewer_roles if state.decision else [],
                },
                "execution": {
                    "outputs": state.execution.outputs if state.execution else [],
                    "success_count": state.execution.success_count if state.execution else 0,
                    "failure_count": state.execution.failure_count if state.execution else 0,
                },
                "evaluation": {
                    "goal_achieved": state.evaluation.goal_achieved if state.evaluation else False,
                    "score": state.evaluation.score if state.evaluation else 0,
                    "criteria": state.evaluation.criteria if state.evaluation else None,
                    "rationale": state.evaluation.rationale if state.evaluation else None,
                    "feedback": state.evaluation.feedback if state.evaluation else "",
                    "missing_aspects": state.evaluation.missing_aspects if state.evaluation else [],
                },
            },
            
            # Iteration history
            "iteration_history": state.iteration_history,
            
            # Detailed LLM request/response log for each call
            "llm_requests": state.llm_requests,
        }
