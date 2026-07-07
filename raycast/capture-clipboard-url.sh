#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Capture Clipboard URL to Obsidian
# @raycast.mode fullOutput

# Optional parameters:
# @raycast.icon 📋
# @raycast.packageName Obsidian PKM
# @raycast.description Read a URL from the clipboard and capture it to Obsidian.
#                      Copy any URL, invoke this command — no typing required.

# ── Setup ─────────────────────────────────────────────────────
# Update PIPELINE_DIR to the absolute path where you cloned this repo.
PIPELINE_DIR="$HOME/AgenticPKMApp"   # ← edit this

if [ ! -d "$PIPELINE_DIR" ]; then
    echo "✗ Pipeline directory not found: $PIPELINE_DIR"
    echo "  Update PIPELINE_DIR in this script."
    exit 1
fi

# ── Read clipboard ────────────────────────────────────────────
URL=$(pbpaste | tr -d '[:space:]')

if [ -z "$URL" ]; then
    echo "✗ Clipboard is empty."
    exit 1
fi

# Basic URL sanity check — must start with http(s):// or be a file path
if [[ ! "$URL" =~ ^https?:// ]] && [[ ! "$URL" =~ ^/ ]] && [[ ! "$URL" =~ ^~ ]]; then
    echo "✗ Clipboard doesn't look like a URL or file path:"
    echo "  $URL"
    exit 1
fi

echo "▸ Capturing: $URL"
echo ""

# ── Run ───────────────────────────────────────────────────────
cd "$PIPELINE_DIR"
python3 capture.py --url "$URL"
