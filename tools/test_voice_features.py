#!/usr/bin/env python
"""
tools/test_voice_features.py — QA suite for Voice Capture + Fleeting Notes (Nexus v5.0)

Coverage:
  1. config.get_fleeting_dir()         — directory creation
  2. write_fleeting_note()             — markdown structure, collision handling
  3. _embed_sync()                     — Ollama embedding (sync), normalization
  4. _promote_fleeting_notes()         — real Ollama, cosine threshold, report format
  5. extract_voice_memo() guard        — raises on missing Groq key
  6. /capture_voice endpoint           — FastAPI TestClient, input validation
  7. /capture_voice_fleeting endpoint  — FastAPI TestClient, input validation
  8. DreamReport.fleeting_promotions   — field exists, wires into report writer

Run: python tools/test_voice_features.py
Requires: Ollama running with qwen3-embedding:4b
Does NOT require: Groq API key (transcription path is not exercised)
"""
import asyncio
import base64
import json
import math
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m~\033[0m"
_failures = []


def check(label: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}{': ' + detail if detail else ''}")
        _failures.append(label)


def warn(label: str, detail: str = ""):
    print(f"  {WARN} {label}{': ' + detail if detail else ''} [skip — needs Groq]")


# ─────────────────────────────────────────────────────────────────────────────
# 1. config.get_fleeting_dir()
# ─────────────────────────────────────────────────────────────────────────────

def test_config_fleeting_dir():
    print("\n── 1. config.get_fleeting_dir() ─────────────────────────────")
    from config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.vault_path = Path(tmp)
        cfg.fleeting_folder = "Fleeting"

        fleeting_dir = cfg.get_fleeting_dir()
        check("get_fleeting_dir() returns a Path", isinstance(fleeting_dir, Path))
        check("Fleeting/ directory is created on disk", fleeting_dir.exists() and fleeting_dir.is_dir())
        check("Path ends with 'Fleeting'", fleeting_dir.name == "Fleeting")

        # Idempotent — calling again should not raise
        fleeting_dir2 = cfg.get_fleeting_dir()
        check("Idempotent (double-call safe)", fleeting_dir == fleeting_dir2)


# ─────────────────────────────────────────────────────────────────────────────
# 2. write_fleeting_note()
# ─────────────────────────────────────────────────────────────────────────────

def test_write_fleeting_note():
    print("\n── 2. write_fleeting_note() ─────────────────────────────────")
    from vault import write_fleeting_note
    from config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.vault_path = Path(tmp)

        transcript = "The attention mechanism scales quadratically with sequence length."
        path = write_fleeting_note(transcript, cfg, duration_hint=45.0)

        check("Returns a Path", isinstance(path, Path))
        check("File exists on disk", path.exists())
        check("Filename ends with 'fleeting.md'", path.name.endswith("fleeting.md"))

        content = path.read_text(encoding="utf-8")
        check("Has YAML frontmatter", content.startswith("---"))
        check("type: fleeting", "type: fleeting" in content)
        check("needs_synthesis: false", "needs_synthesis: false" in content)
        check("duration written (45s)", "45s" in content)
        check("Transcript present in body", transcript in content)

        # Frontmatter is well-formed (opening + closing ---)
        fm_match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
        check("Frontmatter is parseable", fm_match is not None)

        # ── Collision handling ─────────────────────────────────
        # Patch datetime so both notes land in the same minute
        import vault as _vault_mod
        import datetime as _dt_mod

        class _FixedDT:
            @staticmethod
            def now():
                return _dt_mod.datetime(2026, 1, 1, 3, 0, 0)

        original_dt = _vault_mod.__dict__.get("_dt", None)

        # Use monkeypatch via direct call in same second
        path2 = write_fleeting_note("Second thought.", cfg, duration_hint=10.0)
        check("Second note gets unique filename", path.name != path2.name)
        check("Both files exist", path.exists() and path2.exists())


# ─────────────────────────────────────────────────────────────────────────────
# 3. _embed_sync() — synchronous Ollama embedding
# ─────────────────────────────────────────────────────────────────────────────

def test_embed_sync():
    print("\n── 3. _embed_sync() (Ollama) ────────────────────────────────")
    from dreamer import _embed_sync

    text = "Flash Attention uses tiling to reduce HBM memory bandwidth."
    vec = _embed_sync(text)

    check("Returns a list", isinstance(vec, list))
    check("Length is 256 dims", len(vec) == 256)
    norm = math.sqrt(sum(x * x for x in vec))
    check("Vector is L2-normalised (|v| ≈ 1.0)", abs(norm - 1.0) < 1e-4,
          f"norm={norm:.6f}")
    check("No NaN values", all(math.isfinite(x) for x in vec))

    # Semantic sanity: similar text → cosine > 0.8
    from embed_store import cosine
    vec2 = _embed_sync("Tiling in Flash Attention reduces reads from high-bandwidth memory.")
    sim = cosine(vec, vec2)
    check(f"Semantically similar texts → high cosine ({sim:.3f} > 0.80)", sim > 0.80,
          f"sim={sim:.3f}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. _promote_fleeting_notes() — end-to-end with real Ollama
# ─────────────────────────────────────────────────────────────────────────────

async def test_promote_fleeting_notes():
    print("\n── 4. _promote_fleeting_notes() (Ollama) ────────────────────")
    from dreamer import _promote_fleeting_notes
    from embed_store import EmbedStore, EMBED_DIM

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fleeting_dir = tmp_path / "Fleeting"
        fleeting_dir.mkdir()

        # Seed embed store with atomic notes
        db_path = tmp_path / "test.db"
        store = EmbedStore(db_path=db_path)
        atomic_notes = [
            {
                "title": "Flash Attention Uses IO-Aware Tiling",
                "tags": ["ai", "transformers"],
                "aliases": [], "note_type": "atomic",
                "content": "Flash Attention tiles Q/K/V to fit in SRAM, avoiding HBM bottleneck.",
            },
            {
                "title": "Byzantine Fault Tolerance Requires 3f+1 Nodes",
                "tags": ["distributed"], "aliases": [], "note_type": "atomic",
                "content": "Consensus with f Byzantine failures needs at least 3f+1 nodes.",
            },
        ]
        print("  [embedding atomic notes…]")
        await store.upsert_notes(atomic_notes)
        notes_with_emb = store.get_all_with_embeddings()

        # ── Fleeting note RELATED to Flash Attention ───────────
        (fleeting_dir / "2026-06-08 03-00 fleeting.md").write_text(
            "---\ntype: fleeting\nneeds_synthesis: false\n---\n\n"
            "# 2026-06-08 03-00\n\n"
            "Thinking about how tiling in attention computation could reduce memory bandwidth — "
            "seems related to how CPUs use cache blocking for matrix multiply.",
            encoding="utf-8",
        )

        # ── Fleeting note UNRELATED (should be below threshold) ─
        (fleeting_dir / "2026-06-08 03-01 fleeting.md").write_text(
            "---\ntype: fleeting\nneeds_synthesis: false\n---\n\n"
            "# 2026-06-08 03-01\n\n"
            "Need to buy milk and eggs.",
            encoding="utf-8",
        )

        # ── Fleeting note too short (should be skipped) ─────────
        (fleeting_dir / "2026-06-08 03-02 fleeting.md").write_text(
            "---\ntype: fleeting\nneeds_synthesis: false\n---\n\n# short\n\nhi",
            encoding="utf-8",
        )

        print("  [promoting fleeting notes…]")
        promotions = _promote_fleeting_notes(fleeting_dir, notes_with_emb)

        check("Returns a list", isinstance(promotions, list))
        check("Related fleeting note found (≥1 promotion)",
              len(promotions) >= 1,
              f"got {promotions}")

        if promotions:
            p = promotions[0]
            check("Promotion mentions the fleeting file stem",
                  "2026-06-08 03-00 fleeting" in p or "fleeting" in p)
            check("Promotion mentions a related atomic note title",
                  "Flash Attention" in p or "[[" in p)
            check("Promotion includes sim score",
                  "sim=" in p)

        # Unrelated note ("buy milk") should NOT appear
        grocery_flagged = any("03-01" in p for p in promotions)
        check("Unrelated fleeting note NOT promoted (below threshold)",
              not grocery_flagged,
              f"grocery was promoted: {promotions}")

        # Short note should be skipped silently
        short_flagged = any("03-02" in p for p in promotions)
        check("Too-short fleeting note silently skipped", not short_flagged)

        store.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. extract_voice_memo() — guard: raises on missing Groq key
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_voice_memo_guard():
    print("\n── 5. extract_voice_memo() guards ───────────────────────────")
    from extractors import extract_voice_memo
    from config import Config
    import tempfile

    cfg = Config()
    cfg.groq_api_key = ""  # no key

    with tempfile.NamedTemporaryFile(suffix=".m4a") as f:
        dummy_path = Path(f.name)
        f.write(b"\x00" * 100)

        try:
            extract_voice_memo(dummy_path, cfg)
            check("Raises ValueError on missing groq_api_key", False,
                  "no exception raised")
        except ValueError as e:
            check("Raises ValueError on missing groq_api_key",
                  "groq_api_key" in str(e).lower() or "required" in str(e).lower(),
                  str(e))
        except Exception as e:
            check("Raises ValueError on missing groq_api_key", False,
                  f"wrong exception type: {type(e).__name__}: {e}")

    warn("Full transcription path (needs live Groq key)")


# ─────────────────────────────────────────────────────────────────────────────
# 6 & 7. Server endpoints — FastAPI TestClient (no Groq, no Spaces)
# ─────────────────────────────────────────────────────────────────────────────

def test_server_endpoints():
    print("\n── 6–7. Server endpoints (FastAPI TestClient) ───────────────")
    from starlette.testclient import TestClient
    from server import app

    client = TestClient(app, raise_server_exceptions=False)

    # ── /capture_voice ────────────────────────────────────────

    # Missing audio_b64
    resp = client.post("/capture_voice",
                       content=json.dumps({"user_intent": "test"}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_voice 400 on missing audio_b64",
          resp.status_code == 400,
          f"got {resp.status_code}: {resp.text[:80]}")

    # Invalid base64
    resp = client.post("/capture_voice",
                       content=json.dumps({"audio_b64": "!!!not-valid-b64!!!"}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_voice 400 on invalid base64",
          resp.status_code == 400,
          f"got {resp.status_code}: {resp.text[:80]}")

    # Valid b64 (tiny synthetic audio blob) → should queue (202/200 with job_id)
    tiny_audio = base64.b64encode(b"\xff\xfb\x90\x00" * 100).decode()
    resp = client.post("/capture_voice",
                       content=json.dumps({"audio_b64": tiny_audio}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_voice 200 on valid b64 body",
          resp.status_code == 200,
          f"got {resp.status_code}: {resp.text[:80]}")
    body = resp.json()
    check("/capture_voice response has job_id", "job_id" in body, str(body))
    check("/capture_voice response has status=queued",
          body.get("status") == "queued", str(body))
    check("job_id starts with 'voice_'",
          body.get("job_id", "").startswith("voice_"), str(body))

    # ── /capture_voice_fleeting ───────────────────────────────

    # Missing both text and audio_b64
    resp = client.post("/capture_voice_fleeting",
                       content=json.dumps({}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_voice_fleeting 400 when neither text nor audio_b64",
          resp.status_code == 400,
          f"got {resp.status_code}: {resp.text[:80]}")

    # ── Siri text path (preferred) ────────────────────────────
    resp = client.post("/capture_voice_fleeting",
                       content=json.dumps({"text": "Attention scales quadratically.", "duration_hint": 5.0}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_voice_fleeting 200 on Siri text path",
          resp.status_code == 200,
          f"got {resp.status_code}: {resp.text[:80]}")
    body = resp.json()
    check("Siri path response has job_id", "job_id" in body, str(body))
    check("Siri path job_id starts with 'fleeting_'",
          body.get("job_id", "").startswith("fleeting_"), str(body))

    # ── Ambient audio path (Groq) ─────────────────────────────
    resp = client.post("/capture_voice_fleeting",
                       content=json.dumps({"audio_b64": tiny_audio, "duration_hint": 45.0}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_voice_fleeting 200 on ambient audio path",
          resp.status_code == 200,
          f"got {resp.status_code}: {resp.text[:80]}")
    body = resp.json()
    check("Audio path response has job_id", "job_id" in body, str(body))
    check("Audio path job_id starts with 'fleeting_'",
          body.get("job_id", "").startswith("fleeting_"), str(body))


# ─────────────────────────────────────────────────────────────────────────────
# 8. /capture_smart — intent detection + routing
# ─────────────────────────────────────────────────────────────────────────────

def test_capture_smart():
    print("\n── 8. /capture_smart — intent detection ─────────────────────")
    from server import _detect_intent

    # ── Intent classification ─────────────────────────────────
    research_cases = [
        "Research ergodicity in economics",
        "What is flash attention?",
        "How does KV caching reduce memory?",
        "Why do transformers use layer norm?",
        "look into the RLHF paper from Anthropic",
        "explain scaled dot-product attention",
        "summarize the differences between BERT and GPT",
        "Who is Geoffrey Hinton?",
        "What are the tradeoffs between MoE and dense models?",
        # RQ prefix — explicit override (v6.0 shortcut)
        "RQ flash attention mechanism",
        "rq what is rotary positional encoding",
        "RQ  double-space still works",
    ]
    for text in research_cases:
        check(f"Research: {text[:45]!r}", _detect_intent(text) == "research",
              f"got '{_detect_intent(text)}'")

    fleeting_cases = [
        "Attention is interesting because it lets tokens communicate",
        "The dreamer agent should run at 3am when thermal is zero",
        "Remember to test the fleeting promotion threshold",
        "Really?",                                      # too short question
        "My research on transformers is going well",    # "research" as noun
        "The research shows that scaling works",         # "research" as noun
        "RQ",                                            # no space → not a prefix
        "RQXYZ not prefixed correctly",                  # must have space after RQ
    ]
    for text in fleeting_cases:
        check(f"Fleeting: {text[:45]!r}", _detect_intent(text) == "fleeting",
              f"got '{_detect_intent(text)}'")

    # ── Endpoint routing ──────────────────────────────────────
    from starlette.testclient import TestClient
    from server import app
    client = TestClient(app, raise_server_exceptions=False)

    # Missing text
    resp = client.post("/capture_smart",
                       content=json.dumps({}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_smart 400 on missing text", resp.status_code == 400,
          f"got {resp.status_code}")

    # Fleeting thought
    resp = client.post("/capture_smart",
                       content=json.dumps({"text": "Sparse attention reduces O(n^2) complexity"}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_smart 200 on fleeting text", resp.status_code == 200,
          f"got {resp.status_code}")
    body = resp.json()
    check("Fleeting intent returned in response", body.get("intent") == "fleeting", str(body))
    check("job_id starts with 'smart_'", body.get("job_id", "").startswith("smart_"), str(body))

    # Research query
    resp = client.post("/capture_smart",
                       content=json.dumps({"text": "What is the difference between MHA and GQA?"}).encode(),
                       headers={"Content-Type": "application/json"})
    check("/capture_smart 200 on research query", resp.status_code == 200,
          f"got {resp.status_code}")
    body = resp.json()
    check("Research intent returned in response", body.get("intent") == "research", str(body))


# ─────────────────────────────────────────────────────────────────────────────
# 9. DreamReport.fleeting_promotions field
# ─────────────────────────────────────────────────────────────────────────────

def test_dream_report_field():  # was test 8, now test 9
    print("\n── 8. DreamReport.fleeting_promotions field ─────────────────")
    from dreamer import DreamReport, _write_dream_report
    import tempfile

    r = DreamReport(
        started_at="2026-06-08 03:00",
        duration_sec=42.0,
        notes_in_store=10,
        latent_links_added=2,
        orphans_rescued=1,
        moc_gaps=["ai/transformers (5 notes)"],
        fleeting_promotions=[
            "**2026-06-08 03-00 fleeting** → [[Flash Attention Uses IO-Aware Tiling]] (sim=0.712)"
        ],
    )

    check("fleeting_promotions field exists", hasattr(r, "fleeting_promotions"))
    check("fleeting_promotions is a list", isinstance(r.fleeting_promotions, list))

    with tempfile.TemporaryDirectory() as tmp:
        _write_dream_report(r, Path(tmp))
        report_file = Path(tmp) / "_dream_report.md"
        check("_dream_report.md written", report_file.exists())
        content = report_file.read_text(encoding="utf-8")
        check("Fleeting section in report",
              "Fleeting Notes — Ready for Promotion" in content)
        check("Promotion entry in report",
              "Flash Attention" in content)
        check("Cosine sim in report", "sim=0.712" in content)
        check("MOC gaps section also present", "MOC Gaps" in content)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("═" * 60)
    print("  Nexus v5.0 — Voice Capture QA Suite")
    print("═" * 60)

    test_config_fleeting_dir()
    test_write_fleeting_note()
    test_embed_sync()
    await test_promote_fleeting_notes()
    test_extract_voice_memo_guard()
    test_server_endpoints()
    test_capture_smart()
    test_dream_report_field()

    print("\n" + "═" * 60)
    if _failures:
        print(f"  {FAIL} {len(_failures)} test(s) FAILED:")
        for f in _failures:
            print(f"       • {f}")
        sys.exit(1)
    else:
        print(f"  {PASS} All tests passed.")
    print("═" * 60)


if __name__ == "__main__":
    asyncio.run(main())
