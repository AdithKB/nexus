# Video Pipeline Optimization Research
*Scraped: March 24, 2026 — all findings from live web searches*

## 1. MEDIA RESOLUTION & FRAME SAMPLING — N/A (pipeline uses yt-dlp transcripts, not Gemini video URL)
### Finding
Gemini 2.5 models tokenize video at approximately **263-267 tokens per second** (assuming default 1 FPS). The `MEDIA_RESOLUTION_LOW` setting (360p) consumes **64-70 tokens per frame**, whereas `HIGH` resolution consumes **280 tokens per frame**. Benchmarks show that `LOW` is sufficient (84.7% accuracy) for talking-head videos and general scene description, but fails significantly for **OCR-heavy tasks** or dense text slides. The API now supports **Dynamic FPS** from 0.1 FPS to 60 FPS, allowing for extreme cost reduction in long-form "slow" content.

### Sources
- [1] Google AI Studio - Media Resolution Documentation (May 2025)
- [2] VideoMME: Video Multimodal Evaluation Benchmark (2025)
- [3] Google Vertex AI - Video Understanding Best Practices (Jan 2025)

### Gap
Specific "per-frame" token counts for non-standard FPS (e.g., exactly how many tokens a single frame consumes at 0.5 FPS vs 1 FPS) were not explicitly detailed beyond the standard 64-280 range.

---

## 2. GEMINI CONTEXT CACHING ✅ DONE
### Finding
Context caching has become highly accessible in 2025, with the minimum token threshold lowered to **1,024 tokens for Flash** and **2,048 tokens for Pro**. **Gemini 2.5 Flash-Lite** supports both explicit (manual TTL) and implicit (automatic prefix matching) caching. While caching offers a **75-90% discount** on input tokens, a "Text-Only Pipeline" (extracting a transcript once with Flash-Lite and processing only text thereafter) is still **~22x cheaper** than using cached multimodal video tokens for repeated queries.

### Sources
- [1] Google AI Blog - "Introducing Implicit Caching for Gemini 2.5" (May 2025)
- [2] Google Cloud Pricing - Context Caching Storage & Processing Fees (2025)
- [3] Enterprise LLM Cost Optimization Case Studies (Late 2025)

### Gap
The exact "break-even" video length where caching beats a 2-pass low_res+text pipeline depends on query volume; no universal "minutes" figure was found, but the 22x cost difference suggests text-only is almost always cheaper unless visual reasoning is the primary task.

---

## 3. HALLUCINATION REDUCTION & GROUNDING — PARTIAL (Semantic Integrity Gate + source_quote fields done; discriminative verification not yet implemented)
### Finding
The **PTCF (Persona-Task-Context-Format)** framework combined with **XML-style tags** (e.g., `<context>`) is the current standard for grounding. To prevent "extrinsic overclaim" (parametric memory overriding source context), the industry uses **Context-First Constraints** (e.g., "Your internal knowledge is deprecated") and **Structured Grounding** where the model must provide a `source_quote` field for every claim. Chain-of-Verification (CoVe) is being replaced by cheaper **Discriminative Verification**, where a smaller "Judge" model (like Flash-Lite) scores the output of a larger model for a ~1.1x cost overhead.

### Sources
- [1] arXiv:2510.04321 - "Budget-aware Test-time Scaling via Discriminative Verification" (Oct 2025)
- [2] Google Cloud - Grounding with Google Search Documentation (2025)
- [3] ICLR 2025 - "Superposition in LLM Attention: Why Parametric Memory Overrides Context"

### Gap
No "standardized library" for ACE (Agentic Context Engineering) was found; it remains a custom-built architectural pattern rather than a plug-and-play tool.

---

## 4. STRUCTURED JSON OUTPUT QUALITY ✅ DONE
### Finding
**Native JSON Mode** (`response_mime_type: "application/json"`) provides near 100% syntactic reliability but can slightly degrade reasoning depth because it constrains the model's internal "scratchpad." The optimal pattern is to include a **`thinking` or `reasoning` field as the FIRST key** in the schema, forcing the model to reason before outputting the final data. For Gemini 2.5, the recommended **default temperature is 1.0**, as lower temperatures (0.0-0.3) can cause "infinite loops" or degraded performance in the 2.5 architecture.

### Sources
- [1] Google AI - Structured Output Best Practices (2025)
- [2] Pydantic AI - "Reliable JSON Extraction with Gemini 2.5" (Mid-2025)
- [3] LangChain Blog - "Why Temperature 1.0 is the New 0.0 for Reasoning Models" (2025)

### Gap
Benchmarks comparing JSON vs. YAML performance specifically for the 2.5 Flash model were not found; JSON remains the only natively enforced MIME type.

---

## 5. TWO-PASS / MULTI-PASS LLM PIPELINE PATTERNS ✅ DONE (Stage 1 relay + Stage 2 crystallizer IS this pattern)
### Finding
The **"Cheap Extraction, Capable Synthesis"** pattern is the production standard. Pass 1 uses a high-throughput SLM (Flash-Lite or Llama-3-8B) to reduce raw data (video/text) into a **Structured State Object** enriched with citations. Pass 2 uses a frontier reasoning model (Pro or o1) to perform "cross-contract" or "strategic" reasoning on the distilled state. Adding a **Pass 3 Verification** (AutoCrit) is recommended only for high-stakes domains (Legal/Medical), as it reduces error propagation by ~50% but doubles the synthesis cost.

### Sources
- [1] State of AI Report 2025 - "Inference Scaling & Pipeline Patterns"
- [2] LangGraph Documentation - "The State Pattern for Multi-Pass Agents" (2025)
- [3] arXiv:2512.1102 - "Self-Critique and Refinement Preference Optimization (SCRPO)"

### Gap
There is limited research on the "latency penalty" of adding Pass 3 in real-time user workflows beyond the obvious "it takes longer."

---

## 6. YOUTUBE-SPECIFIC GEMINI HANDLING — N/A (pipeline uses yt-dlp for transcript extraction, not Gemini direct URL)
### Finding
Direct **YouTube URL** processing is faster to initialize (Google handles the fetch) but lacks the **`mediaResolution: 'low'`** control found in the **File API**. Long-form videos (>30 min) frequently hit a **"30-minute wall"** due to 120-second gateway timeouts or truncation of the final segments. Best practices include **chunking into 10-15 minute segments** and using `generate_content_stream` to prevent silent timeouts.

### Sources
- [1] Google Developers - Gemini API File vs. URL Comparison (2025)
- [2] Reddit r/Gemini - "Why does Gemini Pro stop watching after 20 minutes?" (Community Report 2025)
- [3] Google I/O 2025 - "Scaling Video Understanding with Gemini"

### Gap
It is unclear if Google's internal fetch (via URL) uses a higher quality/token rate than the File API's default, but most developers assume it defaults to 1 FPS Standard.

---

## 7. COST BENCHMARKS (live pricing) — N/A (reference only)
### Finding
As of June 2025, **Gemini 2.5 Flash-Lite** is the price leader at **$0.10/1M input tokens**, making it roughly **$0.000027 per second of video**. Standard **Flash** is $0.30/1M input. The **Batch API** offers a flat **50% discount** ($0.05/1M for Flash-Lite) but carries a potential 24-hour turnaround, making it impractical for "instant capture" but ideal for bulk vault re-indexing.

### Model | Input ($/1M) | Output ($/1M) | 1-Min Video Cost |
| :--- | :--- | :--- | :--- |
| **2.5 Flash-Lite** | $0.10 | $0.40 | **$0.0016** |
| **2.5 Flash** | $0.30 | $2.50 | **$0.0048** |
| **2.5 Pro** | $1.25 | $10.00 | **$0.0200** |

### Sources
- [1] Google AI Pricing - Stable Model Tiers (Updated June 2025)
- [2] PricePerToken.com - Multi-Model Price Index (Jan 2026)
- [3] Gemini API Console - Batch API Rate Limits & Pricing

### Gap
Pricing for "Thinking Tokens" (reasoning) is currently billed at the standard output rate, but there are rumors of a "reasoning-only" tier that was not yet official at the time of search.

---

## Summary: Top Recommendations (by impact/cost ratio)

1. **Shift Pass 1 to Flash-Lite @ LOW Resolution:** Use `mediaResolution: 'low'` and 1 FPS to describe the video. This is the single biggest cost-saver ($0.0016/min) while maintaining 84%+ description accuracy. — N/A (using yt-dlp transcripts)
2. **Implement "Chain-of-Thought" INSIDE the JSON Schema:** Define the first key of your JSON as `reasoning` or `thought_process`. This preserves the 2.5 Pro/Flash "thinking" advantage while ensuring 100% schema compliance. — ✅ DONE (inline schema in SYSTEM_PROMPT with reasoning-first structure)
3. **Use Implicit Caching via Prefix Alignment:** Place your long system instructions and vault index snippets at the *beginning* of every prompt. This triggers the 75-90% discount automatically for any call over 1,024 tokens. — ✅ DONE (cachedContent implemented in processor.py)
4. **Avoid Pro for Extraction; Reserve for Synthesis:** Flash-Lite is as capable as Pro for "watching and describing" (Pass 1), but Pro is significantly better at "strategic synthesis" (Pass 2). This tiered model saves ~92% on input costs. — ✅ DONE (gemini-flash-lite for Stage 1, local Qwen3 for Stage 2)

## Open Questions
- Does using a custom FPS (e.g. 0.5 FPS) break the native "Implicit Caching" prefix match?
- Is there a specific token count threshold where `application/json` mode starts to drop complex nested fields due to "JSON fatigue"?

---

# 2026 State-of-the-Art Research — PKM Pipeline
*Scraped: 2026-03-31 — all findings from live web searches*

## 1. Atomic Fact Extraction & Verification (AFEV) Framework ✅ DONE

- **Source:** https://arxiv.org/html/2506.07446v1
- **Relevance:** Directly applicable to atomic note generation. The AFEV framework progressively decomposes complex claims into simpler atomic facts using LLMs, with specialized modules for extraction and adaptive verification. This is the exact mechanism needed for breaking down web/video content into Zettelkasten-style atomic notes.
- **Actionable insight:** Implement a three-stage pipeline: (1) dynamic decomposition of claims into atoms, (2) evidence retrieval targeting each fact, (3) adaptive verification per atom. This reduces complexity compared to trying to verify whole documents at once.

---

## 2. A-MEM: Agentic Memory for LLM Agents (Zettelkasten-Based) — PARTIAL (atomic notes + auto-linking implemented; link decay pending)

- **Source:** https://arxiv.org/abs/2502.12110
- **Relevance:** Proposes dynamically organizing agent memories following Zettelkasten principles with automatic indexing and linking. This bridges the gap between LLM reasoning and knowledge graph construction directly.
- **Actionable insight:** Use the A-MEM approach to structure note memory: notes should include contextual descriptions, keywords, tags, and dynamic bidirectional linking. This integrates naturally with Obsidian's link syntax and supports both atomic note creation and knowledge graph construction. Extend `reflector_tier1.py` to periodically re-score link strength using cosine similarity decay. Low-confidence links get flagged for review rather than staying forever.

---

## 3. MLX On-Device Inference Benchmarks ✅ DONE

- **Source:** https://arxiv.org/abs/2510.18921
- **Relevance:** Latest research shows MLX achieves <10 second time-to-first-token for 14B models and <3 seconds for 30B MoE on Apple Silicon, making it viable for real-time local inference in the pipeline.
- **Actionable insight:** Target Qwen3 (27B or 35B-A3B) via MLX for the local inference layer. These models fit in <16GB VRAM on recent Apple Silicon, enabling practical on-device atomic extraction without cloud dependency.

---

## 4. AutoSchemaKG: Autonomous Knowledge Graph Construction ✅ DONE

- **Source:** https://arxiv.org/abs/2505.23628
- **Relevance:** Eliminates the need for manual schema definition. AutoSchemaKG simultaneously extracts knowledge triples and induces schemas directly from text — critical for unsupervised knowledge graph construction from diverse captured content.
- **Actionable insight:** Instead of hardcoding entity/relationship types, use LLM-driven schema induction to automatically discover what node/edge types emerge from notes. This creates a flexible, evolving knowledge graph that adapts to your content domain. The `relations` field in the note schema already captures typed relationships — AutoSchemaKG's schema discovery could auto-evolve relation types as the vault grows.

---

## 5. MDKeyChunker: Structure-Aware Semantic Chunking for Markdown — PARTIAL (semantic enrichment done; hypothetical questions pending)

- **Source:** https://arxiv.org/html/2603.23533
- **Relevance:** Tailored for Markdown (Obsidian format). Three-stage pipeline: structure-aware chunking, semantic enrichment (title, summary, keywords, hypothetical questions), and key-based merging for coherent context windows.
- **Actionable insight:** Use MDKeyChunker's approach to preserve Obsidian's structure (headers, lists, code blocks) during capture. Generate "hypothetical questions" for each chunk — these become natural query anchors for the knowledge graph and improve RAG retrieval accuracy by 50%+. In `vault.py`, generate 2-3 hypothetical questions per note during crystallization and add them to the `**Keywords**` block. Requires one extra LLM call per note — opt-in via `--enrich` flag.

---

## 6. ObsidianRAG + LightRAG: Local Graph RAG for Obsidian Vaults ✅ DONE

- **Source:** https://forum.obsidian.md/t/neural-composer-local-graph-rag-made-easy-lightrag-integration/109891
- **Source:** https://github.com/Vasallo94/ObsidianRAG
- **Relevance:** Existing implementation of Graph RAG (relation-aware retrieval, not just vector similarity) directly integrated with Obsidian + Ollama. Neural Composer solves the "connecting dots" problem that standard vector search misses.
- **Actionable insight:** Layer Graph RAG on top of atomic notes. Instead of retrieving isolated notes, retrieve multi-hop relationships and entity clusters. This dramatically improves synthesis quality when generating new notes or answering queries across the vault. Post-MVP addition to `mcp_nexus.py`.

---

## 7. RAGFlow: Production-Grade Knowledge Base Grounding — PARTIAL (pipeline architecture adopted; PyMuPDF remains)

- **Source:** https://github.com/infiniflow/ragflow (70k+ GitHub stars)
- **Relevance:** Production-ready RAG framework designed for making AI answers traceable and grounded in source documents. Critical for maintaining provenance when extracting atomic facts from multimedia. Their PDF parsing outperforms PyMuPDF for academic papers (especially multi-column layouts).
- **Actionable insight:** Adopt RAGFlow's chunking→embedding→indexing→retrieval pipeline as the canonical architecture. Integrate source attribution (document, page, timestamp, video frame) at every stage so extracted notes maintain full provenance for verification and citation. Worth benchmarking on papers vs current PyMuPDF path.

---

## 8. Qwen3.5: Optimized for Agentic Workflows ✅ DONE (fast path upgraded to Qwen3.5-9B-MLX-4bit; quality path stays Qwen3-14B — 14B tier absent in 3.5 family)

- **Source:** https://qwenlm.github.io/blog/qwen3/
- **Source:** https://github.com/QwenLM/Qwen3.5
- **Relevance:** Qwen3.5 explicitly supports 256K context, thinking modes (for complex reasoning), and multi-turn agent planning — perfect for orchestrating the capture→extract→link→synthesize pipeline. Smaller versions (9B, 27B-A3B MoE) run efficiently on consumer hardware via MLX or Ollama. Active parameters: 3B (fast) to 27B (quality). Fits in 16GB unified memory with 4-bit quant. Has a native thinking mode (`/think` prefix) for complex extraction.
- **Actionable insight:** Direct upgrade from current Qwen3-14B. Thinking mode would improve anchor extraction for dense papers. Check `mlx-community/Qwen3.5-27B-A3B-4bit` availability. No code changes needed — just swap `MLX_MODEL` env var. Use Qwen3.5 Medium (27B-A3B) for quality path and Small (9B) for fast path.

---

## 9. Semantic Chunking + Atomic Proposition Extraction (70% RAG Accuracy Boost) ✅ DONE (atomic notes ARE propositions by design — note filename = claim)

- **Source:** https://langcopilot.com/posts/2025-10-11-document-chunking-for-rag-practical-guide
- **Relevance:** 2025 empirical study shows proposition-based chunking (extracting atomic claim-level statements) boosts RAG accuracy by 70% vs. naive chunking. Critical for the capture pipeline. The current atomic notes already implement this — each note filename IS a proposition — meaning the pipeline is already ahead of most production RAG systems at this granularity.
- **Actionable insight:** When capturing web/video/PDF content, extract atomic propositions at the sentence level (one claim per extracted fact). Group propositions into chunks until reaching context limit (~500 words) or hitting a topic shift. This dual-granularity indexing (sentence atoms + paragraph chunks) improves both precision and recall.

---

## 10. Knowledge Distillation for Efficient Local LLMs — N/A (M4 power makes distillation secondary)

- **Source:** https://link.springer.com/article/10.1007/s10462-025-11423-3
- **Relevance:** 2025 survey shows knowledge distillation techniques (task-specific alignment, rationale-based training) reduce model size by 80%+ while preserving reasoning. Enables sub-3B models to handle complex extraction tasks.
- **Actionable insight:** Consider distilling a Qwen3.5 or Claude model to a 3B-7B student for on-device atomic extraction. Use rationale-based training (teacher explains its extraction decisions, student learns to replicate) to preserve quality while cutting inference latency by 60%+.

---

## Bonus: GitHub Trending Projects to Monitor

- **RAGFlow** (70k stars): Production RAG infrastructure with multimodal support — https://github.com/infiniflow/ragflow
- **Dify** (114k+ stars): Visual workflow builder for AI pipelines — great for prototyping extraction stages — https://github.com/langgenius/dify
- **OpenClaw** (210k+ stars): Agentic automation framework — emerging tool for orchestrating capture + processing pipelines

---

## Recommended Architecture (2026 Synthesis)

```
1. Capture:         web scraper, YouTube transcript extractor, PDF processor (multimodal VLMs for layout)
2. Extraction:      Qwen3.5 (27B-A3B) via MLX → AFEV framework for atomic fact decomposition
3. Enrichment:      MDKeyChunker-style semantic enrichment (summaries, keywords, hypothetical questions)
4. Storage:         Obsidian vault with atomic Zettelkasten structure + RAG-ready embeddings
5. Knowledge graph: AutoSchemaKG dynamic schema induction + Graph RAG retrieval for synthesis
6. Optimization:    Knowledge distillation to 7B model for CPU-only fallback on older hardware
```

---

## Priority Action Items

| Priority | Action | Effort |
|---|---|---|
| 1 | ~~Test `Qwen3.5-27B-A3B-4bit` as quality path replacement~~ → Fast path upgraded to `Qwen3.5-9B-MLX-4bit` | 30 min | ✅ DONE |
| 2 | Add hypothetical question generation to `**Keywords**` block | 2h |
| 3 | Extend `reflector_tier1.py` with link decay scoring (A-MEM) | 3h |
| 4 | Benchmark RAGFlow PDF extraction vs PyMuPDF | 1h |
| 5 | Add LightRAG layer to `mcp_nexus.py` (multi-hop traversal) | 1 day | ✅ DONE |
| 6 | AutoSchemaKG: LLM-driven schema discovery (dynamic relations) | 2-3h | ✅ DONE |
| 7 | Graph Search: Implement native multi-hop graph traversal | 4-6h | ✅ DONE |
| 8 | **LanceDB**: Replace Numpy/JSON with columnar hybrid search | 3-4h | |
| 9 | **Gemma-300M**: Upgrade MiniLM to modern instruction-tuned SLM | 1h | |
| 10 | **NLI Logic Gate**: Implement logical entailment check for audit | 4-6h | |

---

## 2026 SOTA Audit — Final Technical Assessments

### 11. LanceDB: Columnar Hybrid Search (Vector + Keyword)
- **Problem**: Current Numpy-based search requires loading the entire `_index.json` into RAM. It lacks keyword filtering (e.g., "Find notes about 'AI' created in 2025"), leading to broad, noisy results.
- **SOTA 2026**: LanceDB uses a disk-first, SIMD-accelerated format. It handles 100k+ vectors with sub-10ms latency while keeping RAM usage near zero.
- **Actionable Insight**: Replace `embed_store.py` logic with LanceDB. This enables **Hybrid Search**—combining the precision of BM25 keywords with the depth of semantic vectors.

### 12. Modern SLM Embeddings (EmbeddingGemma-300M)
- **Problem**: `all-MiniLM-L6-v2` (22M params) is a 2021-era model. It struggles with technical nuances (medical/coding) and multilingual scripts (Hindi/Devanagari).
- **SOTA 2026**: Small Language Model (SLM) embeddings like **Gemma-300M** or **Qwen3-0.6B** provide "Frontier-level" recall (MTEB 65+) while running comfortably on M4 CPUs. 
- **Actionable Insight**: Upgrade the embedding model in `config.py`. The ~45ms latency increase is negligible compared to the 15-20% boost in retrieval accuracy for complex technical domains.

### 13. NLI-Based Semantic Logic Gate
- **Problem**: Cosine similarity (vector distance) only measures "word overlap." It cannot detect a logical contradiction (e.g., if the AI swaps "increased" with "decreased").
- **SOTA 2026**: High-veracity pipelines use **Natural Language Inference (NLI)** Cross-Encoders (e.g., `deberta-v3-small-nli`) as a final audit step.
- **Actionable Insight**: Implement an "Audit-on-Trigger" in `reflector_tier1.py`. If a note's similarity score is low (<0.90), run an NLI pass. If the model detects a `Contradiction` between the source quote and the atomic claim, the note is automatically flagged with `needs_review: true`.

---

## 2026 SOTA Audit — Graph Health & Topology

### 14. Star-Hub Topology & Link Density (L)
- **Problem**: As vaults grow, they often become "hairballs" (over-connected noise) or "sparse clouds" (isolated silos).
- **SOTA 2026**: A healthy Zettelkasten demonstrates a **Star-Hub Topology**. MOCs (Maps of Content) act as "Gravity Wells" (hubs), while atomic notes form dense clusters around them. 
- **Benchmarks**:
    - **Link Density (L)**: 2.5 - 4.5 links per note.
    - **Cluster Ratio**: 1:20 to 1:100 (one major hub per N notes).
    - **Shannon Entropy (H)**: H > 1.8 (indicates high variety and non-obvious connections).
- **Actionable Insight**: Periodically run a `Link Entropy` scan. If $H < 1.5$, trigger an **Autonomous Linking Pass** to bridge "Peripheral Stars" (isolated note clusters) back to the central core.

### 15. Semantic vs. Typed Logic Edges ✅ DONE
- **Problem**: Generic `[[wikilinks]]` only show *that* two notes are related, not *how*.
- **SOTA 2026**: Moving from a "Knowledge Map" to a "Logic Engine" requires **Typed Relationships** (e.g., *contradicts*, *proves*, *analogous_to*).
- **Actionable Insight**: Use the **VaultLink Pydantic model** to enforce relationship types in the `## Connections` section. This enables the **graph_search** tool to perform multi-hop logical reasoning (e.g., "Find all evidence that contradicts Note A").

---

# PKM Pipeline Optimization Plan
*Created: 2026-03-22 | Status: REVISED — Most phases scrapped*

## Constraints (Reality Check)
- Droplet: 1GB RAM, 541MB available after FastAPI + yt-dlp
- GLiNER, sentence-transformers, BERTopic all require torch → ~200-400MB RAM → OOM risk
- Gemini Flash has 1M token window → LexRank compression loses knowledge for negligible cost savings
- SemHash dedup risks false positives on related-but-distinct content → knowledge loss
- **Conclusion: Keep pipeline high-fidelity. Let Gemini do the heavy lifting.**

## What's Actually Worth Implementing
Only pure-Python, zero-model-download approaches are safe on this droplet:
- **BM25 (`rank-bm25`)**: pure Python, negligible RAM — can augment Gemini embeddings as hybrid reranker
- **URL dedup**: already implemented
- **Gemini embeddings**: keep — better conceptual linking than any local alternative

## Goal
Maintain high-fidelity knowledge capture. Cost optimization is not a priority — Gemini Flash is cheap enough.

## Pipeline Flow (Current)
```
URL
 └─ extractors.py::extract_source()       → SourceMeta
     └─ server.py::_process_job()          → embed_store.query_relevant() [Gemini embedding API]
         └─ processor.py::process_source() → _build_user_prompt() → Gemini LLM call
             └─ vault.py::build_vault_files()
                 └─ vault_writer.py        → writes .md, backlinks, Anki
```

## Pipeline Flow (Optimized)
```
URL
 └─ extract_source()
     └─ [DEDUP GATE]         SemHash/Model2Vec — skip LLM if duplicate (sim > 0.92)
         └─ [COMPRESS]       sumy LexRank — shrink transcript >16k chars to top 25%
             └─ [NER HINTS]  GLiNER — extract entities locally, pass as hints to Gemini
                 └─ Gemini LLM call (smaller input, better guided)
                     └─ [MOC VALIDATE]  BERTopic — enforce consistent MOC taxonomy
                         └─ vault_writer.py
```

Vault search (EmbedStore) replaced by local BM25 + SBERT + RRF — runs in background, no API cost.

---

## Implementation Phases (Ordered by ROI)

### Phase 1 — Transcript Compression ✅ DONE ⭐ Highest ROI
**Impact:** 50-70% token reduction on long-form content (podcasts, 60min+ videos)
**Complexity:** Low

**New file:** `compressor.py`
- Function: `compress_transcript(text, target_ratio=0.25, threshold_chars=16000) -> tuple[str, bool]`
- Uses: `sumy` LexRank extractive summarization
- Extracts top 25% most central sentences, reassembled in original order
- Returns `(compressed_text, was_compressed)`
- Full try/except fallback: returns `(original_text, False)` if sumy unavailable

**Modify:** `processor.py::_build_user_prompt()`
- Call compressor on `meta.transcript` before assembling prompt
- Add compression note to prompt if `was_compressed=True`

**Modify:** `config.py`
```python
compress_enabled: bool = True
compress_threshold_chars: int = 16000   # ~4k tokens
compress_target_ratio: float = 0.25
```

**Install:** `pip install sumy nltk` + `python -m nltk.downloader punkt punkt_tab`

---

### Phase 1b — Transcript Quality Scoring — NOT DONE
**Impact:** Auto-sets needs_review, enables smarter compression
**Complexity:** Low (no new dependencies)

**Modify:** `extractors.py`
- Add `_score_transcript(text) -> dict` helper (TTR, punctuation density, repetition ratio)
- Add `transcript_quality: dict = field(default_factory=dict)` to `SourceMeta`
- Add `related_captures: list = field(default_factory=list)` to `SourceMeta`
- Capture `is_generated` flag from youtube-transcript-api (currently discarded)

**Modify:** `processor.py::_build_user_prompt()`
- Use `meta.transcript_quality` to adjust compression ratio
- If quality is poor: add instruction to Gemini to set `needs_review=true`

---

### Phase 2 — Semantic Dedup Gate — NOT DONE ⭐ Second Highest ROI
**Impact:** 100% cost skip on duplicate captures
**Complexity:** Medium

**New file:** `dedup_gate.py`
- Class `DedupGate`:
  - Stores note vectors in SQLite (`~/.nexus_dedup.db`)
  - Uses Model2Vec `M2V-base-output` (~30MB, CPU, ~10ms inference)
  - `check(meta) -> ("skip"|"flag"|"proceed", [matched_titles])`
  - `index_note(title, embed_text)` — call after successful capture

**Thresholds:**
- `sim > 0.92` → skip LLM entirely, return `already_captured`
- `0.75-0.92` → flag as related, proceed but set `meta.related_captures`
- `< 0.75` → proceed normally

**Modify:** `server.py`
- Instantiate `_dedup_gate` in `main()`
- In `_process_job()`: call `_dedup_gate.check(meta)` before `process_source()`
- After completion: call `_dedup_gate.index_note()` for each new note

**Modify:** `config.py`
```python
dedup_semantic_enabled: bool = True
dedup_skip_threshold: float = 0.92
dedup_flag_threshold: float = 0.75
```

**Install:** `pip install model2vec numpy`

---

### Phase 3 — Hybrid Vault Search (Replace EmbedStore) — NOT DONE
**Impact:** Eliminates Gemini embedding API calls entirely; better recall (+15-30%)
**Complexity:** Medium

**New file:** `hybrid_store.py`
- Class `HybridStore` — drop-in replacement for `EmbedStore`
- Same public API: `count()`, `upsert_notes()`, `query_relevant()`
- Internals: BM25 (`rank-bm25`) + SBERT (`all-MiniLM-L6-v2`, ~90MB) + RRF fusion
- Optional reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` on top-20 (opt-in)
- SQLite storage with numpy binary vectors (4x smaller than JSON)
- Brute-force cosine (numpy matmul) — faster than FAISS for <5000 notes

**Modify:** `server.py::main()`
```python
if config.use_hybrid_store:
    from hybrid_store import HybridStore
    _embed_store = HybridStore()
else:
    from embed_store import EmbedStore
    _embed_store = EmbedStore(api_key=_cfg.gemini_api_key)
```

**Modify:** `config.py`
```python
use_hybrid_store: bool = True
hybrid_reranker_enabled: bool = False   # opt-in, adds ~500ms
```

**Install:** `pip install rank-bm25 sentence-transformers torch`
> On CPU-only server: `pip install torch --index-url https://download.pytorch.org/whl/cpu`

---

### Phase 4 — GLiNER Entity Pre-extraction — NOT DONE
**Impact:** Better KG consistency; reduces Gemini entity discovery load
**Complexity:** Medium

**New file:** `entity_extractor.py`
- Function: `extract_entities(text, entity_types, max_chars=10000) -> dict[str, list[str]]`
- Uses GLiNER `urchade/gliner_medium-v2.1` (~300MB)
- Lazy singleton load (loaded once on first call)
- Returns `{"person": [...], "technology": [...], "concept": [...]}`

**Modify:** `processor.py::_build_user_prompt()`
- Inject entity hints section between CONTENT and VAULT INDEX:
```
ENTITY HINTS (pre-extracted, verify and normalize):
- Technologies/Methods: spaCy, GLiNER, BM25
- Key Concepts: zero-shot NER, relation extraction
- People/Orgs: Urchade Zaratiana, Hugging Face
Use these as the basis for knowledge_graph.entities.
```

**Modify:** `config.py`
```python
gliner_enabled: bool = True
gliner_model: str = "urchade/gliner_medium-v2.1"
gliner_entity_types: list = ["person", "organization", "concept", "technology", "methodology"]
```

**Install:** `pip install gliner`

---

### Phase 5 — BERTopic MOC Validation — NOT DONE
**Impact:** Consistent MOC taxonomy across captures (quality, not cost)
**Complexity:** High

**New file:** `moc_classifier.py`
- Function: `validate_mocs(notes, llm_mocs, existing_mocs) -> list[str]`
- Uses BERTopic zero-shot with existing MOC names as seeds
- Reuses `all-MiniLM-L6-v2` embeddings from Phase 3 (no extra download)
- Only overrides LLM suggestions when BERTopic confidence > 0.65
- Never invents new MOC names — only maps to existing ones

**Modify:** `processor.py::process_source()`
- After parsing LLM JSON: call `moc_classifier.validate_mocs()`
- Pass `existing_mocs` list (from `_vault_index` moc entries)

**Modify:** `config.py`
```python
bertopic_moc_enabled: bool = True
bertopic_min_similarity: float = 0.65
```

**Install:** `pip install bertopic` (reuses sentence-transformers from Phase 3)

---

## Files Created/Modified Summary

| File | Action | Phase | Status |
|------|--------|-------|--------|
| `compressor.py` | CREATE | 1 | ✅ DONE |
| `dedup_gate.py` | CREATE | 2 | NOT DONE |
| `hybrid_store.py` | CREATE | 3 | NOT DONE |
| `entity_extractor.py` | CREATE | 4 | NOT DONE |
| `moc_classifier.py` | CREATE | 5 | NOT DONE |
| `config.py` | MODIFY — add all new config fields | 1-5 | PARTIAL (Phase 1 fields only) |
| `processor.py` | MODIFY — `_build_user_prompt()`, `process_source()` | 1,4,5 | PARTIAL (Phase 1 done; 4,5 not done) |
| `extractors.py` | MODIFY — `SourceMeta`, `_score_transcript()`, `is_generated` | 1b | NOT DONE |
| `server.py` | MODIFY — `main()`, `_process_job()` | 2,3 | NOT DONE |

**`embed_store.py`, `vault.py`, `vault_writer.py`** — untouched.

---

## All Dependencies

```bash
# Phase 1 — Compression
pip install sumy nltk
python -m nltk.downloader punkt punkt_tab

# Phase 2 — Dedup
pip install model2vec numpy

# Phase 3 — Hybrid Store
pip install rank-bm25 sentence-transformers
pip install torch --index-url https://download.pytorch.org/whl/cpu  # CPU server

# Phase 4 — Entity extraction
pip install gliner

# Phase 5 — MOC classification
pip install bertopic
```

**First-run model downloads (cached after):**
- Model2Vec M2V-base-output: ~30MB
- all-MiniLM-L6-v2: ~90MB
- GLiNER gliner_medium-v2.1: ~300MB
- ms-marco reranker (optional): ~85MB
- BERTopic: reuses all-MiniLM — no extra download
- **Total: ~420-500MB**

---

## Token Cost Reduction (Realistic Estimates)

| Phase | When Active | Savings |
|-------|------------|---------|
| Compression | Podcast/video > 16k chars | 50-70% of transcript tokens |
| Dedup Gate | Re-capture of same content | 100% (full skip) |
| Hybrid Store | Always | Eliminates embedding API cost |
| GLiNER | Transcripts > 2k chars | +100-300 tokens (quality tradeoff) |
| BERTopic | Post-LLM | 0 (quality only) |

**Example: 90-min technical podcast**
- Current: ~$0.0045 (est. 30k input tokens at $0.15/M)
- After Phase 1: ~$0.0015
- If re-capture: $0.0000

---

## Fallback Strategy (All Steps Degrade Gracefully)

Every new module wraps its logic in `try/except`. If a dependency is missing or a model fails to download:
- Compressor → returns original transcript unchanged
- Dedup Gate → returns "proceed" (existing URL dedup still works)
- Hybrid Store → falls back to original EmbedStore
- GLiNER → entity hints section omitted from prompt
- BERTopic → LLM's MOC suggestions used unchanged

The pipeline **never breaks** due to an optimization layer failing.

---

# Hallucination Mitigation + Cost Optimization Research
*Scraped 2026-03-23*

## Current Optimal Architecture — PARTIAL (applies to Instagram/web via Gemini; YouTube uses yt-dlp transcripts so video token cost is N/A there)
- Pass 1: Flash-Lite + MEDIA_RESOLUTION_LOW (66 tokens/frame) → detailed description
- Pass 2: Flash + text-only (Pass 1 description + captions) → notes, zero video tokens
- Cost: ~80% cheaper on video tokens vs sending video to both passes

## Future Upgrade: Single-Model Caching
Flash-Lite does NOT support explicit context caching (confirmed forum thread).
For a caching-based approach, both passes must use Flash:
- Upload video via File API → create explicit cache (90% discount = $0.03/1M vs $0.30/1M)
- Pass 1: Flash + cached video + LOW_res → description
- Pass 2: Flash + cached video OR text-only → notes
- Both passes at 90% discount; only worth it for 3+ passes or long videos

## Token Numbers (Gemini 2.5, confirmed official docs)
- MEDIA_RESOLUTION_LOW: 66 tokens/frame (~98 tokens/sec with audio)
- Default / full-res: 258 tokens/frame (~290 tokens/sec)
- Ratio: full-res is 3.9x more expensive
- LOW is sufficient for: action recognition, scene description, overlaid titles, talking-head
- HIGH needed for: dense fine-print text, logos <10% frame area

## Other Cost Levers (deferred)
- FPS reduction: default 1fps, can set 0.5fps for slow-motion/talking-head (halves video tokens)
- Batch API: 50% discount, async (minutes to hours latency)
- Implicit caching: automatic 75% discount on repeated same-URI calls (Flash 2.5 only)


## Hallucination Type

**Extrinsic overclaim hallucination** — real facts, not from this source.
Mechanism: model recognizes a named entity (e.g. "Ole Peters") → activates parametric memory cluster → conflates "what I know about this entity" with "what this source says."
Standard "only use the source" instructions don't reliably prevent this because the model *believes* it is grounded (the facts are correct).

The academic taxonomy (ACM Survey on Hallucinations) classifies this as:
- **Overclaim/Relation-error hallucination**: correct entity, wrong or unsupported relation
- Distinct from confabulation (made-up facts) — this is real knowledge leaking through entity recognition

## Techniques (ordered by impact, low cost first)

### 1. Tighten Pass 1 to verbatim/observational only ✅ DONE (free — prompt change)
Pass 1 must not infer or summarize. If it outputs "He discusses ergodicity, which relates to
time-average vs ensemble-average outcomes" — that inference carries latent parametric knowledge
into Pass 2. Pass 1 should be a transcript extractor only:
- Direct quotes from spoken audio (verbatim or near-verbatim)
- Text visible on screen (verbatim)
- What is shown/demonstrated — NOT what it implies or connects to

### 2. Named-entity explicit warning ✅ DONE (free — prompt change)
Add to system prompt, targeting the specific trigger:
> "Named entities you recognize from training knowledge are NOT automatically permissible.
> If you recognize a name or theory, only include what this source explicitly states about them.
> Do not attach theories, dates, or roles you associate with them from training."

### 3. Move grounding rules to END of prompt ✅ DONE (free — Gemini-specific)
Gemini attends more strongly to end of context window for long inputs. For video + facts + captions,
grounding constraints placed at the top get diluted. Place them at the end.

### 4. Citation-retraction pattern inside Pass 2 ✅ DONE (source_quote field with "NOT IN SOURCE" fallback + needs_review=true + Semantic Integrity Gate)
For each named person, date, theory name, statistic → model must cite the exact Pass 1 bullet
that supports it. If it can't, it removes the claim.
> "After drafting, review each claim. Find a direct quote from the source that supports it.
> If no supporting quote exists, remove that claim and mark the removal with []."
Source: Anthropic docs on reducing hallucinations; Chain-of-Verification (CoVe) arXiv:2309.11495

### 5. Chain-of-Verification (CoVe) as Pass 3 — NOT DONE (higher cost — extra LLM call)
Dropped hallucinated entities from 2.95 → 0.68 per response in the original paper.
Four steps:
1. Pass 2 generates draft notes
2. Separate call (WITHOUT draft visible) generates verification questions: "Does the video explicitly
   state that Bernoulli published in 1738?"
3. Answer those questions against Pass 1 facts + captions only (pure lookup)
4. Pass 2 is given (draft + Q&A pairs) and removes any claim where verification answer was "not found"

Key: Steps 2 and 3 must NOT have the draft in context — prevents confirmation bias.
Use Flash-Lite for steps 2 and 3.

### 6. Self-consistency check — NOT DONE (latency cost — multiple calls)
Run Pass 2 twice at temperature > 0. Claims that vary across runs are flagged as uncertain.
Claims appearing consistently are more likely grounded.

## Gemini-Specific Notes
- Gemini models are explicitly optimized against the FACTS Grounding benchmark (Google DeepMind).
  This means they ARE responsive to well-structured grounding constraints — worth investing in prompt quality.
- "Focus-on-Vision" prompting reduces cross-modal hallucination:
  > "Base your analysis EXCLUSIVELY on what is visually and audibly present in this video.
  > Do not draw on knowledge associated with any person, term, or concept you recognize."

## Key Papers
- [Extrinsic Hallucinations in LLMs — Lilian Weng (2024)](https://lilianweng.github.io/posts/2024-07-07-hallucination/)
- [Chain-of-Verification Reduces Hallucination — arXiv:2309.11495, ACL Findings 2024](https://arxiv.org/abs/2309.11495)
- [Mitigating Hallucination in LLMs: Survey on RAG, Reasoning, Agentic Systems — arXiv:2510.24476](https://arxiv.org/abs/2510.24476)
- [FACTS Grounding Benchmark — Google DeepMind](https://deepmind.google/blog/facts-grounding-a-new-benchmark-for-evaluating-the-factuality-of-large-language-models/)
- [Hallucination of Multimodal LLMs: A Survey — arXiv:2404.18930](https://arxiv.org/abs/2404.18930)
- [Deterministic Quoting — Matt Yeung](https://mattyyeung.github.io/deterministic-quoting)
- [Reduce Hallucinations — Anthropic Docs](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)
- [Mitigating LLM Hallucinations Using a Multi-Agent Framework — MDPI 2025](https://www.mdpi.com/2078-2489/16/7/517)
- [Awesome MLLM Hallucination — GitHub](https://github.com/showlab/Awesome-MLLM-Hallucination)

---

# Nexus v5.0 Strategic Roadmap & Cost Analysis
*Scraped: June 2026 — evaluating force multipliers for a Self-Evolving Knowledge Agent*

## Proposed Strategic Upgrades

### 1. The "Vision-First" Ingestion Upgrade (Docling/Marker)
- **Concept:** Shift from text-based PDF parsing to Document Layout Analysis (DLA) using vision-augmented extraction.
- **Cost Impact:** Low/Moderate. Marginal increase in Stage 1 tokens (due to layout metadata), but minimal local compute overhead as preprocessing happens before synthesis. Highly cost-efficient for the quality gain.

### 2. Transition from Vector RAG to "GraphRAG" (Structural Memory)
- **Concept:** Periodically run community detection to build narrative "Structure Notes" (MOCs) based on vault topology, moving beyond simple similarity search.
- **Cost Impact:** Most Expensive. High cloud token burn if using Gemini for summarization, or massive sustained local compute/thermal load if using Qwen3 locally for graph synthesis.

### 3. The "Critique & Conflict" Agent (Integrity Layer)
- **Concept:** A multi-agent debate or reflexion cycle to flag conceptual contradictions before a note is finalized.
- **Cost Impact:** Highly Expensive. Doubles the local compute budget per capture by running a secondary "Critique Pass" LLM generation. High thermal impact.

### 4. "Ambient & Low-Friction" Capture (Voice-to-Atomic) ✅ DONE
- **Concept:** Leverage local Whisper or Siri Intents to capture fleeting thoughts as "Stage 0" seedlings, bypassing Stage 1.
- **Cost Impact:** Least Expensive. Lowest local compute footprint and zero cloud cost. Highest ROI upgrade.

### 5. Automated "Dreaming" (Autonomous Maintenance)
- **Concept:** A scheduled background agent that runs during thermal downtime (e.g., 3 AM) to discover serendipitous links between disconnected notes.
- **Cost Impact:** Moderately Expensive. Requires sustained local compute but operates during idle time, mitigating thermal bottlenecks.

## Cost-Weighted Strategy Recommendation
For the highest immediate impact with the lowest resource burn on fanless M4 hardware, prioritize **Ambient Capture** (expands input funnel at zero cost) and **Vision-First Ingestion** (massive quality leap for minimal token overhead). GraphRAG and Critique Agents should be treated as major architectural shifts requiring prompt caching and heavy thermal management.

---

# State of the Art (SOTA) — June 2026
*Scraped: June 2026 — defining the frontier of Autonomous PKM and Agentic Memory*

## 1. The "Agentic" Shift in Knowledge Retrieval
The industry has moved from static RAG to **Agentic GraphRAG**. Systems no longer just search; they reason about *how* to search.
- **SOTA Pattern:** **Agentic GraphRAG (Neo4j/Graphiti)**. Agents use MCTS (Monte Carlo Tree Search) to navigate vault edges step-by-step, deciding whether to follow a link or fallback to vector search.
- **Actionable Insight:** The Nexus `graph_search` tool is a precursor to this. Future updates should include **"Failure-Aware Routing"**—if a multi-hop traversal returns low-density results, the agent automatically switches to a global community summary.

## 2. Hardware Frontier: Apple Silicon M4 Family
The M4 generation (June 2026) has solidified local-first AI via extreme memory bandwidth.
- **Performance Tiers:**
    - **M4 Pro (273 GB/s):** Runs 35B models (MoE) at ~15-20 t/s.
    - **M4 Max (546 GB/s):** The definitive AI workstation; runs 70B models at ~30 t/s.
- **SOTA Framework:** **MLX (native)** remains 20-90% faster than llama.cpp for single-stream generation.
- **Actionable Insight:** Nexus's "Fast vs. Quality" path should be calibrated to bandwidth. On M4 Max, the "Fast" path can be a 14B model, and "Quality" can be a 70B (Q4_K_M) without latency penalty.

## 3. Maturation of MCP for Obsidian
MCP has shifted from external scripts to **Native/Headless API** integrations.
- **SOTA Tool:** **MCPVault (`@bitbonsai/mcpvault`)**. A headless, filesystem-direct MCP server that preserves Obsidian frontmatter without the app running.
- **SOTA Tool:** **Obsidian Local REST API 4.0**. Includes a built-in MCP server that triggers Obsidian commands and workspace management directly from AI agents.
- **Actionable Insight:** Consider migrating `mcp_nexus.py` to a filesystem-direct model like MCPVault to enable "Nexus Research" even when Obsidian is closed.

## 4. Cost-Optimized Indexing: LazyGraphRAG
The cost of building a knowledge graph has been slashed by moving from "Full Upfront Indexing" to **Lazy Execution**.
- **SOTA Pattern:** **LazyGraphRAG (Microsoft)**. Skip expensive upfront community summarization. Build a lightweight graph of nodes/edges during ingestion; only perform heavy LLM summarization on the specific subgraph activated at query time.
- **Actionable Insight:** Nexus should adopt this for v5.0. Do not summarize MOCs at 3 AM "Dreaming" time; instead, build the links at 3 AM and summarize the "MOC Narrative" on-demand when the user queries that topic.

## 5. Vision-Augmented PKM (Multimodal Grounding)
SOTA ingestion now treats visual layout as a primary semantic feature.
- **SOTA Tool:** **IBM Docling / Marker**. These reconstruct the semantic structure (headers, tables, captions) into Vision-Augmented Markdown.
- **Actionable Insight:** This validates the Nexus v5.0 "Vision-First" pillar. Text-only PDF extraction is now considered "Legacy."


