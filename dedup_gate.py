"""
dedup_gate.py — Semantic deduplication gate for the Nexus capture pipeline.

Uses Model2Vec potion-base-2M (8 MB, 64-dim, ~10 ms on CPU) to detect
near-duplicate content AFTER extraction but BEFORE the expensive Gemini call.

Three outcomes:
  "skip"    — sim >= skip_threshold (0.92)  → duplicate, abort pipeline
  "flag"    — sim >= flag_threshold (0.75)  → related, set meta.related_captures
  "proceed" — below both thresholds         → novel content, proceed normally
"""
import sqlite3
from pathlib import Path

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False

DEDUP_DB  = Path.home() / ".nexus_dedup.db"
MODEL_ID  = "minishlab/potion-base-2M"
EMBED_DIM = 64   # potion-base-2M fixed output dimension


class DedupGate:
    """
    Lightweight semantic dedup gate.
    Thread-safe for reads; single-writer assumption (one daemon/server).
    """

    def __init__(
        self,
        skip_threshold: float = 0.92,
        flag_threshold: float = 0.75,
        db_path: Path = DEDUP_DB,
    ):
        self.skip_threshold = skip_threshold
        self.flag_threshold = flag_threshold
        self._model = None          # lazy — first check() call triggers ~8 MB download
        self._db = self._init_db(db_path)

    # ── Setup ─────────────────────────────────────────────────

    def _init_db(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                key       TEXT PRIMARY KEY,
                title     TEXT NOT NULL DEFAULT '',
                embedding BLOB NOT NULL
            )
        """)
        conn.commit()
        return conn

    def _get_model(self):
        if self._model is None:
            from model2vec import StaticModel
            print(f"[DEDUP] Loading {MODEL_ID} (first-time ~8 MB download)...", flush=True)
            self._model = StaticModel.from_pretrained(MODEL_ID)
            print("[DEDUP] Model ready.", flush=True)
        return self._model

    # ── Embedding ─────────────────────────────────────────────

    def _embed(self, text: str):
        """64-dim float32 numpy vector, L2-normalised."""
        import joblib
        # Force threading backend so loky never forks subprocess workers.
        # Forked workers leak POSIX semaphores that cause SIGSEGV in LanceDB/MLX/PyTorch.
        with joblib.parallel_config(backend='threading'):
            arr = self._get_model().encode([text[:4000]])[0]
        if not _NUMPY:
            return arr
        arr = arr.astype("float32")
        norm = float((arr * arr).sum() ** 0.5)
        return arr / norm if norm > 0 else arr

    # ── Public API ────────────────────────────────────────────

    def check(self, key: str, text: str) -> tuple[str, list[str]]:
        """
        Check if content is a near-duplicate of anything already indexed.

        Args:
            key:  Unique identifier (URL or content hash) — used for de-indexing
            text: Content to compare (transcript, body text, or URL)

        Returns:
            ("skip"|"flag"|"proceed", [matched_titles_or_keys])
        """
        if not _NUMPY:
            return "proceed", []

        query_vec = self._embed(text)
        rows = self._db.execute("SELECT key, title, embedding FROM seen").fetchall()
        if not rows:
            return "proceed", []

        keys   = [r[0] for r in rows]
        titles = [r[1] or r[0] for r in rows]
        mat    = np.stack([np.frombuffer(r[2], dtype="float32") for r in rows])
        sims   = mat @ query_vec          # cosine (pre-normalised vectors)

        best_idx = int(np.argmax(sims))
        best_sim = float(sims[best_idx])

        if best_sim >= self.skip_threshold:
            return "skip", [titles[best_idx]]
        if best_sim >= self.flag_threshold:
            matched = [titles[i] for i in np.where(sims >= self.flag_threshold)[0]]
            return "flag", matched
        return "proceed", []

    def index(self, key: str, text: str, title: str = "") -> None:
        """Add content to the dedup store after a successful capture."""
        if not _NUMPY:
            return
        vec  = self._embed(text)
        blob = vec.astype("float32").tobytes()
        self._db.execute(
            "INSERT OR REPLACE INTO seen (key, title, embedding) VALUES (?, ?, ?)",
            (key, title, blob),
        )
        self._db.commit()

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def close(self) -> None:
        self._db.close()
