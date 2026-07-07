"""
Vault Writer: writes structured notes to Obsidian vault.
Author: Adith K B

V2 UPGRADES:
  1. URL-based deduplication (scans existing source frontmatter)
  2. Robust slug/sanitization for all OS-safe filenames
  3. Aliases in YAML frontmatter for Obsidian native linking
  4. Source summary preserved in literature note
"""
import re
import hashlib
import math
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import Config
from extractors import SourceMeta, _compute_source_id
from processor import ProcessingResult, AtomicNote, KnowledgeGraph


def _git_snapshot(message: str, vault_path: Path):
    """
    Execute a git commit to create a version-controlled snapshot.
    Supports detached git directories to prevent iCloud sync conflicts.
    """
    try:
        # Check for standard .git OR detached local history folder
        local_git_dir = Path.home() / ".nexus_vault_history"
        
        cmd_base = ["git"]
        if local_git_dir.exists():
            # Use the local-only metadata directory
            cmd_base = ["git", f"--git-dir={local_git_dir}", f"--work-tree={vault_path}"]
        elif not (vault_path / ".git").exists():
            return

        # 1. Add all changes (quietly)
        subprocess.run(cmd_base + ["add", "."], cwd=vault_path, capture_output=True)
        
        # 2. Check if there's anything to commit
        status = subprocess.run(cmd_base + ["status", "--porcelain"], cwd=vault_path, capture_output=True, text=True)
        if not status.stdout.strip():
            return

        # 3. Commit (quietly)
        subprocess.run(cmd_base + ["commit", "-m", message], cwd=vault_path, capture_output=True)
        print(f"[GIT] Snapshot created: {message}", flush=True)
        
    except Exception as e:
        print(f"[GIT] Snapshot skipped (non-critical): {e}", flush=True)


# ── Filename Sanitization ─────────────────────────────────────


# Characters illegal on macOS, Windows, or Linux filesystems
# Also strip Obsidian reserved characters: # ^ | [ ]
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*#\^\[\]\x00-\x1f]')


def slugify(title: str) -> str:
    """
    Convert any LLM-generated title to a safe, space-separated filename.
    Preserves Title Case exactly as the LLM produced it (including acronyms like AI, RAG, LLM).
    Only strips OS-illegal and Obsidian-reserved characters.
    """
    import unicodedata
    # 1. Normalize unicode
    s = title.strip()
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")

    # 2. Clean OS-illegal and Obsidian-reserved characters (preserve original casing)
    s = s.replace("/", " ")
    s = _ILLEGAL_CHARS.sub("", s)

    # 3. Collapse whitespace
    s = re.sub(r"[\s_]+", " ", s)
    s = s.strip(" _.")

    # 4. Ensure first character is uppercase (safety net)
    if s:
        s = s[0].upper() + s[1:]

    if len(s) > 120:
        s = s[:120].rsplit(" ", 1)[0]
    return s.strip() or "Untitled"


# ── Deduplication ─────────────────────────────────────────────


def _scan_existing_source_ids(source_dir: Path) -> set[str]:
    """
    Scan existing Source notes for their `source_id:` hash values.
    Returns a set of 8-char hex IDs for O(1) dedup lookup.
    """
    known_ids: set[str] = set()
    if not source_dir.exists():
        return known_ids

    for md_file in source_dir.glob("*.md"):
        try:
            # Only read frontmatter (first ~20 lines)
            lines = md_file.read_text(encoding="utf-8").splitlines()[:20]
            in_frontmatter = False
            for line in lines:
                if line.strip() == "---":
                    if in_frontmatter:
                        break  # end of frontmatter
                    in_frontmatter = True
                    continue
                if in_frontmatter and line.startswith("source_id:"):
                    sid = line.split("source_id:", 1)[1].strip().strip('"').strip("'")
                    if sid:
                        known_ids.add(sid)
        except Exception:
            continue

    return known_ids


def check_duplicate(url: str, config: Config) -> bool:
    """
    Check if a source has already been captured by matching its source_id hash.
    Returns True if duplicate found.
    """
    if not config.dedup_enabled:
        return False
    source_id = _compute_source_id(url)
    source_dir = config.get_source_dir()
    known_ids = _scan_existing_source_ids(source_dir)
    return source_id in known_ids


# ── Frontmatter Formatting ───────────────────────────────────


def _yaml_list(items: list[str]) -> str:
    """Format list for YAML frontmatter: [item1, item2, item3]"""
    escaped = [item.replace('"', '\\"') for item in items]
    return "[" + ", ".join(escaped) + "]"


def _format_atomic_note(
    note: AtomicNote,
    source_url: str,
    source_title: str,
    source_id: str,
    default_tags: list[str],
    batch_titles: list[str] | None = None,
    vault_titles: set[str] | None = None,
    llm_model: str = "",
    moc_topics: list[str] | None = None,
) -> str:
    """
    Format an atomic note with three tiers of linking:
      1. sibling_links  — other notes in this batch (guaranteed to resolve)
      2. vault_links    — LLM-identified links to existing vault notes (verified against vault_titles)
      3. forward_links  — batch-internal canonical_links stored in frontmatter (speculative stubs)

    Output:
      ---
      aliases: [Multi-Word Alias, Another Compound Term, ...]
      tags: [ai/transformers, engineering, capture, auto-generated]
      source: "https://..."
      date: 2026-03-21
      needs_review: false
      forward_links: [Batch-Internal-Link]
      ---
      # Title Expressed As Declarative Sentence

      Content...

      ## Connections
      - [[Sibling-Note-From-This-Batch]]
      - [[Existing-Vault-Note-LLM-Identified]]

      ---
      *Source: [[Source - Video Title]]*
    """
    now = datetime.now().strftime("%Y-%m-%d")
    # Semantic tags only — strip pipeline-internal tags to reduce noise for LLM retrieval
    semantic_tags = [t for t in note.tags if not t.startswith("model/")
                     and t not in ("capture", "auto-generated")]
    all_tags = list(dict.fromkeys(semantic_tags))
    display_title = note.title  # titles now use natural spaces from the LLM
    source_slug = slugify(source_title)

    # forward_links: batch-internal canonical_links — slugify to match actual filenames
    slugged_links = [slugify(l) for l in note.canonical_links] if note.canonical_links else []
    forward_links_yaml = _yaml_list(slugged_links) if slugged_links else "[]"
    moc_yaml = _yaml_list(moc_topics) if moc_topics else "[]"

    # One-sentence summary from first sentence of content (for fast frontmatter RAG reads)
    first_sentence = (note.content or "").split(".")[0].strip()
    summary_line = f'summary: "{first_sentence}."' if first_sentence else ""

    source_quote = getattr(note, "source_quote", "") or ""

    # Keywords block: aliases + tag leaf-words for synonym matching in grep-based retrieval
    kw_terms = list(note.aliases or [])
    kw_terms += [t.split("/")[-1].replace("-", " ") for t in (note.tags or [])
                 if "/" in t and t.split("/")[-1] not in kw_terms]
    keywords_line = f"**Keywords**: {', '.join(kw_terms)}" if kw_terms else ""

    frontmatter = [
        "---",
        "type: atomic",
        f"note_type: {note.note_type}",
        f"aliases: {_yaml_list(note.aliases)}",
        f"tags: {_yaml_list(all_tags)}",
        f"moc: {moc_yaml}",
        f'source: "{source_url}"',
        f"source_id: {source_id}",
        f"date: {now}",
        *([ f"needs_review: true" ] if note.needs_review else []),
        f"forward_links: {forward_links_yaml}",
        *([ summary_line ] if summary_line else []),
        *([ f"llm_model: {llm_model}" ] if llm_model else []),
        "---",
    ]
    implication = getattr(note, "implication", "") or ""
    _bad_impl = {"requires further context", "requires further context to apply", ""}
    _show_impl = implication.strip().lower() not in _bad_impl
    lines = frontmatter + [
        "",
        f"# {display_title}",
        "",
        *([ keywords_line, "" ] if keywords_line else []),
        note.content,
        *(["", f"> [!tip] Implication", f"> {implication}"] if _show_impl else []),
        *(["", f"> {source_quote}"] if source_quote else []),
    ]

    # ── Connections section ─────────────────────────────────────
    # Verified vault_links_typed (cross-capture semantic links with logic types).
    vault_link_lines = []
    
    # Handle the new typed links
    typed_links = getattr(note, "vault_links_typed", []) or []
    for vl in typed_links:
        # vl is a VaultLink instance
        vl_slug = slugify(vl.title)
        if vault_titles is None or vl_slug in vault_titles or vl.title in vault_titles:
            line = f"- [[{vl_slug}]] — *{vl.rel}*"
            if getattr(vl, "critic_logic", ""):
                line += f"\n    - *Critic Logic*: {vl.critic_logic}"
            vault_link_lines.append(line)

    # Fallback for deprecated untyped links (if any)
    untyped_links = getattr(note, "vault_links", []) or []
    for vl in untyped_links:
        vl_slug = slugify(vl)
        if vault_titles is None or vl_slug in vault_titles or vl in vault_titles:
            # Only add if not already in typed_links
            if not any(slugify(tl.title) == vl_slug for tl in typed_links):
                vault_link_lines.append(f"- [[{vl_slug}]]")

    all_connections = vault_link_lines
    if all_connections:
        lines.append("")
        lines.append("## Connections")
        lines.extend(all_connections)

    lines.extend([
        "",
        "---",
        f"*Source: [[{source_slug}]]*",
        "",
    ])
    return "\n".join(lines)


def _format_source_note(
    meta: SourceMeta,
    summary: str,
    note_titles: list[str],
    default_tags: list[str],
    kg: "KnowledgeGraph | None" = None,
    key_takeaways: list[str] | None = None,
    display_title: str = "",
) -> str:
    """Format the literature/source note, optionally including the knowledge graph."""
    now = datetime.now().strftime("%Y-%m-%d")
    h1_title = display_title or meta.title

    # Source-level aliases: original caption + content-derived title
    aliases = list(dict.fromkeys(filter(None, [meta.title, display_title if display_title != meta.title else ""])))
    if meta.author:
        aliases.append(f"{meta.author} - {h1_title}")

    tags = list(dict.fromkeys(["source", meta.source_type]))

    lines = [
        "---",
        "type: source",
        f"aliases: {_yaml_list(aliases)}",
        f"tags: {_yaml_list(tags)}",
        f'source: "{meta.url}"',
        f"source_id: {meta.source_id}",
        f"date: {now}",
        "---",
        "",
        f"# {h1_title}",
        "",
        summary,
        "",
    ]

    if key_takeaways:
        lines.append("## Key Takeaways")
        for t in key_takeaways:
            lines.append(f"- {t}")
        lines.append("")

    lines += [
        "## Source Details",
        f"- **URL**: [{meta.url}]({meta.url})",
    ]

    if meta.author:
        lines.append(f"- **Author/Channel**: {meta.author}")
    if meta.source_type == "youtube" and meta.duration_sec:
        m, s = divmod(meta.duration_sec, 60)
        lines.append(f"- **Duration**: {m}m {s}s")
    if meta.publish_date:
        lines.append(f"- **Published**: {meta.publish_date}")
    if meta.description:
        lines.append(f"- **Description**: {meta.description[:200]}")

    lines.append("")
    lines.append("## Extracted Notes")
    for title in note_titles:
        lines.append(f"- [[{slugify(title)}]]")
    lines.append("")

    # ── Knowledge Graph (merged in) ────────────────────────────
    if kg and (kg.entities or kg.relations):
        lines.append("## Knowledge Graph")
        lines.append("")
        if kg.entities:
            lines.append("### Entities")
            for entity in sorted(kg.entities):
                lines.append(f"- [[{slugify(entity)}]] *(entity)*")
            lines.append("")
        if kg.relations:
            lines.append("### Relations")
            lines.append("")
            lines.append("| Subject | Relation | Object |")
            lines.append("|---------|----------|--------|")
            for rel in kg.relations:
                if len(rel) == 3:
                    subj, verb, obj = rel
                    lines.append(f"| [[{slugify(subj)}]] | {verb} | [[{slugify(obj)}]] |")
            lines.append("")

    return "\n".join(lines)


# ── MOC Writer ────────────────────────────────────────────────


def _get_moc_emoji(topic: str) -> str:
    """Return an emoji prefix based on MOC topic keywords."""
    t = topic.lower()
    if any(w in t for w in ["ai", "machine learning", "llm", "neural"]):
        return "🤖 "
    if any(w in t for w in ["travel", "tourist", "places to visit", "bangalore"]):
        return "📍 "
    if any(w in t for w in ["guide", "tutorial", "how to", "step-by-step"]):
        return "🛠️ "
    if any(w in t for w in ["econ", "finance", "investing", "market", "risk"]):
        return "📈 "
    if any(w in t for w in ["math", "stat", "probabilit", "logic"]):
        return "🔢 "
    if any(w in t for w in ["productiv", "workflow", "system"]):
        return "⚙️ "
    return ""


_MOC_NOTES_HEADER = "## Notes"


def _write_moc(
    moc_topic: str,
    note_titles: list[str],
    source_title: str,
    moc_dir: Path,
) -> Path:
    """Create or append to a Map of Content file.

    Dual-format: Dataview block for Obsidian UI + static ## Notes section for
    CLI LLMs (claude/gemini) that cannot run Dataview queries.
    """
    moc_file = moc_dir / f"{slugify(moc_topic)}.md"
    source_slug = slugify(source_title)
    emoji = _get_moc_emoji(moc_topic)

    new_links = [f"- [[{slugify(t)}]]" for t in note_titles]
    new_links.append(f"- [[{source_slug}]]")

    if moc_file.exists():
        existing = moc_file.read_text(encoding="utf-8")
        # Ensure the static Notes section exists (upgrade legacy MOCs)
        if _MOC_NOTES_HEADER not in existing:
            existing = existing.rstrip("\n") + f"\n\n{_MOC_NOTES_HEADER}\n"
        for link in new_links:
            if link not in existing:
                existing = existing.rstrip("\n") + f"\n{link}\n"
        moc_file.write_text(existing, encoding="utf-8")
    else:
        now = datetime.now().strftime("%Y-%m-%d")
        content = (
            "---\n"
            "type: moc\n"
            f"aliases: {_yaml_list([moc_topic])}\n"
            "tags: [moc]\n"
            f"date: {now}\n"
            "---\n\n"
            f"# {emoji}{moc_topic}\n\n"
            "```dataview\n"
            "TABLE note_type, date\n"
            'FROM "Captures"\n'
            f'WHERE contains(moc, "{moc_topic}")\n'
            "SORT date DESC\n"
            "```\n\n"
            f"{_MOC_NOTES_HEADER}\n"
        ) + "\n".join(new_links) + "\n"
        moc_file.write_text(content, encoding="utf-8")

    return moc_file


# ── Public API ────────────────────────────────────────────────


# ── Knowledge Graph helpers ───────────────────────────────────


def _kg_inject_entity_links(content: str, kg: KnowledgeGraph, vault_titles: set[str]) -> str:
    """
    Scan note content for bare mentions of KG entities that exist in the vault,
    and wrap the first occurrence of each with a [[wikilink]].

    Only injects links for entities whose slugified name is in vault_titles
    (i.e. the note already exists). Skips entities already wrapped in [[ ]].
    Processes longest entity names first to avoid partial-match clobbering.
    """
    if not kg or not kg.entities or not vault_titles:
        return content

    # Sort descending by length so longer matches take priority
    sorted_entities = sorted(kg.entities, key=len, reverse=True)

    for entity in sorted_entities:
        slug = slugify(entity)
        if slug not in vault_titles:
            continue  # entity doesn't have a vault note yet

        # Build display form (entity as written by LLM, not slug)
        display = entity.strip()
        if not display or len(display) < 4:
            continue

        # Skip if already linked anywhere in content
        if f"[[{slug}]]" in content or f"[[{display}]]" in content:
            continue

        # Replace first bare occurrence (case-insensitive, word-boundary)
        pattern = re.compile(
            r"(?<!\[\[)(?<!\w)" + re.escape(display) + r"(?!\w)(?!\]\])",
            re.IGNORECASE,
        )
        new_content, n = pattern.subn(f"[[{slug}]]", content, count=1)
        if n:
            content = new_content

    return content


def _build_kg_note(kg: KnowledgeGraph, source_title: str, note_titles: list[str]) -> str:
    """
    Build a knowledge graph summary note for this capture.
    Stored in Sources/ alongside the literature note.
    Shows the entity list and relation triples as a navigable Obsidian table.
    """
    now = datetime.now().strftime("%Y-%m-%d")
    source_slug = slugify(source_title)
    lines = [
        "---",
        "type: kg",
        "tags: [knowledge-graph, auto-generated]",
        f"date: {now}",
        "---",
        "",
        f"# Knowledge Graph — {source_title}",
        "",
        f"*Extracted from [[Source - {source_slug}]]*",
        "",
        "## Entities",
    ]
    for entity in sorted(kg.entities):
        slug = slugify(entity)
        lines.append(f"- [[{slug}]] *(entity)*")

    lines += ["", "## Relations", ""]
    if kg.relations:
        lines.append("| Subject | Relation | Object |")
        lines.append("|---------|----------|--------|")
        for rel in kg.relations:
            if len(rel) == 3:
                subj, verb, obj = rel
                lines.append(
                    f"| [[{slugify(subj)}]] | {verb} | [[{slugify(obj)}]] |"
                )
    else:
        lines.append("*(no relations extracted)*")

    lines += ["", "## Notes from this capture"]
    for t in note_titles:
        lines.append(f"- [[{slugify(t)}]]")
    lines.append("")

    return "\n".join(lines)


_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def _model_suffix(config: Config) -> str:
    """Return a filename suffix like '-flash' when a non-default model is used, else ''."""
    if config.llm_provider != "gemini" or config.gemini_model == _DEFAULT_GEMINI_MODEL:
        return ""
    # e.g. gemini-2.5-flash → -flash, gemini-2.5-pro → -pro
    short = config.gemini_model.split("-")[-1]  # last segment
    return f"-{short}"


def build_vault_files(
    result: ProcessingResult,
    meta: SourceMeta,
    config: Config,
    vault_index: list[dict] | None = None,
) -> dict:
    """
    Generate vault file contents in memory without writing to disk.

    Returns:
        {
          "captures": [{"filename": str, "content": str}, ...],
          "sources":  [{"filename": str, "content": str}],
          "mocs":     [{"filename": str, "content": str}, ...],
        }

    Used by the cloud server (/capture?return_files=true) so the iOS Shortcut
    can save files directly into the iCloud Obsidian vault on the phone.
    """
    note_titles = [n.title for n in result.atomic_notes]
    kg = result.knowledge_graph

    # Build a set of known vault titles (slugified) for vault_links validation
    # and KG entity link injection. Includes both vault index AND this batch.
    vault_titles: set[str] | None = None
    if vault_index:
        vault_titles = {slugify(n["title"]) for n in vault_index if n.get("title")}
    else:
        vault_titles = set()
    # Add batch titles so KG can link within-batch too
    vault_titles.update(slugify(t) for t in note_titles)

    # Derive content title — same logic as write_to_vault so source backlinks stay consistent
    _raw_src = (getattr(result, "source_note_title", "") or "").removeprefix("Source - ").strip()
    _src_display_title = _raw_src or meta.title

    captures = []
    for note in result.atomic_notes:
        # Inject KG entity links into note content before formatting
        enriched_note = note.model_copy()
        if kg and kg.entities and vault_titles:
            enriched_note.content = _kg_inject_entity_links(
                note.content, kg, vault_titles
            )

        content = _format_atomic_note(
            enriched_note,
            source_url=meta.url,
            source_title=_src_display_title,
            source_id=meta.source_id,
            default_tags=config.default_tags,
            batch_titles=note_titles,
            vault_titles=vault_titles,
            llm_model=config.gemini_model if config.llm_provider == "gemini" else "",
            moc_topics=result.suggested_mocs,
        )
        suffix = _model_suffix(config)
        captures.append({"filename": f"{slugify(note.title)}{suffix}.md", "content": content})

    suffix = _model_suffix(config)
    _raw_src_title = getattr(result, "source_note_title", "") or ""
    _display_src_title = _raw_src_title.removeprefix("Source - ").strip() or meta.title
    sources = [{
        "filename": f"{slugify(_display_src_title)}{suffix}.md",
        "content": _format_source_note(
            meta, result.source_summary, note_titles, config.default_tags,
            kg=kg, key_takeaways=getattr(result, "key_takeaways", None),
            display_title=_display_src_title,
        ),
    }]

    mocs = [
        {
            "filename": f"{slugify(moc_topic)}{suffix}.md",
            "content": _build_moc_content(moc_topic, note_titles, meta.title),
        }
        for moc_topic in result.suggested_mocs
    ]

    return {"captures": captures, "sources": sources, "mocs": mocs}


def validate_note(note: AtomicNote) -> list[str]:
    """
    Tier 1 Audit: Check a single note for schema drift or tag violations.
    Returns a list of warning strings.
    """
    warnings = []
    
    # 1. Tag format check (must be hierarchical slash, lowercase)
    tag_pattern = re.compile(r'^[a-z0-9\-_]+(/[a-z0-9\-_]+)*$')
    for tag in note.tags:
        if not tag_pattern.match(tag):
            warnings.append(f"Tag format violation: '{tag}' (should be lowercase/hierarchical)")

    # 2. Schema check: required fields
    if not note.title:
        warnings.append("Missing required field: title")
    if not note.content:
        warnings.append("Missing required field: content")
    if not note.source_quote:
        warnings.append("Missing source_quote (required for evidence check)")

    # 3. Canonical links check (self-referential)
    if note.title in (note.canonical_links or []):
        warnings.append("Self-referential canonical link found")

    return warnings


def _write_reciprocal_link(target_title: str, source_title: str, rel: str, captures_dir: Path):
    """
    Ensure the target note has a back-link to the source with the reciprocal relationship.
    No manual intervention; ensures graph bidirectionality.
    """
    reciprocal_map = {
        "contradiction": "contradicted_by",
        "contradicted_by": "contradiction",
        "analogous_to": "analogous_to",
        "analogy": "analogy",
        "evidence_for": "evidenced_by",
        "evidenced_by": "evidence_for",
        "application": "applied_by",
        "applied_by": "application",
        "refines": "refined_by",
        "refinement": "refined_by",
        "refined_by": "refines",
        "solves_problem": "solved_by",
        "solved_by": "solves_problem"
    }
    
    back_rel = reciprocal_map.get(rel)
    if not back_rel:
        return

    target_path = captures_dir / f"{slugify(target_title)}.md"
    if not target_path.exists():
        return

    try:
        content = target_path.read_text(encoding="utf-8")
        link_line = f"- [[{source_title}]] — *{back_rel}*"
        
        # Check if link already exists (either typed or untyped)
        if f"[[{source_title}]]" in content:
            return

        if "## Connections" in content:
            # Append to existing connections
            new_content = content.replace("## Connections", f"## Connections\n{link_line}")
        else:
            # Create connections section at bottom
            new_content = content.rstrip() + f"\n\n## Connections\n{link_line}\n"
        
        target_path.write_text(new_content, encoding="utf-8")
        print(f"[RECIPROCAL] Linked [[{target_title}]] back to [[{source_title}]] ({back_rel})", flush=True)
    except Exception as e:
        print(f"[RECIPROCAL] Failed to write back-link to {target_title}: {e}", flush=True)


def write_seedling(seedling: "RelaySeedling", meta: SourceMeta, config: Config) -> Path:
    """
    Stage 1: The 'Relay' (Cloud-Side)
    Output: Captures/Seedling_[TIMESTAMP]_[SOURCE_ID].md
    Properties: status: seedling, synced: false.
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"Seedling_{timestamp}_{seedling.source_id}.md"
    captures_dir = config.get_notes_dir()
    filepath = captures_dir / filename
    
    tags = ["nexus/seedling"] + [f"entity/{slugify(e)}" for e in seedling.entities]
    
    content = f"""---
type: seedling
status: seedling
synced: false
source_id: {seedling.source_id}
source_url: {meta.url}
title: "{seedling.title}"
entities: {_yaml_list(seedling.entities)}
tags: {_yaml_list(tags)}
date: {datetime.now().strftime("%Y-%m-%d")}
---

# {seedling.title}

> [!NEXUS-RELAY]
> This is a Stage 1 Relay seedling. The Mac-side Crystallizer will process this file to extract full atomic notes and logical links.

## Metadata
- **Source**: {meta.url}
- **ID**: {seedling.source_id}
- **Relay Entities**: {", ".join(seedling.entities)}

## Raw Context
{meta.transcript[:5000]}
"""
    filepath.write_text(content, encoding="utf-8")
    print(f"[RELAY] Seedling created: {filename}", flush=True)
    return filepath


def write_to_vault(
    result: ProcessingResult,
    meta: SourceMeta,
    config: Config,
    vault_index: list[dict] | None = None,
) -> list[Path]:
    """
    Write all notes to the Obsidian vault.
    Returns list of created/updated file paths.
    """
    created = []
    captures_dir = config.get_notes_dir()
    sources_dir = config.get_source_dir()
    moc_dir = config.get_moc_dir()

    note_titles = [n.title for n in result.atomic_notes]
    kg = result.knowledge_graph

    # Derive content-based title upfront so atomic note backlinks and source filename agree
    _raw_source_title = (getattr(result, "source_note_title", "") or "").removeprefix("Source - ").strip()
    display_title = _raw_source_title or meta.title

    # Build vault_titles set (index + this batch) for validation and KG injection
    vault_titles: set[str] = set()
    if vault_index:
        vault_titles = {slugify(n["title"]) for n in vault_index if n.get("title")}
    vault_titles.update(slugify(t) for t in note_titles)

    # ── Pillar 4: Pre-write snapshot ──────────────────────────
    _git_snapshot(f"Auto-capture start: {meta.title}", config.vault_path)

    # ── Write atomic notes ────────────────────────────────────
    for note in result.atomic_notes:
        # Per-note Tier 1 Audit
        warnings = validate_note(note)
        for w in warnings:
            print(f"[AUDIT] [WARN] Note '{note.title[:40]}...': {w}", flush=True)

        enriched_note = note.model_copy()
        if kg and kg.entities:
            enriched_note.content = _kg_inject_entity_links(
                note.content, kg, vault_titles
            )
        content = _format_atomic_note(
            enriched_note,
            source_url=meta.url,
            source_title=display_title,
            source_id=meta.source_id,
            default_tags=config.default_tags,
            batch_titles=note_titles,
            vault_titles=vault_titles,
            llm_model=config.gemini_model if config.llm_provider == "gemini" else "",
            moc_topics=result.suggested_mocs,
        )
        suffix = _model_suffix(config)
        filepath = captures_dir / f"{slugify(note.title)}{suffix}.md"
        filepath.write_text(content, encoding="utf-8")
        created.append(filepath)

        # ── Automated Reciprocity ─────────────────────────────
        # For every link this note made to an EXISTING note, 
        # go to that target note and write a back-link.
        for vl in note.vault_links_typed:
            _write_reciprocal_link(vl.title, note.title, vl.rel, captures_dir)

    # ── Write source/literature note (with KG merged in) ──────
    source_content = _format_source_note(
        meta, result.source_summary, note_titles, config.default_tags,
        kg=kg, key_takeaways=getattr(result, "key_takeaways", None),
        display_title=display_title,
    )
    suffix = _model_suffix(config)
    source_file = sources_dir / f"{slugify(display_title)}{suffix}.md"
    source_file.write_text(source_content, encoding="utf-8")
    created.append(source_file)

    # ── Write/update MOC stubs ────────────────────────────────
    for moc_topic in result.suggested_mocs:
        moc_file = _write_moc(moc_topic, note_titles, meta.title, moc_dir)
        created.append(moc_file)

    # ── Active Entropy Detection ──────────────────────────────
    try:
        # Count edges created in THIS batch
        edges_added = 0
        for note in result.atomic_notes:
            edges_added += len(note.vault_links_typed or [])
        
        # Candidates considered is the size of the vault index provided to the LLM
        candidates_considered = len(vault_index) if vault_index else 0
        
        log_entropy_snapshot(captures_dir, edges_added, candidates_considered)
    except Exception as e:
        print(f"[STATS] Failed to log entropy snapshot: {e}", flush=True)

    # ── Pillar 4: Post-write snapshot ─────────────────────────
    _git_snapshot(f"Auto-capture complete: {meta.title}", config.vault_path)

    # ── Vault-level RAG artifacts ─────────────────────────────
    _regenerate_vault_index(config.vault_path, captures_dir)
    _ensure_query_guides(config.vault_path)

    return created


def compute_link_entropy(captures_dir: Path) -> tuple[float, dict, int, int]:
    """Scan all notes, count relationship types, return Shannon entropy, counts, total edges, and note count."""
    type_counts = Counter()
    note_count = 0
    for note_file in captures_dir.glob("*.md"):
        if note_file.name.startswith("_Index"):
            continue
        note_count += 1
        try:
            content = note_file.read_text(encoding="utf-8")
            # Parse [[Note Title]] — *relationship_type* pattern
            for match in re.finditer(r'\[\[.+?\]\] — \*(\w+)\*', content):
                type_counts[match.group(1)] += 1
        except Exception:
            continue

    total_edges = sum(type_counts.values())
    if total_edges == 0:
        return 0.0, {}, 0, note_count

    entropy = -sum((c/total_edges) * math.log2(c/total_edges) for c in type_counts.values())
    return entropy, dict(type_counts), total_edges, note_count


def log_entropy_snapshot(captures_dir: Path, added_this_capture: int = 0, candidates_considered: int = 0):
    """Compute entropy and append a detailed metrics snapshot to a rolling JSON log."""
    stats_file = captures_dir / "_link_stats.json"
    entropy, type_counts, total_edges, note_count = compute_link_entropy(captures_dir)
    
    edge_density = round(total_edges / note_count, 2) if note_count > 0 else 0.0
    silence_ratio = round(candidates_considered / added_this_capture, 2) if added_this_capture > 0 else 0.0
    
    # Type distribution with percentages
    type_dist = {}
    for t, n in type_counts.items():
        type_dist[t] = {"n": n, "pct": round((n / total_edges) * 100, 1)}

    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "vault_notes": note_count,
        "edges": {
            "total": total_edges,
            "added_this_capture": added_this_capture,
            "density": edge_density
        },
        "entropy": {
            "H": round(entropy, 3),
            "status": "Healthy" if entropy > 2.0 else "Monitoring" if entropy > 1.5 else "Drifting",
            "max_theoretical": round(math.log2(6), 3) # for our 6 types
        },
        "type_distribution": type_dist,
        "linker_efficiency": {
            "candidates_considered": candidates_considered,
            "edges_created": added_this_capture,
            "silence_ratio": silence_ratio
        },
        "alert_flags": []
    }
    
    # Compute Alert Flags
    if entropy < 1.5:
        snapshot["alert_flags"].append("entropy_drift")
    
    for t, data in type_dist.items():
        if data["pct"] > 50.0:
            snapshot["alert_flags"].append("type_monopoly")
            break
            
    if total_edges > 30 and type_counts.get("contradiction", 0) == 0:
        snapshot["alert_flags"].append("contradiction_silence")
        
    if silence_ratio > 3.0:
        snapshot["alert_flags"].append("linker_overwhelmed")
    
    try:
        existing = json.loads(stats_file.read_text(encoding="utf-8")) if stats_file.exists() else []
        
        # Density stall check (compare vs 5 snapshots ago)
        if len(existing) >= 5:
            prev = existing[-5]
            prev_density = prev.get("edges", {}).get("density", 0.0)
            if edge_density <= prev_density:
                snapshot["alert_flags"].append("density_stall")

        existing.append(snapshot)
        # Keep last 52 snapshots
        stats_file.write_text(json.dumps(existing[-52:], indent=2), encoding="utf-8")
        
        print(f"[STATS] H={snapshot['entropy']['H']} ({snapshot['entropy']['status']}) | "
              f"Density={edge_density} | Flags={snapshot['alert_flags']}", flush=True)
    except Exception as e:
        print(f"[STATS] Error updating stats file: {e}", flush=True)


def _regenerate_vault_index(vault_path: Path, captures_dir: Path):
    """
    Generate / overwrite _INDEX.md at vault root with one line per note.
    Format: Title | tags | date
    Lets Claude/Gemini answer "what do I know about X?" by reading one file
    instead of opening 400+ individual notes.
    """
    try:
        entries = []
        for f in sorted(captures_dir.glob("*.md")):
            if f.name.startswith("_"):
                continue
            try:
                text = f.read_text(encoding="utf-8")
                # Extract frontmatter fields
                tags_m = re.search(r'^tags:\s*\[([^\]]*)\]', text, re.MULTILINE)
                date_m = re.search(r'^date:\s*(\S+)', text, re.MULTILINE)
                raw_tags = tags_m.group(1) if tags_m else ""
                date_str = date_m.group(1) if date_m else ""
                title = f.stem
                # Strip pipeline-noise tags to keep index lean for LLM context
                _NOISE = {"capture", "auto-generated"}
                clean_tags = ", ".join(
                    t.strip().strip('"')
                    for t in raw_tags.split(",")
                    if t.strip().strip('"') and
                       t.strip().strip('"') not in _NOISE and
                       not t.strip().strip('"').startswith("model/")
                )
                entries.append(f"- [[{title}]] | {clean_tags} | {date_str}")
            except Exception:
                continue

        index_path = vault_path / "_INDEX.md"
        lines = [
            f"# Vault Index (auto-generated {datetime.now().strftime('%Y-%m-%d')}, {len(entries)} notes)",
            "",
            "## How to Use This Index",
            "Each line: `[[Note Title]] | tags | date`",
            "Search with: `grep -i 'keyword' Captures/*.md` or read this file to find relevant notes.",
            "",
            "## Captures (Atomic Notes)",
            "",
        ] + entries
        index_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        print(f"[INDEX] Failed to regenerate vault index: {e}", flush=True)


def _ensure_query_guides(vault_path: Path):
    """
    Write CLAUDE.md and GEMINI.md to vault root if they don't exist.
    Both CLI tools auto-read these files on startup, priming the LLM with
    vault structure so RAG queries are accurate without extra instructions.
    """
    guide = """\
# Nexus Vault — Query Guide

This vault uses the Zettelkasten method. Each file in Captures/ IS an atomic claim.

## Structure
- `Captures/` — atomic notes; the filename IS the claim (e.g. "AI Breadth Superiority Validates Specialization Necessity.md")
- `Sources/` — full source evidence with summaries and key takeaways
- `MOCs/` — topic maps grouping related notes

## How to Search
1. Check `_INDEX.md` first for a full list of notes with tags and dates
2. Use `grep -ri "keyword" Captures/` to find notes by content
3. Use `grep -ri "keyword" Sources/` to find source summaries
4. Check `aliases:` in frontmatter for synonym terms
5. Tags use slash hierarchy: `ai/transformers`, `neuroscience/bci`

## Frontmatter Fields
- `note_type`: concept | theory | mechanism | argument | model
- `aliases`: 3-5 multi-word phrases for this concept (synonyms)
- `tags`: hierarchical semantic tags
- `summary`: one-sentence claim summary
- `source`: original URL
- `needs_review: true` (only present when flagged — most notes don't have it)

## Tips
- The H1 heading IS the core claim — read it before the body
- `**Keywords**` line below H1 contains synonyms for grep discovery
- Connections section lists related notes with relationship types
- Source note in Sources/ has full context, key takeaways, and knowledge graph
"""
    for filename in ("CLAUDE.md", "GEMINI.md"):
        target = vault_path / filename
        if not target.exists():
            target.write_text(guide, encoding="utf-8")
            print(f"[VAULT] Created {filename} for CLI LLM navigation.", flush=True)


def write_fleeting_note(transcript: str, config, duration_hint: float = 0.0) -> Path:
    """
    Write a raw voice transcript to Vault/Fleeting/ without LLM synthesis.

    Zero-latency capture path: no Stage 1, no Stage 2, no schema.
    The Dreamer promotes these to Captures/ when they gain semantic gravity (≥0.65).
    """
    from datetime import datetime as _dt

    now = _dt.now()
    ts = now.strftime("%Y-%m-%d %H-%M")
    fleeting_dir = config.get_fleeting_dir()
    file_path = fleeting_dir / f"{ts} fleeting.md"

    # Handle same-minute collision
    counter = 1
    while file_path.exists():
        file_path = fleeting_dir / f"{ts} fleeting ({counter}).md"
        counter += 1

    duration_str = f"{duration_hint:.0f}s" if duration_hint > 0 else "unknown"
    content = (
        f"---\n"
        f"type: fleeting\n"
        f"date: {now.strftime('%Y-%m-%d')}\n"
        f"time: {now.strftime('%H:%M')}\n"
        f"duration: {duration_str}\n"
        f"needs_synthesis: false\n"
        f"---\n\n"
        f"# {ts}\n\n"
        f"{transcript.strip()}\n"
    )

    file_path.write_text(content, encoding="utf-8")
    print(f"[FLEETING] Captured: {file_path.name} ({len(transcript)} chars)", flush=True)
    return file_path
