#!/usr/bin/env python
"""
tools/test_new_features.py — Integration smoke test for Phase 1b + Tier 1 + Phase 2

Tests (in order):
  1. Transcript quality scorer  — pure Python, no I/O
  2. pymupdf4llm PDF extraction — local synthetic PDF, no network
  3. EmbedStore.get_all_with_embeddings — real Ollama embeddings
  4. Dreamer session (dry-run)  — populated temp store, no vault writes

Run: python tools/test_new_features.py
"""
import asyncio
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
_failures = []


def check(label: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}{': ' + detail if detail else ''}")
        _failures.append(label)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Transcript Quality Scorer
# ─────────────────────────────────────────────────────────────────────────────

def test_transcript_quality():
    print("\n── 1. Transcript Quality Scorer ─────────────────────────────")
    from extractors import _score_transcript

    clean = (
        "The transformer architecture relies on self-attention, enabling parallel "
        "computation over entire sequences. Positional encodings preserve order "
        "information. Multi-head attention captures diverse semantic relationships. "
        "Layer normalization stabilizes training by normalizing activations. "
        "Residual connections enable gradient flow through deep networks. "
        "Feed-forward layers provide non-linear transformations after attention. "
        "The encoder maps tokens to continuous representations. The decoder "
        "generates outputs autoregressively using cross-attention over the encoder. "
        "Dropout prevents overfitting in smaller datasets and improves generalisation. "
        "Scaled dot-product attention divides by the square root of key dimension."
    )
    r = _score_transcript(clean)
    check("Varied prose → is_poor_quality=False",
          not r["is_poor_quality"],
          f"got {r}")
    check("TTR reasonable (>0.4)",
          r["ttr"] > 0.40,
          f"ttr={r['ttr']:.3f}")

    bad = (
        "yeah so basically you know like the thing is like uh the model just "
        "like learns and like yeah the training you know and like basically it "
        "converges right yeah basically so like the loss goes down and yeah yeah "
        "yeah right so and then basically yeah the learning rate yeah yeah yeah "
        "the gradient you know the gradient yeah the gradient yeah basically "
    ) * 4
    r = _score_transcript(bad)
    check("Auto-caption garbage → is_poor_quality=True",
          r["is_poor_quality"],
          f"got {r}")
    check("Zero punctuation (punct_density < 0.02)",
          r["punctuation_density"] < 0.02,
          f"punct={r['punctuation_density']:.4f}")

    r_short = _score_transcript("hello world")
    check("Short text (<50 words) → always passes",
          not r_short["is_poor_quality"])


# ─────────────────────────────────────────────────────────────────────────────
# 2. pymupdf4llm PDF Extraction (Tier 1)
# ─────────────────────────────────────────────────────────────────────────────

def test_pdf_extraction():
    print("\n── 2. pymupdf4llm PDF Extraction (Tier 1) ───────────────────")
    import fitz
    import pymupdf4llm

    # Build a minimal synthetic PDF with real structure
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = f.name

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72),  "Attention Is All You Need",  fontsize=18)
    page.insert_text((50, 110), "Abstract",                    fontsize=14)
    page.insert_text(
        (50, 135),
        "We propose the Transformer, a model architecture based solely on\n"
        "attention mechanisms, dispensing with recurrence and convolutions\n"
        "entirely. Experiments on machine translation benchmarks show these\n"
        "models to be superior in quality while being more parallelisable.",
        fontsize=11,
    )
    page.insert_text((50, 240), "1  Introduction",             fontsize=14)
    page.insert_text(
        (50, 265),
        "Recurrent models factor computation along the sequence of positions.\n"
        "This inherently sequential nature precludes parallelisation within\n"
        "training examples, which becomes critical at longer sequence lengths.",
        fontsize=11,
    )
    doc.save(pdf_path)
    doc.close()

    md = pymupdf4llm.to_markdown(
        pdf_path, pages=[0], write_images=False, embed_images=False
    )

    check("pymupdf4llm returns non-empty markdown",
          bool(md) and len(md) > 50,
          f"len={len(md)}")
    check("Markdown contains title text",
          "Attention Is All You Need" in md or "Attention" in md)
    check("Markdown contains body text",
          "attention" in md.lower())
    check("No excess whitespace collapse (table-safe)",
          "\n\n\n\n" not in md)

    Path(pdf_path).unlink(missing_ok=True)

    # Whitespace normalisation path (plain-text fallback guard)
    import re
    plain = "Hello  world\n\n\n\nNext paragraph"
    normalised = re.sub(r"\n{3,}", "\n\n", plain)
    normalised = re.sub(r"[ \t]+", " ", normalised)
    check("Plain-text fallback: excess newlines collapsed",
          "\n\n\n" not in normalised)
    check("Plain-text fallback: excess spaces collapsed",
          "  " not in normalised)


# ─────────────────────────────────────────────────────────────────────────────
# 3. EmbedStore.get_all_with_embeddings
# ─────────────────────────────────────────────────────────────────────────────

async def test_embed_store():
    print("\n── 3. EmbedStore.get_all_with_embeddings (Ollama) ───────────")
    from embed_store import EmbedStore, EMBED_DIM

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    store = EmbedStore(db_path=db_path)

    sample_notes = [
        {
            "title": "Attention Mechanism Scales Quadratically",
            "tags": ["ai", "transformers"],
            "aliases": [],
            "note_type": "atomic",
            "content": "Self-attention has O(n^2) complexity with sequence length.",
        },
        {
            "title": "Flash Attention Reduces Memory via Tiling",
            "tags": ["ai", "transformers", "hardware"],
            "aliases": [],
            "note_type": "atomic",
            "content": "IO-aware tiling reduces HBM reads/writes, enabling longer contexts.",
        },
        {
            "title": "Byzantine Fault Tolerance in Distributed Systems",
            "tags": ["distributed", "consensus"],
            "aliases": [],
            "note_type": "atomic",
            "content": "Consensus requires 3f+1 nodes to tolerate f Byzantine failures.",
        },
    ]

    print("  [embedding 3 notes via Ollama qwen3-embedding:4b…]")
    n = await store.upsert_notes(sample_notes)
    check(f"Upserted {n} notes (expected 3)", n == 3)

    all_with_emb = store.get_all_with_embeddings()
    check(f"get_all_with_embeddings returns {len(all_with_emb)} rows",
          len(all_with_emb) == 3)
    check("Each row has 'title', 'tags', 'embedding' keys",
          all({"title", "tags", "embedding"}.issubset(r.keys())
              for r in all_with_emb))
    check(f"Embedding vectors are {EMBED_DIM}-dim",
          all(len(r["embedding"]) == EMBED_DIM for r in all_with_emb))
    check("Vectors are L2-normalised (|v| ≈ 1.0)",
          all(abs(math.sqrt(sum(x*x for x in r["embedding"])) - 1.0) < 1e-4
              for r in all_with_emb))

    store.close()
    db_path.unlink(missing_ok=True)
    return all_with_emb  # pass to dreamer test


# ─────────────────────────────────────────────────────────────────────────────
# 4. Dreamer Session (dry-run, no vault writes)
# ─────────────────────────────────────────────────────────────────────────────

async def test_dreamer(embedded_notes: list):
    print("\n── 4. Dreamer Session (dry-run, temp vault) ─────────────────")
    from dreamer import (
        _find_latent_links,
        _find_orphans,
        _detect_moc_gaps,
        _append_latent_link,
    )

    # Simulate vault_links: no existing connections
    existing = {n["title"]: set() for n in embedded_notes}

    pairs = _find_latent_links(embedded_notes, existing)
    print(f"  Latent pairs (0.55–0.74): {[(p.note_a[:30], p.note_b[:30], p.similarity) for p in pairs]}")
    check("_find_latent_links returns a list", isinstance(pairs, list))

    # All notes are orphans (no vault files exist in temp dir)
    with tempfile.TemporaryDirectory() as tmp_vault:
        tmp_path = Path(tmp_vault)
        orphans = _find_orphans(embedded_notes, existing, tmp_path)
        check("_find_orphans returns list", isinstance(orphans, list))

        # Test _append_latent_link on a real temp note file
        note_file = tmp_path / "Flash Attention Reduces Memory via Tiling.md"
        note_file.write_text(
            "---\ntype: atomic\n---\n"
            "# Flash Attention Reduces Memory via Tiling\n\n"
            "IO-aware tiling reduces HBM reads.\n\n"
            "---\n*Source: [[Source - Dao 2022]]*\n",
            encoding="utf-8",
        )
        wrote = _append_latent_link(
            note_file, "Attention Mechanism Scales Quadratically"
        )
        check("_append_latent_link writes *latent* link", wrote)
        content = note_file.read_text()
        check("Link uses *latent* rel_type",
              "— *latent*" in content)
        check("## Connections section created",
              "## Connections" in content)

        # MOC gap detection
        gaps = _detect_moc_gaps(embedded_notes, tmp_path / "MOCs")
        check("_detect_moc_gaps returns list", isinstance(gaps, list))
        print(f"  MOC gaps detected: {gaps}")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("═" * 60)
    print("  Nexus Pipeline — New Features Integration Test")
    print("═" * 60)

    test_transcript_quality()
    test_pdf_extraction()

    embedded = await test_embed_store()
    await test_dreamer(embedded)

    print("\n" + "═" * 60)
    if _failures:
        print(f"  {FAIL} {len(_failures)} test(s) failed: {_failures}")
        sys.exit(1)
    else:
        print(f"  {PASS} All tests passed.")
    print("═" * 60)


if __name__ == "__main__":
    asyncio.run(main())
