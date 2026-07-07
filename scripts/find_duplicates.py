#!/usr/bin/env python3
"""
find_duplicates.py — Surface near-duplicate atomic notes in the Nexus vault.

Uses title-level n-gram overlap (no model needed) to find notes with >70%
word overlap in their titles. Groups them and prints consolidation candidates.

Usage:
    python3 scripts/find_duplicates.py [--threshold 0.70] [--output report.md]
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

VAULT = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Nexus"
CAPTURES = VAULT / "Captures"

STOPWORDS = {"a", "an", "the", "and", "or", "of", "in", "to", "for", "with",
             "on", "at", "by", "from", "is", "are", "was", "were", "be",
             "that", "this", "as", "it", "its", "not", "but", "can", "do"}


def title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9']+", title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicate_clusters(threshold: float = 0.70):
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
            sim = jaccard(notes[i][1], notes[j][1])
            if sim >= threshold:
                cluster.append(j)
                visited.add(j)
        if len(cluster) > 1:
            visited.add(i)
            clusters.append([notes[k] for k in cluster])

    return clusters


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--output", type=str, default="")
    args = parser.parse_args()

    clusters = find_duplicate_clusters(args.threshold)

    lines = [
        f"# Duplicate Detection Report",
        f"Threshold: {args.threshold:.0%} title-word overlap",
        f"Clusters found: {len(clusters)}",
        f"Total notes in clusters: {sum(len(c) for c in clusters)}",
        "",
    ]

    for i, cluster in enumerate(clusters, 1):
        lines.append(f"## Cluster {i} ({len(cluster)} notes)")
        for title, tokens, path in cluster:
            lines.append(f"- [[{title}]]")
        lines.append("")

    report = "\n".join(lines)
    print(report)

    if args.output:
        out = Path(args.output)
        out.write_text(report, encoding="utf-8")
        print(f"\nReport saved to {out}", file=sys.stderr)

    print(f"\nSummary: {len(clusters)} duplicate clusters across "
          f"{sum(len(c) for c in clusters)} notes out of {len(list(CAPTURES.glob('*.md')))} total.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
