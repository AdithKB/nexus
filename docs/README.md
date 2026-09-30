# Nexus: Sovereign PKM Pipeline

**YouTube · Web · PDF · Instagram · Voice · Course Documents → High-Fidelity Zettelkasten Notes**

Nexus implements a "Passive Sovereign" model: a cloud LLM (Gemini) acts as a high-speed multimodal sensor (Stage 1 — extraction), while all reasoning, synthesis, and long-term memory run on local Apple Silicon via Qwen3 (Stage 2 — the Crystallizer). No user data is retained by cloud providers beyond the duration of a single capture.

**For the full system design — pipeline stages, model routing, the Dreamer Agent, dedup gate, vector store, MCP tools, atomic note schema — see [`architecture.md`](architecture.md). This file is intentionally just a quick-start pointer; architecture.md is the source of truth and is kept current, this file is not.**

---

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
brew install yt-dlp
```

### 2. Configure

Copy `.env.example` to `.env` and fill in your keys (Gemini, Jina, object storage, etc.) — see `config.py` for every field and its default. Object storage is any S3-compatible provider (Oracle Object Storage, DigitalOcean Spaces, Cloudflare R2, ...) via `SPACES_ENDPOINT_URL`.

### 3. Start local inference

```bash
ollama serve
ollama pull qwen3:14b            # quality path
ollama pull qwen3.5:9b           # fast path
ollama pull qwen3-embedding:4b   # semantic search
```

### 4. Start the Crystallizer daemon

```bash
python watcher.py
# or via LaunchAgent: deploy/stealth_crystallizer.sh
```

### 5. Capture something

```bash
python capture.py --url "https://youtube.com/watch?v=..."
python capture.py --url "./paper.pdf" --notes 10
python capture.py --batch urls.txt
```

Or run the full pipeline as a webhook server (`python server.py`) for the iOS Shortcut / remote capture flow — see `deploy/` for the relay server setup (Caddy + systemd unit + LaunchAgents). Currently hosted on Oracle Cloud's Always Free tier; `deploy/oracle_setup.sh` bootstraps a fresh Ubuntu VM end to end.

---

## Project Structure

```
AgenticPKMApp/
├── capture.py, watcher.py, processor.py, extractors.py   # core pipeline
├── vault.py, vault_writer.py, embed_store.py              # vault + storage
├── dedup_gate.py, dreamer.py, compressor.py                # supporting agents
├── config.py, server.py, mcp_nexus.py, reflector_tier1.py
├── deploy/       # LaunchAgents, Caddyfile, relay server systemd unit + Oracle bootstrap script
├── docs/         # architecture.md (current), this file, research notes
├── scripts/      # vault maintenance CLIs (dedup, reindex)
├── shortcuts/    # iOS Shortcut builder (build_v6.py — current)
└── tools/        # QA scripts, one-off audits
```

---

**Author:** Adith K B · **License:** MIT
