"""
crystallizer.py — Stage 2 daemon for the Nexus v4.0 pipeline.

Watches Vault/Seedlings/ for JSON seedling files written by vault_writer.py.
For each seedling:
  1. Reconstructs SourceMeta from the stored fields (no re-extraction).
  2. Routes to Qwen3.5-9B (fast) or Qwen3-14B (quality) based on source type
     and video duration, both via MLX-Textgen.
  3. Calls run_crystallizer_stage() directly with the pre-computed
     relay_description — Stage 1 (Gemini) is never re-run here.
  4. Writes atomic notes to vault, deletes the seedling.

Model routing heuristic
-----------------------
  PDF or YouTube ≥ 1 hour  →  Qwen3-14B-4bit  (complex, needs deep context)
  YouTube < 1 hour          →  Qwen3.5-9B-4bit   (2× faster, ~90% quality)
  Web / Instagram / other   →  Qwen3.5-9B-4bit   (short-form, fast is fine)

Author: Adith K B
"""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from config import Config
from extractors import SourceMeta
from processor import run_crystallizer_stage
from vault import write_to_vault
from embed_store import EmbedStore


# ── Helpers ───────────────────────────────────────────────────


def _get_thermal_level() -> int:
    """
    Read macOS thermal level via sysctl (0=Normal, 1=Elevated, 2=Critical).
    Prevents heat-soaking on the fanless M4 Air.
    """
    try:
        res = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.thermal_level"],
            capture_output=True, text=True,
        )
        return int(res.stdout.strip())
    except Exception:
        return 0


def _force_icloud_download(path: Path) -> None:
    try:
        subprocess.run(["brctl", "download", str(path)], capture_output=True)
    except Exception:
        pass


def _is_icloud_placeholder(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    try:
        return path.stat().st_size == 0
    except Exception:
        return True


def _select_model(config: Config, source_type: str, duration_sec: int) -> str:
    """
    Return the mlx_model string to use for Stage 2.

    Rules (in priority order):
      1. PDF → always 14B (academic depth matters more than speed)
      2. YouTube / Instagram ≥ 3600s → 14B (long lecture, context-heavy)
      3. Everything else → 9B (web articles, short clips — fast path)
    """
    if source_type == "pdf":
        return config.mlx_model
    if source_type in ("youtube", "instagram") and duration_sec >= 3600:
        return config.mlx_model
    return config.mlx_model_fast


# ── Seedling handler ──────────────────────────────────────────


class SeedlingHandler(FileSystemEventHandler):
    def __init__(self, config: Config, seedlings_dir: Path):
        self.config = config
        self.seedlings_dir = seedlings_dir
        self.processing: set[str] = set()

    def _should_process(self, path: Path) -> bool:
        return (
            not path.name.startswith(".")
            and path.suffix == ".json"
            and path.name.startswith("seedling_")
            and path.name not in self.processing
        )

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not self._should_process(path):
            return
        if _is_icloud_placeholder(path):
            _force_icloud_download(path)
        else:
            asyncio.run(self.crystallize(path))

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not self._should_process(path):
            return
        if _is_icloud_placeholder(path):
            _force_icloud_download(path)
            return
        asyncio.run(self.crystallize(path))

    async def crystallize(self, path: Path) -> None:
        if path.name in self.processing:
            return
        self.processing.add(path.name)

        try:
            # ── Thermal guard ─────────────────────────────────
            t_level = _get_thermal_level()
            if t_level > 0:
                cooldown = 120 if t_level > 1 else 60
                print(
                    f"[THERMAL] Level {t_level} — cooling for {cooldown}s...",
                    flush=True,
                )
                await asyncio.sleep(cooldown)

            # ── Load seedling JSON ─────────────────────────────
            try:
                seedling = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[CRYSTALLIZE] Bad JSON in {path.name}: {e}", flush=True)
                return

            url = seedling.get("url", "")
            relay_description = seedling.get("relay_description", "")
            meta_data = seedling.get("meta", {})

            if not relay_description:
                print(
                    f"[CRYSTALLIZE] No relay_description in {path.name} — skipping.",
                    flush=True,
                )
                return

            source_type = meta_data.get("source_type", "web")
            duration_sec = int(meta_data.get("duration_sec") or 0)

            # Reconstruct SourceMeta from stored fields — no re-extraction.
            meta = SourceMeta(
                title=meta_data.get("title", url),
                url=url,
                source_type=source_type,
                author=meta_data.get("author", ""),
                duration_sec=duration_sec,
                transcript=meta_data.get("transcript", ""),
                source_id=meta_data.get("source_id", ""),
            )

            # ── Model routing ──────────────────────────────────
            chosen_model = _select_model(self.config, source_type, duration_sec)
            tier = "14B (quality)" if chosen_model == self.config.mlx_model else "9B (fast)"
            print(
                f"[CRYSTALLIZE] {path.name} | {source_type} | {duration_sec}s "
                f"→ {tier}",
                flush=True,
            )

            local_config = Config()
            local_config.llm_provider = "mlx"
            local_config.mlx_model = chosen_model

            # ── Stage 2: synthesis (relay_description already computed) ──
            store = EmbedStore(api_key=local_config.gemini_api_key)
            result = await run_crystallizer_stage(
                description=relay_description,
                meta=meta,
                config=local_config,
                embed_store=store,
                user_intent=seedling.get("user_intent", ""),
            )

            # ── Write vault notes ──────────────────────────────
            agents_md_path = Path("AGENTS.md")
            agents_md = agents_md_path.read_text() if agents_md_path.exists() else ""
            write_to_vault(result, meta, local_config)

            # ── Clean up seedling ──────────────────────────────
            path.unlink(missing_ok=True)
            print(
                f"[CRYSTALLIZE] Done — {len(result.atomic_notes)} notes written.",
                flush=True,
            )

            # Mandatory burst cooldown to protect fanless chassis
            print("[STEALTH] 30s cooldown...", flush=True)
            await asyncio.sleep(30)

        except Exception as e:
            import traceback
            print(f"[CRYSTALLIZE] Failed on {path.name}: {e}", flush=True)
            traceback.print_exc()
        finally:
            self.processing.discard(path.name)


# ── Entry point ───────────────────────────────────────────────


def main() -> None:
    config = Config()

    # Seedlings live under Vault/Seedlings/ (written by vault_writer.py)
    seedlings_dir = config.vault_path / "Seedlings"
    seedlings_dir.mkdir(parents=True, exist_ok=True)

    # Process any seedlings that arrived while the daemon was offline
    existing = list(seedlings_dir.glob("seedling_*.json"))
    if existing:
        print(
            f"[CRYSTALLIZER] Found {len(existing)} pending seedling(s) on startup.",
            flush=True,
        )

        async def _drain():
            handler = SeedlingHandler(config, seedlings_dir)
            for p in existing:
                if not _is_icloud_placeholder(p):
                    await handler.crystallize(p)

        asyncio.run(_drain())

    # Start filesystem watcher
    handler = SeedlingHandler(config, seedlings_dir)
    observer = Observer()
    observer.schedule(handler, str(seedlings_dir), recursive=False)

    fast = config.mlx_model_fast.split("/")[-1]
    quality = config.mlx_model.split("/")[-1]
    print(
        f"[CRYSTALLIZER] Watching {seedlings_dir}",
        flush=True,
    )
    print(
        f"[CRYSTALLIZER] Models — fast: {fast}  |  quality: {quality}",
        flush=True,
    )
    print("[STEALTH] Thermal safety active.", flush=True)

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
