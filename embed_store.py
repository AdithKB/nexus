"""
embed_store.py — LanceDB-backed semantic vault index.

2026 upgrade: replaced SQLite+JSON (brute-force numpy cosine) with LanceDB
columnar format. Same public API — drop-in replacement.

Benefits:
  - Disk-first (no full index in RAM)
  - SIMD-accelerated vector search via Lance Rust core
  - Native merge_insert (upsert by title key)
  - Auto-migrates data from legacy ~/.nexus_embed.db on first run

Embedding model: Qwen3-Embedding-4B via Ollama (256-dim Matryoshka truncation).
"""
import hashlib
import math
import re
import unicodedata
from pathlib import Path
from typing import Optional

from config import ILLEGAL_FILENAME_CHARS

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False

# ── Constants ────────────────────────────────────────────────

EMBED_DIM  = 256
MODEL_ID   = "qwen3-embedding:4b"
LANCE_DIR  = Path.home() / ".nexus_lancedb"
LEGACY_DB  = Path.home() / ".nexus_embed.db"   # migrated on first run

_ILLEGAL_CHARS = ILLEGAL_FILENAME_CHARS


def _slugify_title(title: str) -> str:
    """
    Same normalization as vault.slugify() — duplicated here (not imported)
    because processor.py imports EmbedStore, and vault.py imports processor.py,
    so `from vault import slugify` would create a circular import.

    Titles stored here MUST match vault.slugify(note.title) exactly, since
    that's what write_to_vault() uses for the actual filename. A mismatch
    (e.g. a title containing "/") makes Dreamer's latent-link suggestions
    point at wikilinks that don't resolve to any real file.
    """
    s = title.strip()
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.replace("/", " ")
    s = _ILLEGAL_CHARS.sub("", s)
    s = re.sub(r"[\s_]+", " ", s)
    s = s.strip(" _.")
    if s:
        s = s[0].upper() + s[1:]
    if len(s) > 120:
        s = s[:120].rsplit(" ", 1)[0]
    return s.strip() or "Untitled"


# ── Math helpers (module-level — used by dreamer.py) ─────────

def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))

def _norm(v) -> float:
    return math.sqrt(sum(x * x for x in v))

def cosine(a, b) -> float:
    denom = _norm(a) * _norm(b)
    return _dot(a, b) / denom if denom else 0.0



# ── Embedding (async, Ollama) ─────────────────────────────────

def _normalise(full_vec: list[float]) -> list[float]:
    truncated = full_vec[:EMBED_DIM]
    norm = math.sqrt(sum(x * x for x in truncated))
    return [x / norm for x in truncated] if norm > 0 else [0.0] * EMBED_DIM


async def _embed_texts(texts: list[str], keep_alive: int = 300) -> list[list[float]]:
    """
    Batch embed via Ollama /api/embed (one model load for N texts).
    keep_alive: seconds to hold the model resident; 0 = evict immediately.
    No num_ctx override — embedding models don't use KV cache; passing 32768
    would pre-allocate ~7 GB of unused cache on top of the 2.4 GB weights.
    """
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "http://localhost:11434/api/embed",
            json={"model": MODEL_ID, "input": texts, "keep_alive": keep_alive},
        )
        resp.raise_for_status()
        return [_normalise(v) for v in resp.json()["embeddings"]]


async def _embed_text(text: str, api_key: str = "", keep_alive: int = 300) -> list[float]:
    """Single-text convenience wrapper around _embed_texts."""
    return (await _embed_texts([text], keep_alive=keep_alive))[0]


async def warmup_embedding_model(ollama_base_url: str = "http://localhost:11434",
                                 keep_alive: int = 300) -> bool:
    """
    Pre-load the embedding model into Ollama's model slot so the first real
    embed request pays zero cold-start cost.  Fire-and-forget at watcher startup.
    Returns True if the model loaded successfully.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{ollama_base_url}/api/embed",
                json={"model": MODEL_ID, "input": ["warmup"], "keep_alive": keep_alive},
            )
            resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[EMBED] Warmup skipped (Ollama not ready): {e}", flush=True)
        return False


# ── LanceDB schema ────────────────────────────────────────────

def _get_schema():
    import pyarrow as pa
    return pa.schema([
        pa.field("title",        pa.utf8()),
        pa.field("aliases",      pa.utf8()),
        pa.field("tags",         pa.utf8()),
        pa.field("note_type",    pa.utf8()),
        pa.field("content_hash", pa.utf8()),
        pa.field("vector",       pa.list_(pa.float32(), EMBED_DIM)),
    ])


# ── Store ─────────────────────────────────────────────────────

class EmbedStore:
    """
    LanceDB-backed semantic index for vault notes.
    Identical public API to the legacy SQLite-based EmbedStore.
    """

    def __init__(self, db_path: Path = LANCE_DIR, api_key: str = ""):
        self.api_key = api_key
        self._db_path = db_path
        import lancedb
        self._db_conn = lancedb.connect(str(db_path))
        self._table = self._open_or_create_table()
        # In-memory hash cache for change detection (avoids re-embedding unchanged notes)
        self._hash_cache: dict[str, str] = {
            r["title"]: r.get("content_hash", "")
            for r in self._table.to_arrow().to_pylist()
        }
        self._run_migration_if_needed()

    def _open_or_create_table(self):
        schema = _get_schema()
        if "notes" in self._db_conn.table_names():
            tbl = self._db_conn.open_table("notes")
            # Wipe and recreate if vector dimension changed
            rows = tbl.to_arrow().to_pylist()
            if rows:
                vec = rows[0].get("vector", [])
                if len(vec) != EMBED_DIM:
                    print(
                        f"[EMBED] Dimension mismatch ({len(vec)} vs {EMBED_DIM}). "
                        "Rebuilding LanceDB table...", flush=True,
                    )
                    self._db_conn.drop_table("notes")
                    return self._db_conn.create_table("notes", schema=schema, data=[])
            return tbl
        return self._db_conn.create_table("notes", schema=schema, data=[])

    def _run_migration_if_needed(self):
        """One-time migration from legacy SQLite store at ~/.nexus_embed.db.
        Only runs when using the default production path, not custom test paths."""
        if not LEGACY_DB.exists() or self.count() > 0 or self._db_path != LANCE_DIR:
            return
        try:
            import sqlite3, json as _json
            conn = sqlite3.connect(str(LEGACY_DB))
            rows = conn.execute(
                "SELECT title, aliases, tags, note_type, content_hash, embedding FROM notes"
            ).fetchall()
            conn.close()
            if not rows:
                return
            records = []
            for title, aliases, tags, note_type, chash, emb_json in rows:
                try:
                    vec = _json.loads(emb_json)
                    if len(vec) != EMBED_DIM:
                        continue
                except Exception:
                    continue
                records.append({
                    "title": title or "", "aliases": aliases or "",
                    "tags": tags or "", "note_type": note_type or "",
                    "content_hash": chash or "", "vector": [float(x) for x in vec],
                })
            if records:
                self._table.add(records)
                self._hash_cache = {r["title"]: r["content_hash"] for r in records}
                print(f"[EMBED] Migrated {len(records)} notes from legacy SQLite → LanceDB.", flush=True)
        except Exception as e:
            print(f"[EMBED] Migration skipped: {e}", flush=True)

    # ── Internal helpers ──────────────────────────────────────

    def _make_embed_text(self, note: dict) -> str:
        title   = note.get("title", "")
        tags    = " ".join(note.get("tags", [])[:10])
        aliases = " | ".join(note.get("aliases", [])[:10])
        content = note.get("content", "")
        return f"TITLE: {title}\nTAGS: {tags}\nALIASES: {aliases}\nCONTENT: {content}"

    def _content_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    # ── Public API ────────────────────────────────────────────

    def count(self) -> int:
        try:
            return self._table.count_rows()
        except Exception:
            return 0

    def get_all_as_index(self) -> list[dict]:
        """Return all notes as vault_index-compatible dicts (no embeddings)."""
        rows = self._table.to_arrow().to_pylist()
        return [
            {
                "title":     r["title"],
                "aliases":   [a for a in r.get("aliases", "").split(",") if a],
                "tags":      [t for t in r.get("tags", "").split(",") if t],
                "note_type": r.get("note_type", ""),
            }
            for r in rows
        ]

    def get_all_with_embeddings(self) -> list[dict]:
        """Return all notes with embedding vectors (used by Dreamer Agent)."""
        rows = self._table.to_arrow().to_pylist()
        result = []
        for r in rows:
            vec = r.get("vector", [])
            if len(vec) != EMBED_DIM:
                continue
            result.append({
                "title":     r["title"],
                "tags":      [t for t in r.get("tags", "").split(",") if t],
                "embedding": list(vec),
            })
        return result

    def find_all_related_pairs(self) -> dict:
        """
        Sync: return all note pairs grouped by similarity band.
        Called from /related endpoint via run_in_executor.
          auto    — sim >= 0.85 (safe to auto-link)
          suggest — sim 0.75–0.85 (surface for review)
        """
        if not _NUMPY:
            return {"auto": [], "suggest": []}
        notes = self.get_all_with_embeddings()
        if len(notes) < 2:
            return {"auto": [], "suggest": []}
        mat = np.array([n["embedding"] for n in notes], dtype="float32")
        sim_mat = mat @ mat.T
        auto, suggest = [], []
        n = len(notes)
        for i in range(n):
            for j in range(i + 1, n):
                sim = float(sim_mat[i, j])
                pair = {"a": notes[i]["title"], "b": notes[j]["title"], "similarity": round(sim, 4)}
                if sim >= 0.85:
                    auto.append(pair)
                elif sim >= 0.75:
                    suggest.append(pair)
        return {"auto": auto, "suggest": suggest}

    async def upsert_notes(self, notes: list[dict], force: bool = False,
                           keep_alive: int = 300) -> int:
        """
        Embed and upsert notes that are new or have changed content.
        Uses a single batched /api/embed call (one model load for all N notes).
        Returns the number of notes actually embedded.
        """
        # Phase 1: filter to only notes that need re-embedding
        pending: list[tuple[dict, str, str, str]] = []   # (note, embed_text, chash, title)
        for note in notes:
            if not note.get("title", "").strip():
                continue
            title = _slugify_title(note["title"])
            embed_text = self._make_embed_text(note)
            chash = self._content_hash(embed_text)
            if not force and self._hash_cache.get(title) == chash:
                continue
            pending.append((note, embed_text, chash, title))

        if not pending:
            return 0

        # Phase 2: one batch embed call for all pending notes
        titles_str = ", ".join(p[3] for p in pending)
        print(f"[EMBED] batch({len(pending)}): {titles_str[:80]}", flush=True)
        try:
            vectors = await _embed_texts([p[1] for p in pending], keep_alive=keep_alive)
        except Exception as e:
            print(f"[EMBED] Batch embed failed: {e}", flush=True)
            return 0

        # Phase 3: build records and upsert
        to_upsert = []
        for (note, _, chash, title), vector in zip(pending, vectors):
            to_upsert.append({
                "title":        title,
                "aliases":      ",".join(note.get("aliases", [])),
                "tags":         ",".join(note.get("tags", [])),
                "note_type":    note.get("note_type", ""),
                "content_hash": chash,
                "vector":       [float(x) for x in vector],
            })

        try:
            (
                self._table.merge_insert("title")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(to_upsert)
            )
        except Exception as e:
            print(f"[EMBED] merge_insert failed ({e}), using add fallback.", flush=True)
            self._table.add(to_upsert)
        for r in to_upsert:
            self._hash_cache[r["title"]] = r["content_hash"]

        return len(to_upsert)

    async def query_relevant(
        self,
        query_text: str,
        k: int = 7,
        min_similarity: float = 0.60,
        keep_alive: int = 300,
    ) -> list[dict]:
        """
        Embed query_text and return top-k most similar vault notes.
        LanceDB SIMD cosine search (replaces numpy brute-force).
        """
        try:
            self._table.checkout_latest()
        except Exception:
            pass
        if self.count() == 0:
            return []
        try:
            query_vec = await _embed_text(query_text, self.api_key, keep_alive=keep_alive)
        except Exception as e:
            print(f"[EMBED] Query embedding failed: {e}", flush=True)
            return []

        # Fetch more than k, then apply min_similarity filter
        results = (
            self._table.search(query_vec, vector_column_name="vector")
            .metric("cosine")
            .limit(max(k * 3, 20))
            .to_arrow().to_pylist()
        )

        # LanceDB cosine distance = 1 - cosine_similarity (for unit vectors)
        filtered = []
        for r in results:
            sim = 1.0 - r.get("_distance", 1.0)
            if sim >= min_similarity:
                filtered.append((sim, r))
        filtered.sort(key=lambda x: x[0], reverse=True)

        return [
            {
                "title":     r["title"],
                "aliases":   [a for a in r.get("aliases", "").split(",") if a],
                "tags":      [t for t in r.get("tags", "").split(",") if t],
                "note_type": r.get("note_type", ""),
                "similarity": round(sim, 4),
            }
            for sim, r in filtered[:k]
        ]

    def close(self) -> None:
        pass   # LanceDB connections are file-based; no explicit close needed
