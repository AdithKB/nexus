#!/usr/bin/env python3
"""
stress_test.py — Nexus v4.0 Synthesis Pipeline Stress Test

Tests three modes:
  1. THERMAL  — 5 back-to-back syntheses via Ollama, monitors thermal level throughout
  2. MLX      — verifies in-process MLX load/free lifecycle via outlines native API
  3. FALLBACK — bad MLX model path → Ollama fallback completes synthesis

Usage:
  python stress_test.py            # all tests
  python stress_test.py thermal    # thermal test only
  python stress_test.py mlx        # MLX lifecycle only
  python stress_test.py fallback   # fallback path only
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from embed_store import EmbedStore, LANCE_DIR as DB_PATH
from processor import run_crystallizer_stage
from extractors import SourceMeta
from watcher import _start_mlx_server, _stop_mlx_server

# ── Synthetic seedling fixtures ────────────────────────────────

WEB_RELAY = """
MANDATORY TECHNICAL ANCHORS (must appear in notes):
- Transformer architecture: "Attention is All You Need (2017) introduced the transformer, replacing RNNs with self-attention"
- FlashAttention: "FlashAttention-2 reduces memory from O(n²) to O(n) via tiled computation"
- KV cache: "Key-value cache stores past attention states to avoid recomputation during autoregressive decoding"

SOURCE SUMMARY:
A technical overview of transformer inference optimization covering attention mechanisms, memory bottlenecks, and production deployment patterns.

NOTES TO SYNTHESIZE:
Discuss transformer self-attention, FlashAttention memory optimization, and KV cache trade-offs in modern LLM serving.
"""

INSTAGRAM_RELAY = """
MANDATORY TECHNICAL ANCHORS (must appear in notes):
- LoRA: "Low-Rank Adaptation reduces trainable parameters by 10,000x via rank decomposition of weight matrices"
- QLoRA: "QLoRA combines 4-bit quantization with LoRA adapters enabling 65B fine-tuning on a single 48GB GPU"

SOURCE SUMMARY:
Short-form technical content explaining parameter-efficient fine-tuning methods for large language models.

NOTES TO SYNTHESIZE:
Explain LoRA and QLoRA fine-tuning approaches and their memory efficiency gains.
"""

PDF_RELAY = """
MANDATORY TECHNICAL ANCHORS (must appear in notes):
- Backpropagation: "Reverse-mode automatic differentiation computes gradients via chain rule in O(n) time"
- Vanishing gradient: "Sigmoid activations cause gradients to shrink exponentially in deep networks"
- Batch normalization: "BatchNorm normalizes layer activations to zero mean and unit variance, stabilizing training"
- Dropout: "Dropout randomly zeroes activations with probability p during training, acting as ensemble regularization"
- Adam optimizer: "Adam combines momentum and RMSprop, using first and second moment estimates of gradients"

SOURCE SUMMARY:
Dense academic PDF covering deep learning training dynamics, optimization theory, and regularization techniques.

NOTES TO SYNTHESIZE:
Cover backpropagation mathematics, gradient flow problems, normalization strategies, and modern optimizers.
"""


def make_seedling(job_id: str, url: str, source_type: str, title: str,
                  duration_sec: int, relay_description: str) -> dict:
    return {
        "version": "4.0",
        "job_id": job_id,
        "url": url,
        "user_intent": "stress test",
        "timestamp": time.time(),
        "meta": {
            "title": title,
            "author": "Stress Test",
            "source_type": source_type,
            "duration_sec": duration_sec,
            "transcript": relay_description,  # Provide content for grounding
            "source_id": job_id,
        },
        "relay_description": relay_description,
    }


FIXTURES = [
    make_seedling("st_web_01", "https://example.com/transformers", "web", "Transformer Inference Optimization", 0, WEB_RELAY),
    make_seedling("st_web_02", "https://example.com/attention", "web", "Attention Mechanism Deep Dive", 0, WEB_RELAY),
    make_seedling("st_ig_01", "https://instagram.com/reel/test1", "instagram", "LoRA Fine-Tuning Explained", 90, INSTAGRAM_RELAY),
    make_seedling("st_ig_02", "https://instagram.com/reel/test2", "instagram", "QLoRA Memory Efficiency", 75, INSTAGRAM_RELAY),
    make_seedling("st_pdf_01", "https://example.com/dl_theory.pdf", "pdf", "Deep Learning Training Dynamics", 0, PDF_RELAY),
]


# ── Helpers ────────────────────────────────────────────────────

def thermal_level() -> int:
    try:
        r = subprocess.run(["sysctl", "-n", "machdep.cpu.thermal_level"],
                           capture_output=True, text=True, timeout=2)
        return int(r.stdout.strip())
    except Exception:
        return 0


def print_separator(label: str):
    print(f"\n{'━'*60}", flush=True)
    print(f"  {label}", flush=True)
    print('━'*60, flush=True)


async def synthesize_one(fixture: dict, cfg: Config, embed: EmbedStore) -> dict:
    """Run a single synthesis and return a result summary."""
    meta_dict = fixture["meta"]
    meta = SourceMeta(
        url=fixture["url"],
        source_type=meta_dict["source_type"],
        title=meta_dict["title"],
        author=meta_dict["author"],
        duration_sec=meta_dict["duration_sec"],
        transcript=meta_dict.get("transcript", ""),
        source_id=meta_dict["source_id"],
    )

    t_before = thermal_level()
    t_start = time.monotonic()

    try:
        result = await asyncio.wait_for(
            run_crystallizer_stage(fixture["relay_description"], meta, cfg, embed),
            timeout=600,
        )
        elapsed = time.monotonic() - t_start
        t_after = thermal_level()
        return {
            "title": meta.title[:50],
            "provider": cfg.llm_provider,
            "model": cfg.ollama_model if cfg.llm_provider == "ollama" else cfg.mlx_model.split("/")[-1],
            "notes": len(result.atomic_notes) if result else 0,
            "anchors_recovered": result.anchors_recovered if result else -1,
            "elapsed_s": round(elapsed, 1),
            "thermal_before": t_before,
            "thermal_after": t_after,
            "ok": bool(result and result.atomic_notes),
        }
    except asyncio.TimeoutError:
        return {"title": meta.title[:50], "ok": False, "error": "timeout", "elapsed_s": 300}
    except Exception as e:
        return {"title": meta.title[:50], "ok": False, "error": str(e)[:80],
                "elapsed_s": round(time.monotonic() - t_start, 1)}


def print_result(r: dict):
    status = "✓" if r.get("ok") else "✗"
    if r.get("ok"):
        print(
            f"  {status} [{r['provider'].upper():6} {r['model'][:20]:20}] "
            f"{r['notes']} notes | {r['anchors_recovered']} recovered | "
            f"{r['elapsed_s']}s | thermal {r['thermal_before']}→{r['thermal_after']}",
            flush=True,
        )
    else:
        print(f"  {status} FAILED: {r.get('error', 'unknown')} ({r.get('elapsed_s', '?')}s)", flush=True)


# ── Test 1: Thermal (5 back-to-back via Ollama 9B) ─────────────

async def test_thermal():
    print_separator("TEST 1 — THERMAL: 5 back-to-back syntheses (Ollama qwen3.5:9b)")
    print("  Monitors thermal level before/after each synthesis.", flush=True)

    cfg = Config()
    cfg.llm_provider = "ollama"
    cfg.ollama_model = "qwen3.5:9b"
    embed = EmbedStore(db_path=DB_PATH)
    results = []

    for i, fixture in enumerate(FIXTURES[:5]):
        print(f"\n  [{i+1}/5] {fixture['meta']['title'][:45]}...", flush=True)
        r = await synthesize_one(fixture, cfg, embed)
        print_result(r)
        results.append(r)

        if i < 4:
            print("  Cooldown 15s...", flush=True)
            await asyncio.sleep(15)

    max_thermal = max(r.get("thermal_after", 0) for r in results)
    failures = [r for r in results if not r.get("ok")]
    print(f"\n  Summary: {len(results)-len(failures)}/5 passed | Peak thermal: {max_thermal}", flush=True)
    if max_thermal >= 2:
        print("  ⚠  Thermal level hit 2 — cooldowns need to be longer.", flush=True)
    elif max_thermal == 1:
        print("  ℹ  Thermal level hit 1 — pre-synthesis guard will add 60s delay. Expected.", flush=True)
    else:
        print("  ✓  Chip stayed cool throughout.", flush=True)
    return len(failures) == 0


# ── Test 2: MLX Lifecycle (start/stop, GPU freed) ─────────────

async def test_mlx_lifecycle():
    print_separator("TEST 2 — MLX LIFECYCLE: in-process load/free via outlines")
    mlx_model = "mlx-community/Qwen3.5-9B-MLX-4bit"

    from processor import free_mlx_model, _mlx_loaded

    print("  [1/3] MLX model should be unloaded initially...", flush=True)
    if _mlx_loaded["path"] is not None:
        print("  ℹ  Model already loaded — freeing first.", flush=True)
        free_mlx_model()

    print(f"  [2/3] Running synthesis via MLX (lazy load of {mlx_model.split('/')[-1]})...", flush=True)
    cfg = Config()
    cfg.llm_provider = "mlx"
    cfg.mlx_model = mlx_model
    embed = EmbedStore(db_path=DB_PATH)
    r = await synthesize_one(FIXTURES[0], cfg, embed)
    print_result(r)

    print("  [3/3] Freeing MLX model from GPU...", flush=True)
    free_mlx_model()
    if _mlx_loaded["path"] is not None:
        print("  ✗  Model still loaded after free!", flush=True)
        return False
    print("  ✓  Model freed. GPU memory released.", flush=True)

    return r.get("ok", False)


# ── Test 3: Fallback (MLX killed mid-synthesis) ────────────────

async def test_fallback():
    print_separator("TEST 3 — FALLBACK: MLX bad model path → Ollama takes over")

    print("  [1/2] Configuring MLX with an invalid model path (simulates load failure)...", flush=True)
    cfg = Config()
    cfg.llm_provider = "mlx"
    cfg.mlx_model = "mlx-community/DoesNotExist-9B"  # bad path → mlx_lm.load raises
    cfg.ollama_model_fast = "qwen3.5:9b"
    embed = EmbedStore(db_path=DB_PATH)

    print("  [2/2] Synthesis should detect failure and fall back to Ollama...", flush=True)
    r = await synthesize_one(FIXTURES[1], cfg, embed)
    print_result(r)

    if r.get("ok"):
        print("  ✓  Fallback worked — synthesis completed despite bad MLX path.", flush=True)
        return True
    else:
        print(f"  ✗  Synthesis failed: {r.get('error')}", flush=True)
        return False


# ── Main ───────────────────────────────────────────────────────

async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    print(f"\n{'═'*60}", flush=True)
    print(f"  Nexus v4.0 Stress Test  (mode: {mode})", flush=True)
    print(f"  Thermal level at start: {thermal_level()}", flush=True)
    print('═'*60, flush=True)

    results = {}

    if mode in ("all", "thermal"):
        results["thermal"] = await test_thermal()

    if mode in ("all", "mlx"):
        results["mlx"] = await test_mlx_lifecycle()

    if mode in ("all", "fallback"):
        results["fallback"] = await test_fallback()

    from processor import free_mlx_model
    free_mlx_model()  # ensure GPU memory is freed after all tests

    print_separator("RESULTS")
    all_pass = True
    for name, passed in results.items():
        icon = "✓" if passed else "✗"
        print(f"  {icon}  {name}", flush=True)
        if not passed:
            all_pass = False

    print(f"\n  Final thermal level: {thermal_level()}", flush=True)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
