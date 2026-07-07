#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Capture to Obsidian
# @raycast.mode fullOutput
# @raycast.argument1 { "type": "text", "placeholder": "URL to capture" }

# Optional parameters:
# @raycast.icon 🧠
# @raycast.packageName Obsidian PKM
# @raycast.description Capture a URL → Chain-of-Density Zettelkasten notes in Obsidian.

# ── Setup ─────────────────────────────────────────────────────
# Update PIPELINE_DIR to the absolute path where you cloned this repo.
PIPELINE_DIR="$HOME/AgenticPKMApp"   # ← edit this

URL="$1"

if [ -z "$URL" ]; then
    echo "✗ No URL provided."
    exit 1
fi

if [ ! -d "$PIPELINE_DIR" ]; then
    echo "✗ Pipeline directory not found: $PIPELINE_DIR"
    echo "  Update PIPELINE_DIR in this script."
    exit 1
fi

# ── Run ───────────────────────────────────────────────────────
cd "$PIPELINE_DIR"
python3 capture.py --url "$URL"
