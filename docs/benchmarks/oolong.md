## OOLONG Benchmark Integration

This document describes how the OOLONG benchmark is wired into the Agentic Traffic Testbed and how to run it end-to-end.

---

### 1. Data source and layout

- **Upstream repo**: `https://github.com/yale-nlp/OOLONG`
- **Split used**: `trec_coarse` (as in the RLM paper)

By default the testbed expects a local clone of the OOLONG repo alongside this project:

- `<parent>/agentic-traffic-testing`
- `<parent>/OOLONG`

The root can be overridden with the `OOLONG_ROOT` environment variable.

Within the OOLONG checkout the runner looks for:

- `data/oolong/trec_coarse.jsonl` (preferred)
- or `data/trec_coarse.jsonl`

---

### 2. Loader implementation

File: `benchmarks/oolong/loader.py`

- **`OolongExample`**: dataclass with fields:
  - `task_id: str`
  - `input_context: str`
  - `query: str`
  - `ground_truth: str`
- **`load_trec_coarse(oolong_root: Path | None = None)`**:
  - Resolves the OOLONG root (using `OOLONG_ROOT` or the default sibling checkout).
  - Locates `trec_coarse.jsonl`.
  - Yields `OolongExample` instances for each record.
- **`iter_trec_coarse_tuples(oolong_root: Path | None = None)`**:
  - Convenience wrapper that yields:
    - `(task_id, input_context, query, ground_truth)`
  - This matches the interface described in `docs/to_do.md` Phase 1.1.

The loader is tolerant to minor schema differences in the upstream JSONL. It tries common field names such as:

- Context: `context`, `input_context`, `input`
- Query: `question`, `query`
- Ground truth: `answer`, `ground_truth`, `label`

---

### 3. Scoring implementation

File: `benchmarks/oolong/scorer.py`

- **Scoring rule**:
  - For numerical answers:
    - `score(ŷ) = 0.75 ** |y - ŷ|`
  - For non-numerical answers:
    - Exact match on case-folded, trimmed strings.
- **Helpers**:
  - `_try_parse_number(text: str) -> Optional[Number]`
    - Attempts to parse int/float, handling common prefixes like `"Answer:"`.
- **Public API**:
  - `oolong_score(y_true: str, y_pred: str) -> ScoreResult`
    - Returns:
      - `score: float` in `[0, 1]`
      - `is_numeric: bool`
      - `abs_error: Optional[float]` for numeric cases
  - `oolong_score_scalar(y_true: str, y_pred: str) -> float`
    - Convenience wrapper returning just the scalar score.

---

### 4. Runner implementation

File: `benchmarks/oolong/runner.py`

The runner connects the OOLONG dataset to Agent A’s `/task` endpoint and writes per-task results.

#### 4.1. CLI

Run via:

```bash
python -m benchmarks.oolong.runner \
  --scenario agentic_simple \
  --max-tasks 50 \
  --context-size 20000 \
  --output logs/benchmarks/oolong_trec_coarse.jsonl
```

Arguments:

- `--agent-url`:
  - Agent A `/task` endpoint.
  - Default: `http://localhost:8101/task` (or `AGENT_A_URL` env var if set).
- `--scenario`:
  - One of: `agentic_simple`, `agentic_multi_hop`, `agentic_parallel`.
- `--max-tasks`:
  - Maximum number of OOLONG items to run (default: all).
- `--context-size`:
  - Approximate maximum **context size in characters**.
  - If set, `input_context` is truncated to this many characters before being sent.
  - This is a character-level proxy for the token-based context sizes mentioned in the roadmap.
- `--output`:
  - Path to the JSONL file for per-task results.
  - Default: `logs/benchmarks/oolong_trec_coarse.jsonl`.
- `--timeout`:
  - Per-request timeout in seconds for calls to Agent A (default: `300`).

#### 4.2. Request construction

For each OOLONG item `(task_id, input_context, query, ground_truth)`:

1. Optionally truncate `input_context` based on `--context-size` (keep the tail).
2. Build the task text as:
   - `"{truncated_context}\n\nQuestion: {query}"`
3. Send a POST to Agent A:

   - URL: `--agent-url` (default `http://localhost:8101/task`)
   - JSON payload:
     - `task`: combined context + question
     - `scenario`: selected scenario
     - `benchmark_source`: `"oolong"` (tag for downstream metrics)

4. Expect a JSON response with at least:
   - `output`: final answer text
   - `task_id`: the internal task identifier (if present).

If `output` is missing, the entire response is serialised as the model answer so the scoring step can still run.

#### 4.3. Scoring and output schema

The runner:

1. Scores each `(ground_truth, model_answer)` pair via `oolong_score`.
2. Writes one JSON object per line to the output file with fields:
   - `benchmark_source`: `"oolong"`
   - `benchmark_split`: `"trec_coarse"`
   - `oolong_task_id`: the original dataset ID
   - `task_id`: Agent A task ID (if present)
   - `scenario`: chosen scenario
   - `context_size_chars`: effective context-size parameter (or `null`)
   - `ground_truth`: ground-truth answer
   - `model_answer`: model’s answer text
   - `score`: numeric score in `[0, 1]`
   - `is_numeric`: whether the score used the numeric formula
   - `abs_error`: absolute numeric error (if applicable)
   - `agent_response`: full Agent A JSON response for debugging

If the call to Agent A fails, the runner writes a record with:

- `error`: stringified exception
- `score = 0.0`
- `model_answer = null`
- `agent_response = null`

and continues to the next task.

At the end of the run it prints a brief summary to stderr:

- Number of tasks processed
- Mean score
- Count of numeric items

---

### 5. End-to-end script

File: `scripts/experiment/run_oolong_benchmark.sh`

This script automates cloning the OOLONG repo (if needed) and running the Python runner:

```bash
./scripts/experiment/run_oolong_benchmark.sh \
  --scenario agentic_simple \
  --max-tasks 50 \
  --context-size 20000
```

Behaviour:

- Resolves the repository root.
- Sets `OOLONG_ROOT` to:
  - `${REPO_ROOT}/../OOLONG` by default (overridable via env).
- Clones `https://github.com/yale-nlp/OOLONG.git` into `OOLONG_ROOT` if the directory does not exist.
- Exports `OOLONG_ROOT` for the loader.
- Invokes:
  - `python -m benchmarks.oolong.runner "$@"`

You can pass any of the runner flags through the shell script.

