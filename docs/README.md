# Nexus v4.3: Sovereign PKM Pipeline (April 2026)

**YouTube · Web · PDF · Instagram · Clipboard Text · Course Documents → High-Fidelity Zettelkasten Notes**

Nexus v4.3 implements a **"Passive Sovereign"** model: cloud infrastructure acts as a high-speed multimodal sensor (Stage 1), while all reasoning, synthesis, and long-term memory live on local Apple Silicon (Stage 2). No user data is retained by cloud providers beyond the duration of a single capture.

v4.3 adds **autonomous multi-source research**, **clipboard text capture**, **PDF file upload**, and a fully redesigned iOS shortcut that works both as a Share Sheet extension and a standalone widget.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  INPUT — 4 paths via NexusPro Shortcut (iPhone widget or share)  │
│                                                                  │
│  [URL]    Share URL  →  /capture          Stage 1 on relay       │
│  [TEXT]   Paste text →  /capture_text     Stage 1 skipped        │
│  [FILE]   Pick PDF   →  /capture_file     Stage 1 on relay       │
│  [TOPIC]  Research   →  /research_topic   Multi-source loop      │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  STAGE 1 — RELAY (DigitalOcean droplet, nexus.adithkb.dev)       │
│  gemini-3.1-flash-lite-preview  ·  google-genai SDK              │
│  Explicit cachedContent · LexRank transcript compression         │
│  Output: relay_description (JSON anchors + factual claims)       │
│  Cost: ~₹0.04/capture  (text path: ₹0.00 — Stage 1 skipped)     │
└───────────────────────────────┬──────────────────────────────────┘
                                │  seedling_*.json → DigitalOcean Spaces
                                ▼
                         vault_writer.py
                   (polls Spaces every 30s, routes to Vault/Seedlings/)
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  STAGE 2 — CRYSTALLIZER (M4 MacBook Air, local)                  │
│  watcher.py  ·  mlx_lm.generate in-process (no HTTP server)      │
│                                                                  │
│  Model routing:                                                  │
│    source_type==pdf OR duration≥3600s OR anchors≥5               │
│                           →  Qwen3-14B  (quality, Ollama)        │
│    web / short video / text paste                                │
│                           →  Qwen3.5-9B (fast, MLX in-process)   │
│                                                                  │
│  Six Integrity Pillars · Thermal guard · Hard synthesis timeout  │
│  Cost: $0.00 (local inference)                                   │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
                         Obsidian Vault (local iCloud)
                         ├── Sources/     source evidence notes
                         ├── Captures/    atomic Zettelkasten notes
                         └── MOCs/        auto-taxonomy topic maps
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  MCP SERVER — mcp_nexus.py v1.6                                  │
│  search_vault (semantic) · graph_search (LightRAG multi-hop)     │
│  research_topic (autonomous multi-source synthesis)              │
│  read_note · list_recent_captures · create_note                  │
│  nexus:// Resources → Claude Desktop / Gemini CLI / Claude CLI   │
└──────────────────────────────────────────────────────────────────┘
```

---

## NexusPro Shortcut — 9 Input Modes

Runs as both a **Share Sheet extension** (share from Safari/YouTube) and a **standalone widget** (tap from home screen). URL is pre-populated from the share sheet; on direct launch a URL prompt appears.

| # | Mode | How to trigger | What it captures |
|---|---|---|---|
| 1 | 🤖 AI / Tech | Share or paste URL | Technical content, papers, talks |
| 2 | 📍 Travel | Share or paste URL | Places, guides, itineraries |
| 3 | 🛠️ Guide | Share or paste URL | How-tos, tutorials, setup guides |
| 4 | 📋 Ordered List | Share or paste URL | Ranked lists, step-by-step content |
| 5 | 🌐 General | Share or paste URL | Anything without a specific intent |
| 6 | ✍️ Custom Intent | Share or paste URL + type intent | User-specified framing |
| 7 | 📝 Paste Content | Reads clipboard automatically | Text from Perplexity, ChatGPT, web clippings |
| 8 | 📎 Pick File (PDF) | iOS Files picker | Course PDFs, exported documents |
| 9 | 🔬 Research Topic | Type question + optional search terms | Multi-source autonomous research |

The `user_intent` field for modes 1–6 steers Stage 2 framing without changing the pipeline — AI/Tech → technical depth, Travel → actionable tips, Guide → procedural structure.

---

## What Gets Written to the Vault

### Modes 1–6 (URL Capture)
| Folder | Output |
|---|---|
| `Sources/` | 1 source note — URL, title, author, duration |
| `Captures/` | 3–7 atomic notes — one concept per note, tagged, backlinked |
| `MOCs/` | Existing matching MOC updated, or new MOC created |

### Mode 7 (Paste Content)
| Folder | Output |
|---|---|
| `Sources/` | 1 source note — user-supplied title, author "User", no URL |
| `Captures/` | 3–7 atomic notes — same pipeline as URL capture |
| `MOCs/` | Same auto-taxonomy MOC linking |

Stage 1 is skipped — the pasted text is used directly as `relay_description`. Cost: ₹0.00.

### Mode 8 (Pick File — PDF)
| Folder | Output |
|---|---|
| `Sources/` | 1 source note — PDF filename, metadata extracted from PDF |
| `Captures/` | 5–10+ atomic notes — 14B quality path produces more notes |
| `MOCs/` | Course/topic MOC updated or created |

PDF always routes to 14B quality path. Stage 2 synthesis: 90–180s.

### Mode 9 (Research Topic)
| Folder | Output |
|---|---|
| `Sources/` | 4 source notes — one per Jina-discovered URL |
| `Captures/` | 8–20 atomic notes — 2–5 per source; later notes reference earlier ones (knowledge compounding) |
| `MOCs/` | Dedicated `MOC - Topic Research - {question}` always created; existing matching MOCs also updated |

---

## Research Topic Pipeline (Autonomous Multi-Source)

```
/research_topic {topic_intent, search_query}
        │
        ▼
Spaces research_requests/ → vault_writer routes to Seedlings/
        │
        ▼
watcher._process_research_request()
        │
        ├── Jina Search API → 4 URLs auto-discovered
        │
        ├── Parallel Stage 1 (asyncio.gather + run_in_executor)
        │     URL 1 ─── extract_source() ──┐
        │     URL 2 ─── extract_source() ──┤  all simultaneous
        │     URL 3 ─── extract_source() ──┤
        │     URL 4 ─── extract_source() ──┘
        │
        └── Sequential Stage 2 (knowledge compounding)
              URL 1 → process_source() → write_to_vault() → vault_index.append()
              URL 2 → process_source() → write_to_vault() → vault_index.append()
                         ↑ sees URL 1 notes → creates forward links
              URL 3 → ...sees URL 1+2 notes...
              URL 4 → ...sees URL 1+2+3 notes...
                                │
                                ▼
              MOC - Topic Research - {topic} created
              Existing MOCs updated (Stage 2 suggests matching topics from vault_index)
```

**Why sequential Stage 2 matters**: each source's notes are embedded into the vault index before the next source runs. Later sources can reference earlier concepts — producing a connected graph, not isolated silos.

---

## Six Integrity Pillars

1. **Inline Schema Enforcement** — Full `NoteCollection` JSON schema in `SYSTEM_PROMPT`. Model sees exact required structure before generating. Replaced MLX-Textgen `guided_json` which broke on outlines 1.x.
2. **Mandatory Technical Anchors** — Stage 1 tracks `{term, quote}` pairs in JSON. Injected into Stage 2 prompt; all anchors must survive to vault.
3. **Semantic Integrity Gate** — `all-MiniLM-L6-v2` (cosine ≥ 0.85) detects dropped anchors post-synthesis; force-appends as `## Source Anchors (Unsynthesized)` with `needs_review: true`.
4. **JSON Parser Hardening** — `_parse_and_repair` 5-step pipeline: markdown stripping → `raw_decode` multi-candidate scan → thinking-block removal (`<think>` tags + `"Thinking Process:"` prefix) → `relations` type normalization → field defaults + first/last `{}` fallback.
5. **Thermal Safety** — `sysctl machdep.cpu.thermal_level` guard; 60s/120s cooldown at level 1/2. Post-synthesis cooldown: 120s (14B) / 30s (9B). MLX freed via `mx.clear_cache()` after each run.
6. **Hallucination Firewall** — (a) named-entity explicit warning — model may not attach training-knowledge facts to recognized entities unless stated in source; (b) grounding rules placed at END of both prompts — Gemini and Qwen attend most strongly to end-of-context.

---

## Quick Start

### 1. Install
```bash
pip install -r requirements.txt
brew install yt-dlp
```

### 2. Configure
```bash
export GEMINI_API_KEY="your-key"
export JINA_API_KEY="your-key"   # for Research Topic autonomous discovery
export OBSIDIAN_VAULT="~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Nexus"
```

### 3. Start local inference
```bash
ollama serve
ollama pull qwen3:14b    # quality path
ollama pull qwen3.5:9b   # fast path fallback
# MLX fast path (Qwen3.5-9B-MLX-4bit) loads in-process automatically
```

### 4. Start the Crystallizer daemon
```bash
python watcher.py
# Or via LaunchAgent: stealth_crystallizer.sh
```

### 5. Capture

```bash
# URL capture
python capture.py --url "https://youtube.com/watch?v=..."

# PDF
python capture.py --url "./papers/attention.pdf" --notes 10

# With intent
python capture.py --url "https://..." --intent "focus on implementation"
```

---

## CLI Reference

```
python capture.py --url URL [options]
python capture.py --batch FILE [options]

Options:
  --vault PATH         Override vault path
  --provider NAME      gemini | ollama | mlx
  --notes N            Max atomic notes per source (default: 10)
  --force              Skip dedup check
  --intent TEXT        Custom extraction instructions
```

---

## Monitoring

| Signal | Location | Action Threshold |
|---|---|---|
| Link entropy | `Captures/_link_stats.json` | < 1.5 → run standalone linker |
| Context cache | `[RELAY]` log | "Reusing" = cache hit; "Created" = miss |
| Model routing | `[CRYSTALLIZER]` log | "9B (fast)" vs "14B (quality)" |
| Anchor recovery | `[QUALITY]` log | ≥ 5 recovered → consider quality-path re-run |
| Research discovery | `[RESEARCH]` log | "Discovered N URLs via Jina" |
| Sync log | `~/.nexus-sync.log` | Rotated at 5 MB |

---

## End-to-End Benchmark (2026-03-30)

| Source | Path | Notes | Stage 1 | Stage 2 | Total |
|---|---|---|---|---|---|
| YouTube (9 min CS lecture) | URL capture | 3 | ₹0.04 | $0.00 | 185s |
| Web article | URL capture | 5 | $0.00 | $0.00 | ~120s |
| Perplexity answer (clipboard) | Paste Content | 4 | ₹0.00 | $0.00 | ~90s |
| Research: "Mamba SSM 2026" | Research Topic | 14 (4 sources) | ₹0.16 | $0.00 | ~480s |

---

## v4.2 → v4.3 Delta

| Feature | v4.2 | v4.3 |
|---|---|---|
| Input paths | 1 (URL via Share Sheet) | 4 (URL, Clipboard Text, PDF File, Research Topic) |
| Shortcut | Share Sheet only, 6-category URL menu | Widget + Share Sheet; 9-item flat menu; direct launch supported |
| Clipboard text capture | Not supported | `/capture_text` — skips Stage 1; text is relay_description directly |
| PDF file upload | Not supported | `/capture_file` — base64 upload; Stage 1 extraction on relay; 14B quality path |
| Research Topic (shortcut) | Not supported | `/research_topic` → Spaces → watcher autonomous research loop |
| Autonomous source discovery | Not supported | Jina Search API discovers 4 URLs per research query |
| Parallel Stage 1 (research) | Sequential | `asyncio.gather` + `run_in_executor` — all 4 URLs simultaneously |
| Knowledge compounding (research) | Not supported | vault_index incremented between each source; later notes reference earlier concepts |
| Research MOC | Not supported | Always created; existing MOCs also updated via auto-taxonomy Stage 2 suggestions |
| MCP `research_topic` tool | Not supported | Full autonomous research from Claude/Gemini CLI sessions |
| `capture_text` source_type | Not supported | `"text"` — bypasses Stage 1, routes to 9B fast model |

---

## Project Structure

```
AgenticPKMApp/
│
├── capture.py                 # CLI entry point — local capture & config bootstrap
├── watcher.py                 # Stage 2 daemon — watchdog, model routing, thermal guard,
│                              #   research request handler
├── processor.py               # All LLM calls — Stage 1 relay + Stage 2 crystallizer
├── vault.py                   # Note formatting, MOC/Source writing, vault RAG artifacts
├── extractors.py              # Stage 1 multimodal extraction (Gemini, Jina, yt-dlp, PyMuPDF)
├── embed_store.py             # Qwen3-Embedding 4B + numpy cosine similarity search
├── compressor.py              # LexRank extractive transcript compression
├── config.py                  # All configuration defaults + env var overrides
├── server.py                  # DigitalOcean droplet FastAPI relay
│                              #   /capture · /capture_text · /capture_file
│                              #   /research_topic · /pending · /index
├── vault_writer.py            # Polls DO Spaces; routes research_requests/ → Seedlings/
├── reflector_tier1.py         # Post-capture vault audit (broken links, orphans, cycles)
├── mcp_nexus.py               # MCP server v1.6 — search, graph, research, write
├── stealth_crystallizer.sh    # LaunchAgent wrapper — Utility QoS on M4
├── requirements.txt
│
├── docs/
│   ├── README.md              # This file
│   ├── architecture.md        # Full system design and v4.2→v4.3 delta
│   └── skills.md              # Shortcut builder reference
│
├── shortcuts/
│   ├── build_shortcut.py      # Generates NexusPro.shortcut plist — 9-mode iOS shortcut
│   └── NexusPro.shortcut      # Signed shortcut — import to iPhone
│
└── tools/
    ├── stress_test.py
    ├── clean_vault.py
    └── run_veracity_audit.py
```

---

**Author:** Adith K B · **License:** MIT
