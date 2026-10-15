#!/usr/bin/env python3
from __future__ import annotations
"""
vault_writer.py — Syncs the local Obsidian vault with the Nexus server.

Runs on Mac (can run with Mac asleep via power nap / scheduled wake).
Three responsibilities:
  1. Pull completed notes from Spaces and write them to the vault.
  2. Push vault index (titles, aliases, tags, MOC names) to the server
     so the LLM can reference existing notes when generating links.
  3. Retroactive linking — pull semantically similar note pairs from the
     server and write bidirectional links into vault notes.

Usage:
    python3 vault_writer.py          # runs forever, Ctrl-C to stop
    python3 vault_writer.py --once   # run once and exit
"""
import argparse
import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from config import Config

SERVER = "https://nexus.adithkb.dev"
VAULT = Config().vault_path
FOLDERS = {
    "captures": VAULT / "Captures",
    "mocs":     VAULT / "MOCs",
}
SUGGEST_FILE = VAULT / "_Suggested-Links.md"
AGENTS_FILE  = VAULT / "AGENTS.md"

INDEX_PUSH_INTERVAL = 300   # seconds between index pushes (5 min)
PULL_INTERVAL = 30          # seconds between /pending polls
RELINK_INTERVAL = 3600      # seconds between retroactive link passes (1 hr)
COST_SUMMARY_INTERVAL = 600 # seconds between _cost_summary.md refreshes (10 min)

LOG_PATH = Path.home() / ".nexus-sync.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
LOG_BACKUP_COUNT = 3


# ── AGENTS.md vocab sync ──────────────────────────────────────


def update_agents_vocab() -> None:
    """
    Refresh the <!-- BEGIN_VOCAB --> / <!-- END_VOCAB --> block in AGENTS.md
    with the current list of MOC names from MOCs/.
    """
    if not AGENTS_FILE.exists():
        return
    mocs_dir = FOLDERS["mocs"]
    moc_names = sorted(
        f.stem for f in mocs_dir.glob("*.md")
        if mocs_dir.exists() and not f.stem.startswith("_")
    )
    vocab_lines = "\n".join(f"- [[{name}]]" for name in moc_names) if moc_names else "(none yet)"
    new_block = f"<!-- BEGIN_VOCAB -->\n{vocab_lines}\n<!-- END_VOCAB -->"
    try:
        text = AGENTS_FILE.read_text(encoding="utf-8")
        updated = re.sub(
            r"<!-- BEGIN_VOCAB -->.*?<!-- END_VOCAB -->",
            new_block,
            text,
            flags=re.DOTALL,
        )
        if updated != text:
            AGENTS_FILE.write_text(updated, encoding="utf-8")
            print(f"[agents] Vocab updated: {len(moc_names)} MOC(s)", flush=True)
    except Exception as e:
        print(f"[agents] Vocab update failed: {e}", flush=True)


# ── Pre-warming signal ────────────────────────────────────────


def signal_prewarm() -> None:
    """Create a .warm file in Seedlings/ to trigger pre-warming in watcher.py."""
    warm_file = VAULT / "Seedlings" / ".warm"
    try:
        warm_file.parent.mkdir(parents=True, exist_ok=True)
        warm_file.touch()
    except Exception as e:
        print(f"[sync] Failed to signal pre-warm: {e}", flush=True)


# ── Vault index ───────────────────────────────────────────────


def _parse_frontmatter(text: str) -> dict:
    """Extract key frontmatter fields from a note."""
    result = {"title": "", "aliases": [], "tags": [], "note_type": ""}
    lines = text.splitlines()
    in_fm = False
    for line in lines[:40]:
        if line.strip() == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if not in_fm:
            continue
        matched_field = next((f for f in ("aliases", "tags") if line.startswith(f"{f}:")), None)
        if matched_field:
            raw = line.split(f"{matched_field}:", 1)[1].strip()
            # The bare-item branch must exclude quote chars — otherwise, once
            # scanning lands between two quoted items (e.g. the space after a
            # comma), it greedily swallows straight through the next item's
            # opening quote as literal content instead of yielding to the
            # quoted-item branch, embedding stray `"..."` in the parsed value.
            items = re.findall(r'"([^"]+)"|\'([^\']+)\'|([^,\[\]"\']+)', raw)
            result[matched_field] = [
                (a or b or c).strip() for a, b, c in items if (a or b or c).strip()
            ]
        elif line.startswith("note_type:"):
            result["note_type"] = line.split("note_type:", 1)[1].strip()
    return result


def build_vault_index() -> list[dict]:
    """
    Scan capture notes + existing MOC names and return a compact index.
    MOC entries have note_type="moc" so the LLM reuses existing topic names.
    """
    index = []

    captures_dir = FOLDERS["captures"]
    if captures_dir.exists():
        for md_file in sorted(captures_dir.glob("*.md")):
            if md_file.stem.startswith("_"):
                continue
            title = md_file.stem
            try:
                text = md_file.read_text(encoding="utf-8")
                fm = _parse_frontmatter(text)
            except Exception:
                fm = {"aliases": [], "tags": [], "note_type": ""}
            index.append({
                "title": title,
                "aliases": fm["aliases"],
                "tags": fm["tags"],
                "note_type": fm.get("note_type", ""),
            })

    mocs_dir = FOLDERS["mocs"]
    if mocs_dir.exists():
        for md_file in sorted(mocs_dir.glob("*.md")):
            if md_file.stem.startswith("_"):
                continue
            index.append({
                "title": md_file.stem,
                "aliases": [],
                "tags": [],
                "note_type": "moc",
            })

    return index


def push_vault_index(client: httpx.Client) -> int:
    """Push vault index + AGENTS.md to server and update local EmbedStore."""
    update_agents_vocab()
    index = build_vault_index()
    agents_md = ""
    try:
        agents_md = AGENTS_FILE.read_text(encoding="utf-8")
    except Exception:
        pass

    # ── Update Local EmbedStore ────────────────────────────────
    try:
        from embed_store import EmbedStore
        cfg = Config()
        store = EmbedStore(api_key=cfg.gemini_api_key)
        
        # We need an event loop to run the async upsert
        loop = asyncio.new_event_loop()
        n = loop.run_until_complete(store.upsert_notes(index))
        loop.close()
        if n > 0:
            print(f"[index] Local EmbedStore updated: {n} new/changed notes", flush=True)
    except Exception as e:
        print(f"[index] Local sync failed: {e}", flush=True)

    # ── Push to Remote Server ──────────────────────────────────
    try:
        resp = client.post(
            f"{SERVER}/index",
            json={"notes": index, "agents_md": agents_md},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"[index] Pushed {len(index)} notes + AGENTS.md to server", flush=True)
        return len(index)
    except Exception as e:
        print(f"[index] Remote push failed: {e}", flush=True)
        return 0


# ── Cost summary ──────────────────────────────────────────────

COST_SUMMARY_FILE = VAULT / "_cost_summary.md"


def update_cost_summary(client: httpx.Client) -> None:
    """
    Write a vault note combining the live token-based cost estimate
    (GET /costs on the relay server) with the real GCP-billed figure once the
    BigQuery billing export starts flowing (cost_tracker.py — gracefully
    None until the export is linked and the first daily cycle lands).
    Dashboard.md embeds this note with ![[_cost_summary]].
    """
    estimate = None
    try:
        resp = client.get(f"{SERVER}/costs", timeout=10)
        resp.raise_for_status()
        estimate = resp.json()
    except Exception as e:
        print(f"[cost] Estimate fetch failed: {e}", flush=True)

    real = None
    try:
        from cost_tracker import query_real_gemini_cost
        real = query_real_gemini_cost()
    except Exception as e:
        print(f"[cost] Real billing query failed: {e}", flush=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---", "type: cost_summary", f"updated: {now}", "---", "",
        "# 💰 Gemini Cost Summary", "",
    ]

    if estimate:
        lines += [
            f"**Live estimate** (instant, token-based — see `docs/architecture.md` §18a for pricing caveats) · updated {now}",
            "",
            "| Period | Cost |",
            "|---|---|",
            f"| Today | ${estimate.get('today_usd', 0):.4f} |",
            f"| Last 30 days | ${estimate.get('last_30_days_usd', estimate.get('total_usd', 0)):.4f} |",
            f"| All-time | ${estimate.get('total_usd', 0):.4f} |",
            "",
        ]
        by_model = estimate.get("by_model", {})
        if by_model:
            lines += ["**By model (all-time):**", "", "| Model | Cost | Calls |", "|---|---|---|"]
            for model, m in sorted(by_model.items(), key=lambda kv: -kv[1].get("usd", 0)):
                lines.append(f"| {model} | ${m.get('usd', 0):.4f} | {m.get('calls', 0)} |")
            lines.append("")
    else:
        lines += ["*Live estimate unavailable — relay server unreachable.*", ""]

    if real:
        lines += [
            "---", "",
            f"**Real GCP billing** (last {real['window_days']} days, reconciled as of {real['last_updated'][:16]}) — "
            "the actual billed figure, not an estimate:",
            "",
            f"**Total: {real['total']} {real['currency']}**",
            "",
        ]
        if real.get("by_service"):
            lines += ["| Service | Cost |", "|---|---|"]
            for svc, cost in real["by_service"].items():
                lines.append(f"| {svc} | {cost} {real['currency']} |")
            lines.append("")
    else:
        lines += [
            "---", "",
            "*Real GCP billing: not available yet — either the BigQuery export isn't linked in "
            "Cloud Billing Console, or the first daily export cycle hasn't landed (Google's export "
            "has no real-time guarantee and doesn't backfill). The live estimate above is current.*",
            "",
        ]

    try:
        COST_SUMMARY_FILE.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        print(f"[cost] Failed to write {COST_SUMMARY_FILE.name}: {e}", flush=True)


# ── Pending notes ─────────────────────────────────────────────

_MOC_NOTES_HEADER = "## Notes"
_MOC_LINK_RE = re.compile(r"^- \[\[.+\]\]\s*$", re.MULTILINE)


def _merge_moc_content(existing: str, incoming: str) -> str:
    """
    Merge a server-built MOC's links into an already-existing local MOC file
    instead of overwriting it. The server (vault.build_vault_files) can only
    ever build "MOC doesn't exist yet" content since it has no local vault
    access — mirrors _write_moc()'s own existing-file branch in vault.py.
    """
    if _MOC_NOTES_HEADER not in existing:
        existing = existing.rstrip("\n") + f"\n\n{_MOC_NOTES_HEADER}\n"
    for link in _MOC_LINK_RE.findall(incoming):
        link = link.strip()
        if link not in existing:
            existing = existing.rstrip("\n") + f"\n{link}\n"
    return existing


def pull_pending_files(client: httpx.Client) -> int:
    """
    Poll /pending for new files uploaded to Spaces and write them to local vault.
    Returns number of files written.
    """
    try:
        resp = client.get(f"{SERVER}/pending", timeout=30)
        resp.raise_for_status()
        files = resp.json().get("files", [])
    except Exception as e:
        print(f"[pull] /pending failed: {e}", flush=True)
        return 0

    written = 0
    for f in files:
        rel_path = f.get("path", "")
        content = f.get("content", "")
        if not rel_path or not content:
            continue

        # Research requests are not vault notes — route them to Seedlings/
        # so watcher.py can dispatch them as research_topic jobs.
        if rel_path.startswith("research_requests/"):
            filename = Path(rel_path).name  # e.g. research_request_rt_abc123.json
            dest = VAULT / "Seedlings" / filename
        else:
            dest = VAULT / rel_path

        dest.parent.mkdir(parents=True, exist_ok=True)
        if rel_path.startswith("MOCs/") and dest.exists():
            existing = dest.read_text(encoding="utf-8")
            content = _merge_moc_content(existing, content)
        dest.write_text(content, encoding="utf-8")
        print(f"[pull] Wrote {rel_path}", flush=True)
        
        # Coordination for Tier 1 Audit CLI: log new files
        try:
            # Rotate if file exceeds 5 MB
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
                    src = LOG_PATH.with_suffix(f".log.{i}")
                    dst = LOG_PATH.with_suffix(f".log.{i+1}")
                    if src.exists():
                        src.rename(dst)
                LOG_PATH.rename(LOG_PATH.with_suffix(".log.1"))
            with open(LOG_PATH, "a", encoding="utf-8") as lf:
                lf.write(f"{datetime.now().isoformat()} | {rel_path}\n")
        except Exception:
            pass

        written += 1

    return written


# ── Retroactive linking ───────────────────────────────────────


def relink_vault(client: httpx.Client) -> int:
    """
    Pull semantically related note pairs from the server and write
    bidirectional links into local vault notes.

    Pairs >= 0.85 similarity → auto-write '## Related Notes' section.
    Pairs 0.75–0.85           → write to _Suggested-Links.md for review.

    Returns number of new links written.
    """
    try:
        resp = client.get(f"{SERVER}/related-notes", timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[relink] /related-notes failed: {e}", flush=True)
        return 0

    total_notes = data.get("total_notes", 0)
    if total_notes < 10:
        return 0  # not enough notes yet to make linking meaningful

    captures_dir = FOLDERS["captures"]
    written = 0

    for title_a, title_b, sim in data.get("auto", []):
        for note_title, link_title in [(title_a, title_b), (title_b, title_a)]:
            note_file = captures_dir / f"{note_title}.md"
            if not note_file.exists():
                continue
            try:
                content = note_file.read_text(encoding="utf-8")
                link = f"[[{link_title}]]"
                if link in content:
                    continue  # already linked
                entry = f"- {link} *(sim: {sim})*"
                if "## Related Notes" in content:
                    content = content.replace(
                        "## Related Notes\n",
                        f"## Related Notes\n{entry}\n",
                    )
                else:
                    content = content.rstrip() + f"\n\n## Related Notes\n{entry}\n"
                note_file.write_text(content, encoding="utf-8")
                written += 1
            except Exception as e:
                print(f"[relink] Failed to update {note_title}: {e}", flush=True)

    suggestions = data.get("suggest", [])
    if suggestions:
        lines = [f"# Suggested Links — {time.strftime('%Y-%m-%d %H:%M')}\n"]
        for ta, tb, s in suggestions:
            lines.append(f"- [[{ta}]] ↔ [[{tb}]] *(sim: {s})*")
        SUGGEST_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[relink] {len(suggestions)} suggestions → {SUGGEST_FILE}", flush=True)

    if written:
        print(f"[relink] Wrote {written} new auto-links (≥0.85 similarity)", flush=True)
    return written


# ── Main ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    print(f"Nexus vault writer — server: {SERVER}", flush=True)
    print(f"Vault: {VAULT}", flush=True)

    with httpx.Client(timeout=httpx.Timeout(10.0, read=75.0)) as client:
        pull_pending_files(client)
        push_vault_index(client)
        update_cost_summary(client)

        if args.once:
            return

        last_push = time.time()
        last_relink = time.time()
        last_cost = time.time()
        
        print(f"[sync] Starting long-poll loop (latency: 0s)", flush=True)
        while True:
            try:
                # Long-poll: wait for server to signal that seedlings are ready
                # This replaces the fixed PULL_INTERVAL sleep.
                resp = client.get(f"{SERVER}/pending/wait", params={"timeout": 60})
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ready":
                        print(f"[sync] {datetime.now().strftime('%H:%M:%S')} — Signal received", flush=True)
                        signal_prewarm()  # START PRE-WARMING IMMEDIATELY (Mac loads model while pulling)
                        n = pull_pending_files(client)
                        if n > 0:
                            push_vault_index(client)
                            last_push = time.time()
                    # If timeout, we just loop and wait again
                else:
                    # Fallback for older servers or errors
                    time.sleep(PULL_INTERVAL)
                    pull_pending_files(client)
            except Exception as e:
                print(f"[sync] Connection error ({e}), retrying in {PULL_INTERVAL}s...", flush=True)
                time.sleep(PULL_INTERVAL)

            # Periodic tasks
            now = time.time()
            if now - last_push >= INDEX_PUSH_INTERVAL:
                push_vault_index(client)
                last_push = now
            if now - last_relink >= RELINK_INTERVAL:
                relink_vault(client)
                last_relink = now
            if now - last_cost >= COST_SUMMARY_INTERVAL:
                update_cost_summary(client)
                last_cost = now


if __name__ == "__main__":
    main()
