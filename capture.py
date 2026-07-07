#!/usr/bin/env python3
"""
capture.py — Obsidian Capture Pipeline v2
Author: Adith K B

Usage:
    # Single URL
    python capture.py --url "https://youtube.com/watch?v=..."
    python capture.py --url "https://example.com/article" --provider groq
    python capture.py --url "./paper.pdf" --vault ~/my-vault --notes 5

    # Batch from file (one URL per line, # for comments)
    python capture.py --batch urls.txt
    python capture.py --batch urls.txt --force --provider groq

    # Webhook server (see server.py)
    python server.py

Env vars:
    GEMINI_API_KEY    Google Gemini API key (default — natively multimodal)
    GROQ_API_KEY      Groq API key (text-only fallback, must set --provider groq)
    OPENAI_API_KEY    OpenAI-compatible API key
    JINA_API_KEY      Jina Reader API key (optional, free tier works)
    OBSIDIAN_VAULT    Path to vault (default: ~/obsidian-vault)
    LLM_PROVIDER      gemini | groq | ollama | openai
"""
import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import Config
from embed_store import EmbedStore
from extractors import extract_source
from processor import process_source
from vault import write_to_vault, check_duplicate


# ── Terminal output ───────────────────────────────────────────


def _c(text: str, code: int) -> str:
    return f"\033[{code}m{text}\033[0m"

def info(msg: str):    print(f"  {_c('▸', 36)} {msg}")
def success(msg: str): print(f"  {_c('✓', 32)} {msg}")
def warn(msg: str):    print(f"  {_c('!', 33)} {msg}")
def error(msg: str):   print(f"  {_c('✗', 31)} {msg}", file=sys.stderr)

def header(msg: str):
    print(f"\n{_c('━' * 60, 90)}")
    print(f"  {_c(msg, 1)}")
    print(f"{_c('━' * 60, 90)}")


def _detect_source_type(url: str) -> str:
    lower = url.lower().strip()
    if any(x in lower for x in ["youtube.com", "youtu.be", "yt.be"]):
        return "youtube"
    elif "instagram.com" in lower:
        return "instagram"
    elif lower.endswith(".pdf") or lower.startswith("file://"):
        return "pdf"
    return "web"


# ── Pipeline result ───────────────────────────────────────────


@dataclass
class PipelineResult:
    """Returned by run_pipeline — used by CLI, batch runner, and webhook server."""
    status: str          # "captured" | "skipped" | "failed"
    url: str
    n_notes: int = 0
    n_files: int = 0
    elapsed: float = 0.0
    cost_str: str = ""
    total_tokens: int = 0
    error_msg: str = ""


# ── Main pipeline ─────────────────────────────────────────────


async def run_pipeline(
    url: str, 
    config: Config, 
    user_intent: str = "", 
    embed_store: EmbedStore | None = None
) -> PipelineResult:
    """
    Full extract → structure → write pipeline for a single URL.
    Returns a PipelineResult; never calls sys.exit() — callers decide on exit codes.
    """
    t0 = time.time()
    source_type = _detect_source_type(url)

    # ... (rest of function logic unchanged until Step 2) ...

    # ── Step 0: Dedup check ───────────────────────────────────
    header(f"PIPELINE · {source_type.upper()} → STRUCTURE → VAULT")
    info(f"URL: {url}")

    if check_duplicate(url, config):
        warn("This source has already been captured in your vault.")
        warn("Skipping to avoid duplicates. Use --force to override.")
        return PipelineResult(status="skipped", url=url, elapsed=time.time() - t0)

    # ── Step 1: Extract ───────────────────────────────────────
    header("STEP 1 · Extracting source content")
    info(f"Adapter: {source_type}")

    try:
        meta = extract_source(url, config)
    except Exception as e:
        error(f"Extraction failed: {e}")
        return PipelineResult(
            status="failed", url=url, elapsed=time.time() - t0, error_msg=str(e)
        )

    success(f"Title: {meta.title}")
    if meta.author:
        success(f"Author: {meta.author}")
    if meta.duration_sec:
        m, s = divmod(meta.duration_sec, 60)
        success(f"Duration: {m}m {s}s")
    success(f"Content: {len(meta.transcript):,} chars")
    if meta.source_type == "pdf" and meta.description:
        info(f"Pages: {meta.description}")

    # ── Step 2: Structure with LLM ────────────────────────────
    header("STEP 2 · Chain-of-Density extraction")
    model_name = {
        "gemini": config.gemini_model,
        "groq": config.groq_model,
        "openai": config.openai_model,
        "ollama": config.ollama_model,
    }.get(config.llm_provider, "unknown")
    info(f"Provider: {config.llm_provider} ({model_name})")
    info(f"Target: {config.min_notes}-{config.max_notes} atomic notes, {config.alias_count} aliases each")

    try:
        result = await process_source(
            meta, config, user_intent=user_intent, 
            embed_store=embed_store
        )
    except Exception as e:
        error(f"LLM processing failed: {e}")
        return PipelineResult(
            status="failed", url=url, elapsed=time.time() - t0, error_msg=str(e)
        )

    success(f"Generated {len(result.atomic_notes)} atomic notes")

    review_count = 0
    for note in result.atomic_notes:
        alias_preview = ", ".join(note.aliases[:3])
        more = f" (+{len(note.aliases)-3})" if len(note.aliases) > 3 else ""
        review_flag = f"  {_c('⚠ needs review', 33)}" if note.needs_review else ""
        print(f"    📝 {_c(note.title, 1)}{review_flag}")
        print(f"       aliases: {alias_preview}{more}")
        if note.canonical_links:
            links = ", ".join(f"[[{l}]]" for l in note.canonical_links)
            print(f"       links:   {links}")
        print(f"       tags:    {', '.join(note.tags[:3])}")
        if note.needs_review:
            review_count += 1

    if result.suggested_mocs:
        info(f"Suggested MOCs: {', '.join(result.suggested_mocs)}")

    # Token usage + cost
    cost_str = ""
    total_tokens = 0
    if result.token_usage:
        t = result.token_usage
        total_tokens = t.get("total_tokens", 0)
        info(
            f"Tokens: {t.get('input_tokens', '?')} in → "
            f"{t.get('output_tokens', '?')} out "
            f"({total_tokens:,} total)"
        )
        if config.llm_provider == "gemini":
            cost = (
                t.get("input_tokens", 0) * 0.15 / 1_000_000
                + t.get("output_tokens", 0) * 0.60 / 1_000_000
            )
            cost_str = f"${cost:.4f}"
            info(f"Est. cost: {cost_str}")
        elif config.llm_provider in ("groq", "ollama"):
            cost_str = "$0.00"
            info(f"Est. cost: {cost_str} ({config.llm_provider} free tier)")
        else:
            cost_str = "n/a"

    # ── Step 3: Write to vault ────────────────────────────────
    header("STEP 3 · Writing to Obsidian vault")
    info(f"Vault: {config.vault_path}")

    created_files = write_to_vault(result, meta, config)

    for f in created_files:
        try:
            rel = f.relative_to(config.vault_path)
        except ValueError:
            rel = f
        success(f"  {rel}")

    # ── Per-URL summary ───────────────────────────────────────
    elapsed = time.time() - t0
    n_notes = len(result.atomic_notes)
    n_files = len(created_files)

    token_str = f"{total_tokens:,}" if total_tokens else "n/a"
    if cost_str and cost_str not in ("n/a", "$0.00"):
        cost_display = f"~{cost_str}"
    elif cost_str == "$0.00":
        cost_display = "$0.00 (free)"
    else:
        cost_display = "n/a"

    header("DONE")
    success(f"Created {n_notes} notes. Tokens: {token_str} | Est. Cost: {cost_display}")
    if review_count:
        warn(f"{review_count} notes flagged for review")
    info(f"{n_files} files written · {elapsed:.1f}s · {config.llm_provider}/{model_name}")
    info("Open Obsidian → enable 'Unlinked Mentions' sidebar for alias-based linking")
    print()

    return PipelineResult(
        status="captured",
        url=url,
        n_notes=n_notes,
        n_files=n_files,
        elapsed=elapsed,
        cost_str=cost_str,
        total_tokens=total_tokens,
    )


# ── Config builder (shared with server.py) ────────────────────


def build_config(
    vault: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    notes: int = 5,
    force: bool = False,
) -> Config:
    """Construct a Config from CLI/webhook overrides."""
    config = Config()
    if vault:
        config.vault_path = Path(vault).expanduser()
    if provider:
        config.llm_provider = provider
    if model:
        setattr(config, f"{config.llm_provider}_model", model)
    config.max_notes = notes
    config.min_notes = max(3, notes - 2)
    if force:
        config.dedup_enabled = False
    return config


def validate_key(config: Config) -> None:
    """Print an error and sys.exit(1) if the required API key is missing."""
    key_map = {
        "gemini": ("gemini_api_key", "GEMINI_API_KEY"),
        "groq":   ("groq_api_key",   "GROQ_API_KEY"),
        "openai": ("openai_api_key", "OPENAI_API_KEY"),
        "ollama": (None, None),
    }
    attr, env = key_map.get(config.llm_provider, (None, None))
    if attr and not getattr(config, attr):
        error(f"No API key for {config.llm_provider}. Set {env} env var.")
        sys.exit(1)


# ── Vault initialiser ────────────────────────────────────────


def init_vault(vault_path: Path) -> None:
    """
    Scaffold a fresh vault:
      1. Create Captures/, Sources/, MOCs/ directories.
      2. Copy vault-template/*.md dashboard files (skips existing files).
    """
    template_dir = Path(__file__).parent / "vault-template"

    header("INITIALISING VAULT")
    info(f"Vault: {vault_path}")

    # Standard capture directories
    for folder in ("Captures", "Sources", "MOCs"):
        d = vault_path / folder
        d.mkdir(parents=True, exist_ok=True)
        success(f"  {folder}/")

    # Dashboard templates
    if not template_dir.exists():
        warn("vault-template/ not found alongside capture.py — skipping templates.")
    else:
        print()
        info("Installing dashboard templates:")
        for tmpl in sorted(template_dir.glob("*")):
            dest = vault_path / tmpl.name
            if dest.exists():
                warn(f"  {tmpl.name}  (already exists — skipped)")
            else:
                shutil.copy2(tmpl, dest)
                success(f"  {tmpl.name}")

    print()
    success("Vault ready.")
    info("Next: open in Obsidian, install the Dataview plugin, then run your first capture.")
    info("Obsidian v1.9+: .base files are also installed for Obsidian Bases dashboards.")
    print()


# ── Batch runner ──────────────────────────────────────────────


def _read_batch_file(path: Path) -> list[str]:
    """Read URLs from a batch file. Skips blank lines and # comments."""
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


async def _run_batch(batch_file: Path, config: Config, embed_store: EmbedStore | None = None) -> None:
    urls = _read_batch_file(batch_file)
    if not urls:
        error(f"No URLs found in {batch_file}")
        sys.exit(1)

    t_batch_start = time.time()
    results: list[PipelineResult] = []

    for i, url in enumerate(urls, 1):
        print(f"\n{_c('━' * 60, 90)}")
        print(f"  {_c(f'[{i}/{len(urls)}]', 90)}  {url}")
        print(f"{_c('━' * 60, 90)}")
        result = await run_pipeline(url, config, embed_store=embed_store)
        results.append(result)

    # ── Batch summary ─────────────────────────────────────────
    captured = [r for r in results if r.status == "captured"]
    skipped  = [r for r in results if r.status == "skipped"]
    failed   = [r for r in results if r.status == "failed"]
    total_elapsed = time.time() - t_batch_start
    total_notes = sum(r.n_notes for r in captured)

    header(f"BATCH COMPLETE · {len(urls)} URLs")
    success(f"Captured:  {len(captured)}  ({total_notes} notes total)")
    if skipped:
        warn(   f"Skipped:   {len(skipped)}  (already in vault)")
    if failed:
        error(  f"Failed:    {len(failed)}")
        for r in failed:
            error(f"  ✗ {r.url}  —  {r.error_msg}")
    info(f"Total time: {total_elapsed:.1f}s")
    print()

    if failed:
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Capture → Structure → Link → Obsidian Vault (v2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # --url / --batch are mutually exclusive; required unless --init-vault is used
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--url",   metavar="URL",  help="Single URL to capture")
    source.add_argument("--batch", metavar="FILE", help="File of URLs, one per line (# for comments)")

    parser.add_argument("--vault",       help="Obsidian vault path")
    parser.add_argument("--provider",    choices=["gemini", "groq", "ollama", "openai"],
                        help="LLM provider")
    parser.add_argument("--model",       help="Override model name")
    parser.add_argument("--notes",       type=int, default=5,
                        help="Max atomic notes per source (default: 5)")
    parser.add_argument("--force",       action="store_true",
                        help="Skip dedup check, force re-capture")
    parser.add_argument("--intent",      metavar="TEXT",
                        help="User intent override (e.g. 'list', 'framework', 'one note only')")
    parser.add_argument("--init-vault",  action="store_true",
                        help="Scaffold vault directories and install dashboard templates, then exit")

    args = parser.parse_args()
    config = build_config(args.vault, args.provider, args.model, args.notes, args.force)

    print(f"\n  {_c('Obsidian Capture Pipeline v2.0', 1)}")
    print(f"  {_c('YouTube · Web · PDF · Instagram → Zettelkasten Notes', 90)}")
    print(f"  {_c('Author: Adith K B', 90)}\n")

    if args.init_vault:
        init_vault(config.vault_path)
        return

    if not args.url and not args.batch:
        parser.error("one of the arguments --url / --batch is required (or use --init-vault)")

    validate_key(config)

    # Initialize EmbedStore for semantic context retrieval
    store = None
    if config.gemini_api_key:
        try:
            store = EmbedStore(api_key=config.gemini_api_key)
        except Exception:
            pass

    if args.url:
        result = asyncio.run(run_pipeline(args.url, config, user_intent=args.intent or "", embed_store=store))
        if result.status == "failed":
            sys.exit(1)
    else:
        batch_file = Path(args.batch)
        if not batch_file.exists():
            error(f"Batch file not found: {batch_file}")
            sys.exit(1)
        asyncio.run(_run_batch(batch_file, config, embed_store=store))


if __name__ == "__main__":
    main()
