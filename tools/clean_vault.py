#!/usr/bin/env python3
"""
clean_vault.py — Delete all notes in Captures/, MOCs/, Sources/.
Root-level files are never touched.

Usage:
    python3 clean_vault.py          # dry run (shows what would be deleted)
    python3 clean_vault.py --confirm  # actually delete
"""
import argparse
from pathlib import Path

VAULT = Path("/Users/adithkb/Library/Mobile Documents/iCloud~md~obsidian/Documents/Nexus")
CLEAN_DIRS = ["Captures", "MOCs", "Sources"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="Actually delete files")
    args = parser.parse_args()

    files = []
    for folder in CLEAN_DIRS:
        d = VAULT / folder
        if d.exists():
            files.extend(f for f in d.glob("*.md") if not f.stem.startswith("_"))

    if not files:
        print("Vault already clean.")
        return

    for f in files:
        rel = f.relative_to(VAULT)
        print(f"  {'DELETE' if args.confirm else 'WOULD DELETE'}: {rel}")

    if args.confirm:
        for f in files:
            f.unlink()
        print(f"\nDeleted {len(files)} file(s).")
    else:
        print(f"\n{len(files)} file(s) would be deleted. Run with --confirm to proceed.")


if __name__ == "__main__":
    main()
