# Nexus v6.0: Architecture Reference (June 2026)

## 1. Design Philosophy — Passive Sovereign

Cloud infrastructure acts as a high-speed multimodal **sensor** (Stage 1). All reasoning, synthesis, and long-term memory run on local Apple Silicon (Stage 2). No user data is retained by cloud providers beyond the duration of a single API call.

v6.0 adds three layers on top of the v4.3 foundation:
1. **Ambient Capture** — unified iOS shortcut with zero-friction Cognitive Load Gradient (tap → text/URL, 🎙️ → voice, share → URL pipeline)
2. **Dreamer Agent** — LazyGraphRAG autonomous vault maintenance at 3 AM
3. **Semantic Hardening** — LanceDB vector store, Marker PDF extraction, Model2Vec dedup gate

---

## 2. Input Paths

### 2a. URL Capture → `/capture`

The original path. Share sheet from Safari/YouTube/Instagram → relay extracts content via Gemini Stage 1 → seedling → local crystallizer.

`user_intent` is injected at the end of the Stage 2 prompt (recency bias). Steers framing without changing note count.

### 2b. Clipboard / Smart Text → `/capture_smart`

Accepts `{text}`. Server-side `_detect_intent()` classifies without any LLM:

```
Starts with "RQ "   →  research pipeline (research_request_*.json → Spaces)
Valid URL            →  full synthesis pipeline (/capture)
Anything else        →  fleeting note (incoming/Fleeting/)  ← zero latency, no LLM
```

The `RQ ` prefix is the explicit override — works for both typed and voice-to-text inputs. Natural phrasing (`what is`, `how does`, `explain`, etc.) is also detected. Always saves a fleeting note as provenance before routing to research.

### 2c. PDF File Upload → `/capture_file`

User picks a PDF from iOS Files → shortcut base64-encodes → POST to relay. Extraction tier cascade: **Marker → pymupdf4llm → plain text** (see §3a). `source_type = "pdf"` routes to 14B quality path unconditionally.

### 2d. Research Topic → `/research_topic`

No URL required. Saves `research_request_*.json` to Spaces → vault_writer routes to `Seedlings/` → watcher's `_process_research_request` runs the autonomous loop (see §5).

### 2e. Fleeting Notes → `/capture_voice_fleeting`

Zero-latency, zero-LLM capture path. Two sub-modes:

| Mode | Body | Server action |
|---|---|---|
| Siri dictation | `{text, duration_hint}` | Writes markdown directly to `incoming/Fleeting/` |
| Ambient audio | `{audio_b64}` | Groq Whisper transcription → markdown → `incoming/Fleeting/` |

Fleeting notes bypass Stage 1 and Stage 2 entirely. vault_writer routes `incoming/Fleeting/*.md` → `VAULT/Fleeting/`. The Dreamer Agent reviews them nightly for promotion candidates.

**"RQ " spoken in audio** is detected server-side after Groq transcription — also queues `research_request_*.json` to Spaces so the research pipeline runs in parallel with the fleeting save.

### 2f. Ambient Voice → `/capture_voice`

Full pipeline path for audio: Groq Whisper transcription → `run_relay_stage` → seedling → Spaces. Uses `user_intent` for Stage 2 framing.

---

## 3. Two-Stage Pipeline

### Stage 1 — The Relay (DigitalOcean droplet)

| Property | Value |
|---|---|
| **Model** | `gemini-3.1-flash-lite-preview` |
| **SDK** | `google-genai >= 1.0.0` (explicit cachedContent API) |
| **Context Caching** | Explicit `cachedContent` resource; 1,024-token minimum for Flash-tier |
| **Cache TTL** | 55-min effective TTL; hash-based invalidation on system prompt change |
| **Cost** | ~₹0.04/capture (10× input discount on cached tokens); ₹0.00 for text/fleeting paths |
| **Function** | Multimodal extraction — YouTube, Instagram, PDF, web articles |
| **Output format** | JSON: `source_context`, `mandatory_technical_anchors`, `factual_claims`, `entities` |
| **Transcript compression** | LexRank extractive; triggered at 16,000 chars; keeps top 25% of sentences |
| **Transcript quality scoring** | `_score_transcript()`: punct_density, TTR, 4-gram repetition; `is_poor_quality = flags ≥ 2` |
| **Skipped for** | `source_type = "text"` and `source_type = "fleeting"` |
| **Semantic dedup gate** | Model2Vec check runs AFTER extraction, BEFORE Gemini call — skips API cost on duplicates |

### Stage 2 — The Crystallizer (M4 MacBook Air)

| Property | Value |
|---|---|
| **Daemon** | `watcher.py` (LaunchAgent `com.adithkb.nexus-crystallizer`) |
| **Inference engine** | `mlx_lm.generate` in-process for 9B; Ollama HTTP for 14B |
| **Quality model** | `qwen3:14b` via Ollama (~45–55 tok/s) |
| **Fast model** | `mlx-community/Qwen3.5-9B-MLX-4bit` via MLX in-process (~82–100 tok/s) |
| **Thinking suppression** | `<think>\n</think>` prefill (MLX); assistant prefill (Ollama) |
| **JSON reliability** | Inline schema in `SYSTEM_PROMPT` + `_parse_and_repair` 5-step pipeline |
| **Synthesis timeout** | 480s (14B) / 240s (9B) — hard `asyncio.wait_for`; seedling kept for retry |
| **Embedding** | Qwen3-Embedding-4B via Ollama, 256-dim Matryoshka truncation |
| **Vector store** | **LanceDB** — SIMD-accelerated columnar format; replaces numpy/JSON SQLite store |
| **Process isolation** | `LOKY_MAX_CPU_COUNT=1` set at top of `watcher.py` before any imports — forces joblib/loky into sequential mode (no subprocess workers, no leaked POSIX semaphores). Without this, the DedupGate's model2vec call leaves orphaned semaphores that cause SIGSEGV in any subsequent native library: LanceDB (Rust/Arrow), MLX (Metal), PyTorch (SentenceTransformer). |
| **Cost** | $0.00 (local inference) |

### 3a. PDF Extraction Tier Cascade

Three-tier fallback in `extract_pdf()`. Each tier falls through on `ImportError` or quality failure (`len < 100`):

```
Tier 0: Marker        pip install marker-pdf   — VLM layout detection, 95.67% accuracy
         ↓ fallback
Tier 1: pymupdf4llm   pip install pymupdf4llm  — structure-aware markdown, 86.71% accuracy
         ↓ fallback
Tier 2: PyMuPDF       always available          — plain text, works on all PDFs
```

Marker models (`surya` + `texify`) are downloaded once (~300 MB) to `~/.cache/marker` and cached as a process-level singleton. Disable with `MARKER_ENABLED=false`.

---

## 4. Model Routing (Stage 2)

```
source_type == "pdf"                              →  14B quality path (Ollama)
source_type in (youtube, instagram)
  AND duration_sec >= 3600                        →  14B quality path (Ollama)
anchor_count >= 5 (dense technical content)       →  14B quality path (Ollama)
everything else (web, text, short video)          →  9B fast path (MLX in-process)
source_type == "fleeting"                         →  skipped entirely (no LLM)
```

### 4a. Full-Cloud Capture Path (`llm_provider == "gemini"`)

Opt-in per-capture escape hatch for when the Mac is off, asleep, or unreachable — both stages run on the droplet via Gemini, so the note finishes without the Mac at all. Triggered by the iOS Shortcut's "☁️ Cloud Capture" menu choice, which adds `"provider": "gemini"` to the `/capture` POST body; `build_config(provider=req.provider, ...)` threads it into `Config.llm_provider`.

| Property | Value |
|---|---|
| **Stage 1 model** | `gemini_model` — same `gemini-3.1-flash-lite-preview` extraction tier as the default path |
| **Stage 2 model** | Cascade-routed by `_select_gemini_synthesis_model()` — see below |
| **Where it runs** | `server.py`'s `_process_job()` — after `run_relay_stage()`, calls `run_crystallizer_stage()` directly with `config.llm_provider == "gemini"` instead of building a seedling for the Mac |
| **File delivery** | `vault.build_vault_files()` builds Capture/Source/MOC file contents in memory (no local vault access on the droplet) → `_upload_to_spaces()` pushes them straight to Spaces `incoming/` |
| **Pull side** | `vault_writer.py`'s `pull_pending_files()` — MOC paths get merged into the existing local file (`_merge_moc_content`) instead of overwritten, since the droplet can't know if a MOC already exists locally |
| **Phone delivery independent of Mac** | The same Spaces `incoming/` prefix is also polled directly by the "Remotely Save" Obsidian plugin on the phone — cloud captures can appear on the phone even with the Mac fully off |

#### Stage 2 cascade routing

`processor._select_gemini_synthesis_model(meta, anchor_count, config)` picks per-capture, cheap tier by default — 2026 LLM-routing best practice is to route the obvious majority cheap and escalate only what needs it, rather than pre-classifying everything through a trained router ([digitalapplied.com](https://www.digitalapplied.com/blog/llm-model-routing-2026-cost-quality-optimization-engineering-guide), [leanlm.ai](https://leanlm.ai/blog/llm-model-routing)):

| Tier | Model (config field) | When used |
|---|---|---|
| Cheap (default) | `gemini_synthesis_model` = `gemini-3.7-flash` | Everything else — the majority: short web articles, text, short video |
| Quality (escalation) | `gemini_synthesis_model_quality` = `gemini-3.1-pro-preview` | `source_type == "pdf"`, video ≥3600s, or ≥5 mandatory technical anchors — content a real quality-comparison test showed the cheap tier under-extracts on. Also escalated to on any JSON parse failure in `_parse_and_repair`'s repair path — a cheap-tier miss is itself evidence the content was harder than expected. |

`ProcessingResult.model_used` records which tier actually ran (reflects the primary attempt; a rare repair-path escalation isn't separately distinguished, since `_parse_and_repair` doesn't report which attempt succeeded) and flows into both the `llm_model:` frontmatter field and `vault._model_suffix()` — escalated notes get a filename suffix (e.g. `-preview`), default-tier notes stay unsuffixed so the common case keeps clean filenames.

**Cost**: no spend cap — opt-in is the guardrail. Measured against a real web-article capture (6,482 input / 4,690 output tokens, incl. thinking): **~$0.022/capture** on the cheap tier vs **~$0.069/capture** on the quality tier — the cascade keeps the common case near the cheap-tier price while still escalating dense/long content. Longer sources cost proportionally more regardless of tier. Stage 1 relay cost is separate and unaffected. Gemini 2.5+ models get automatic implicit prompt-prefix caching, but it needs ≥4,096 matching prefix tokens to trigger (confirmed against [ai.google.dev/gemini-api/docs/caching](https://ai.google.dev/gemini-api/docs/caching)) — the static `SYSTEM_PROMPT` alone is ~1,822 tokens, under that bar on its own, so it likely isn't actually benefiting today. Explicit caching (as Stage 1 already uses, 1,024-token minimum) would clear it, but the per-hour storage fee only pays off under bursty capture patterns — not worth building for occasional use.

### 4b. Concurrency — bounded, not unbounded

`/capture`'s cloud-mode branch returns immediately via a `BackgroundTask`, so posting several captures back-to-back queues several `_process_job()` runs on the droplet's single event loop. Two protections keep this safe rather than racing:

- **`_job_semaphore` (limit 2)** bounds how many full pipeline runs (Stage 1 + Stage 2 Gemini calls) execute concurrently — extra jobs queue behind the semaphore instead of firing an unbounded burst at the Gemini API, which is what actually risks a capture silently failing (rate-limit error). Applies to both the full-cloud path and the seedling-relay path, since both call Gemini for Stage 1. Job status (`_jobs[job_id]`) is set to `"processing"` before the semaphore is ever awaited, so `/result/{job_id}` correctly shows queued jobs immediately, not just running ones.
- **MOC read-merge-write** (`_upload_to_spaces()`) — two concurrent captures that both touch the same MOC (e.g. same topic) each independently build a full "MOC doesn't exist yet" version in memory (the droplet has no local vault to check against). Without a merge step, whichever job's `PUT` lands second on Spaces would silently overwrite the first job's links. Fixed by GET-ing the current Spaces object under the existing per-filename `asyncio.Lock` and merging in only the new `- [[...]]` lines before writing back — verified with a simulated concurrent-write test (two fake-concurrent uploads to the same MOC filename, both sets of links present in the final content, no loss).

A `_process_job()` failure (e.g. a transient API error) is still caught by the outer `try/except` and recorded as `_jobs[job_id] = {"status": "failed", ...}` — it won't hang forever, but note the iOS Shortcut doesn't currently poll `/result/{job_id}` to surface that failure to the phone; a failed capture is silently absent from the vault unless someone checks server logs or queries the job directly.

Standard (local) captures don't need any of this — `seedling_{job_id}.json` filenames are collision-free per job, and `watcher.py`'s serial worker (already verified single-concurrency) processes whatever lands in `Seedlings/` one at a time regardless of how fast they arrive.

---

## 5. Research Topic Pipeline — Sequential Smart Loop

```
POST /research_topic {topic_intent, search_query}
  │  relay saves research_request_{job_id}.json → Spaces/incoming/research_requests/
  ▼
vault_writer.py → routes to Vault/Seedlings/research_request_{job_id}.json
  ▼
watcher.py _process_research_request()
  ├─ 1. Jina Search API → 4 URLs discovered
  ├─ 2. Parallel Stage 1 (asyncio.gather + run_in_executor)
  └─ 3. Sequential Stage 2 (knowledge compounding)
         vault_index grows between each source → cross-source links in notes
         → MOC - Topic Research - {topic_intent}
```

**Knowledge compounding**: `store.get_all_as_index()` hits LanceDB once at loop start. Each `vault_index.append(new_notes)` is in-memory. `store.upsert_notes()` writes embeddings incrementally. Net: research loop is fast even on large vaults.

---

## 6. Dreamer Agent — Autonomous Vault Maintenance

`dreamer.py` runs as a zero-LLM background pass during thermal downtime (2 AM – 5 AM).

### Activation gates (all four must pass)
1. `2 ≤ local_hour < 5`
2. `thermal_level == 0`
3. `not handler.processing` (no active synthesis job)
4. Last dream session > 20 hours ago

### What it does

| Task | Threshold | Action |
|---|---|---|
| **Latent link discovery** | cosine 0.55 – 0.74 | Appends `- [[title]] — *latent*` inside `## Connections` |
| **Orphan rescue** | top-3 matches ≥ 0.50 | Writes outward link from orphan to rescuers |
| **MOC gap detection** | ≥ 3 notes share tag sub-domain, no MOC | Report only (no writes) |
| **Fleeting promotion** | cosine ≥ 0.65 match to Captures | Report only — flags for human review |

**Key constraint**: Dreamer never generates text. It only writes wikilinks and the nightly report.

Threshold bands deliberately below the hourly linker (0.75) — Dreamer surfaces serendipitous, non-obvious connections the primary pipeline misses.

### Embedding in Dreamer

`_embed_sync()` uses synchronous `requests.post` to Ollama (not async httpx) — required because Dreamer runs inside `loop.run_in_executor()`. Pattern consistent with `_transcribe_groq`.

### State file

`~/.nexus_dream_state.json` — `{last_dream_ts, last_dream_date, latent_links_added, orphans_rescued}`.

---

## 7. Semantic Dedup Gate

`dedup_gate.py` — intercepts between Stage 1 extraction and the Gemini API call.

| Model | `minishlab/potion-base-2M` |
|---|---|
| **Size** | 8 MB |
| **Dimension** | 64-dim float32, L2-normalised |
| **Latency** | ~10ms on CPU (M4) |
| **Storage** | SQLite at `~/.nexus_dedup.db` |

```
verdict = check(url, transcript[:4000])

"skip"    → sim ≥ 0.92  → abort pipeline, return {status: "duplicate"}
"flag"    → sim ≥ 0.75  → set meta.related_captures, continue
"proceed" → sim < 0.75  → novel content
```

On successful relay completion, `index(url, transcript, title)` stores the vector for future comparisons.

**Process constraint**: model2vec's `encode()` internally triggers joblib which defaults to the loky multiprocessing backend. Loky forks subprocess workers that leak POSIX semaphores on exit. These orphaned semaphores cause SIGSEGV in any subsequent native code loaded in the same process (LanceDB/Rust, MLX/Metal, PyTorch). Fixed in `watcher.py` by setting `os.environ["LOKY_MAX_CPU_COUNT"] = "1"` before any imports — loky runs sequentially, no forks, no leaks. Performance impact is negligible: potion-base-2M runs in ~10ms regardless of parallelism.

---

## 8. LanceDB Vector Store

`embed_store.py` — full rewrite from SQLite+JSON+numpy to LanceDB columnar format.

**Same public API** — drop-in replacement. Key internal changes:

| | Legacy (v4.x) | v6.0 |
|---|---|---|
| Storage | `~/.nexus_embed.db` (JSON strings) | `~/.nexus_lancedb/` (Lance columnar) |
| Search | numpy `matrix @ query` BLAS | LanceDB SIMD cosine search |
| Upsert | SQLite `INSERT OR REPLACE` | `merge_insert("title")` |
| Migration | — | Auto-migrates legacy SQLite on first run |

Schema: `[title, aliases, tags, note_type, content_hash, vector(256)]`.

`find_all_related_pairs()` (sync, used by `/related` endpoint): numpy pairwise cosine over all embeddings. Returns `{auto: [...], suggest: [...]}` grouped by similarity band (auto ≥ 0.85, suggest 0.75–0.85).

---

## 9. Six Integrity Pillars

1. **Inline Schema Enforcement** — Full `NoteCollection` JSON schema in `SYSTEM_PROMPT`.
2. **Mandatory Technical Anchors** — `{term, quote}` pairs tracked Stage 1 → Stage 2; all must survive to vault.
3. **Semantic Integrity Gate** — `all-MiniLM-L6-v2` (cosine ≥ 0.85) detects dropped anchors; force-appended with `needs_review: true`.
4. **Context-First Mandate** — Raw data before instructions; grounding rules at END of both prompts.
5. **JSON Parser Hardening** — `_parse_and_repair` 5-step pipeline (fence strip → raw_decode → think-block removal → type normalisation → field defaults).
6. **Hallucination Firewall** — Named-entity explicit warning; `source_quote` field required for every claim.

---

## 10. Auto-Taxonomy MOC System

Stage 2 `suggested_mocs` populated by LLM which sees `vault_index` (existing MOC titles included) → natural consistency. `_write_moc` appends to existing or creates new stub with Dataview query.

**Bidirectional parent MOC linking** (MCP `research_topic` only): embed store queried for existing MOCs (sim ≥ 0.75) → `## Part Of` written into new research MOC → parent MOCs updated. The watcher path relies on LLM suggestions alone.

**MOC emoji anchors**: `_get_moc_emoji()` — 🤖 AI/ML, 📍 travel, 🛠️ engineering, 📈 economics, 🔢 math, ⚙️ software.

---

## 11. Atomic Note Format

```markdown
---
type: atomic
note_type: mechanism | theory | claim | result | validation | content | method
aliases: [Multi-Word Alias]
tags: [ai/transformers, pattern/optimization]
moc: [Topic Name]
source: "https://..."
source_id: abc123
date: 2026-06-09
needs_review: true          ← only written when true
forward_links: [Linked-Note-Title]
summary: "One-sentence core claim for fast retrieval."
llm_model: qwen3:14b
---

# Title as Declarative Claim in Title Case

**Keywords**: Alias One, Alias Two

[2-4 sentence synthesized content]

> verbatim source quote

## Connections
- [[Linked-Note]] — *relationship-type*
- [[Latent-Link]] — *latent*     ← added by Dreamer Agent

---
*Source: [[Source Title]]*
```

**Tag format**: lowercase/slash/hierarchy. Allowed top-level: `ai`, `neuroscience`, `engineering`, `math`, `economics`, `productivity`, `software-engineering`, `data-science`, `career-development`, `pattern`.

### Fleeting Note Format

```markdown
---
type: fleeting
intent: fleeting | research
date: 2026-06-09
time: 14:32
duration: 45s
needs_synthesis: false
---

# 2026-06-09 14-32

[Raw thought or Groq transcript]
```

Stored in `VAULT/Fleeting/`. Dreamer Agent reviews weekly for promotion candidates (cosine ≥ 0.65 match to an existing Capture).

---

## 12. Vault RAG Layer

| File | Purpose | Update cadence |
|---|---|---|
| `CLAUDE.md` | Auto-read by Claude Code CLI; vault search strategy | Static |
| `GEMINI.md` | Auto-read by Gemini CLI; identical content | Static |
| `_INDEX.md` | One line per note: `[[Title]] \| semantic-tags \| date` | Regenerated per capture |

---

## 13. Knowledge Graph Layer

- **AutoSchemaKG** — Relation types inferred by Stage 2 LLM from co-occurrence patterns. No hard-coded categories. Consistency enforced via SYSTEM_PROMPT rule.
- **VaultLink** — `vault_links_typed` on `AtomicNote`; `rel` = "Infer from content"
- **Reciprocal backlinks** — `_write_reciprocal_link()` writes back-edges to existing notes
- **Shannon entropy tracking** — `Captures/_link_stats.json`; action threshold H < 1.5
- **graph_search (LightRAG)** — MCP tool; multi-hop traversal over `## Connections` edges; no separate graph DB
- **Latent links** — Dreamer Agent adds sub-0.75 cosine links tagged `*latent*` in `## Connections`

---

## 14. Thermal Safety (M4 MacBook Air — Fanless)

```
thermal_level == 0  →  proceed immediately
thermal_level == 1  →  60s pre-synthesis cooldown
thermal_level >= 2  →  120s pre-synthesis cooldown
```

Post-synthesis cooldown: 120s (14B) / 30s (9B). MLX model freed via `free_mlx_model()` → `mx.clear_cache()` before cooldown. Hard timeout: 480s (14B), 240s (9B).

**Dreamer Agent** only activates at `thermal_level == 0` — runs its embedding-only workload during guaranteed thermal headroom.

---

## 15. iOS Shortcut — Nexus v6.6 (Share Sheet)

Single `.shortcut` file (`shortcuts/build_v6.py`), one surface: the share sheet.

```
Share sheet (Safari, YouTube, Instagram…)
  └─ "Capture Mode" menu: [📝 Standard Capture | ☁️ Cloud Capture]
        │
        ├─ 📝 Standard Capture
        │   └─ POST /capture {url: ExtensionInput, return_files: true}
        │       → seedling queued for Stage 2 on the Mac (watcher.py)
        │
        └─ ☁️ Cloud Capture
            └─ POST /capture {url, return_files: true, provider: "gemini"}
                → both stages run on the droplet (§4a) — no Mac required
```

`/capture_smart` and `/capture_voice_fleeting` (used by the Sync Bridge diagram below) are live server routes with no client surface currently wired to them in this shortcut — a known gap, not part of this version's scope.

---

## 16. Sync Bridge

```
Nexus v6.0 Shortcut (iPhone)
  │  Share sheet → POST /capture
  │  Tap + 📝    → POST /capture_smart
  │  Tap + 🎙️   → POST /capture_voice_fleeting
  │  Files app   → POST /capture_file
  │  Research    → POST /research_topic
  ▼
DigitalOcean Droplet (nexus.adithkb.dev)
  │  Stage 1 extraction (URL + File + Voice paths)
  │  Semantic dedup gate (Model2Vec, pre-Gemini)
  │  Saves to Spaces:
  │    seedling_*.json               → synthesis queue
  │    research_request_*.json       → research queue
  │    incoming/Fleeting/*.md        → zero-latency vault write
  ▼
DigitalOcean Spaces (incoming/ prefix)
  ▼
vault_writer.py (Mac, polls every 30s)
  │  Routes:
  │    seedling_*.json               → Vault/Seedlings/
  │    research_requests/*.json      → Vault/Seedlings/research_request_*.json
  │    Fleeting/*.md                 → Vault/Fleeting/
  │    *.md                          → Vault/{path}
  ▼
watcher.py (Mac, watchdog on Vault/Seedlings/)
  │  process_seedling()              → URL/text/file captures
  │  _process_research_request()     → autonomous multi-source research
  │  _maybe_run_dream_session()      → Dreamer Agent (2–5 AM, thermal 0)
  ▼
Obsidian Vault (local iCloud Drive) → iCloud sync → iPhone Obsidian app
```

---

## 17. MCP Server (v1.6)

`mcp_nexus.py` exposes the vault to Claude/Gemini CLI sessions and GUI clients.

| Tool | Description |
|---|---|
| `search_vault` | Semantic search via Qwen3-Embedding-4B + LanceDB; top-k with similarity scores |
| `read_note` | Full note content by title; searches Captures/ → MOCs/ → Sources/ |
| `list_recent_captures` | Last N notes sorted by mtime |
| `graph_search` | LightRAG multi-hop traversal over `## Connections` edges |
| `inspect_graph_schema` | Frequency table of all relation types — use before `graph_search` |
| `research_topic` | Autonomous: Jina discovery + parallel Stage 1 + sequential Stage 2 + MOC |
| `research_to_vault` | Single URL → full pipeline (Stage 1 + Stage 2) |
| `create_note` | Draft atomic note with enforced frontmatter schema |

---

## 18. Self-Auditing Infrastructure

`reflector_tier1.py` — post-capture: broken wikilinks, orphan nodes, circular prerequisite detection, per-note `validate_note()`.

---

## 19. Active Monitoring Signals

| Signal | Location | Action threshold |
|---|---|---|
| Link entropy | `Captures/_link_stats.json` | H < 1.5 → run standalone linker |
| Model routing | `[CRYSTALLIZER]` log | "9B (fast)" vs "14B (quality)" |
| Anchor recovery | `[QUALITY]` log | ≥ 5 recovered → re-run on quality path |
| Synthesis timeout | `[CRYSTALLIZER]` log | Seedling kept for retry |
| Research discovery | `[RESEARCH]` log | "Discovered N URLs via Jina" |
| Context cache | `[RELAY]` log | "Reusing" = hit; "Created" = miss |
| Dedup gate | `[JOB]` log | "Duplicate content. Matched: [...]" |
| Dream session | `~/.nexus_dream_state.json` | `latent_links_added`, `orphans_rescued` |
| Sync log | `~/.nexus-sync.log` | Rotated at 5 MB, 3 backups |

---

## 20. Deferred Features

| Feature | Trigger |
|---|---|
| LanceDB hybrid search (BM25 + vector) | ~2K notes — add `create_fts_index` + RRF fusion |
| Hypothetical question generation | Opt-in `--enrich` flag; +1 local LLM call per note; +50% RAG recall |
| Link decay scoring (A-MEM) | ~500 typed edges — `reflector_tier1.py` extension |
| RAGFlow PDF benchmark | Compare vs Marker for academic multi-column papers |
| NLI Logic Gate (`deberta-v3-small`) | High-stakes domain captures; cosine < 0.90 triggers audit |
| MOC grouped static index | Tag-based subtopic grouping under `## Notes` |
| Research synthesis note | `generate_synthesis=True` in request (currently off by default) |

---

## 21. v4.3 → v6.0 Delta

| Feature | v4.3 | v6.0 |
|---|---|---|
| iOS Shortcut | `build_v5.py` — 6-option share sheet menu | `build_v6.py` — 3 surfaces: share / tap / 🎙️ |
| Smart intent routing | Manual menu selection | `_detect_intent()` — "RQ " prefix, natural NL, URL heuristic |
| Fleeting notes | Not present | `/capture_voice_fleeting` → `Fleeting/` — zero LLM, zero latency |
| Ambient voice | Not present | Groq Whisper → fleeting + optional research queue |
| Dreamer Agent | Not present | `dreamer.py` (512 lines) — nightly latent links + orphan rescue |
| PDF extraction | Tier 1: pymupdf4llm → plain text | Tier 0: **Marker** → Tier 1: pymupdf4llm → Tier 2: plain text |
| Vector store | SQLite + JSON + numpy brute-force | **LanceDB** columnar, SIMD cosine, `merge_insert` upsert |
| Semantic dedup | Not present | **DedupGate** — Model2Vec potion-base-2M, 8 MB, ~10ms CPU |
| Embedding model | Qwen3-Embedding-4B (unchanged) | Unchanged (already SOTA) |
| `_detect_intent` | Not present | Regex: "RQ ", natural question, URL — no LLM |
| Voice → research | Not present | "RQ " spoken → Groq transcribes → research queue + fleeting save |
| Dedup gate position | Not present | Runs post-extraction, pre-Gemini (saves API cost on duplicates) |
| `find_all_related_pairs` | Dead reference in `/related` | Implemented: numpy pairwise over LanceDB embeddings |

### v6.5 → v6.6 Delta

| Feature | v6.5 | v6.6 |
|---|---|---|
| Full-cloud capture path | Not present — Stage 2 always local | `llm_provider == "gemini"` (§4a): both stages via Gemini, no Mac required |
| iOS Shortcut | Share sheet → `/capture`, no mode choice | Share sheet → "Capture Mode" menu: 📝 Standard / ☁️ Cloud Capture (§15) |
| `build_vault_files()` | Deleted in an earlier cleanup pass (dead at the time) | Rebuilt — in-memory note/MOC file builder for server-side (no local vault) writes |
| `vault_writer.py` MOC pulls | Blind overwrite of `MOCs/*.md` | Merges incoming links into existing local MOC file (`_merge_moc_content`) |

---

## 22. Key File Map

| File | Role |
|---|---|
| `capture.py` | CLI entry point — local capture & config bootstrap |
| `config.py` | All configuration defaults + env var overrides |
| `extractors.py` | Multimodal extraction — Marker/pymupdf4llm/PyMuPDF tiers; Groq Whisper; transcript scoring |
| `processor.py` | Stage 1 relay + Stage 2 crystallizer; all LLM calls; density-aware prompt |
| `vault.py` | Note construction, MOC/Source writing, fleeting note writer, `_INDEX.md` |
| `embed_store.py` | LanceDB vector store — Qwen3-Embedding-4B, 256-dim, merge_insert upsert |
| `dedup_gate.py` | Model2Vec semantic dedup — 8 MB, 64-dim, ~10ms CPU |
| `dreamer.py` | Dreamer Agent — LazyGraphRAG autonomous vault maintenance (nightly, zero LLM) |
| `compressor.py` | LexRank transcript compression |
| `watcher.py` | Stage 2 daemon — watchdog, model routing, thermal guard, research loop, Dreamer trigger |
| `server.py` | FastAPI relay — `/capture`, `/capture_smart`, `/capture_voice`, `/capture_voice_fleeting`, `/capture_text`, `/capture_file`, `/research_topic` |
| `vault_writer.py` | Polls Spaces; routes seedlings/research/fleeting/markdown to vault |
| `mcp_nexus.py` | MCP server v1.6 — search, graph, research, write tools, `nexus://` resources |
| `reflector_tier1.py` | Post-capture vault audit |
| `shortcuts/build_v5.py` | Legacy 6-option share sheet shortcut |
| `shortcuts/build_v6.py` | **Current** — unified 3-surface shortcut (share / tap / 🎙️) |
| `tools/test_new_features.py` | QA: transcript scoring, pymupdf4llm, EmbedStore, Dreamer dry-run |
| `tools/test_voice_features.py` | QA: fleeting notes, embed_sync, `/capture_smart` intent (25 cases), Dreamer report |
