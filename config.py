"""
Configuration for Obsidian Capture Pipeline v2.
Author: Adith K B

Set env vars or edit defaults below.
"""
import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# OS-illegal / Obsidian-reserved filename characters. Shared by vault.slugify()
# and embed_store._slugify_title() — kept here since both files eventually
# import config.py, so this has zero circular-import risk (unlike the two
# slugify *functions* themselves, which must stay duplicated).
ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*#\^\[\]\x00-\x1f]')


@dataclass
class Config:
    # ── Vault ──────────────────────────────────────────────────
    vault_path: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "OBSIDIAN_VAULT",
                "/Users/adithkb/Library/Mobile Documents/iCloud~md~obsidian/Documents/Nexus",
            )
        ).expanduser()
    )
    notes_folder: str = "Captures"
    source_folder: str = "Sources"
    moc_folder: str = "MOCs"
    fleeting_folder: str = "Fleeting"

    # ── LLM Provider ──────────────────────────────────────────
    # Supported: "gemini" (Cloud), "ollama" (Local), "mlx" (Local SOTA)
    # 2026 Value Stack: Ultimate Sovereign
    #   Step 1 (Relay): Gemini 3.1 Flash-Lite (Cloud Sensor)
    #   Step 2 (Crystallizer): Qwen 3-14B via MLX-Textgen
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama")
    )

    # ── Gemini (Stage 1: The Relay) ───────────────────────────
    # Tier 1 Paid API — Flash-Lite with 90% Context Caching.
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
    # Stage 2 synthesis, used only when llm_provider == "gemini" (full-cloud capture
    # path — bypasses local Ollama/MLX entirely). Deliberately a stronger tier than
    # gemini_model above: this replaces 14B-Qwen-quality note synthesis, not extraction.
    gemini_synthesis_model: str = os.getenv("GEMINI_SYNTHESIS_MODEL", "gemini-3.7-flash")
    # Escalation tier: used only for content that demonstrably benefits (PDFs,
    # long-form video, technically dense sources) or when the cheap tier's
    # output fails to parse — see _select_gemini_synthesis_model() in processor.py.
    gemini_synthesis_model_quality: str = os.getenv("GEMINI_SYNTHESIS_MODEL_QUALITY", "gemini-3.1-pro-preview")

    # ── Gemini cost tracker ────────────────────────────────────
    # $ per 1M tokens, keyed by pipeline role (not an exact per-model rate card —
    # these are estimates calibrated against the one real measured benchmark we
    # have, docs/architecture.md's "~$0.022/capture, cheap tier, 6,482 in/4,690
    # out incl. thinking" test. The relay's cached/uncached input split IS a
    # documented real rate (see processor.py's context-cache comment); the rest
    # are directional. Reconcile periodically against aistudio.google.com/spend
    # and correct via env vars if your actual billing differs — every
    # _gemini_generate() call logs to cost_log_path regardless of accuracy here,
    # so historical entries can be re-priced later if these defaults were off.
    gemini_relay_price_in: float = float(os.getenv("GEMINI_RELAY_PRICE_IN", "0.025"))            # cached
    gemini_relay_price_in_uncached: float = float(os.getenv("GEMINI_RELAY_PRICE_IN_UNCACHED", "0.25"))
    gemini_relay_price_out: float = float(os.getenv("GEMINI_RELAY_PRICE_OUT", "0.30"))
    gemini_cheap_price_in: float = float(os.getenv("GEMINI_CHEAP_PRICE_IN", "0.15"))
    gemini_cheap_price_out: float = float(os.getenv("GEMINI_CHEAP_PRICE_OUT", "4.50"))
    gemini_quality_price_in: float = float(os.getenv("GEMINI_QUALITY_PRICE_IN", "1.25"))
    gemini_quality_price_out: float = float(os.getenv("GEMINI_QUALITY_PRICE_OUT", "10.00"))
    # JSONL append-log, one line per Gemini call — read by GET /costs in server.py.
    cost_log_path: str = os.getenv("COST_LOG_PATH", str(Path.home() / ".nexus_cost_log.jsonl"))

    # ── Groq (explicit text-only fallback) ────────────────────
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # OpenAI-compatible (DeepSeek, etc.)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    # ── Local Inference (Stage 2: Crystallizer) ───────────────
    # MLX-Textgen is the preferred engine for M4 (Logit Masking support)
    mlx_base_url: str = os.getenv("MLX_BASE_URL", "http://localhost:8080/v1")
    mlx_model: str = os.getenv("MLX_MODEL", "mlx-community/Qwen3-14B-4bit")
    # Fast model for short-form sources (web articles, short YouTube videos)
    # Qwen3.5-9B: ~82 tok/s vs ~48 tok/s for 14B; 20% better reasoning than Qwen3-8B.
    # Qwen3.5-27B/35B excluded — exceed 16GB M4 memory limit (swap → timeout).
    mlx_model_fast: str = os.getenv("MLX_MODEL_FAST", "mlx-community/Qwen3.5-9B-MLX-4bit")
    
    # Ollama (primary synthesis engine — MLX reserved for when GPU is not thermally constrained)
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3:14b")        # quality path (PDF, long video)
    ollama_model_fast: str = os.getenv("OLLAMA_MODEL_FAST", "qwen3.5:9b")  # fast path (web, short video)
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:4b")
    # Seconds to keep Ollama models resident after last use.
    # 300 = stays warm for 5 min of scrolling; 0 = evict immediately (use on 1 GB VPS).
    ollama_keep_alive: int = int(os.getenv("OLLAMA_KEEP_ALIVE", "300"))
    # Required Ollama server env vars (set before `ollama serve` on Mac):
    #   OLLAMA_MAX_LOADED_MODELS=2   — hold both embedding + synthesis models simultaneously
    #   OLLAMA_NUM_PARALLEL=1        — prevent concurrent requests from spiking RAM on 16 GB

    # ── Note Generation (2026 SOTA Limits) ────────────────────
    max_notes: int = 10
    min_notes: int = 3
    alias_count: int = 5
    temperature: float = 1.0
    default_tags: list = field(
        default_factory=lambda: ["capture", "auto-generated"]
    )

    # ── PDF Handling ──────────────────────────────────────────
    pdf_max_pages: int = 40          # hard cap on pages extracted
    pdf_chunk_chars: int = 60000     # max chars sent to LLM

    # ── Object Storage (Mac-Off bridge) ───────────────────────
    # Server uploads completed notes here; iPhone Remotely Save plugin pulls them.
    # Leave blank to disable upload (falls back to Mac vault_writer.py only).
    # Works with any S3-compatible provider — DigitalOcean Spaces, Oracle Object
    # Storage, Cloudflare R2, etc. — via SPACES_ENDPOINT_URL.
    #
    # Setup (DigitalOcean Spaces):
    #   1. Create a Space in your DO account (e.g. region=nyc3, name=nexus-vault)
    #   2. Generate two API keys under API → Spaces Keys:
    #      - Server key: Read+Write  → set below
    #      - Phone key:  Read-only   → use in Remotely Save on iPhone
    #   3. Leave SPACES_ENDPOINT_URL blank — defaults to
    #      https://<SPACES_REGION>.digitaloceanspaces.com
    #
    # Setup (Oracle Object Storage, S3-compatible API):
    #   1. Create a bucket in the OCI console (Object Storage → Buckets)
    #   2. Generate a Customer Secret Key under Identity → Users → <you> →
    #      Customer Secret Keys → set as SPACES_ACCESS_KEY / SPACES_SECRET_KEY
    #   3. Set SPACES_ENDPOINT_URL = https://<namespace>.compat.objectstorage.<region>.oraclecloud.com
    #      (namespace is shown at the top of the Buckets page)
    #
    # Remotely Save settings (iPhone):
    #   Endpoint  = SPACES_ENDPOINT_URL (or the DO default above if blank)
    #   Bucket    = <SPACES_BUCKET>
    #   Remote Base Dir = _incoming   ← maps _incoming/ in Spaces to vault root
    spaces_region: str = os.getenv("SPACES_REGION", "nyc3")  # e.g. nyc3, sfo3, ams3
    spaces_access_key: str = os.getenv("SPACES_ACCESS_KEY", "")
    spaces_secret_key: str = os.getenv("SPACES_SECRET_KEY", "")
    spaces_bucket: str = os.getenv("SPACES_BUCKET", "nexus-vault")
    spaces_prefix: str = os.getenv("SPACES_PREFIX", "incoming")  # maps to vault root
    spaces_endpoint_url: str = os.getenv("SPACES_ENDPOINT_URL", "")  # blank = DO Spaces default

    # ── Web Extraction ────────────────────────────────────────
    jina_api_key: str = os.getenv("JINA_API_KEY", "")  # optional, free tier works
    jina_timeout: int = 20

    # ── Apify (Instagram video download) ──────────────────────
    # Free tier: ~1,900 reels/month at no cost. Get key at apify.com.
    # If blank, Instagram extraction falls back to yt-dlp (text-only on VPS).
    apify_api_key: str = os.getenv("APIFY_API_KEY", "")

    # ── Instagram DM Capture Bot ───────────────────────────────
    # Share a reel to a dedicated bot Instagram account instead of using the
    # iOS Shortcut — see docs/architecture.md §2g. Setup: "Instagram API with
    # Instagram Login" at developers.facebook.com (no linked Facebook Page
    # needed), permission instagram_business_manage_messages.
    meta_app_secret: str = os.getenv("META_APP_SECRET", "")
    instagram_webhook_verify_token: str = os.getenv("INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "")
    instagram_access_token: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    instagram_bot_user_id: str = os.getenv("INSTAGRAM_BOT_USER_ID", "")
    # Webhook signature proves Meta sent the request, not that YOU sent the
    # share — set this to your personal IGSID (surfaced in the first real
    # webhook payload's sender.id) once known. Blank = accept from anyone,
    # fine only during initial payload-shape calibration.
    instagram_allowed_sender_id: str = os.getenv("INSTAGRAM_ALLOWED_SENDER_ID", "")
    # Phase 2 — off by default; core capture works with zero DM reply.
    instagram_ack_reply_enabled: bool = os.getenv("INSTAGRAM_ACK_REPLY_ENABLED", "false").lower() == "true"

    # ── Semantic Dedup Gate (Model2Vec potion-base-2M, 8 MB) ─
    dedup_enabled: bool = True
    dedup_skip_threshold: float = 0.92   # sim >= this → skip (duplicate content)
    dedup_flag_threshold: float = 0.75   # sim >= this → flag (related content)

    # ── PDF Extraction tiers ──────────────────────────────────
    # Tier 0: Marker (VLM layout detection, ~300 MB one-time download)
    # Tier 1: pymupdf4llm (fast, already installed)
    # Tier 2: plain PyMuPDF text (fallback)
    marker_enabled: bool = field(
        default_factory=lambda: os.getenv("MARKER_ENABLED", "true").lower() != "false"
    )

    # ── Transcript Compression ────────────────────────────────
    compress_enabled: bool = field(
        default_factory=lambda: os.getenv("COMPRESS_ENABLED", "true").lower() != "false"
    )
    compress_threshold_chars: int = 16_000   # chars; ~4k tokens
    compress_target_ratio: float = 0.25      # keep top 25% of sentences

    def _dir(self, folder: str) -> Path:
        d = self.vault_path / folder
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_notes_dir(self) -> Path:
        return self._dir(self.notes_folder)

    def get_source_dir(self) -> Path:
        return self._dir(self.source_folder)

    def get_moc_dir(self) -> Path:
        return self._dir(self.moc_folder)

    def get_fleeting_dir(self) -> Path:
        return self._dir(self.fleeting_folder)
