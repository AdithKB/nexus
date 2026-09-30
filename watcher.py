#!/usr/bin/env python3
"""
watcher.py — Local Crystallizer for Nexus v6.5
Monitors for '.seedling.json' files from the Cloud Relay.

Author: Adith K B
"""
import os
# Must be set before any import that may trigger joblib/loky (model2vec, sklearn, etc.)
# Prevents loky from forking subprocess workers that leak POSIX semaphores, which
# cause SIGSEGV in any subsequent native code (LanceDB/Rust, MLX/Metal, PyTorch).
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")

import asyncio
import json
import subprocess
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from config import Config
from extractors import SourceMeta
from processor import run_crystallizer_stage, free_mlx_model, prewarm_mlx_model
from vault import write_to_vault, check_duplicate
from embed_store import EmbedStore
from dedup_gate import DedupGate


def _start_mlx_server(model_hf_path: str) -> bool:
    """
    No-op: MLX inference is now in-process via outlines (no separate server).
    Kept for API compatibility with stress_test.py.
    """
    return True  # model will be loaded in-process on first generate call


def _stop_mlx_server():
    """Free in-process MLX model from GPU memory."""
    free_mlx_model()


def _get_thermal_level() -> int:
    """Read macOS thermal level (0=Normal, 1=Elevated, 2=Critical)."""
    try:
        res = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.thermal_level"],
            capture_output=True, text=True,
        )
        return int(res.stdout.strip())
    except Exception:
        return 0


# ── Configuration ─────────────────────────────────────────────

# Directory where vault_writer.py downloads the seedlings
from capture import build_config
_tmp_config = build_config()
SEEDLING_DIR = _tmp_config.vault_path / "Seedlings"

_DREAM_STATE = Path.home() / ".nexus_dream_state.json"


async def _maybe_run_dream_session(handler, config, embed_store) -> None:
    """
    Fire the Dreamer Agent when all four conditions hold:
      - 2 AM ≤ current hour < 5 AM
      - CPU thermal level = 0 (Normal)
      - No seedling actively processing
      - Last dream session was >20 hours ago
    """
    import datetime
    now = datetime.datetime.now()
    if not (2 <= now.hour < 5):
        return
    if _get_thermal_level() != 0:
        return
    if handler.processing:
        return

    min_interval_sec = 20 * 3600
    try:
        if _DREAM_STATE.exists():
            state = json.loads(_DREAM_STATE.read_text())
            if time.time() - state.get("last_dream_ts", 0) < min_interval_sec:
                return
    except Exception:
        pass

    print(f"[DREAMER] Starting dream session at {now.strftime('%H:%M')}...", flush=True)
    try:
        from dreamer import run_dream_session
        report = await run_dream_session(config, embed_store)
        _DREAM_STATE.write_text(json.dumps({
            "last_dream_ts": time.time(),
            "last_dream_date": now.strftime("%Y-%m-%d %H:%M"),
            "latent_links_added": report.latent_links_added,
            "orphans_rescued": report.orphans_rescued,
        }))
        print(
            f"[DREAMER] Complete: {report.latent_links_added} links, "
            f"{report.orphans_rescued} orphans rescued, "
            f"{len(report.moc_gaps)} MOC gaps → {report.duration_sec:.0f}s",
            flush=True,
        )
    except Exception as e:
        import traceback
        print(f"[DREAMER] Session failed: {e}", flush=True)
        traceback.print_exc()


class SeedlingHandler(FileSystemEventHandler):
    def __init__(self, config, embed_store, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop,
                 dedup_gate: "DedupGate | None" = None):
        self.config = config
        self.embed_store = embed_store
        self.dedup_gate = dedup_gate if dedup_gate is not None else DedupGate()
        self.processing = set()   # currently being processed (prevents concurrent dupes)
        self.processed = set()    # already done (prevents ghost events after deletion)
        self._queue = queue
        self._loop = loop         # main event loop — needed for threadsafe enqueue

    def _enqueue(self, path: Path) -> None:
        """Thread-safe enqueue from watchdog callback thread → main asyncio loop."""
        self._loop.call_soon_threadsafe(self._queue.put_nowait, path)

    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(".warm"):
            print("[CRYSTALLIZER] Pre-warm signal detected — loading model...", flush=True)
            try:
                asyncio.run_coroutine_threadsafe(
                    prewarm_mlx_model(self.config), self._loop
                )
                Path(event.src_path).unlink(missing_ok=True)
            except Exception as e:
                print(f"[CRYSTALLIZER] Pre-warm failed: {e}", flush=True)
            return
        if not event.src_path.endswith(".json"):
            return
        self._enqueue(Path(event.src_path))

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith(".json"):
            return
        self._enqueue(Path(event.src_path))

    async def process_seedling(self, file_path: Path):
        # Route research requests to the dedicated handler
        if file_path.name.startswith("research_request_"):
            await self._process_research_request(file_path)
            return

        # Only process files with our seedling prefix
        if "seedling_" not in file_path.name:
            return

        # Skip if already processed (ghost events after iCloud deletion)
        if file_path in self.processed:
            return

        if file_path in self.processing:
            return

        self.processing.add(file_path)
        print(f"\n[CRYSTALLIZER] New seedling detected: {file_path.name}", flush=True)

        try:
            # Wait a moment for iCloud sync to finish writing
            await asyncio.sleep(2)

            # ── Thermal guard ──────────────────────────────────────
            t_level = _get_thermal_level()
            if t_level > 0:
                cooldown = 120 if t_level > 1 else 60
                print(
                    f"[THERMAL] Level {t_level} — cooling for {cooldown}s before synthesis...",
                    flush=True,
                )
                await asyncio.sleep(cooldown)

            # 1. Load Seedling Data from Cloud Stage 1
            data = json.loads(file_path.read_text())
            meta_dict = data.get("meta", {})
            description = data.get("relay_description", "")

            if not description:
                print(f"[CRYSTALLIZER] Skipping {file_path.name}: No relay_description.", flush=True)
                return

            source_type = meta_dict.get("source_type", "web")
            duration_sec = int(meta_dict.get("duration_sec") or 0)

            # 2. Semantic dedup gate — re-enabled now that LOKY_MAX_CPU_COUNT=1 (set at the
            # top of this file, before any imports) prevents the model2vec/loky POSIX
            # semaphore leak that used to SIGSEGV subsequent native library calls
            # (LanceDB/MLX). Server-side dedup catches most duplicates before a seedling
            # is even created; this is a second check for anything that slips through.
            dedup_key = data.get("url") or meta_dict.get("source_id", file_path.stem)
            dedup_matched: list = []
            if self.dedup_gate and self.config.dedup_enabled:
                verdict, dedup_matched = self.dedup_gate.check(dedup_key, description)
                if verdict == "skip":
                    print(
                        f"[DEDUP] Skipping near-duplicate (sim ≥ 0.92): previously seen as {dedup_matched}",
                        flush=True,
                    )
                    self.processed.add(file_path)
                    file_path.unlink(missing_ok=True)
                    self.processing.discard(file_path)
                    return
                if verdict == "flag":
                    print(f"[DEDUP] Related content (sim ≥ 0.75): {dedup_matched}", flush=True)

            # 4. Reconstruct SourceMeta — no re-extraction needed
            meta = SourceMeta(
                url=data.get("url", ""),
                source_type=source_type,
                title=meta_dict.get("title", "Untitled"),
                author=meta_dict.get("author", "Unknown"),
                duration_sec=duration_sec,
                transcript=meta_dict.get("transcript", ""),
                source_id=meta_dict.get("source_id", "local-id"),
            )
            if dedup_matched:
                meta.related_captures = dedup_matched

            # 5. Model routing: always 14B until native-crash diagnosis complete (9B fails structured
            # JSON; MLX disabled — crashes at startup on this machine, mlx_lm import SIGSEGV)
            chosen_model = self.config.ollama_model
            tier = "14B (quality)"
            is_quality_path = True
            print(
                f"[CRYSTALLIZER] {meta.title[:60]} | {source_type} "
                f"| {duration_sec}s → {tier}",
                flush=True,
            )

            # Clone config so we don't mutate the shared instance
            import copy
            local_config = copy.copy(self.config)
            local_config.llm_provider = "ollama"
            local_config.ollama_model = chosen_model

            # 6. Run Stage 2 with a hard timeout to prevent GPU hangs from freezing the system
            # 9B synthesis: typical 30-60s, timeout at 240s
            # 14B synthesis: typical 90-180s, timeout at 480s
            synthesis_timeout = 480 if is_quality_path else 240

            try:
                result = await asyncio.wait_for(
                    run_crystallizer_stage(
                        description=description,
                        meta=meta,
                        config=local_config,
                        embed_store=self.embed_store,
                        user_intent=data.get("user_intent", ""),
                    ),
                    timeout=synthesis_timeout,
                )
            except asyncio.TimeoutError:
                print(
                    f"[CRYSTALLIZER] Synthesis timed out after {synthesis_timeout}s — "
                    f"seedling kept for retry.",
                    flush=True,
                )
                free_mlx_model()  # free GPU memory before giving up
                self.processing.discard(file_path)
                return

            # 7. Write to Local Vault
            if result.atomic_notes:
                write_to_vault(result, meta, local_config)

                # Index new notes into LanceDB so MCP search_vault finds them
                notes_to_index = [
                    {
                        "title":     note.title,
                        "aliases":   note.aliases,
                        "tags":      note.tags,
                        "note_type": note.note_type or "fact",
                    }
                    for note in result.atomic_notes
                ]
                try:
                    asyncio.ensure_future(
                        self.embed_store.upsert_notes(notes_to_index, keep_alive=60)
                    )
                except Exception as _ie:
                    print(f"[EMBED] Index update skipped: {_ie}", flush=True)

                if self.dedup_gate and self.config.dedup_enabled:
                    try:
                        self.dedup_gate.index(dedup_key, description, title=meta.title)
                    except Exception as _de:
                        print(f"[DEDUP] Index update skipped: {_de}", flush=True)

                # Quality signal: warn when integrity gate had to recover many anchors
                # (indicates model missed significant technical content)
                if result.anchors_recovered >= 5:
                    print(
                        f"[QUALITY] ⚠ {result.anchors_recovered} anchors recovered by integrity gate "
                        f"(model: {chosen_model}). Consider re-running with quality path.",
                        flush=True,
                    )

                print(
                    f"[CRYSTALLIZER] SUCCESS: {len(result.atomic_notes)} notes | "
                    f"{result.anchors_recovered} anchors recovered",
                    flush=True,
                )
                # Mark before unlinking to suppress ghost watchdog events
                self.processed.add(file_path)
                file_path.unlink(missing_ok=True)
                print(f"[CRYSTALLIZER] Seedling cleaned up: {file_path.name}", flush=True)

                # Free MLX model from GPU immediately after synthesis so the chip
                # can cool before the next job (no active GPU load during cooldown)
                if local_config.llm_provider == "mlx":
                    free_mlx_model()

                # Adaptive burst cooldown: 14B runs longer → chip needs more recovery time
                cooldown = 120 if is_quality_path else 30
                print(f"[STEALTH] {cooldown}s cooldown ({tier})...", flush=True)
                await asyncio.sleep(cooldown)
            else:
                print(
                    f"[CRYSTALLIZER] No notes generated — seedling kept for retry.",
                    flush=True,
                )

        except FileNotFoundError:
            # Ghost event from iCloud after deletion — suppress future events
            self.processed.add(file_path)
            self.processing.discard(file_path)
        except Exception as e:
            import traceback
            print(f"[CRYSTALLIZER] Error on {file_path.name}: {e}", flush=True)
            traceback.print_exc()
            _stop_mlx_server()  # always free GPU on error
        finally:
            self.processing.discard(file_path)

    async def _process_research_request(self, file_path: Path):
        """Handle a research_request_*.json dropped by vault_writer into Seedlings/."""
        if file_path in self.processed or file_path in self.processing:
            return
        self.processing.add(file_path)
        print(f"\n[RESEARCH] Research request detected: {file_path.name}", flush=True)

        try:
            await asyncio.sleep(1)  # brief settle for iCloud sync
            data = json.loads(file_path.read_text(encoding="utf-8"))

            topic_intent = data.get("topic_intent", "").strip()
            search_query = data.get("search_query", topic_intent).strip()
            generate_synthesis = data.get("generate_synthesis", False)
            job_id = data.get("job_id", file_path.stem)

            if not topic_intent:
                print(f"[RESEARCH] Skipping {file_path.name}: no topic_intent.", flush=True)
                self.processed.add(file_path)
                file_path.unlink(missing_ok=True)
                return

            print(f"[RESEARCH] Topic: {topic_intent!r} | search: {search_query!r}", flush=True)

            from extractors import extract_source
            from processor import process_source
            from vault import write_to_vault, slugify
            import urllib.request, urllib.parse

            cfg = self.config
            store = self.embed_store

            # ── Auto-discover URLs via Jina search ─────────────────
            urls = []
            if cfg.jina_api_key and search_query:
                try:
                    req = urllib.request.Request(
                        f"https://r.jina.ai/search?q={urllib.parse.quote(search_query)}",
                        headers={
                            "Authorization": f"Bearer {cfg.jina_api_key}",
                            "Accept": "application/json",
                            "X-Return-Format": "json",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        result_data = json.loads(resp.read().decode())
                    urls = [item["url"] for item in result_data.get("data", [])[:4] if item.get("url")]
                    print(f"[RESEARCH] Discovered {len(urls)} URLs via Jina.", flush=True)
                except Exception as e:
                    print(f"[RESEARCH] Jina search failed: {e} — no URLs to process.", flush=True)

            if not urls:
                print(f"[RESEARCH] No URLs found for {topic_intent!r}. Aborting.", flush=True)
                self.processed.add(file_path)
                file_path.unlink(missing_ok=True)
                return

            # ── Parallel Stage 1 extraction ────────────────────────
            loop = asyncio.get_event_loop()
            metas = await asyncio.gather(
                *[loop.run_in_executor(None, extract_source, u, cfg) for u in urls],
                return_exceptions=True,
            )

            # ── Sequential Stage 2 synthesis ──────────────────────
            old_max, old_min = cfg.max_notes, cfg.min_notes
            cfg.max_notes = 5
            cfg.min_notes = 2
            completed = []
            all_note_titles = []

            try:
                vault_index = store.get_all_as_index()
                for url, meta in zip(urls, metas):
                    if isinstance(meta, Exception):
                        print(f"[RESEARCH] Extract failed for {url}: {meta}", flush=True)
                        continue
                    try:
                        result = await process_source(meta, cfg, vault_index=vault_index,
                                                      user_intent=topic_intent, embed_store=store)
                        write_to_vault(result, meta, cfg, vault_index=vault_index)
                        notes_to_index = [{
                            "title": n.title, "aliases": n.aliases or [],
                            "tags": n.tags or [], "note_type": n.note_type, "content": n.content
                        } for n in result.atomic_notes]
                        await store.upsert_notes(notes_to_index)
                        for nd in notes_to_index:
                            vault_index.append(nd)
                        completed.append(url)
                        all_note_titles.extend([n.title for n in result.atomic_notes])
                        print(f"[RESEARCH] Processed {url} → {len(result.atomic_notes)} notes", flush=True)
                    except Exception as e:
                        print(f"[RESEARCH] Stage 2 failed for {url}: {e}", flush=True)
            finally:
                cfg.max_notes = old_max
                cfg.min_notes = old_min

            # ── Template MOC ───────────────────────────────────────
            if completed:
                from datetime import datetime
                moc_title = f"MOC - Topic Research - {topic_intent}"
                moc_file = cfg.get_moc_dir() / f"{slugify(moc_title)}.md"
                moc_content = (
                    f"---\ntype: moc\ntags: [research, {slugify(topic_intent)}]\n"
                    f"date: {datetime.now().strftime('%Y-%m-%d')}\n---\n\n"
                    f"# {topic_intent}\n\n## Findings Overview\n"
                )
                for t in all_note_titles:
                    moc_content += f"- [[{t}]]\n"
                moc_file.write_text(moc_content, encoding="utf-8")
                print(f"[RESEARCH] Created MOC: {moc_title}", flush=True)

            print(
                f"[RESEARCH] DONE: {len(completed)}/{len(urls)} sources | "
                f"{len(all_note_titles)} notes | job {job_id}",
                flush=True,
            )

        except Exception as e:
            import traceback
            print(f"[RESEARCH] Error on {file_path.name}: {e}", flush=True)
            traceback.print_exc()
        finally:
            self.processed.add(file_path)
            file_path.unlink(missing_ok=True)
            self.processing.discard(file_path)


async def _seedling_worker(handler: "SeedlingHandler", queue: asyncio.Queue) -> None:
    """
    Single async worker that drains the seedling queue one item at a time.
    Guarantees:
      - No concurrent Ollama calls (no memory pressure spikes)
      - Every seedling enqueued is eventually processed (no loss)
      - Captures from a burst (Mac wake-up backlog) are handled in order
    """
    while True:
        path = await queue.get()
        try:
            await handler.process_seedling(path)
        except Exception as e:
            import traceback
            print(f"[WORKER] Unhandled error processing {path.name}: {e}", flush=True)
            traceback.print_exc()
        finally:
            queue.task_done()


async def _warmup_ollama(config) -> None:
    """
    Pre-load both the embedding model and synthesis model into Ollama's model
    slots so first-capture latency is generation time only, not reload time.

    Requires OLLAMA_MAX_LOADED_MODELS=2 (and OLLAMA_NUM_PARALLEL=1) set in the
    environment before `ollama serve` to keep both models resident simultaneously.
    """
    import httpx
    from embed_store import warmup_embedding_model

    keep_alive = getattr(config, "ollama_keep_alive", 300)

    async def _warmup_synthesis():
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{config.ollama_base_url}/api/chat",
                    json={
                        "model": config.ollama_model_fast,
                        "messages": [],
                        "keep_alive": keep_alive,
                    },
                )
                # 400 = model found but empty messages rejected — that's fine, model is loaded
                if resp.status_code not in (200, 400):
                    resp.raise_for_status()
        except Exception as e:
            print(f"[CRYSTALLIZER] Synthesis warmup skipped: {e}", flush=True)

    embed_ok, _ = await asyncio.gather(
        warmup_embedding_model(config.ollama_base_url, keep_alive=keep_alive),
        _warmup_synthesis(),
    )
    if embed_ok:
        print(f"[CRYSTALLIZER] Models pre-loaded (keep_alive={keep_alive}s). "
              "First capture will skip cold-start.", flush=True)


def _rescan_seedling_backlog(handler: "SeedlingHandler", queue: asyncio.Queue) -> int:
    """
    Re-glob Seedlings/ and re-queue anything not currently processing or done.

    The startup scan (main(), step 5) only runs once per daemon launch — if that
    single attempt fails (e.g. Ollama's own daemon hasn't finished starting yet,
    a real race at login/wake), the watchdog observer alone won't recover it:
    watchdog only fires on *new* filesystem events, not on files that already
    existed before it started watching. A stuck seedling then silently sits
    forever until the next full restart. This periodic sweep (called every 5min
    from main()'s tick loop) closes that gap without needing a restart.
    """
    pending = sorted(SEEDLING_DIR.glob("seedling_*.json")) + sorted(SEEDLING_DIR.glob("research_request_*.json"))
    requeued = [f for f in pending if f not in handler.processing and f not in handler.processed]
    for f in requeued:
        queue.put_nowait(f)
    return len(requeued)


async def main():
    # 1. Initialize
    from capture import build_config
    config = build_config()
    embed_store = EmbedStore(api_key=config.gemini_api_key)

    # Ensure Seedling dir exists
    SEEDLING_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n[Nexus v6.0 Crystallizer] Monitoring: {SEEDLING_DIR}", flush=True)
    print(f"[Nexus v6.0 Crystallizer] Engine: Ollama | fast: {config.ollama_model_fast} | quality: {config.ollama_model}", flush=True)
    print(f"[Nexus v6.0 Crystallizer] keep_alive={config.ollama_keep_alive}s | "
          "Set OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_NUM_PARALLEL=1 before `ollama serve`", flush=True)
    print("[Nexus v6.0 Crystallizer] Thermal safety active. Press Ctrl+C to stop.\n", flush=True)

    # 2. Pre-warm both models in parallel (eliminates cold-start on first capture)
    await _warmup_ollama(config)

    # 3. Set up queue + handler
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    dedup_gate = DedupGate(
        skip_threshold=config.dedup_skip_threshold,
        flag_threshold=config.dedup_flag_threshold,
    )
    event_handler = SeedlingHandler(config, embed_store, queue, loop, dedup_gate=dedup_gate)

    # 4. Start filesystem watcher
    observer = Observer()
    observer.schedule(event_handler, str(SEEDLING_DIR), recursive=False)
    observer.start()

    # 5. Startup scan — push any backlog into the queue (Mac was off, seedlings accumulated)
    n = _rescan_seedling_backlog(event_handler, queue)
    if n:
        print(f"[CRYSTALLIZER] Startup scan: {n} pending seedling(s) queued.", flush=True)

    # 6. Single worker serialises all synthesis — no concurrent Ollama calls, no memory spikes
    worker = asyncio.create_task(_seedling_worker(event_handler, queue))

    try:
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            if tick % 60 == 0:
                await _maybe_run_dream_session(event_handler, config, embed_store)
            if tick % 300 == 0:
                n = _rescan_seedling_backlog(event_handler, queue)
                if n:
                    print(f"[CRYSTALLIZER] Backlog rescan: re-queued {n} stuck seedling(s).", flush=True)
    except KeyboardInterrupt:
        worker.cancel()
        observer.stop()
    observer.join()

if __name__ == "__main__":
    asyncio.run(main())
