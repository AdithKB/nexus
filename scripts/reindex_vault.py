#!/usr/bin/env python3
"""
reindex_vault.py — Backfill all vault notes into LanceDB for MCP semantic search.

Reads every Captures/ and Sources/ note, extracts frontmatter, and upserts
into the shared ~/.nexus_lancedb store that mcp_nexus.py queries.

Usage:
    python3 scripts/reindex_vault.py
"""
import asyncio
import re
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from embed_store import EmbedStore
from config import Config

BATCH_SIZE = 32  # notes per embed call


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return fm


def extract_tags(fm: dict) -> list[str]:
    raw = fm.get("tags", "")
    raw = raw.strip("[]")
    return [t.strip().strip('"') for t in raw.split(",") if t.strip()]


def extract_aliases(fm: dict) -> list[str]:
    raw = fm.get("aliases", "")
    raw = raw.strip("[]")
    return [a.strip().strip('"') for a in raw.split(",") if a.strip()]


def load_notes(vault_path: Path) -> list[dict]:
    notes = []
    for subdir in ("Captures", "Sources"):
        d = vault_path / subdir
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.stem.startswith("_"):
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
            fm = parse_frontmatter(text)
            note_type = fm.get("note_type") or fm.get("type") or "fact"
            tags = extract_tags(fm)
            aliases = extract_aliases(fm)
            # summary / body for richer embeddings
            summary = fm.get("summary", "")
            # grab first non-frontmatter paragraph as content hint
            body_match = re.search(r"^---.*?^---\s*(.*?)(?:\n#|\Z)", text, re.DOTALL | re.MULTILINE)
            body = (body_match.group(1).strip()[:300] if body_match else "")
            notes.append({
                "title":    f.stem,
                "aliases":  aliases,
                "tags":     tags,
                "note_type": note_type,
                "summary":  summary or body,
            })
    return notes


async def main():
    config = Config()
    store = EmbedStore()
    vault_path = config.vault_path

    print(f"Vault: {vault_path}")
    notes = load_notes(vault_path)
    print(f"Found {len(notes)} notes to index")

    total = 0
    for i in range(0, len(notes), BATCH_SIZE):
        batch = notes[i : i + BATCH_SIZE]
        titles = [n["title"] for n in batch]
        print(f"  Batch {i//BATCH_SIZE + 1}: {titles[0]!r} … ({len(batch)} notes)")
        indexed = await store.upsert_notes(batch, keep_alive=300)
        total += indexed
        print(f"    → {indexed} embedded")

    print(f"\nDone. {total} notes newly embedded into LanceDB.")


if __name__ == "__main__":
    asyncio.run(main())
