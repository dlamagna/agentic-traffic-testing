#!/usr/bin/env python3
"""
KV cache calculator for vLLM — returns the largest power-of-2 max_model_len
that fits a target number of concurrent slots given current GPU memory.

GPU free memory is read automatically from nvidia-smi.
Model is read automatically from infra/.env (LLM_MODEL).

Usage:
  python3 kv_cache_calc.py -c 6
  python3 kv_cache_calc.py -c 6 --dtype bfloat16
  python3 kv_cache_calc.py -c 6 --model llama-3.1-8b   # override .env
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Model presets  (layers, kv_heads, head_dim, params_b)
# ---------------------------------------------------------------------------
MODELS = {
    "llama-3.2-3b": dict(layers=28, kv_heads=8,  head_dim=128, params_b=3.21,
                         label="meta-llama/Llama-3.2-3B-Instruct"),
    "llama-3.1-8b": dict(layers=32, kv_heads=8,  head_dim=128, params_b=8.03,
                         label="meta-llama/Llama-3.1-8B-Instruct"),
    "qwen2.5-7b":   dict(layers=28, kv_heads=4,  head_dim=128, params_b=7.62,
                         label="Qwen/Qwen2.5-7B-Instruct"),
}

# Map HuggingFace model IDs (from LLM_MODEL in .env) to preset keys
HF_MODEL_MAP = {
    "meta-llama/Llama-3.2-3B-Instruct": "llama-3.2-3b",
    "meta-llama/Llama-3.1-8B-Instruct": "llama-3.1-8b",
    "Qwen/Qwen2.5-7B-Instruct":         "qwen2.5-7b",
}

DTYPE_BYTES = {"float16": 2, "bfloat16": 2, "float32": 4}

# CUDA driver overhead: physical total − CUDA-visible (measured: 24 GiB → 23.57 GiB)
DRIVER_OVERHEAD_GIB = 0.43

# vLLM default KV block size in tokens
VLLM_BLOCK_SIZE = 16


# ---------------------------------------------------------------------------
# .env reader
# ---------------------------------------------------------------------------

def find_env_file() -> Path | None:
    """Walk up from this script to find infra/.env."""
    here = Path(__file__).resolve().parent
    for candidate in [here, here.parent, here.parent.parent]:
        p = candidate / "infra" / ".env"
        if p.exists():
            return p
    return None


def read_env_key(env_path: Path, key: str) -> str | None:
    """Return the value of a key from a .env file, or None."""
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip() or None
    return None


def model_preset_from_env(env_path: Path) -> str | None:
    """Map LLM_MODEL in .env to a MODELS preset key, or None."""
    hf_name = read_env_key(env_path, "LLM_MODEL")
    if hf_name is None:
        return None
    preset_key = HF_MODEL_MAP.get(hf_name)
    if preset_key is None:
        print(f"warning: LLM_MODEL='{hf_name}' not in known presets; "
              "use --model to override.", file=sys.stderr)
    return preset_key


# ---------------------------------------------------------------------------
# GPU memory detection
# ---------------------------------------------------------------------------

def gpu_free_and_total_gib() -> tuple[float, float]:
    """Read free and total GPU memory from nvidia-smi (GPU 0)."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"error: nvidia-smi failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Take first GPU line; values are in MiB
    line = out.strip().splitlines()[0]
    free_mib, total_mib = (int(x.strip()) for x in line.split(","))
    return free_mib / 1024, total_mib / 1024


# ---------------------------------------------------------------------------
# Core formulas
# ---------------------------------------------------------------------------

def powers_of_2_decomposition(n: int) -> str:
    """Express n as a human-readable sum of powers of 2, e.g. 10240 → '8192 + 2048'."""
    if n <= 0:
        return str(n)
    parts = []
    for bit in range(n.bit_length() - 1, -1, -1):
        if n & (1 << bit):
            parts.append(str(1 << bit))
    return " + ".join(parts)


def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int,
                        dtype_bytes: int) -> int:
    """
    KV cache bytes consumed by one token across all layers:
      layers × 2 (K+V) × kv_heads × head_dim × dtype_bytes
    """
    return layers * 2 * kv_heads * head_dim * dtype_bytes


def calc(free_gib: float, total_gib: float, concurrent: int,
         layers: int, kv_heads: int, head_dim: int,
         params_b: float, dtype_bytes: int, buffer_gib: float) -> dict:
    cuda_total    = total_gib - DRIVER_OVERHEAD_GIB
    gpu_util      = (free_gib - buffer_gib) / cuda_total
    reserved_gib  = gpu_util * cuda_total
    weights_gib   = params_b * 1e9 * dtype_bytes / (1024 ** 3)
    budget_gib    = reserved_gib - weights_gib
    bpt           = kv_bytes_per_token(layers, kv_heads, head_dim, dtype_bytes)
    total_tokens  = int(budget_gib * (1024 ** 3) / bpt)
    raw_per_slot  = total_tokens // concurrent
    # Align down to vLLM block size — no further rounding, maximise context length
    max_model_len = (raw_per_slot // VLLM_BLOCK_SIZE) * VLLM_BLOCK_SIZE

    return dict(
        free_gib=free_gib,
        total_gib=total_gib,
        cuda_total=cuda_total,
        gpu_util=gpu_util,
        reserved_gib=reserved_gib,
        weights_gib=weights_gib,
        budget_gib=budget_gib,
        bpt=bpt,
        total_tokens=total_tokens,
        raw_per_slot=raw_per_slot,
        max_model_len=max_model_len,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def W(width=62): return "─" * width


def print_result(r: dict, concurrent: int, model_label: str,
                 layers: int, kv_heads: int, head_dim: int,
                 params_b: float, dtype: str, buffer_gib: float):
    dtype_bytes = DTYPE_BYTES[dtype]
    decomp = powers_of_2_decomposition(r['max_model_len'])
    print()
    print("═" * 62)
    print("  vLLM KV Cache Calculator")
    print("═" * 62)
    print(f"  {'Model':<34} {model_label}")
    print(f"  {'Architecture':<34} {layers}L · {kv_heads} KV heads · {head_dim}d · {dtype}")
    print(f"  {'Parameters':<34} {params_b:.2f} B")
    print(W())
    print("  Memory budget")
    print(W())
    print(f"  {'nvidia-smi free / total':<34} {r['free_gib']:.2f} / {r['total_gib']:.2f} GiB")
    print(f"  {'  − driver overhead ({:.2f} GiB)':<34} → CUDA-visible: {r['cuda_total']:.2f} GiB".format(DRIVER_OVERHEAD_GIB))
    print(f"  {'  − buffer ({:.2f} GiB)':<34} → gpu_util: {r['gpu_util']:.4f}  ({r['reserved_gib']:.2f} GiB reserved)".format(buffer_gib))
    print(f"  {'  − model weights':<34} {r['weights_gib']:.2f} GiB  ({params_b:.2f}B × {dtype_bytes} B)")
    print(f"  {'  = KV cache budget':<34} {r['budget_gib']:.2f} GiB")
    print(W())
    print("  KV cache sizing")
    print(W())
    print(f"  {'Bytes per token':<34} {r['bpt']:,}  ({layers}×2×{kv_heads}×{head_dim}×{dtype_bytes})")
    print(f"  {'Total KV token capacity':<34} {r['total_tokens']:,}")
    print(f"  {'÷ {concurrent} concurrent slots':<34} = {r['raw_per_slot']:,} tokens/slot".format(concurrent=concurrent))
    print(f"  {'→ aligned to block size (÷{bs})':<34} {r['max_model_len']:,}  ({decomp})".format(bs=VLLM_BLOCK_SIZE))
    print()
    print(f"  ┌─ .env settings ──────────────────────────────────┐")
    print(f"  │  LLM_GPU_MEMORY_UTILIZATION={r['gpu_util']:.2f}               │")
    print(f"  │  LLM_MAX_NUM_SEQS={concurrent:<4}                         │")
    print(f"  │  LLM_MAX_MODEL_LEN={r['max_model_len']:<6}                      │")
    print(f"  │  LLM_MAX_NUM_BATCHED_TOKENS={r['max_model_len']:<6}             │")
    print(f"  └───────────────────────────────────────────────────┘")
    print("═" * 62)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-c", "--concurrent", type=int, required=True,
                   help="Number of concurrent slots (LLM_MAX_NUM_SEQS)")
    p.add_argument("--model", choices=list(MODELS.keys()), default=None,
                   help="Model preset override (default: read LLM_MODEL from infra/.env)")
    p.add_argument("--dtype", choices=list(DTYPE_BYTES.keys()), default=None,
                   help="Weight/KV dtype override (default: read LLM_DTYPE from infra/.env, "
                        "fallback float16)")
    # Manual architecture overrides (optional)
    p.add_argument("--layers",   type=int,   help="Override: transformer layer count")
    p.add_argument("--kv-heads", type=int,   help="Override: KV attention heads")
    p.add_argument("--head-dim", type=int,   help="Override: attention head dimension")
    p.add_argument("--params-b", type=float, help="Override: parameter count in billions")
    p.add_argument("--buffer", type=float, default=0.5,
                   help="GiB to hold back as free buffer (default: 0.5 ≈ 500 MiB)")
    return p.parse_args()


def main():
    args = parse_args()

    # Resolve model preset: CLI > .env > error
    model_key = args.model
    env_path  = find_env_file()
    if model_key is None:
        if env_path is not None:
            model_key = model_preset_from_env(env_path)
        if model_key is None:
            print("error: could not determine model. Pass --model or set LLM_MODEL in infra/.env",
                  file=sys.stderr)
            sys.exit(1)

    # Resolve dtype: CLI > .env > float16
    dtype = args.dtype
    if dtype is None and env_path is not None:
        dtype = read_env_key(env_path, "LLM_DTYPE")
    if dtype not in DTYPE_BYTES:
        dtype = "float16"

    preset    = MODELS[model_key].copy()
    layers    = args.layers   or preset["layers"]
    kv_heads  = args.kv_heads or preset["kv_heads"]
    head_dim  = args.head_dim or preset["head_dim"]
    params_b  = args.params_b or preset["params_b"]
    label     = preset["label"]
    dtype_b   = DTYPE_BYTES[dtype]

    free_gib, total_gib = gpu_free_and_total_gib()

    r = calc(free_gib, total_gib, args.concurrent,
             layers, kv_heads, head_dim, params_b, dtype_b, args.buffer)

    if r["budget_gib"] <= 0:
        print(f"error: KV budget is {r['budget_gib']:.2f} GiB — model weights exceed "
              "available GPU memory.", file=sys.stderr)
        sys.exit(1)

    print_result(r, args.concurrent, label, layers, kv_heads, head_dim,
                 params_b, dtype, args.buffer)


if __name__ == "__main__":
    main()
