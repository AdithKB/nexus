#!/usr/bin/env python3
"""
dreamer.py — Autonomous Vault Maintenance Agent (Nexus v5.0)

LazyGraphRAG pattern: builds edges cheaply during thermal idle at 2–5 AM.
Never calls an LLM. Never generates text. Zero hallucination risk.

Three operations (all cosine-similarity only):
  1. find_latent_links  — pairs with sim 0.55–0.74 not yet linked
  2. rescue_orphans     — zero-referenced notes linked to top-3 semantic neighbors
  3. detect_moc_gaps    — tag clusters ≥3 notes with no MOC (report only; synthesize on demand)

Output: [[wikilinks]] appended to existing notes + _dream_report.md in vault root.
"""
import asyncio
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False


# ── Result types ──────────────────────────────────────────────


@dataclass
class LatentLink:
    note_a: str
    note_b: str
    similarity: float
    added: bool = False


@dataclass
class DreamReport:
    started_at: str = ""
    duration_sec: float = 0.0
    notes_in_store: int = 0
    latent_links_added: int = 0
    orphans_rescued: int = 0
    moc_gaps: list = field(default_factory=list)
    latent_pairs: list = field(default_factory=list)
    orphan_titles: list = field(default_factory=list)
    fleeting_promotions: list = field(default_factory=list)


# ── Entry point ───────────────────────────────────────────────


async def run_dream_session(config, embed_store) -> DreamReport:
    """Non-blocking entry point. Runs the sync dream logic in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _dream_sync, config, embed_store)


def _dream_sync(config, embed_store) -> DreamReport:
    report = DreamReport(started_at=time.strftime("%Y-%m-%d %H:%M"))
    t0 = time.time()

    captures_dir: Path = config.get_notes_dir()
    mocs_dir: Path = config.get_moc_dir()

    # 1. Load all notes with raw embedding vectors from SQLite
    notes = embed_store.get_all_with_embeddings()
    report.notes_in_store = len(notes)

    if len(notes) < 3:
        print("[DREAMER] <3 notes in store — nothing to analyse. Skipping.", flush=True)
        report.duration_sec = time.time() - t0
        return report

    print(f"[DREAMER] {len(notes)} notes loaded. Scanning vault links...", flush=True)

    # 2. Build link map from Captures/ (used for both latent dedup and orphan detection)
    vault_links = _scan_vault_links(captures_dir)

    # 3. Find latent link candidates (sim 0.55–0.74, not already linked)
    latent_candidates = _find_latent_links(notes, vault_links)
    print(f"[DREAMER] {len(latent_candidates)} latent link candidates.", flush=True)

    for pair in latent_candidates:
        a_file = captures_dir / f"{pair.note_a}.md"
        b_file = captures_dir / f"{pair.note_b}.md"
        ok_a = _append_latent_link(a_file, pair.note_b)
        ok_b = _append_latent_link(b_file, pair.note_a)
        if ok_a or ok_b:
            pair.added = True
            report.latent_links_added += 1
            report.latent_pairs.append(pair)

    # 4. Rescue orphans — notes never referenced by any Captures link
    orphans = _find_orphans(notes, vault_links, captures_dir)
    print(f"[DREAMER] {len(orphans)} orphan notes found.", flush=True)

    for orphan in orphans:
        if _rescue_orphan(orphan, notes, captures_dir):
            report.orphans_rescued += 1
            report.orphan_titles.append(orphan["title"])

    # 5. Detect MOC gaps — report only, no synthesis (LazyGraphRAG)
    report.moc_gaps = _detect_moc_gaps(notes, mocs_dir)
    if report.moc_gaps:
        print(f"[DREAMER] {len(report.moc_gaps)} MOC gap(s) detected.", flush=True)

    # 6. Promote fleeting notes — flag candidates, never synthesize
    fleeting_dir = config.vault_path / config.fleeting_folder
    report.fleeting_promotions = _promote_fleeting_notes(fleeting_dir, notes)
    if report.fleeting_promotions:
        print(f"[DREAMER] {len(report.fleeting_promotions)} fleeting note(s) ready for promotion.", flush=True)

    # 8. Write human-readable report to vault root
    report.duration_sec = time.time() - t0
    _write_dream_report(report, config.vault_path)

    return report


# ── Vault scanning ────────────────────────────────────────────


def _scan_vault_links(captures_dir: Path) -> dict:
    """
    Returns {note_title: set_of_linked_titles} for every .md in Captures/.
    Used to detect existing links before adding new ones.
    """
    links: dict = {}
    if not captures_dir.exists():
        return links
    for md_file in captures_dir.glob("*.md"):
        title = md_file.stem
        linked: set = set()
        try:
            content = md_file.read_text(encoding="utf-8")
            for m in re.finditer(r'\[\[([^\]|#]+)', content):
                linked.add(m.group(1).strip())
        except Exception:
            pass
        links[title] = linked
    return links


# ── Latent link discovery ─────────────────────────────────────


def _find_latent_links(notes: list, existing_links: dict) -> list:
    """
    Pairwise cosine similarity over all embedded notes.
    Returns pairs in [LATENT_LOW, LATENT_HIGH] not already linked, capped at MAX_PAIRS.

    The hourly vault_writer linker handles ≥0.75. The dreamer handles 0.55–0.74
    (serendipitous connections that wouldn't surface in routine linking).
    """
    LATENT_LOW  = 0.55
    LATENT_HIGH = 0.74
    MAX_PAIRS   = 30   # cap per session — avoid overwhelming the vault overnight

    if not notes:
        return []

    titles = [n["title"] for n in notes]
    vecs   = [n["embedding"] for n in notes]
    candidates = []

    if _NUMPY:
        mat = np.array(vecs, dtype=np.float32)    # (N, 256), already L2-normalised
        sim_matrix = mat @ mat.T                   # (N, N) — cosine via dot product
        rows, cols = np.where(
            (sim_matrix >= LATENT_LOW) & (sim_matrix <= LATENT_HIGH)
        )
        candidates = [
            (int(i), int(j), float(sim_matrix[i, j]))
            for i, j in zip(rows, cols) if i < j
        ]
    else:
        from embed_store import cosine as _cosine
        for i in range(len(notes)):
            for j in range(i + 1, len(notes)):
                s = _cosine(vecs[i], vecs[j])
                if LATENT_LOW <= s <= LATENT_HIGH:
                    candidates.append((i, j, s))

    candidates.sort(key=lambda x: -x[2])  # highest similarity first

    # Filter already-linked pairs; limit one new link per note per session
    seen: set = set()
    result = []
    for i, j, sim in candidates:
        if len(result) >= MAX_PAIRS:
            break
        a, b = titles[i], titles[j]
        if b in existing_links.get(a, set()) or a in existing_links.get(b, set()):
            continue
        if a in seen or b in seen:
            continue
        result.append(LatentLink(note_a=a, note_b=b, similarity=round(sim, 3)))
        seen.add(a)
        seen.add(b)

    return result


# ── Orphan rescue ─────────────────────────────────────────────


def _find_orphans(notes: list, vault_links: dict, captures_dir: Path) -> list:
    """
    Notes that are never the target of any [[link]] in Captures/.
    Only considers notes that exist as .md files on disk.
    """
    all_referenced: set = set()
    for linked_set in vault_links.values():
        all_referenced.update(linked_set)

    # One glob instead of N stat() calls
    on_disk: set = {f.stem for f in captures_dir.glob("*.md")} if captures_dir.exists() else set()

    return [
        n for n in notes
        if n["title"] not in all_referenced and n["title"] in on_disk
    ]


def _rescue_orphan(orphan: dict, all_notes: list, captures_dir: Path) -> bool:
    """
    Append links from the orphan to its top-3 most similar neighbours (sim ≥ 0.50).
    Writes on the orphan side only — pulls it back into the semantic graph.
    Returns True if at least one link was written.
    """
    orphan_title = orphan["title"]
    orphan_file  = captures_dir / f"{orphan_title}.md"
    if not orphan_file.exists():
        return False

    orphan_vec = orphan["embedding"]
    others = [n for n in all_notes if n["title"] != orphan_title]
    if not others:
        return False

    if _NUMPY:
        other_vecs = np.array([n["embedding"] for n in others], dtype=np.float32)
        q_vec = np.array(orphan_vec, dtype=np.float32)
        scores = other_vecs @ q_vec
        top_idx = np.argsort(scores)[::-1][:3]
        neighbors = [
            (others[i]["title"], float(scores[i]))
            for i in top_idx if scores[i] >= 0.50
        ]
    else:
        from embed_store import cosine as _cosine
        scored = sorted(
            [(n["title"], _cosine(orphan_vec, n["embedding"])) for n in others],
            key=lambda x: -x[1],
        )
        neighbors = [(t, s) for t, s in scored[:3] if s >= 0.50]

    written = False
    for target_title, _ in neighbors:
        if _append_latent_link(orphan_file, target_title):
            written = True

    return written


# ── MOC gap detection ─────────────────────────────────────────


def _detect_moc_gaps(notes: list, mocs_dir: Path) -> list:
    """
    Find tag sub-domains (first two levels: e.g., ai/transformers) with ≥3 notes
    but no corresponding MOC file. Reports only — no files created.
    Synthesis happens on demand via mcp_nexus.py (LazyGraphRAG principle).
    """
    MIN_FOR_MOC = 3

    tag_counts: Counter = Counter()
    for note in notes:
        for tag in note.get("tags", []):
            parts = tag.split("/")
            sub = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
            tag_counts[sub] += 1

    existing_moc_names = set()
    if mocs_dir.exists():
        for f in mocs_dir.glob("*.md"):
            existing_moc_names.add(
                f.stem.lower().replace("-", " ").replace("_", " ")
            )

    gaps = []
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        if count < MIN_FOR_MOC:
            continue
        tag_words = tag.replace("/", " ").replace("-", " ").lower()
        has_moc = any(tag_words in moc for moc in existing_moc_names)
        if not has_moc:
            gaps.append(f"{tag} ({count} notes)")

    return gaps


# ── Fleeting note promotion ───────────────────────────────────


def _embed_sync(text: str, dim: int = 256) -> list:
    """
    Synchronous Ollama embedding for use within the thread-executor context.
    Embedding is not generation — no text is produced, no LLM synthesis occurs.
    """
    import requests as _req
    resp = _req.post(
        "http://localhost:11434/api/embed",
        json={"model": "qwen3-embedding:4b", "input": [text], "keep_alive": 300},
        timeout=30,
    )
    resp.raise_for_status()
    full_vec = resp.json()["embeddings"][0][:dim]
    norm = math.sqrt(sum(x * x for x in full_vec))
    return [x / norm for x in full_vec] if norm > 0 else [0.0] * dim


def _promote_fleeting_notes(fleeting_dir: Path, notes: list) -> list:
    """
    Find fleeting notes with cosine ≥ 0.65 to existing Captures.
    Flags candidates in the dream report only — NO files are modified,
    NO synthesis is triggered. User promotes on-demand (LazyGraphRAG).
    """
    PROMOTE_THRESHOLD = 0.65

    if not fleeting_dir.exists() or not notes:
        return []

    fleeting_files = sorted(fleeting_dir.glob("*.md"))
    if not fleeting_files:
        return []

    promotions = []
    for md_file in fleeting_files:
        try:
            raw = md_file.read_text(encoding="utf-8")
            # Strip YAML frontmatter
            body = re.sub(r"^---\n.*?\n---\n", "", raw, flags=re.DOTALL).strip()
            if len(body) < 20:
                continue
        except Exception:
            continue

        try:
            vec = _embed_sync(body)
        except Exception as e:
            print(f"[DREAMER] Could not embed {md_file.name}: {e}", flush=True)
            continue

        best_title, best_sim = "", 0.0
        if _NUMPY:
            import numpy as np
            mat = np.array([n["embedding"] for n in notes], dtype="float32")
            q   = np.array(vec, dtype="float32")
            scores = mat @ q
            idx = int(scores.argmax())
            best_sim = float(scores[idx])
            best_title = notes[idx]["title"]
        else:
            from embed_store import cosine as _cosine
            for n in notes:
                s = _cosine(vec, n["embedding"])
                if s > best_sim:
                    best_sim, best_title = s, n["title"]

        if best_sim >= PROMOTE_THRESHOLD:
            promotions.append(
                f"**{md_file.stem}** → [[{best_title}]] (sim={best_sim:.3f})"
            )

    return promotions


# ── Link appender ─────────────────────────────────────────────


def _append_latent_link(note_file: Path, target_title: str) -> bool:
    """
    Append `- [[target_title]] — *latent*` to the note's ## Connections section.
    Creates the section before the source footer if absent.
    Returns True if the file was actually modified.
    """
    if not note_file.exists():
        return False

    try:
        content = note_file.read_text(encoding="utf-8")
    except Exception:
        return False

    # Skip if the link already appears in any form
    if f"[[{target_title}]]" in content:
        return False

    new_link = f"- [[{target_title}]] — *latent*"
    lines = content.split("\n")

    if "## Connections" in content:
        # Append inside the existing section, before the next ## heading
        # or before the trailing --- source footer
        insert_at = None
        in_section = False
        for idx, line in enumerate(lines):
            if line.strip() == "## Connections":
                in_section = True
                continue
            if in_section:
                if line.startswith("## ") or (
                    line.strip() == "---" and idx > len(lines) // 2
                ):
                    insert_at = idx
                    break
        if insert_at is not None:
            lines.insert(insert_at, new_link)
        else:
            lines.append(new_link)
    else:
        # No Connections section: insert a new one before the trailing --- footer
        insert_at = None
        for idx in range(len(lines) - 1, max(len(lines) - 10, 0), -1):
            if lines[idx].strip() == "---" and idx > len(lines) // 2:
                insert_at = idx
                break

        block = ["", "## Connections", new_link]
        if insert_at is not None:
            for offset, bl in enumerate(block):
                lines.insert(insert_at + offset, bl)
        else:
            lines.extend(block)

    try:
        note_file.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


# ── Dream report ──────────────────────────────────────────────


def _write_dream_report(report: DreamReport, vault_path: Path) -> None:
    """Write a human-readable _dream_report.md to the vault root."""
    lines = [
        f"# Dream Report — {report.started_at}",
        "",
        f"**Duration:** {report.duration_sec:.0f}s  ",
        f"**Notes analysed:** {report.notes_in_store}  ",
        f"**Latent links added:** {report.latent_links_added}  ",
        f"**Orphans rescued:** {report.orphans_rescued}  ",
        "",
    ]

    if report.latent_pairs:
        lines += ["## Latent Links Added", ""]
        for lp in report.latent_pairs:
            if lp.added:
                lines.append(
                    f"- [[{lp.note_a}]] ↔ [[{lp.note_b}]] (sim={lp.similarity})"
                )
        lines.append("")

    if report.orphan_titles:
        lines += ["## Orphans Rescued", ""]
        for title in report.orphan_titles:
            lines.append(f"- [[{title}]]")
        lines.append("")

    if report.moc_gaps:
        lines += [
            "## MOC Gaps — Synthesize On Demand",
            "> These clusters have ≥3 notes but no MOC. "
            "Query via `mcp_nexus graph_search` to synthesize a narrative.",
            "",
        ]
        for gap in report.moc_gaps:
            lines.append(f"- {gap}")
        lines.append("")

    if report.fleeting_promotions:
        lines += [
            "## Fleeting Notes — Ready for Promotion",
            "> Cosine ≥ 0.65 match found. Trigger synthesis on-demand: "
            "`mcp_nexus research_to_vault` or open in Obsidian and run /crystallize.",
            "",
        ]
        for p in report.fleeting_promotions:
            lines.append(f"- {p}")
        lines.append("")

    lines += [
        "---",
        "_Generated by Nexus Dreamer — no LLM calls, no hallucination risk_",
    ]

    report_file = vault_path / "_dream_report.md"
    try:
        report_file.write_text("\n".join(lines), encoding="utf-8")
        print(f"[DREAMER] Report written → {report_file.name}", flush=True)
    except Exception as e:
        print(f"[DREAMER] Could not write report: {e}", flush=True)
