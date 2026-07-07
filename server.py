"""
server.py — Webhook server for the Obsidian Capture Pipeline.
Author: Adith K B

Two modes:

  LOCAL mode  (return_files=false, default)
    Writes notes directly to the local vault on disk.

  CLOUD mode  (return_files=true, or iOS Shortcut detected)
    Async: POST /capture returns a job_id immediately.
    GET /result/{job_id} to poll for completed files.
    iOS Shortcut: POST → get job_id → wait → poll → save files.

Usage:
    python server.py                    # localhost:8000
    python server.py --host 0.0.0.0    # expose publicly (behind Caddy)
"""
import argparse
import asyncio
import json as _json
import re
import time
import uuid
from pathlib import Path
from typing import Optional

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from capture import _c, build_config, run_pipeline, validate_key
from embed_store import EmbedStore
from extractors import extract_source
from processor import process_source, run_relay_stage
from vault import build_vault_files


# ── App ───────────────────────────────────────────────────────


app = FastAPI(
    title="Obsidian Capture Pipeline",
    version="2.0",
    description="Headless capture endpoint — POST a URL, get Zettelkasten notes.",
)


# ── Long-Polling Signaling ───────────────────────────────────

# This event is 'set' whenever a new file or seedling is uploaded to Spaces.
# vault_writer.py polls /pending/wait which hangs on this event.
_seedling_available = asyncio.Event()


def _signal_seedling_available():
    """Wakes any long-polling clients hanging on /pending/wait."""
    _seedling_available.set()
    # Don't clear here — the /pending/wait endpoint clears it after responding,
    # ensuring the waiting coroutine always sees the event before it's cleared.


# ── Job persistence ───────────────────────────────────────────

_JOBS_PATH = Path.home() / ".nexus_jobs.json"
_INDEX_PATH = Path.home() / ".nexus_vault_index.json"
_MAX_PERSISTED_JOBS = 100  # keep last N completed jobs on disk


def _persist_jobs():
    """Write _jobs + _latest_job_id to disk. Keeps last _MAX_PERSISTED_JOBS."""
    try:
        # Only persist completed/failed jobs (not in-progress ones)
        to_save = {
            jid: job for jid, job in _jobs.items()
            if job.get("status") != "processing"
        }
        # Trim to last N by keeping insertion order (Python 3.7+)
        if len(to_save) > _MAX_PERSISTED_JOBS:
            excess = len(to_save) - _MAX_PERSISTED_JOBS
            to_save = dict(list(to_save.items())[excess:])
        _JOBS_PATH.write_text(
            _json.dumps({"latest_job_id": _latest_job_id, "jobs": to_save}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[PERSIST] Write failed: {e}", flush=True)


def _load_persisted_jobs():
    """Load jobs from disk into _jobs on startup."""
    global _jobs, _latest_job_id
    try:
        data = _json.loads(_JOBS_PATH.read_text(encoding="utf-8"))
        _jobs = data.get("jobs", {})
        _latest_job_id = data.get("latest_job_id")
        print(f"[PERSIST] Loaded {len(_jobs)} jobs, latest={_latest_job_id}", flush=True)
    except FileNotFoundError:
        pass  # first run
    except Exception as e:
        print(f"[PERSIST] Load failed: {e}", flush=True)


# ── Vault index persistence ───────────────────────────────────


def _persist_vault_index():
    """Write _vault_index to disk so it survives server restarts."""
    try:
        _INDEX_PATH.write_text(_json.dumps(_vault_index), encoding="utf-8")
    except Exception as e:
        print(f"[INDEX] Persist failed: {e}", flush=True)


def _load_vault_index():
    """Load vault index from disk on startup."""
    global _vault_index
    try:
        _vault_index = _json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
        print(f"[INDEX] Loaded {len(_vault_index)} notes from disk", flush=True)
    except FileNotFoundError:
        pass  # first run
    except Exception as e:
        print(f"[INDEX] Load failed: {e}", flush=True)


# ── DigitalOcean Spaces upload ────────────────────────────────


def _spaces_client(config):
    """Build a boto3 S3 client pointed at DigitalOcean Spaces."""
    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.spaces_region}.digitaloceanspaces.com",
        aws_access_key_id=config.spaces_access_key,
        aws_secret_access_key=config.spaces_secret_key,
        region_name=config.spaces_region,
    )


async def _upload_to_spaces(files: dict, config) -> int:
    """
    Upload completed vault files to DigitalOcean Spaces.

    Captures and Sources are uploaded directly (unique per job).
    MOCs use a per-filename asyncio lock to prevent concurrent overwrites —
    MOC content is idempotent (Dataview query) so the lock is belt-and-suspenders.

    Returns number of files uploaded. Silently skips if Spaces not configured.
    """
    if not _BOTO3_AVAILABLE:
        return 0
    if not all([config.spaces_access_key, config.spaces_secret_key, config.spaces_bucket]):
        return 0

    loop = asyncio.get_event_loop()

    def _put(client, spaces_key, content):
        client.put_object(
            Bucket=config.spaces_bucket,
            Key=spaces_key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )

    uploaded = 0
    s3 = _spaces_client(config)

    # Captures + Sources: unique per job, upload directly
    for key, subfolder in [("captures", "Captures"), ("sources", "Sources")]:
        for item in files.get(key, []):
            filename = item.get("filename", "")
            content  = item.get("content", "")
            if not filename:
                continue
            spaces_key = f"{config.spaces_prefix}/{subfolder}/{filename}"
            try:
                await loop.run_in_executor(None, _put, s3, spaces_key, content)
                uploaded += 1
            except (BotoCoreError, ClientError) as e:
                print(f"[SPACES] Upload failed for {spaces_key}: {e}", flush=True)

    # MOCs: serialise per filename to prevent concurrent overwrites
    for item in files.get("mocs", []):
        filename = item.get("filename", "")
        content  = item.get("content", "")
        if not filename:
            continue
        if filename not in _moc_locks:
            _moc_locks[filename] = asyncio.Lock()
        spaces_key = f"{config.spaces_prefix}/MOCs/{filename}"
        async with _moc_locks[filename]:
            try:
                await loop.run_in_executor(None, _put, s3, spaces_key, content)
                uploaded += 1
            except (BotoCoreError, ClientError) as e:
                print(f"[SPACES] Upload failed for {spaces_key}: {e}", flush=True)

    if uploaded > 0:
        print(
            f"[SPACES] Uploaded {uploaded} file(s) to "
            f"{config.spaces_bucket}/{config.spaces_prefix}/",
            flush=True,
        )
        _signal_seedling_available()

    return uploaded


async def _upload_review_video(video_path: str, source_id: str, config) -> None:
    """
    Upload a downloaded video to Spaces review/ prefix for quality checking.
    Deletes the local file after upload. vault_writer pulls it to ~/.nexus_review/
    and auto-deletes after 10 minutes.
    """
    if not _BOTO3_AVAILABLE:
        return
    if not all([config.spaces_access_key, config.spaces_secret_key, config.spaces_bucket]):
        Path(video_path).unlink(missing_ok=True)
        return

    import time as _time
    loop = asyncio.get_event_loop()
    filename = f"{source_id}.mp4"
    spaces_key = f"review/{filename}"

    def _put_binary():
        s3 = _spaces_client(config)
        with open(video_path, "rb") as fh:
            data = fh.read()
        s3.put_object(
            Bucket=config.spaces_bucket,
            Key=spaces_key,
            Body=data,
            ContentType="video/mp4",
            Metadata={"uploaded-at": str(int(_time.time()))},
        )
        return len(data)

    try:
        size = await loop.run_in_executor(None, _put_binary)
        print(f"[REVIEW] Uploaded {filename} ({size // 1024}KB) to review/", flush=True)
    except Exception as e:
        print(f"[REVIEW] Upload failed: {e}", flush=True)
    finally:
        Path(video_path).unlink(missing_ok=True)
        print(f"[REVIEW] Deleted local {video_path}", flush=True)


# ── In-memory job store ──────────────────────────────────────


_jobs: dict[str, dict] = {}  # job_id → {status, result, error}
_latest_job_id: str | None = None  # track latest job for /latest endpoint
_vault_index: list[dict] = []  # [{title, aliases}] pushed by vault_writer.py
_embed_store: EmbedStore | None = None  # semantic search index (lazy init)
_dedup_gate = None  # DedupGate — set in main()
_moc_locks: dict[str, asyncio.Lock] = {}  # per-MOC filename lock for safe concurrent uploads


# ── Request / Response models ─────────────────────────────────


class CaptureRequest(BaseModel):
    url: str
    provider: Optional[str] = None
    model: Optional[str] = None
    notes: int = 5
    force: bool = False
    vault: Optional[str] = None
    return_files: bool = False
    user_intent: Optional[str] = None
    relay: bool = False


# ── Background processing ────────────────────────────────────


async def _process_job(job_id: str, url: str, config, user_intent: str = ""):
    """
    STAGE 1 (CLOUD RELAY): 
    Run multimodal extraction via Gemini 3.1 Flash-Lite and save as a Seedling.
    """
    try:
        from processor import run_relay_stage

        print(f"[JOB {job_id}] RELAY: Starting Stage 1 for {url}", flush=True)
        meta = extract_source(url, config)

        # Semantic dedup — runs after extraction, before Gemini (saves API cost)
        if _dedup_gate and config.dedup_enabled and meta.transcript:
            verdict, matched = _dedup_gate.check(url, meta.transcript[:4000])
            if verdict == "skip":
                print(f"[JOB {job_id}] DEDUP: Duplicate content. Matched: {matched}", flush=True)
                _jobs[job_id] = {
                    "status": "completed",
                    "result": {"status": "duplicate", "matched": matched, "error": None},
                }
                _persist_jobs()
                return
            if verdict == "flag":
                meta.related_captures = matched
                print(f"[JOB {job_id}] DEDUP: Related content: {matched}", flush=True)

        # 1. Run Relay (Gemini Cloud)
        description = await run_relay_stage(meta, config)

        
        # 2. Build Seedling Package
        seedling = {
            "version": "4.0",
            "job_id": job_id,
            "url": url,
            "user_intent": user_intent,
            "timestamp": time.time(),
            "meta": {
                "title": meta.title,
                "author": meta.author,
                "source_type": meta.source_type,
                "duration_sec": meta.duration_sec,
                "transcript": meta.transcript, # Include raw transcript for local cross-ref
                "source_id": meta.source_id
            },
            "relay_description": description
        }

        # 3. Save Seedling locally
        seedling_path = Path.home() / f".nexus_seedlings/{job_id}.seedling.json"
        seedling_path.parent.mkdir(parents=True, exist_ok=True)
        seedling_text = _json.dumps(seedling, indent=2)
        seedling_path.write_text(seedling_text, encoding="utf-8")
        
        # 4. Upload to Spaces (incoming/) so vault_writer pulls it to Mac
        if _BOTO3_AVAILABLE and all([config.spaces_access_key, config.spaces_secret_key, config.spaces_bucket]):
            try:
                s3 = _spaces_client(config)
                # Save as a .json file in the incoming prefix
                s3_key = f"{config.spaces_prefix.rstrip('/')}/seedling_{job_id}.json"
                s3.put_object(
                    Bucket=config.spaces_bucket,
                    Key=s3_key,
                    Body=seedling_text,
                    ContentType="application/json",
                    ACL="private"
                )
                _signal_seedling_available()
                print(f"[JOB {job_id}] RELAY: Seedling uploaded to Spaces.", flush=True)
            except Exception as e:
                print(f"[JOB {job_id}] RELAY: Spaces upload failed: {e}", flush=True)

        print(f"[JOB {job_id}] RELAY: Stage 1 Complete.", flush=True)

        # Index for future dedup checks
        if _dedup_gate and meta.transcript:
            _dedup_gate.index(url, meta.transcript[:4000], title=meta.title or url)

        _jobs[job_id] = {
            "status": "completed",
            "result": {
                "status": "relay_complete",
                "url": url,
                "seedling_id": job_id,
                "error": None,
            },
        }
        _persist_jobs()

        # Upload video for quality review, then clean up
        if meta.video_path and Path(meta.video_path).exists():
            await _upload_review_video(meta.video_path, meta.source_id, config)

    except Exception as e:
        _jobs[job_id] = {
            "status": "failed",
            "result": {"status": "failed", "error": str(e)},
        }
        print(f"[JOB {job_id}] RELAY FAILED: {e}", flush=True)
        _persist_jobs()


# ── Routes ────────────────────────────────────────────────────


@app.get("/health")
def health():
    """Liveness check."""
    return {"status": "ok", "service": "obsidian-capture-pipeline"}


@app.post("/index")
async def update_index(request: Request):
    """
    Receive vault index from vault_writer.py.
    Stored in memory for full-list fallback and incrementally embedded
    in the semantic store for relevance-ranked retrieval.
    """
    global _vault_index, _embed_store
    body = await request.json()
    _vault_index = body.get("notes", [])
    print(f"[INDEX] Updated: {len(_vault_index)} notes in vault index", flush=True)
    _persist_vault_index()

    # Trigger background embedding update (non-blocking — don't delay response)
    if _embed_store is not None:
        asyncio.create_task(_background_embed_update(_vault_index))

    return {"status": "ok", "indexed": len(_vault_index)}


async def _background_embed_update(notes: list[dict]):
    """Embed new/changed notes in the background after index push."""
    try:
        n = await _embed_store.upsert_notes(notes)
        if n > 0:
            print(f"[EMBED] Embedded {n} new/changed notes ({_embed_store.count()} total)", flush=True)
    except Exception as e:
        print(f"[EMBED] Background update failed: {e}", flush=True)


@app.post("/capture")
async def capture(request: Request):
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty request body")

    body = None
    # Attempt 1: Standard UTF-8 JSON parse

    try:
        body = _json.loads(raw.decode("utf-8"))
    except Exception:
        pass

    # Attempt 2: If raw bytes are just the JSON (common in 'File' body type)
    if body is None:
        try:
            body = _json.loads(raw)
        except Exception:
            # Final desperate attempt: is it multi-part?
            raise HTTPException(status_code=400, detail="Invalid JSON body")

    # iOS Shortcuts sends booleans as 0/1 — normalize return_files
    rf = body.get("return_files", False)
    if isinstance(rf, str):
        body["return_files"] = rf.lower() in ("true", "1", "yes")
    elif isinstance(rf, int):
        body["return_files"] = bool(rf)

    # Detect iOS Shortcuts user-agent and force cloud mode
    ua = request.headers.get("user-agent", "")
    if not body.get("return_files") and ("Shortcuts/" in ua or "CFNetwork" in ua):
        body["return_files"] = True

    try:
        req = CaptureRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request data: {e}")

    config = build_config(
        vault=req.vault,
        provider=req.provider,
        model=req.model,
        notes=req.notes,
        force=req.force,
    )

    # Validate API key
    key_map = {
        "gemini": ("gemini_api_key", "GEMINI_API_KEY"),
        "groq":   ("groq_api_key",   "GROQ_API_KEY"),
        "openai": ("openai_api_key", "OPENAI_API_KEY"),
        "ollama": (None, None),
    }
    attr, env = key_map.get(config.llm_provider, (None, None))
    if attr and not getattr(config, attr):
        raise HTTPException(
            status_code=400,
            detail=f"No API key for provider '{config.llm_provider}'. Set {env}.",
        )

    # ── Stage 1: The 'Relay' (iOS-to-Cloud-Seedling) ──────────
    if req.relay:
        try:
            # 1. Extract content (YouTube transcript, Web text, etc.)
            meta = extract_source(req.url, config)
            # 2. Run Relay Stage 1 (Gemini 3.1 Flash-Lite)
            description = await run_relay_stage(meta, config)
            
            # 3. Return as 4.0 Seedling
            return {
                "status": "seedling_created",
                "version": "4.0",
                "meta": {
                    "title": meta.title,
                    "author": meta.author,
                    "source_type": meta.source_type,
                    "source_id": meta.source_id
                },
                "relay_description": description
            }
        except Exception as e:
            print(f"[RELAY] Failed: {e}", flush=True)
            raise HTTPException(status_code=500, detail=f"Relay failed: {e}")

    # ── CLOUD MODE (async — Mac vault_writer.py picks up result) ─
    if req.return_files:
        job_id = str(uuid.uuid4())[:8]
        global _latest_job_id
        _jobs[job_id] = {"status": "processing", "result": None}
        _latest_job_id = job_id
        print(f"[JOB {job_id}] Queued: {req.url} | intent={req.user_intent!r}", flush=True)
        # Use BackgroundTask so HTTP response is sent BEFORE processing starts.
        # asyncio.create_task() doesn't help when extract_source() blocks the event loop.
        return JSONResponse(
            {"status": "queued", "job_id": job_id},
            background=BackgroundTask(_process_job, job_id, req.url, config, req.user_intent or ""),
        )

    # ── LOCAL MODE (sync) ──────────────────────────────────────
    # Lazy init EmbedStore for local mode too
    global _embed_store
    if _embed_store is None:
        try:
            from embed_store import EmbedStore
            _embed_store = EmbedStore(api_key=config.gemini_api_key)
        except Exception:
            pass

    # We don't have a full vault_index in memory in local mode unless pushed,
    # but process_source will now use _embed_store for semantic context.
    result = await run_pipeline(req.url, config, user_intent=req.user_intent)
    return {
        "status":        result.status,
        "url":           result.url,
        "notes_created": result.n_notes,
        "files_written": result.n_files,
        "elapsed_sec":   round(result.elapsed, 2),
        "tokens":        result.total_tokens,
        "cost":          result.cost_str or "n/a",
        "error":         result.error_msg or None,
    }


@app.post("/research_topic")
async def research_topic(request: Request):
    """
    Queue a multi-source research request for vault_writer.py to dispatch.
    Accepts {topic_intent, search_query, generate_synthesis}.
    Does NO AI processing — just saves a request JSON to Spaces and returns immediately.
    vault_writer.py detects research_request_*.json and calls research_topic locally.
    """
    raw = await request.body()
    try:
        body = _json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    topic_intent = body.get("topic_intent", "").strip()
    if not topic_intent:
        raise HTTPException(status_code=400, detail="topic_intent is required")

    search_query = body.get("search_query", "").strip()
    generate_synthesis = body.get("generate_synthesis", False)

    job_id = f"rt_{uuid.uuid4().hex[:12]}"
    config = build_config()

    request_payload = _json.dumps({
        "type": "research_topic",
        "job_id": job_id,
        "topic_intent": topic_intent,
        "search_query": search_query or topic_intent,
        "generate_synthesis": generate_synthesis,
    })

    async def _upload_research_request():
        if not (_BOTO3_AVAILABLE and config.spaces_access_key):
            return
        loop = asyncio.get_event_loop()
        s3 = _spaces_client(config)
        def _put():
            s3.put_object(
                Bucket=config.spaces_bucket,
                Key=f"{config.spaces_prefix}/research_requests/research_request_{job_id}.json",
                Body=request_payload.encode("utf-8"),
                ContentType="application/json",
            )
            _signal_seedling_available()

        try:
            await loop.run_in_executor(None, _put)
            print(f"[RESEARCH] Queued job {job_id}: {topic_intent!r}", flush=True)
        except Exception as e:
            print(f"[RESEARCH] Spaces upload failed: {e}", flush=True)

    # BackgroundTask: respond immediately, upload to Spaces after HTTP response is sent
    return JSONResponse(
        {"status": "queued", "job_id": job_id, "topic_intent": topic_intent},
        background=BackgroundTask(_upload_research_request),
    )


async def _put_seedling_to_spaces(job_id: str, seedling_text: str, config) -> bool:
    """Upload a seedling JSON to DigitalOcean Spaces. Returns True on success."""
    if not (_BOTO3_AVAILABLE and config.spaces_access_key
            and config.spaces_secret_key and config.spaces_bucket):
        return False
    loop = asyncio.get_event_loop()
    s3 = _spaces_client(config)
    s3_key = f"{config.spaces_prefix.rstrip('/')}/seedling_{job_id}.json"

    def _put():
        s3.put_object(
            Bucket=config.spaces_bucket,
            Key=s3_key,
            Body=seedling_text.encode("utf-8"),
            ContentType="application/json",
            ACL="private",
        )
        _signal_seedling_available()

    try:
        await loop.run_in_executor(None, _put)
        print(f"[SEEDLING {job_id}] Uploaded to Spaces.", flush=True)
        return True
    except Exception as e:
        print(f"[SEEDLING {job_id}] Spaces upload failed: {e}", flush=True)
        return False


@app.post("/capture_text")
async def capture_text(request: Request):
    """
    Crystallize pasted text (research from other AIs, web clippings, notes).
    Accepts {text, title?, user_intent?}.
    Skips Stage 1 extraction — the pasted text is used directly as the relay_description.
    """
    raw = await request.body()
    try:
        body = _json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    title = body.get("title", "Pasted Content").strip() or "Pasted Content"
    user_intent = body.get("user_intent", "")
    job_id = f"txt_{uuid.uuid4().hex[:8]}"
    config = build_config()

    seedling = {
        "version": "4.0",
        "job_id": job_id,
        "url": "",
        "user_intent": user_intent,
        "timestamp": time.time(),
        "meta": {
            "title": title,
            "author": "User",
            "source_type": "text",
            "duration_sec": 0,
            "transcript": text,
            "source_id": job_id,
        },
        "relay_description": text,
    }
    seedling_text = _json.dumps(seedling, indent=2)
    print(f"[TEXT {job_id}] Queued: {len(text)} chars | title={title!r} | intent={user_intent!r}", flush=True)
    # BackgroundTask: respond immediately, upload to Spaces after HTTP response is sent
    return JSONResponse(
        {"status": "queued", "job_id": job_id},
        background=BackgroundTask(_put_seedling_to_spaces, job_id, seedling_text, config),
    )


async def _process_file_job(job_id: str, tmp_path: str, filename: str, config, user_intent: str):
    """Background: Stage 1 relay extraction from an uploaded file, then upload seedling to Spaces."""
    import dataclasses
    try:
        print(f"[FILE {job_id}] Stage 1 extraction: {filename}", flush=True)
        meta = extract_source(f"file://{tmp_path}", config)
        # Use the original filename as the display URL (not the temp path)
        meta = dataclasses.replace(meta, url=filename)

        description = await run_relay_stage(meta, config)

        seedling = {
            "version": "4.0",
            "job_id": job_id,
            "url": filename,
            "user_intent": user_intent,
            "timestamp": time.time(),
            "meta": {
                "title": meta.title or filename,
                "author": meta.author,
                "source_type": meta.source_type,
                "duration_sec": meta.duration_sec,
                "transcript": meta.transcript,
                "source_id": meta.source_id or job_id,
            },
            "relay_description": description,
        }
        await _put_seedling_to_spaces(job_id, _json.dumps(seedling, indent=2), config)
        print(f"[FILE {job_id}] Stage 1 complete: {len(description)} chars", flush=True)

    except Exception as e:
        import traceback
        print(f"[FILE {job_id}] Failed: {e}", flush=True)
        traceback.print_exc()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/capture_file")
async def capture_file(request: Request):
    """
    Crystallize an uploaded file (PDF from course modules, documents).
    Accepts {filename, content_base64, user_intent?}.
    Runs Stage 1 extraction on the file asynchronously, then uploads seedling to Spaces.
    Max practical size: ~15 MB (iOS Shortcuts limit).
    """
    raw = await request.body()
    try:
        body = _json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    filename = body.get("filename", "document.pdf").strip()
    content_b64 = body.get("content_base64", "").strip()
    user_intent = body.get("user_intent", "")

    if not content_b64:
        raise HTTPException(status_code=400, detail="content_base64 is required")

    import base64 as _b64, tempfile
    try:
        file_bytes = _b64.b64decode(content_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")

    job_id = f"file_{uuid.uuid4().hex[:8]}"
    config = build_config()

    ext = Path(filename).suffix.lower() or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    print(
        f"[FILE {job_id}] Received: {filename} ({len(file_bytes) // 1024}KB) | intent={user_intent!r}",
        flush=True,
    )
    return JSONResponse(
        {"status": "queued", "job_id": job_id, "filename": filename},
        background=BackgroundTask(_process_file_job, job_id, tmp_path, filename, config, user_intent),
    )


async def _process_voice_job(job_id: str, audio_path: str, config, user_intent: str = ""):
    """
    STAGE 1 (CLOUD RELAY) for voice memos.
    Transcribe via Groq Whisper → run_relay_stage → upload seedling to Spaces.
    """
    try:
        from processor import run_relay_stage
        from extractors import extract_voice_memo

        meta = extract_voice_memo(Path(audio_path), config)
        description = await run_relay_stage(meta, config)

        seedling = {
            "version": "4.0",
            "job_id": job_id,
            "url": "",
            "user_intent": user_intent,
            "timestamp": time.time(),
            "meta": {
                "title": meta.title,
                "author": meta.author,
                "source_type": meta.source_type,
                "duration_sec": meta.duration_sec,
                "transcript": meta.transcript,
                "source_id": meta.source_id,
                "transcript_quality": meta.transcript_quality,
            },
            "relay_description": description,
        }
        seedling_text = _json.dumps(seedling, indent=2)

        if _BOTO3_AVAILABLE and all([config.spaces_access_key, config.spaces_secret_key, config.spaces_bucket]):
            s3 = _spaces_client(config)
            s3_key = f"{config.spaces_prefix.rstrip('/')}/seedling_{job_id}.json"
            s3.put_object(Bucket=config.spaces_bucket, Key=s3_key,
                          Body=seedling_text, ContentType="application/json", ACL="private")
            _signal_seedling_available()
            print(f"[VOICE {job_id}] Seedling uploaded to Spaces.", flush=True)

        _jobs[job_id] = {"status": "completed",
                         "result": {"status": "relay_complete", "seedling_id": job_id, "error": None}}
        _persist_jobs()
    except Exception as e:
        _jobs[job_id] = {"status": "failed", "result": {"status": "failed", "error": str(e)}}
        print(f"[VOICE {job_id}] Failed: {e}", flush=True)
        _persist_jobs()
    finally:
        Path(audio_path).unlink(missing_ok=True)


async def _process_voice_fleeting_job(
    job_id: str, config, duration_hint: float = 0.0,
    text: str = "", audio_path: str = ""
):
    """
    Fleeting capture → raw markdown → Spaces incoming/Fleeting/.
    No LLM synthesis. vault_writer.py routes incoming/Fleeting/* → VAULT/Fleeting/.

    Two paths (mutually exclusive):
      text      — Siri-dictated text already available in the Shortcut (preferred)
      audio_path — ambient recording; transcribed via Groq Whisper
    """
    try:
        from datetime import datetime as _dt

        if text:
            transcript = text.strip()
        elif audio_path:
            from extractors import _transcribe_groq
            transcript = _transcribe_groq(Path(audio_path), config.groq_api_key)
            if not transcript:
                raise ValueError("Empty transcript from Groq Whisper")
        else:
            raise ValueError("Either 'text' or 'audio_path' must be provided")

        intent = _detect_intent(transcript)
        now = _dt.now()
        ts = now.strftime("%Y-%m-%d %H-%M")
        duration_str = f"{duration_hint:.0f}s" if duration_hint > 0 else "unknown"
        md_content = (
            f"---\ntype: fleeting\nintent: {intent}\n"
            f"date: {now.strftime('%Y-%m-%d')}\ntime: {now.strftime('%H:%M')}\n"
            f"duration: {duration_str}\nneeds_synthesis: false\n---\n\n"
            f"# {ts}\n\n{transcript.strip()}\n"
        )

        if _BOTO3_AVAILABLE and all([config.spaces_access_key, config.spaces_secret_key, config.spaces_bucket]):
            s3 = _spaces_client(config)
            filename = f"{ts} fleeting.md"
            s3_key = f"{config.spaces_prefix.rstrip('/')}/Fleeting/{filename}"
            s3.put_object(Bucket=config.spaces_bucket, Key=s3_key,
                          Body=md_content.encode("utf-8"), ContentType="text/markdown", ACL="private")
            print(f"[FLEETING {job_id}] Uploaded {filename} (intent={intent}).", flush=True)

            if intent == "research":
                import json as _json
                research_id = job_id + "_rq"
                research_payload = _json.dumps({
                    "topic_intent": transcript.strip(),
                    "search_query": transcript.strip(),
                    "generate_synthesis": True,
                }).encode("utf-8")
                rq_key = (
                    f"{config.spaces_prefix.rstrip('/')}/research_requests/"
                    f"research_request_{research_id}.json"
                )
                s3.put_object(Bucket=config.spaces_bucket, Key=rq_key,
                              Body=research_payload, ContentType="application/json", ACL="private")
                print(f"[FLEETING {job_id}] Also queued research request.", flush=True)

        _jobs[job_id] = {
            "status": "completed",
            "result": {"status": "captured", "intent": intent, "chars": len(transcript), "error": None},
        }
        _persist_jobs()
    except Exception as e:
        _jobs[job_id] = {"status": "failed", "result": {"status": "failed", "error": str(e)}}
        print(f"[FLEETING {job_id}] Failed: {e}", flush=True)
        _persist_jobs()
    finally:
        if audio_path:
            Path(audio_path).unlink(missing_ok=True)


_RESEARCH_RE = re.compile(
    r"^(research\s|look into\s|find out\s|"
    r"what is\s|what are\s|what was\s|what were\s|"
    r"how does\s|how do\s|how did\s|"
    r"why does\s|why do\s|why did\s|"
    r"who is\s|who are\s|who was\s|"
    r"when did\s|where is\s|"
    r"explain\s|summarise\s|summarize\s|tell me about\s)",
    re.IGNORECASE,
)


def _detect_intent(text: str) -> str:
    """
    Classify text as 'research' or 'fleeting'. No LLM — regex only.

    Explicit override: prefix with "RQ " (case-insensitive) to force research.
    Natural phrasing (what/how/why/research/explain…) also detected.
    """
    stripped = text.strip()
    # Explicit "RQ " prefix — highest priority
    if re.match(r"^rq\s+", stripped, re.IGNORECASE):
        return "research"
    if _RESEARCH_RE.match(stripped):
        return "research"
    # Genuine question with ≥4 words (filters rhetorical "Really?")
    if stripped.endswith("?") and len(stripped.split()) >= 4:
        return "research"
    return "fleeting"


async def _background_smart_capture(job_id: str, text: str, config):
    """
    Smart capture background task.
    Always writes a fleeting note for provenance.
    Additionally queues the research pipeline if intent = research.
    """
    from datetime import datetime as _dt
    now = _dt.now()
    ts  = now.strftime("%Y-%m-%d %H-%M")
    intent = _detect_intent(text)

    # ── 1. Always write a fleeting note (provenance for both paths) ──
    md_content = (
        f"---\ntype: fleeting\ndate: {now.strftime('%Y-%m-%d')}\n"
        f"time: {now.strftime('%H:%M')}\nduration: unknown\n"
        f"intent: {intent}\nneeds_synthesis: false\n---\n\n"
        f"# {ts}\n\n{text.strip()}\n"
    )
    if _BOTO3_AVAILABLE and all([config.spaces_access_key, config.spaces_secret_key, config.spaces_bucket]):
        try:
            s3 = _spaces_client(config)
            s3_key = f"{config.spaces_prefix.rstrip('/')}/Fleeting/{ts} fleeting.md"
            s3.put_object(Bucket=config.spaces_bucket, Key=s3_key,
                          Body=md_content.encode("utf-8"), ContentType="text/markdown", ACL="private")
            print(f"[SMART {job_id}] Fleeting note saved ({intent}).", flush=True)
        except Exception as e:
            print(f"[SMART {job_id}] Fleeting upload failed: {e}", flush=True)

    # ── 2. Research path: additionally queue research pipeline ────────
    if intent == "research":
        research_id = f"rt_{uuid.uuid4().hex[:12]}"
        payload = _json.dumps({
            "type": "research_topic",
            "job_id": research_id,
            "topic_intent": text.strip(),
            "search_query": text.strip(),
            "generate_synthesis": False,
        })
        if _BOTO3_AVAILABLE and all([config.spaces_access_key, config.spaces_secret_key, config.spaces_bucket]):
            try:
                s3 = _spaces_client(config)
                s3_key = (
                    f"{config.spaces_prefix.rstrip('/')}/research_requests/"
                    f"research_request_{research_id}.json"
                )
                s3.put_object(Bucket=config.spaces_bucket, Key=s3_key,
                              Body=payload.encode("utf-8"), ContentType="application/json")
                _signal_seedling_available()
                print(f"[SMART {job_id}] Research queued → {research_id}: {text[:50]!r}", flush=True)
            except Exception as e:
                print(f"[SMART {job_id}] Research upload failed: {e}", flush=True)

    _jobs[job_id] = {
        "status": "completed",
        "result": {"status": "captured", "intent": intent, "chars": len(text), "error": None},
    }
    _persist_jobs()


@app.post("/capture_smart")
async def capture_smart(request: Request):
    """
    Unified text capture endpoint for the iOS tap gesture.
    Accepts Siri-dictated or typed text; routes automatically:
      • Fleeting thought → Vault/Fleeting/ (zero-latency, no LLM)
      • Research query   → Vault/Fleeting/ (provenance) + research pipeline

    Detection is regex-only — no LLM call, no menu, no extra tap.
    Body: {"text": "...", "duration_hint": 0.0}
    """
    raw = await request.body()
    try:
        body = _json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="'text' is required")

    job_id = f"smart_{uuid.uuid4().hex[:8]}"
    config = build_config()
    intent = _detect_intent(text)

    print(f"[SMART {job_id}] intent={intent} | {text[:60]!r}", flush=True)
    return JSONResponse(
        {"status": "queued", "job_id": job_id, "intent": intent},
        background=BackgroundTask(_background_smart_capture, job_id, text, config),
    )


@app.post("/capture_voice")
async def capture_voice(request: Request):
    """
    Full synthesis capture for voice memos.
    Transcribes via Groq Whisper → Stage 1 (Gemini relay) → seedling to Spaces → Stage 2 on Mac.
    Body: {"audio_b64": "<base64>", "user_intent": "optional context"}
    """
    raw = await request.body()
    try:
        body = _json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    audio_b64 = body.get("audio_b64", "").strip()
    user_intent = body.get("user_intent", "")
    if not audio_b64:
        raise HTTPException(status_code=400, detail="audio_b64 is required")

    import base64 as _b64, tempfile
    try:
        audio_bytes = _b64.b64decode(audio_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")

    job_id = f"voice_{uuid.uuid4().hex[:8]}"
    config = build_config()

    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    print(f"[VOICE {job_id}] Received {len(audio_bytes)//1024}KB | intent={user_intent!r}", flush=True)
    return JSONResponse(
        {"status": "queued", "job_id": job_id},
        background=BackgroundTask(_process_voice_job, job_id, tmp_path, config, user_intent),
    )


@app.post("/capture_voice_fleeting")
async def capture_voice_fleeting(request: Request):
    """
    Zero-latency fleeting capture. No LLM synthesis.
    The Dreamer promotes flagged notes to Captures/ on-demand.

    Two accepted body shapes:
      {"text": "Siri-dictated string", "duration_hint": 12.0}   ← preferred (no Groq)
      {"audio_b64": "<base64 m4a>",   "duration_hint": 45.0}   ← ambient recording (Groq)

    Siri dictation path is preferred: free, on-device, zero upload overhead.
    Groq path is for ambient recordings where Siri's interactive UI is impractical.
    """
    raw = await request.body()
    try:
        body = _json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text      = body.get("text", "").strip()
    audio_b64 = body.get("audio_b64", "").strip()
    duration_hint = float(body.get("duration_hint", 0.0))

    if not text and not audio_b64:
        raise HTTPException(status_code=400, detail="Provide either 'text' (Siri) or 'audio_b64' (ambient)")

    job_id = f"fleeting_{uuid.uuid4().hex[:8]}"
    config = build_config()

    if text:
        print(f"[FLEETING {job_id}] Siri text: {len(text)} chars", flush=True)
        return JSONResponse(
            {"status": "queued", "job_id": job_id},
            background=BackgroundTask(
                _process_voice_fleeting_job, job_id, config, duration_hint, text=text
            ),
        )

    import base64 as _b64, tempfile
    try:
        audio_bytes = _b64.b64decode(audio_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")

    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    print(f"[FLEETING {job_id}] Ambient audio: {len(audio_bytes)//1024}KB → Groq", flush=True)
    return JSONResponse(
        {"status": "queued", "job_id": job_id},
        background=BackgroundTask(
            _process_voice_fleeting_job, job_id, config, duration_hint, audio_path=tmp_path
        ),
    )


@app.get("/result/{job_id}")
async def get_result(job_id: str):
    """Poll for job result. Returns processing/completed/failed status."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] == "processing":
        return {"status": "processing", "job_id": job_id}

    return job["result"]


@app.get("/pending")
async def get_pending():
    """
    Return all files waiting in Spaces incoming/ and delete them.
    vault_writer.py polls this to pull new notes to the local vault.
    Returns: {"files": [{"path": "Captures/foo.md", "content": "..."}, ...]}
    """
    if not _BOTO3_AVAILABLE:
        return {"files": []}
    try:
        from config import Config
        cfg = Config()
    except Exception:
        return {"files": []}
    if not all([cfg.spaces_access_key, cfg.spaces_secret_key, cfg.spaces_bucket]):
        return {"files": []}

    loop = asyncio.get_event_loop()

    def _fetch_and_delete():
        s3 = _spaces_client(cfg)
        prefix = cfg.spaces_prefix.rstrip("/") + "/"
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=cfg.spaces_bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))

        files = []
        for key in keys:
            try:
                # 2026 Sovereign Update: Handle both .md and .json (seedlings)
                if not (key.endswith(".md") or key.endswith(".json")):
                    continue
                    
                obj = s3.get_object(Bucket=cfg.spaces_bucket, Key=key)
                content = obj["Body"].read().decode("utf-8")
                rel_path = key[len(prefix):]
                
                # If it's a seedling, force it into the Seedlings/ directory locally
                if rel_path.startswith("seedling_"):
                    rel_path = f"Seedlings/{rel_path}"
                
                files.append({"path": rel_path, "content": content})
                s3.delete_object(Bucket=cfg.spaces_bucket, Key=key)
            except Exception as e:
                print(f"[PENDING] Error fetching {key}: {e}", flush=True)
        return files

    files = await loop.run_in_executor(None, _fetch_and_delete)
    if files:
        print(f"[PENDING] Served {len(files)} file(s) to vault_writer", flush=True)
    return {"files": files}


@app.get("/review")
async def get_review():
    """
    List videos available in Spaces review/ prefix.
    Returns: {"videos": [{"filename": "abc123.mp4", "size": 12345, "uploaded_at": 1234567890}]}
    vault_writer.py calls this to discover new review videos to download.
    """
    if not _BOTO3_AVAILABLE:
        return {"videos": []}
    try:
        from config import Config
        cfg = Config()
    except Exception:
        return {"videos": []}
    if not all([cfg.spaces_access_key, cfg.spaces_secret_key, cfg.spaces_bucket]):
        return {"videos": []}

    loop = asyncio.get_event_loop()

    def _list():
        s3 = _spaces_client(cfg)
        paginator = s3.get_paginator("list_objects_v2")
        videos = []
        for page in paginator.paginate(Bucket=cfg.spaces_bucket, Prefix="review/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                filename = key[len("review/"):]
                if not filename:
                    continue
                videos.append({
                    "filename": filename,
                    "size": obj.get("Size", 0),
                })
        return videos

    videos = await loop.run_in_executor(None, _list)
    return {"videos": videos}


@app.get("/review/{filename}")
async def download_review_video(filename: str):
    """
    Serve a review video from Spaces review/ prefix.
    Returns raw video bytes with video/mp4 content type.
    """
    import re
    if not re.match(r'^[\w\-\.]+\.mp4$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not _BOTO3_AVAILABLE:
        raise HTTPException(status_code=503, detail="Spaces not available")
    try:
        from config import Config
        cfg = Config()
    except Exception:
        raise HTTPException(status_code=503, detail="Config unavailable")

    loop = asyncio.get_event_loop()

    def _get():
        s3 = _spaces_client(cfg)
        obj = s3.get_object(Bucket=cfg.spaces_bucket, Key=f"review/{filename}")
        return obj["Body"].read()

    try:
        data = await loop.run_in_executor(None, _get)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Not found: {e}")

    from fastapi.responses import Response
    return Response(content=data, media_type="video/mp4")


@app.delete("/review/{filename}")
async def delete_review_video(filename: str):
    """
    Delete a review video from Spaces review/ prefix.
    vault_writer.py calls this after downloading + 10-min window expires.
    """
    import re
    if not re.match(r'^[\w\-\.]+\.mp4$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not _BOTO3_AVAILABLE:
        return {"deleted": False}
    try:
        from config import Config
        cfg = Config()
    except Exception:
        return {"deleted": False}

    loop = asyncio.get_event_loop()

    def _del():
        s3 = _spaces_client(cfg)
        s3.delete_object(Bucket=cfg.spaces_bucket, Key=f"review/{filename}")

    try:
        await loop.run_in_executor(None, _del)
        print(f"[REVIEW] Deleted review/{filename} from Spaces", flush=True)
        return {"deleted": True, "filename": filename}
    except Exception as e:
        return {"deleted": False, "error": str(e)}


@app.get("/related-notes")
async def get_related_notes():
    """
    Return semantically related note pairs from the embedding store.
    vault_writer.py uses this to write retroactive links into vault notes.
    Returns:
      auto    — pairs >= 0.85 similarity: safe to auto-link
      suggest — pairs 0.75–0.85: surface for human review
    """
    if _embed_store is None or _embed_store.count() == 0:
        return {"auto": [], "suggest": [], "total_notes": 0}

    loop = asyncio.get_event_loop()
    pairs = await loop.run_in_executor(None, _embed_store.find_all_related_pairs)
    pairs["total_notes"] = _embed_store.count()
    return pairs


@app.get("/latest")
async def get_latest():
    """Poll for the most recent job result. Fixed URL for iOS Shortcuts."""
    if not _latest_job_id:
        raise HTTPException(status_code=404, detail="No jobs yet")

    job = _jobs.get(_latest_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job expired")

    if job["status"] == "processing":
        return {"status": "processing"}

    return job["result"]


@app.get("/pending/wait")
async def wait_for_pending(timeout: int = 60):
    """
    Long-polling endpoint for vault_writer.py.
    Hangs until _seedling_available event is set (new files uploaded).
    Returns {"status": "ready"} if something happened, or "timeout".
    """
    try:
        await asyncio.wait_for(_seedling_available.wait(), timeout=timeout)
        _seedling_available.clear()  # clear after waking so next long-poll actually waits
        return {"status": "ready"}
    except asyncio.TimeoutError:
        return {"status": "timeout"}


# ── Entry point ───────────────────────────────────────────────


def main():
    global _embed_store

    # Restore jobs and vault index from last run
    _load_persisted_jobs()
    _load_vault_index()

    parser = argparse.ArgumentParser(
        description="Obsidian Capture Pipeline — Webhook Server"
    )
    parser.add_argument("--host",   default="127.0.0.1",
                        help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port",   type=int, default=8000,
                        help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload on code changes (dev mode)")
    args = parser.parse_args()

    # Init semantic embedding store
    try:
        from config import Config
        _cfg = Config()
        _embed_store = EmbedStore(api_key=_cfg.gemini_api_key)
        print(f"  {_c(f'Semantic index: {_embed_store.count()} notes (LanceDB)', 90)}")
    except Exception as e:
        print(f"  {_c(f'Semantic index: init failed ({e})', 90)}")

    # Init semantic dedup gate (Model2Vec, lazy model load on first check)
    global _dedup_gate
    try:
        from dedup_gate import DedupGate
        _cfg = _cfg if "_cfg" in dir() else Config()
        _dedup_gate = DedupGate(
            skip_threshold=_cfg.dedup_skip_threshold,
            flag_threshold=_cfg.dedup_flag_threshold,
        )
        print(f"  {_c(f'Dedup gate: {_dedup_gate.count()} entries indexed', 90)}")
    except Exception as e:
        print(f"  {_c(f'Dedup gate: unavailable ({e})', 90)}")

    print(f"\n  {_c('Obsidian Capture Pipeline — Webhook Server', 1)}")
    print(f"  {_c(f'Listening on  http://{args.host}:{args.port}', 90)}")
    print(f"  {_c('POST /capture    GET /health', 90)}\n")

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
