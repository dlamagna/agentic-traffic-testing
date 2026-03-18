"""
AgentBench adapter — bridges the AgentBench HTTP controller with Agent A.

Architecture
------------
In *controller mode* (``agentbench_url`` is set) the adapter connects to a
running AgentBench task server and runs the multi-turn interaction loop on
behalf of Agent A.  For each turn it:

  1. Formats the current conversation history as a structured prompt.
  2. POSTs to Agent A's ``/task`` endpoint.
  3. Parses Agent A's text response for a tool call intent.
  4. Returns the formatted response back to the AgentBench controller.

The controller then executes the tool (bash command, SQL query, …) and
returns the tool result in the updated history, ready for the next turn.

Agent A is used purely as a text-generation backend; it does not need to
support native function calling.  The adapter injects tool descriptions into
the prompt and parses Agent A's free-text output for structured action
patterns.

Response format sent to controller
-----------------------------------
The adapter sends a plain-text response to the AgentBench ``/interact``
endpoint using the text-action format that the task servers recognise.

For OS tasks the server's ``_extract_action`` parser expects::

    Think: <reasoning>
    Act: bash
    ```bash
    <command>
    ```

or::

    Act: answer(<value>)

For DB/KG tasks a JSON-formatted tool call is attempted first, falling
back to the ``Think/Act`` text format.

Note: the exact parsing behaviour depends on which version of the
AgentBench task server is running.  The text format is the most broadly
compatible.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.agentbench.loader import (
    TASK_TYPE_DB,
    TASK_TYPE_KG,
    TASK_TYPE_OS,
    AgentBenchTask,
)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_INTRO = """\
You are an AI agent solving a task step by step using the available tools.
At each step you must call exactly one tool.
Respond ONLY in the format shown below — do not add any extra prose outside
the format block.\
"""

_TOOL_CALL_FORMAT = """\
Respond with:

Think: <one sentence of reasoning>
Act: <tool_name>
Arguments:
```json
{<tool arguments as JSON>}
```

If the task is done or you have the final answer, respond with:

Think: <reasoning>
Act: commit_final_answer
Arguments:
```json
{"answers": ["<value1>", ...]}
```\
"""

_OS_TOOL_CALL_FORMAT = """\
Respond with ONE of the following formats:

To run a bash command:
    Think: <reasoning>
    Act: bash
    ```bash
    <command>
    ```

To submit a text answer:
    Think: <reasoning>
    Act: answer(<your answer>)

To finish when the task is complete:
    Think: <reasoning>
    Act: finish\
"""


def _format_tool_descriptions(tools: List[Dict[str, Any]]) -> str:
    """Render a compact list of available tools as plain text."""
    if not tools:
        return ""
    lines = ["Available tools:"]
    for tool in tools:
        fn = tool.get("function", tool)
        name = fn.get("name", "?")
        desc = fn.get("description", "")
        params = fn.get("parameters", {}).get("properties", {})
        param_names = ", ".join(params.keys())
        lines.append(f"  - {name}({param_names}): {desc}")
    return "\n".join(lines)


def _format_history_as_text(history: List[Dict[str, Any]]) -> str:
    """
    Serialize an OpenAI-format chat history to a readable text block.

    Tool call results are rendered as ``[Tool: name] result``.
    """
    lines = []
    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""

        if role == "system":
            lines.append(f"[System]\n{content}")
        elif role == "user":
            lines.append(f"[User]\n{content}")
        elif role == "assistant":
            # May have tool_calls instead of / alongside content
            tool_calls = msg.get("tool_calls") or []
            if content:
                lines.append(f"[Assistant]\n{content}")
            for tc in tool_calls:
                fn = tc.get("function", {})
                fname = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                    args_str = json.dumps(args, ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    args_str = fn.get("arguments", "{}")
                lines.append(f"[Tool Call: {fname}]\n{args_str}")
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            lines.append(f"[Tool Result]\n{content}")
        else:
            lines.append(f"[{role}]\n{content}")

    return "\n\n".join(lines)


def build_agent_prompt(
    task: AgentBenchTask,
    history: List[Dict[str, Any]],
) -> str:
    """
    Build the prompt to send to Agent A for the current conversation turn.

    The prompt includes:
    - System instructions and tool descriptions
    - Serialised conversation history (tool calls + results)
    - A format reminder at the end
    """
    tool_desc = _format_tool_descriptions(task.tools)
    history_text = _format_history_as_text(history) if history else "(no history yet)"
    fmt = _OS_TOOL_CALL_FORMAT if task.task_type == TASK_TYPE_OS else _TOOL_CALL_FORMAT

    parts = [
        _SYSTEM_INTRO,
        "",
        tool_desc,
        "",
        "--- Conversation so far ---",
        history_text,
        "--- End of conversation ---",
        "",
        fmt,
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_agent_response(
    task_type: str,
    tools: List[Dict[str, Any]],
    agent_text: str,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    Parse Agent A's free-text response into a tool call.

    Returns ``(tool_name, formatted_content, parsed_args)`` where
    ``formatted_content`` is the string to send to ``/interact`` and
    ``parsed_args`` is the parsed argument dict (or None on failure).
    """
    if task_type == TASK_TYPE_OS:
        return _parse_os_response(agent_text)
    return _parse_generic_response(tools, agent_text)


def _parse_os_response(
    text: str,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Parse OS-style Think/Act format."""
    # Extract thought
    think_m = re.search(r'Think:\s*(.+?)(?:\nAct:|$)', text, re.DOTALL | re.IGNORECASE)
    thought = think_m.group(1).strip() if think_m else ""

    act_m = re.search(r'Act:\s*(.+)', text, re.IGNORECASE)
    act_raw = act_m.group(1).strip() if act_m else ""

    # bash action
    bash_m = re.search(r'```bash\s*\n(.*?)\n```', text, re.DOTALL)
    if bash_m or act_raw.lower().startswith("bash"):
        script = bash_m.group(1).strip() if bash_m else ""
        content = f"Think: {thought}\nAct: bash\n```bash\n{script}\n```"
        return "bash_action", content, {"script": script}

    # finish action
    if act_raw.lower().startswith("finish"):
        content = f"Think: {thought}\nAct: finish"
        return "finish_action", content, {"thought": thought}

    # answer action
    ans_m = re.search(r'answer\((.+)\)', act_raw, re.IGNORECASE)
    if ans_m:
        answer = ans_m.group(1).strip().strip('"\'')
        content = f"Think: {thought}\nAct: answer({answer})"
        return "answer_action", content, {"answer": answer}

    # fallback: treat the entire response as a bash command attempt
    # (better than sending nothing)
    code_m = re.search(r'```(?:bash|sh)?\s*\n(.*?)\n```', text, re.DOTALL)
    if code_m:
        script = code_m.group(1).strip()
        content = f"Think: {thought}\nAct: bash\n```bash\n{script}\n```"
        return "bash_action", content, {"script": script}

    # Nothing parseable — send a finish
    content = f"Think: Could not determine action.\nAct: finish"
    return "finish_action", content, {"thought": "parse_failed"}


def _parse_generic_response(
    tools: List[Dict[str, Any]],
    text: str,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    Parse a structured ``Think/Act/Arguments`` response.

    Falls back to extracting JSON blocks if the format is not followed exactly.
    """
    think_m = re.search(r'Think:\s*(.+?)(?:\nAct:|$)', text, re.DOTALL | re.IGNORECASE)
    thought = think_m.group(1).strip() if think_m else ""

    act_m = re.search(r'Act:\s*(\w+)', text, re.IGNORECASE)
    tool_name = act_m.group(1).strip() if act_m else ""

    # Extract JSON arguments block
    args: Optional[Dict[str, Any]] = None
    json_m = re.search(r'```json\s*\n(\{.*?\})\s*```', text, re.DOTALL)
    if json_m:
        try:
            args = json.loads(json_m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    if not args:
        # Try any inline JSON object
        inline_m = re.search(r'(\{[^{}]+\})', text, re.DOTALL)
        if inline_m:
            try:
                args = json.loads(inline_m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

    if not tool_name and args:
        # Infer tool from argument keys
        tool_name = _infer_tool_name(tools, args)

    if not tool_name:
        # Last resort: look for commit_final_answer patterns
        ans_m = re.search(
            r'(?:final answer|answer)\s*[:\-]?\s*(.+)', text, re.IGNORECASE
        )
        if ans_m:
            answer = ans_m.group(1).strip()
            tool_name = "commit_final_answer"
            args = {"answers": [answer]}

    if not tool_name:
        tool_name = "commit_final_answer"
        args = {"answers": [text[:200].strip()]}

    args_str = json.dumps(args or {}, ensure_ascii=False)
    content = (
        f"Think: {thought}\n"
        f"Act: {tool_name}\n"
        f"Arguments:\n```json\n{args_str}\n```"
    )
    return tool_name, content, args


def _infer_tool_name(
    tools: List[Dict[str, Any]], args: Dict[str, Any]
) -> str:
    """Guess tool name by matching argument keys against tool parameter names."""
    for tool in tools:
        fn = tool.get("function", tool)
        params = set(fn.get("parameters", {}).get("properties", {}).keys())
        if params and params.issubset(set(args.keys())):
            return fn.get("name", "")
    return ""


# ---------------------------------------------------------------------------
# Controller interaction helpers
# ---------------------------------------------------------------------------


def make_agent_output(content: str) -> Dict[str, Any]:
    """Build an ``AgentOutput``-compatible dict for the ``/interact`` endpoint."""
    return {"status": "normal", "content": content}


def make_interact_request(session_id: int, content: str) -> Dict[str, Any]:
    """Build a full ``InteractRequest`` dict."""
    return {
        "session_id": session_id,
        "agent_response": make_agent_output(content),
    }
