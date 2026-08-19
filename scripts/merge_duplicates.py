#!/usr/bin/env python3
"""
merge_duplicates.py — Merge near-duplicate atomic notes into canonical ones.

Strategy: keep the note with the longest, most specific title (highest token count).
Merge unique aliases and content from deleted variants into the canonical note.
Rewrite all backlinks across the vault to point to the canonical title.

Usage:
    python3 scripts/merge_duplicates.py --dry-run   # preview only
    python3 scripts/merge_duplicates.py             # execute
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _dedup_common import title_tokens, jaccard

VAULT = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Nexus"
CAPTURES = VAULT / "Captures"


def find_clusters(threshold: float = 0.65):
    notes = []
    for f in sorted(CAPTURES.glob("*.md")):
        title = f.stem
        tokens = title_tokens(title)
        notes.append((title, tokens, f))

    n = len(notes)
    visited = set()
    clusters = []

    for i in range(n):
        if i in visited:
            continue
        cluster = [i]
        for j in range(i + 1, n):
            if j in visited:
                continue
            if jaccard(notes[i][1], notes[j][1]) >= threshold:
                cluster.append(j)
                visited.add(j)
        if len(cluster) > 1:
            visited.add(i)
            clusters.append([notes[k] for k in cluster])

    return clusters


def pick_canonical(cluster):
    """Pick the note with the most specific title (longest token count, then alphabetical)."""
    return sorted(cluster, key=lambda x: (-len(x[1]), x[0]))[0]


def extract_aliases(content: str) -> list[str]:
    m = re.search(r'^aliases:\s*\[([^\]]*)\]', content, re.MULTILINE)
    if not m:
        return []
    raw = m.group(1)
    return [a.strip().strip('"\'') for a in raw.split(',') if a.strip()]


def extract_body_paragraph(content: str) -> str:
    """The synthesized claim paragraph — the first non-empty line after the H1."""
    body = re.sub(r'^---.*?---\n', '', content, flags=re.DOTALL)
    lines = [l for l in body.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("# "):
            for follow in lines[i + 1:]:
                if not follow.startswith("**Keywords**"):
                    return follow.strip()
    return ""


def merge_and_delete(canonical, variants, dry_run: bool):
    canon_title, canon_tokens, canon_path = canonical

    # Read canonical content
    canon_content = canon_path.read_text(encoding="utf-8")
    canon_aliases = set(extract_aliases(canon_content))
    canon_body = extract_body_paragraph(canon_content)
    extra_aliases = set()
    preserved_claims = []  # (title, paragraph) for variants saying something distinct

    for var_title, _, var_path in variants:
        if var_path == canon_path:
            continue
        var_content = var_path.read_text(encoding="utf-8")
        # Collect aliases from variant
        for alias in extract_aliases(var_content):
            if alias.lower() != canon_title.lower():
                extra_aliases.add(alias)
        # Add variant title itself as alias if meaningfully different
        extra_aliases.add(var_title)

        # Content is only safe to discard if it's essentially restating the
        # canonical paragraph. Word-overlap below the threshold means the
        # variant carries a distinct fact (e.g. a specific number or detail)
        # that a title/alias merge alone would silently lose.
        var_body = extract_body_paragraph(var_content)
        if var_body:
            canon_words = set(re.findall(r"\w+", canon_body.lower()))
            var_words = set(re.findall(r"\w+", var_body.lower()))
            overlap = len(canon_words & var_words) / max(1, len(var_words))
            if overlap < 0.6:
                preserved_claims.append((var_title, var_body))

    new_aliases = sorted((set(canon_aliases) | extra_aliases) - {canon_title})

    print(f"\n→ CANONICAL: {canon_title}")
    for var_title, _, var_path in variants:
        if var_path != canon_path:
            print(f"  DELETE: {var_title}")
    new_set = set(new_aliases)
    canon_set = set(canon_aliases)
    if new_set - canon_set:
        print(f"  NEW ALIASES: {sorted(new_set - canon_set)}")
    if preserved_claims:
        print(f"  PRESERVING {len(preserved_claims)} distinct claim(s) from deleted variant(s)")

    if dry_run:
        return 0

    # Update aliases in canonical note
    if new_aliases:
        alias_str = ", ".join(f'"{a}"' if " " in a else a for a in sorted(new_aliases))
        canon_content = re.sub(
            r'^aliases:\s*\[[^\]]*\]',
            f'aliases: [{alias_str}]',
            canon_content,
            flags=re.MULTILINE,
        )

    # Append any variant claims that weren't just a restatement of the canonical
    # paragraph, so merging never silently discards a distinct fact.
    if preserved_claims:
        section = "\n## Also Noted In Merged Duplicates\n"
        for title, para in preserved_claims:
            section += f"- **{title}**: {para}\n"
        canon_content = canon_content.rstrip("\n") + "\n" + section

    canon_path.write_text(canon_content, encoding="utf-8")

    # Rewrite all backlinks across the vault
    redirected = 0
    for var_title, _, var_path in variants:
        if var_path == canon_path:
            continue
        for md in list(CAPTURES.glob("*.md")) + list((VAULT / "Sources").glob("*.md")) + list((VAULT / "MOCs").glob("*.md")):
            if md == var_path:
                continue
            txt = md.read_text(encoding="utf-8")
            if f"[[{var_title}]]" in txt:
                txt = txt.replace(f"[[{var_title}]]", f"[[{canon_title}]]")
                md.write_text(txt, encoding="utf-8")
                redirected += 1
        var_path.unlink()

    return redirected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    clusters = find_clusters(args.threshold)
    print(f"Found {len(clusters)} clusters ({sum(len(c) for c in clusters)} notes)")

    if args.dry_run:
        print("DRY RUN — no changes will be made\n")

    total_deleted = 0
    total_redirected = 0

    for cluster in clusters:
        canonical = pick_canonical(cluster)
        redirected = merge_and_delete(canonical, cluster, args.dry_run)
        if not args.dry_run:
            deleted = len(cluster) - 1
            total_deleted += deleted
            total_redirected += redirected

    if not args.dry_run:
        print(f"\nDone. Deleted {total_deleted} duplicate notes, rewrote {total_redirected} backlinks.")
    else:
        print(f"\nDry run complete. Would delete {sum(len(c)-1 for c in clusters)} notes.")


if __name__ == "__main__":
    main()
