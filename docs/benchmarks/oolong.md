## OOLONG Benchmark Integration

This document describes how the OOLONG benchmark is wired into the Agentic Traffic Testbed and how to run it end-to-end.

Upstream repo: **https://github.com/abertsch72/oolong**

---

### 1. Data source and layout

OOLONG-synth is published on the HuggingFace Hub:

```
oolongbench/oolong-synth  (test split, 5200 examples across 10 datasets)
```

The `datasets` library downloads and caches it automatically — no manual data download or local JSONL files required.

The benchmark runner filters to a single dataset within the synth split.  The default is `trec_coarse` (as used in the RLM paper).  Other available datasets: `metaphors`, `multinli`, `negation`, `yahoo`, `imdb`, `formality`, `agnews`, `spam`, `app_reviews`.

---

### 2. OOLONG repo dependency (for scoring)

Scoring delegates to the official `eval_helpers.py` from the OOLONG repo.  You need a local clone:

```bash
# Default expected path (sibling of this project):
git clone https://github.com/abertsch72/oolong.git ../oolong
```

Or set `OOLONG_ROOT` to point at an existing clone:

```bash
export OOLONG_ROOT=/home/dlamagna/projects/oolong
```

The scorer adds `${OOLONG_ROOT}/src/eval` to `sys.path` and imports `synth_process_response` from `eval_helpers.py`.

Install the OOLONG dependencies (in addition to the testbed's own deps):

```bash
pip install datasets python-dateutil
# Full OOLONG deps if needed:
# pip install -r /path/to/oolong/requirements.txt
```

---

### 3. Loader implementation

File: `benchmarks/oolong/loader.py`

- **`OolongExample`**: dataclass with fields:
  - `task_id: str` — `str(record["id"])` from HF
  - `input_context: str` — `record["context_window_text"]`
  - `query: str` — `record["question"]`
  - `ground_truth: str` — `str(record["answer"])`, e.g. `"['entity']"` or `"[3]"`
  - `raw: dict` — full HF record (passed unchanged to the scorer)
- **`load_oolong_synth(dataset_filter, split)`**:
  - Loads `oolongbench/oolong-synth` from HuggingFace.
  - Filters by `dataset` field if `dataset_filter` is set.
  - Normalises the `answer` field to a string (handles HF schema variants).
  - Yields `OolongExample` instances.
- **`load_trec_coarse()` / `iter_trec_coarse_tuples()`**:
  - Thin wrappers kept for backwards compatibility.

---

### 4. Scoring implementation

File: `benchmarks/oolong/scorer.py`

Scoring uses the official OOLONG `synth_process_response` function from the upstream repo (`src/eval/eval_helpers.py`).

- **`oolong_score(datapoint, y_pred, model) -> ScoreResult`**:
  - Calls `synth_process_response(datapoint, y_pred, model)` from `eval_helpers.py`.
  - Returns a `ScoreResult` with fields:
    - `score: float` in `[0, 1]`
    - `is_numeric: bool` — True for `ANSWER_TYPE.NUMERIC`
    - `abs_error: Optional[float]` — integer distance for numeric answers
    - `parse_confidence: str` — from the OOLONG answer parser (`"vhigh"`, `"high"`, `"med"`, `"low"`)
    - `attempted_parse: Optional[str]` — the extracted answer string before comparison
- **`oolong_score_scalar(datapoint, y_pred, model) -> float`**:
  - Convenience wrapper returning just the scalar score.

The OOLONG scoring rules (from `eval_helpers.py`):
- **Numeric** (`ANSWER_TYPE.NUMERIC`): `score = 0.75 ** |gold - pred|`
- **Label/string**: exact match after normalisation
- **Date** (`ANSWER_TYPE.DATE`): parsed date equality via `python-dateutil`

---

### 5. Runner implementation

File: `benchmarks/oolong/runner.py`

#### 5.1. CLI

```bash
python -m benchmarks.oolong.runner \
  --scenario agentic_simple \
  --dataset trec_coarse \
  --max-tasks 50 \
  --context-size 20000 \
  --output logs/benchmarks/oolong_trec_coarse.jsonl
```

Arguments:

- `--agent-url`: Agent A `/task` endpoint (default: `http://localhost:8101/task` or `AGENT_A_URL` env var).
- `--scenario`: `agentic_simple`, `agentic_multi_hop`, or `agentic_parallel`.
- `--dataset`: OOLONG-synth dataset filter (default: `trec_coarse`). Pass empty string for all datasets.
- `--max-tasks`: Maximum number of items to run (default: all).
- `--context-size`: Truncate `context_window_text` to this many characters (tail kept).
  Intended for scaling experiments (Phase 6.2) only — omit for standard runs.
- `--output`: Output JSONL path (default: `logs/benchmarks/oolong_trec_coarse.jsonl`).
- `--timeout`: Per-request timeout in seconds (default: `300`).

#### 5.2. Request construction

For each example:

1. Optionally truncate `input_context` based on `--context-size` (tail kept).
2. Build the task text as `"{truncated_context}\n\nQuestion: {query}"`.
3. POST to Agent A with payload `{task, scenario, benchmark_source: "oolong"}`.

#### 5.3. Scoring and output schema

Each output line contains:

| Field | Description |
|-------|-------------|
| `benchmark_source` | `"oolong"` |
| `benchmark_split` | dataset filter used (e.g. `"trec_coarse"`) |
| `oolong_task_id` | original HF `id` |
| `task_id` | Agent A task ID (if present) |
| `scenario` | chosen scenario |
| `context_size_chars` | `--context-size` value or `null` |
| `ground_truth` | raw answer string, e.g. `"['entity']"` |
| `model_answer` | model's full text output |
| `score` | numeric score in `[0, 1]` |
| `is_numeric` | whether numeric scoring was used |
| `abs_error` | integer error for numeric answers |
| `parse_confidence` | OOLONG parser confidence (`vhigh`/`high`/`med`/`low`/`error`) |
| `attempted_parse` | extracted answer token |
| `error` | error string (only on failed calls) |
| `agent_response` | full Agent A JSON response |

**Failed calls** (network errors, Agent A errors) are recorded with `score=0.0`,
`parse_confidence="error"`, and counted toward the aggregate mean — not excluded.

At the end the runner prints a summary to stderr:

```
OOLONG trec_coarse run complete: 50 tasks (errors=1), mean score=0.3412, numeric_items=12
```

---

### 6. End-to-end script

File: `scripts/experiment/run_oolong_benchmark.sh`

```bash
./scripts/experiment/run_oolong_benchmark.sh \
  --scenario agentic_simple \
  --dataset trec_coarse \
  --max-tasks 50
```

Behaviour:

- Resolves repo root.
- Sets `OOLONG_ROOT` to `${REPO_ROOT}/../oolong` (overridable via env).
- Clones `https://github.com/abertsch72/oolong.git` into `OOLONG_ROOT` if not present.
- Exports `OOLONG_ROOT` so the scorer can find `eval_helpers.py`.
- Invokes `python -m benchmarks.oolong.runner "$@"`.

Any `runner.py` flag can be passed through the shell script.
