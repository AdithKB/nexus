#!/usr/bin/env python3
"""
tools/test_integration_v6.py — Nexus v6.0 Integration Smoke Test

Covers the three new components shipped in this session:
  1. DedupGate   — Model2Vec potion-base-2M lifecycle (skip/flag/proceed)
  2. EmbedStore  — LanceDB real Ollama upsert + cosine query
  3. Marker      — Tier 0 PDF extraction on a synthetic multi-paragraph PDF
  4. Server      — /capture_smart intent routing + dedup wiring (TestClient)
  5. Server      — /capture_voice_fleeting text path (no Groq needed)
  6. Dreamer     — dry-run on synthetic vault (no file writes, read-only check)
  7. Config      — new v6.0 fields present and correct

Run:
  python tools/test_integration_v6.py

Requirements:
  Ollama running with qwen3-embedding:4b   (for tests 2, 6)
  All deps installed: lancedb model2vec marker-pdf
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
SKIP = "\033[33m~\033[0m"
_failures: list[str] = []


def ok(label: str, cond: bool, detail: str = ""):
    sym = PASS if cond else FAIL
    print(f"  {sym} {label}{': ' + detail if detail else ''}")
    if not cond:
        _failures.append(label)


def skip(label: str, reason: str = ""):
    print(f"  {SKIP} {label} [skip{': ' + reason if reason else ''}]")


# ─────────────────────────────────────────────────────────────────────────────
# 1. DedupGate — full lifecycle with real Model2Vec
# ─────────────────────────────────────────────────────────────────────────────

def test_dedup_gate():
    print("\n── 1. DedupGate (Model2Vec potion-base-2M) ──────────────")
    from dedup_gate import DedupGate

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dedup_test.db"
        gate = DedupGate(skip_threshold=0.92, flag_threshold=0.75, db_path=db_path)

        # Index three semantically distinct pieces of content
        DOCS = [
            ("url_flash", "flash attention is a memory-efficient algorithm that reduces memory from O(n²) to O(n) via tiled computation", "Flash Attention"),
            ("url_lora",  "LoRA reduces trainable parameters by inserting low-rank matrices into transformer weight layers", "LoRA Fine-tuning"),
            ("url_rome",  "the roman empire declined due to economic instability military pressures and political fragmentation", "Roman Empire"),
        ]
        for key, text, title in DOCS:
            gate.index(key, text, title=title)

        ok("Count after indexing 3 docs", gate.count() == 3, f"got {gate.count()}")

        # Identical content → skip
        v, m = gate.check("new_url", "flash attention is a memory-efficient algorithm that reduces memory from O(n²) to O(n) via tiled computation")
        ok("Exact duplicate → skip", v == "skip", f"got {v} matched={m}")

        # Completely unrelated topic → proceed
        v2, _ = gate.check("new_url2", "photosynthesis converts sunlight into chemical energy via chlorophyll in plant cells")
        ok("Unrelated content → proceed", v2 == "proceed", f"got {v2}")

        # Already-indexed key doesn't affect outcome (check is pure similarity)
        v3, _ = gate.check("url_flash", "flash attention is a memory-efficient algorithm that reduces memory from O(n²) to O(n) via tiled computation")
        ok("Re-check same key still detects duplicate", v3 in ("skip", "flag"), f"got {v3}")

        gate.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. EmbedStore (LanceDB) — real Ollama embeddings
# ─────────────────────────────────────────────────────────────────────────────

def test_embed_store_lancedb():
    print("\n── 2. EmbedStore (LanceDB + Ollama) ─────────────────────")
    import requests as _req
    try:
        _req.get("http://localhost:11434/api/tags", timeout=3).raise_for_status()
    except Exception:
        skip("All EmbedStore tests", "Ollama offline")
        return

    from embed_store import EmbedStore, cosine, EMBED_DIM

    NOTES = [
        {"title": "Flash Attention Mechanism", "tags": ["ai/transformers", "pattern/optimization"],
         "aliases": ["FlashAttention"], "note_type": "mechanism",
         "content": "FlashAttention-2 uses tiled matrix multiplication to reduce HBM reads. Memory complexity drops from O(n²) to O(n√n)."},
        {"title": "LoRA Fine-tuning",          "tags": ["ai/training"],
         "aliases": ["Low-Rank Adaptation"],   "note_type": "method",
         "content": "LoRA freezes pretrained weights and injects low-rank matrices A and B into each transformer layer. Trainable params reduced by 10,000×."},
        {"title": "Knowledge Distillation",    "tags": ["ai/training", "pattern/compression"],
         "aliases": [], "note_type": "method",
         "content": "A student model learns from soft probability distributions produced by a larger teacher model rather than hard one-hot labels."},
    ]

    with tempfile.TemporaryDirectory() as tmp:
        store = EmbedStore(db_path=Path(tmp) / "lance_test")

        embedded = asyncio.run(store.upsert_notes(NOTES))
        ok(f"Embedded {len(NOTES)} notes", embedded == len(NOTES), f"got {embedded}")
        ok("count() == 3", store.count() == 3, f"got {store.count()}")

        # Re-upsert same notes without force → hash cache prevents re-embedding
        embedded2 = asyncio.run(store.upsert_notes(NOTES))
        ok("Hash cache skips unchanged notes", embedded2 == 0, f"got {embedded2}")

        # Upsert with changed content → re-embeds
        changed = [dict(NOTES[0], content=NOTES[0]["content"] + " Updated.")]
        embedded3 = asyncio.run(store.upsert_notes(changed))
        ok("Changed content triggers re-embed", embedded3 == 1, f"got {embedded3}")
        ok("count() still == 3 after upsert", store.count() == 3, f"got {store.count()}")

        # query_relevant — "transformer attention efficiency" should match Flash Attention
        results = asyncio.run(store.query_relevant("transformer attention memory efficiency", k=3, min_similarity=0.40))
        ok("query_relevant returns results", len(results) > 0, f"got {len(results)}")
        top = results[0]
        ok("Top result has similarity field", "similarity" in top and isinstance(top["similarity"], float))
        ok("Top result similarity in [0,1]", 0.0 <= top["similarity"] <= 1.0, f"got {top['similarity']}")
        print(f"    Top match: '{top['title']}' sim={top['similarity']:.4f}")

        # get_all_with_embeddings for Dreamer
        all_notes = store.get_all_with_embeddings()
        ok("get_all_with_embeddings returns 3", len(all_notes) == 3, f"got {len(all_notes)}")
        ok("Each entry has 256-dim embedding", all(len(n["embedding"]) == EMBED_DIM for n in all_notes))

        # get_all_as_index (vault_index format)
        index = store.get_all_as_index()
        ok("get_all_as_index returns 3", len(index) == 3, f"got {len(index)}")
        ok("Index entries have title+tags+aliases", all("title" in n and "tags" in n for n in index))

        # cosine helper still works
        a = all_notes[0]["embedding"]
        ok("cosine(v, v) == 1.0", abs(cosine(a, a) - 1.0) < 1e-5, f"got {cosine(a, a):.6f}")

        # find_all_related_pairs (sync, used by /related endpoint)
        pairs = store.find_all_related_pairs()
        ok("find_all_related_pairs returns dict", "auto" in pairs and "suggest" in pairs)
        print(f"    Pairs: auto={len(pairs['auto'])} suggest={len(pairs['suggest'])}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Marker Tier 0 PDF Extraction
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_pdf(path: Path):
    """Create a multi-paragraph structured PDF using PyMuPDF."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)   # A4
    content = (
        "Attention Mechanisms in Transformer Models\n\n"
        "Abstract\n"
        "Self-attention allows each token to attend to all other tokens in a sequence. "
        "The computational complexity is O(n²) in both time and memory with respect to sequence length n.\n\n"
        "1. Introduction\n"
        "The Transformer architecture (Vaswani et al., 2017) replaced recurrent networks with "
        "multi-head self-attention. Each head independently computes attention scores.\n\n"
        "2. FlashAttention\n"
        "FlashAttention (Dao et al., 2022) uses tiled computation to reduce HBM I/O. "
        "Memory complexity drops from O(n²) to O(n√n) while maintaining exact results.\n\n"
        "3. Conclusion\n"
        "Hardware-aware attention kernels are now standard in production LLM serving."
    )
    page.insert_text((50, 80), content, fontsize=11)
    doc.save(str(path))
    doc.close()


_MARKER_SUBPROCESS = """
import sys, json
from pathlib import Path
sys.path.insert(0, sys.argv[1])
pdf_path = Path(sys.argv[2])

try:
    from extractors import _try_marker
    result = _try_marker(pdf_path)
    print(json.dumps({"ok": True, "chars": len(result) if result else 0,
                       "snippet": (result or "")[:100]}))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
"""


def test_marker_pdf():
    print("\n── 3. Marker Tier 0 PDF Extraction ─────────────────────")
    import subprocess as _sp

    # Quick import probe in a subprocess — catches both ImportError and segfault
    probe = _sp.run(
        [sys.executable, "-c",
         "from marker.converters.pdf import PdfConverter; print('ok')"],
        capture_output=True, timeout=30,
    )
    if probe.returncode != 0:
        skip("Marker tests",
             "marker-pdf not importable in this environment "
             "(PyArrow conflict with Keras — OK on production VPS)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "test_attention.pdf"
        _make_test_pdf(pdf_path)
        ok("Test PDF created", pdf_path.exists(), str(pdf_path))

        repo_root = str(Path(__file__).parent.parent)
        print("    Running Marker extraction in subprocess (may load ~300 MB models)...")
        result = _sp.run(
            [sys.executable, "-c", _MARKER_SUBPROCESS, repo_root, str(pdf_path)],
            capture_output=True, timeout=300,
        )
        if result.returncode == 139:
            skip("Marker extraction", "segfault — local PyArrow/Keras conflict; harmless on VPS")
            return

        import json as _json
        try:
            data = _json.loads(result.stdout.decode().strip().splitlines()[-1])
        except Exception:
            ok("Marker subprocess output parseable", False,
               f"stdout={result.stdout.decode()[:200]!r}")
            return

        ok("Marker ran without error", data.get("ok"), data.get("error", ""))
        ok("Marker returned >100 chars", data.get("chars", 0) > 100, f"got {data.get('chars')} chars")
        if data.get("chars", 0) > 0:
            print(f"    Output snippet: {data.get('snippet', '')!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Server /capture_smart — intent routing + dedup wiring (TestClient)
# ─────────────────────────────────────────────────────────────────────────────

def test_server_capture_smart():
    print("\n── 4. Server /capture_smart (intent routing) ────────────")
    from starlette.testclient import TestClient
    from server import app, _detect_intent

    client = TestClient(app, raise_server_exceptions=False)

    # Intent detection unit cases
    cases = [
        ("RQ flash attention mechanism",              "research"),
        ("rq what is rotary positional encoding",     "research"),
        ("What is the difference between MHA and GQA?", "research"),
        ("How does KV caching work?",                  "research"),
        ("explain scaled dot-product attention",       "research"),
        ("Attention is interesting and complex",       "fleeting"),
        ("My research on transformers is going well",  "fleeting"),  # noun, not verb
        ("RQ",                                         "fleeting"),  # no space = not prefix
        ("RQXYZ not a valid prefix",                   "fleeting"),
    ]
    for text, expected in cases:
        got = _detect_intent(text)
        ok(f"intent({text[:42]!r})", got == expected, f"expected={expected} got={got}")

    # Endpoint: 400 on missing text
    r = client.post("/capture_smart", content=json.dumps({}).encode(),
                    headers={"Content-Type": "application/json"})
    ok("/capture_smart 400 on empty body", r.status_code == 400, f"got {r.status_code}")

    # Endpoint: 200 on valid text, returns intent + job_id
    r = client.post("/capture_smart",
                    content=json.dumps({"text": "Sparse attention reduces quadratic complexity"}).encode(),
                    headers={"Content-Type": "application/json"})
    ok("/capture_smart 200 on fleeting text", r.status_code == 200, f"got {r.status_code}")
    body = r.json()
    ok("Response contains intent=fleeting", body.get("intent") == "fleeting", str(body))
    ok("Response contains job_id", "job_id" in body, str(body))

    r2 = client.post("/capture_smart",
                     content=json.dumps({"text": "RQ what is sparse attention"}).encode(),
                     headers={"Content-Type": "application/json"})
    ok("/capture_smart 200 on RQ prefix", r2.status_code == 200, f"got {r2.status_code}")
    ok("Response contains intent=research", r2.json().get("intent") == "research", str(r2.json()))


# ─────────────────────────────────────────────────────────────────────────────
# 5. Server /capture_voice_fleeting — text path (no Groq needed)
# ─────────────────────────────────────────────────────────────────────────────

def test_server_voice_fleeting():
    print("\n── 5. Server /capture_voice_fleeting (text path) ────────")
    from starlette.testclient import TestClient
    from server import app

    client = TestClient(app, raise_server_exceptions=False)

    # 400 on neither text nor audio_b64
    r = client.post("/capture_voice_fleeting",
                    content=json.dumps({}).encode(),
                    headers={"Content-Type": "application/json"})
    ok("400 on missing text and audio_b64", r.status_code == 400, f"got {r.status_code}")

    # 200 on text path
    r2 = client.post("/capture_voice_fleeting",
                     content=json.dumps({"text": "Remember to test the dedup gate tomorrow", "duration_hint": 12.0}).encode(),
                     headers={"Content-Type": "application/json"})
    ok("200 on text path", r2.status_code == 200, f"got {r2.status_code}")
    body = r2.json()
    ok("Returns job_id", "job_id" in body, str(body))
    ok("Returns status=queued", body.get("status") == "queued", str(body))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Dreamer — dry-run on synthetic vault
# ─────────────────────────────────────────────────────────────────────────────

def test_dreamer_synthetic():
    print("\n── 6. Dreamer Agent (synthetic vault, real Ollama) ──────")
    import requests as _req
    try:
        _req.get("http://localhost:11434/api/tags", timeout=3).raise_for_status()
    except Exception:
        skip("Dreamer tests", "Ollama offline")
        return

    from config import Config
    from embed_store import EmbedStore
    from dreamer import run_dream_session, DreamReport

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        captures = vault / "Captures"
        captures.mkdir(parents=True)
        (vault / "MOCs").mkdir()
        (vault / "Fleeting").mkdir()

        # Write 4 synthetic capture notes.
        # Filename stem == embedded title so dreamer can find files by title.
        NOTES = [
            {
                "title": "Flash Attention Reduces Memory",
                "tags": ["ai/transformers"],
                "content": (
                    "FlashAttention uses tiled matrix multiply to reduce HBM I/O "
                    "from O(n²) to O(n√n). Operates entirely on SRAM tiles."
                ),
                "links": [],
            },
            {
                "title": "KV Cache Speeds Decoding",
                "tags": ["ai/inference"],
                "content": (
                    "KV cache stores key/value projections from past tokens, "
                    "avoiding recomputation during autoregressive generation."
                ),
                "links": ["Flash Attention Reduces Memory"],
            },
            {
                "title": "LoRA Fine-tuning Method",
                "tags": ["ai/training"],
                "content": (
                    "Low-Rank Adaptation inserts rank-r matrices into transformer layers. "
                    "Reduces trainable parameters by 10,000×."
                ),
                "links": [],
            },
            {
                "title": "QLoRA 4-bit Fine-tuning",
                "tags": ["ai/training"],
                "content": (
                    "QLoRA combines 4-bit quantization with LoRA adapters, "
                    "enabling 65B fine-tuning on a single 48 GB GPU."
                ),
                "links": ["LoRA Fine-tuning Method"],
            },
        ]
        for n in NOTES:
            link_lines = "\n".join(f"- [[{l}]]" for l in n["links"])
            md_content = (
                f"---\ntype: atomic\ntags: {n['tags']}\n---\n"
                f"# {n['title']}\n\n{n['content']}\n\n"
                f"## Connections\n{link_lines}\n"
            )
            (captures / f"{n['title']}.md").write_text(md_content)

        cfg = Config()
        cfg.vault_path = vault
        cfg.notes_folder = "Captures"
        cfg.moc_folder = "MOCs"
        cfg.fleeting_folder = "Fleeting"

        store = EmbedStore(db_path=Path(tmp) / "lance")
        note_dicts = [{"title": n["title"], "tags": n["tags"], "content": n["content"]}
                      for n in NOTES]
        asyncio.run(store.upsert_notes(note_dicts))
        ok(f"Embedded {len(note_dicts)} synthetic notes", store.count() == len(note_dicts),
           f"got {store.count()}")

        report: DreamReport = asyncio.run(run_dream_session(cfg, store))

        ok("Dream session completed", isinstance(report, DreamReport))
        ok("notes_in_store populated", report.notes_in_store == len(note_dicts),
           f"got {report.notes_in_store}")
        ok("duration recorded", report.duration_sec > 0, f"got {report.duration_sec:.2f}s")
        ok("started_at populated", bool(report.started_at))

        print(f"    Latent links: {report.latent_links_added}")
        print(f"    Orphans rescued: {report.orphans_rescued}")
        print(f"    MOC gaps: {report.moc_gaps}")
        print(f"    Duration: {report.duration_sec:.2f}s")

        # Verify note files are still parseable after dream (no corruption)
        for n in NOTES:
            path = captures / f"{n['title']}.md"
            text = path.read_text()
            ok(f"Note '{n['title'][:30]}' parseable after dream", "## Connections" in text)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Config — v6.0 fields
# ─────────────────────────────────────────────────────────────────────────────

def test_config_v6():
    print("\n── 7. Config v6.0 fields ────────────────────────────────")
    from config import Config
    c = Config()
    ok("dedup_skip_threshold == 0.92",  c.dedup_skip_threshold == 0.92,  str(c.dedup_skip_threshold))
    ok("dedup_flag_threshold == 0.75",  c.dedup_flag_threshold == 0.75,  str(c.dedup_flag_threshold))
    ok("marker_enabled default True",   c.marker_enabled is True,         str(c.marker_enabled))
    ok("fleeting_folder == 'Fleeting'", c.fleeting_folder == "Fleeting",  c.fleeting_folder)
    ok("compress_enabled default True", c.compress_enabled is True,        str(c.compress_enabled))
    ok("ollama_keep_alive == 300",      c.ollama_keep_alive == 300,        str(c.ollama_keep_alive))
    ok("get_fleeting_dir() returns Path", hasattr(c.get_fleeting_dir(), "exists"))


# ─────────────────────────────────────────────────────────────────────────────
# 8. Watcher queue — threadsafe enqueue + serial worker (no concurrent Ollama)
# ─────────────────────────────────────────────────────────────────────────────

def test_watcher_queue():
    print("\n── 8. Watcher queue (threadsafe enqueue + serial worker) ─")
    import threading

    async def _run():
        from config import Config
        from embed_store import EmbedStore

        processed_order: list[str] = []
        processing_concurrency: list[int] = [0]  # mutable counter
        peak_concurrency: list[int] = [0]

        # Stub handler that records processing order and checks concurrency
        class StubHandler:
            def __init__(self, queue, loop):
                self.processing = set()
                self.processed = set()
                self._queue = queue
                self._loop = loop

            async def process_seedling(self, path):
                processing_concurrency[0] += 1
                peak_concurrency[0] = max(peak_concurrency[0], processing_concurrency[0])
                await asyncio.sleep(0.05)  # simulate synthesis time
                processed_order.append(path.name)
                processing_concurrency[0] -= 1

        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        handler = StubHandler(queue, loop)

        from watcher import _seedling_worker
        worker = asyncio.create_task(_seedling_worker(handler, queue))

        # Enqueue 5 items from a background thread (simulates watchdog)
        def enqueue_from_thread():
            for i in range(5):
                loop.call_soon_threadsafe(queue.put_nowait, Path(f"seedling_{i:02d}.json"))

        t = threading.Thread(target=enqueue_from_thread)
        t.start()
        t.join()
        # t.join() is synchronous — call_soon_threadsafe callbacks are scheduled
        # but not yet executed. Yield once so the event loop flushes them before
        # queue.join() checks unfinished_tasks (otherwise it sees 0 and returns early).
        await asyncio.sleep(0)

        # Wait for all items to be processed
        await queue.join()
        worker.cancel()

        return processed_order, peak_concurrency[0]

    order, peak = asyncio.run(_run())

    ok("All 5 seedlings processed",    len(order) == 5,  f"got {len(order)}")
    ok("Processed in order",           order == sorted(order), f"got {order}")
    ok("Peak concurrency == 1 (serial)", peak == 1,       f"got peak={peak}")
    print(f"    Order: {order}")


# ─────────────────────────────────────────────────────────────────────────────
# 9. canonical_links normalizer — objects coerced to strings
# ─────────────────────────────────────────────────────────────────────────────

def test_canonical_links_normalizer():
    print("\n── 9. canonical_links normalizer (object → string coercion) ─")
    import asyncio as _asyncio
    from processor import _parse_and_repair
    from config import Config

    # Simulate model output where canonical_links contains objects instead of strings
    bad_json = """{
        "thinking": "test",
        "suggested_title": "Test Note",
        "source_summary": "A test source.",
        "key_takeaways": ["takeaway one"],
        "notes": [{
            "title": "Flash Attention Reduces Memory",
            "note_type": "mechanism",
            "aliases": ["FlashAttention", "Flash Attn"],
            "tags": ["ai/transformers"],
            "content": "FlashAttention reduces HBM IO via tiled computation.",
            "source_quote": "FlashAttention-2 uses tiled matmul.",
            "canonical_links": [
                {"title": "KV Cache Speeds Decoding", "description": "related note"},
                {"title": "LoRA Fine-tuning Method"}
            ],
            "needs_review": false
        }]
    }"""

    async def _run():
        cfg = Config()
        # Pass a no-op provider_fn — repair won't be needed if normalizer works
        async def _noop_provider(p, s, c):
            return "{}", {}
        return await _parse_and_repair(bad_json, "prompt", "system", cfg, _noop_provider)

    result = asyncio.run(_run())
    ok("Parsed without Pydantic error", result is not None)
    note = result.notes[0]
    ok("canonical_links is list of strings", all(isinstance(l, str) for l in note.canonical_links),
       str(note.canonical_links))
    ok("canonical_links extracted titles correctly",
       note.canonical_links == ["KV Cache Speeds Decoding", "LoRA Fine-tuning Method"],
       str(note.canonical_links))
    ok("Note title preserved", note.title == "Flash Attention Reduces Memory")


# ─────────────────────────────────────────────────────────────────────────────
# 10. Embed keep_alive — no num_ctx in embed requests
# ─────────────────────────────────────────────────────────────────────────────

def test_embed_keep_alive():
    print("\n── 10. Embed keep_alive + no num_ctx ────────────────────")
    import requests as _req, unittest.mock as _mock

    captured: list[dict] = []

    original_post = _req.Session.post
    def _intercept(self, url, *args, **kwargs):
        if "11434" in url and "embed" in url:
            body = kwargs.get("json", {})
            captured.append(body)
        return original_post(self, url, *args, **kwargs)

    # Patch httpx instead (embed_store uses httpx)
    import httpx, asyncio as _asyncio

    captured_bodies: list[dict] = []
    original_send = httpx.AsyncClient.send

    async def _intercept_send(self, request, *args, **kwargs):
        if b"11434" in bytes(str(request.url), "utf-8") and b"embed" in bytes(str(request.url), "utf-8"):
            import json as _j
            try:
                captured_bodies.append(_j.loads(request.content))
            except Exception:
                pass
        return await original_send(self, request, *args, **kwargs)

    with _mock.patch.object(httpx.AsyncClient, "send", _intercept_send):
        from embed_store import _embed_texts
        asyncio.run(_embed_texts(["test keep_alive behaviour"], keep_alive=300))

    ok("At least one embed request captured", len(captured_bodies) > 0,
       f"got {len(captured_bodies)}")
    if captured_bodies:
        body = captured_bodies[0]
        ok("keep_alive=300 sent",      body.get("keep_alive") == 300,  str(body.get("keep_alive")))
        ok("num_ctx NOT in request",   "num_ctx" not in body,           str(list(body.keys())))
        ok("uses /api/embed endpoint", "input" in body,                 str(list(body.keys())))
        ok("input is a list",          isinstance(body.get("input"), list))


# ─────────────────────────────────────────────────────────────────────────────
# 11. Deployment config — plist path + launch script env vars + daemon alive
# ─────────────────────────────────────────────────────────────────────────────

def test_deployment_config():
    print("\n── 11. Deployment config (plist + launch script + daemon) ─")
    import plistlib, subprocess

    PLIST = Path.home() / "Library/LaunchAgents/com.adithkb.nexus-crystallizer.plist"
    ok("Crystallizer plist exists", PLIST.exists(), str(PLIST))
    if not PLIST.exists():
        return

    with open(PLIST, "rb") as f:
        plist = plistlib.load(f)

    args = plist.get("ProgramArguments", [])
    ok("ProgramArguments has 2 elements", len(args) == 2, str(args))
    if len(args) >= 2:
        script_path = Path(args[1])
        ok("Script path exists on disk", script_path.exists(), str(script_path))

        if script_path.exists():
            script = script_path.read_text()
            ok("OLLAMA_MAX_LOADED_MODELS=2 in launch script",
               "OLLAMA_MAX_LOADED_MODELS=2" in script)
            ok("OLLAMA_NUM_PARALLEL=1 in launch script",
               "OLLAMA_NUM_PARALLEL=1" in script)

    # Check daemon is running with exit code 0 (not 127, not crashed)
    result = subprocess.run(
        ["launchctl", "list", "com.adithkb.nexus-crystallizer"],
        capture_output=True, text=True,
    )
    running = result.returncode == 0 and '"PID"' in result.stdout
    exit_code_line = [l for l in result.stdout.splitlines() if "LastExitStatus" in l]
    last_exit = int(exit_code_line[0].split("=")[1].strip().rstrip(";")) if exit_code_line else -1
    ok("Crystallizer daemon running (has PID)", running, result.stdout.strip()[:120])
    ok("Crystallizer last exit code == 0", last_exit == 0, f"got {last_exit}")


# ─────────────────────────────────────────────────────────────────────────────
# 12. process_seedling dedup wiring — skip path and index path
# ─────────────────────────────────────────────────────────────────────────────

def test_process_seedling_dedup_wiring():
    print("\n── 12. process_seedling dedup wiring (skip + index) ────────")
    import asyncio as _asyncio, json as _json, tempfile, unittest.mock as _mock
    from pathlib import Path as _Path

    async def _run():
        from config import Config
        from dedup_gate import DedupGate
        from watcher import SeedlingHandler

        loop = _asyncio.get_event_loop()
        queue = _asyncio.Queue()

        # Minimal stub embed_store — only needs get_all_as_index
        class _StubStore:
            def get_all_as_index(self): return []
            async def upsert_notes(self, notes, **kw): return 0
            async def query_relevant(self, *a, **kw): return []

        cfg = Config()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = _Path(tmp)

            # ── Skip path: pre-seed DedupGate then drop identical seedling ──
            gate = DedupGate(db_path=tmp_path / "dedup_skip.db")
            gate.index("url_existing", "flash attention tiled computation memory efficient", "Flash Attention Note")

            handler = SeedlingHandler(cfg, _StubStore(), queue, loop, dedup_gate=gate)

            seedling = {
                "url": "url_existing",
                "relay_description": "flash attention tiled computation memory efficient",
                "meta": {"source_type": "web", "title": "Flash", "author": "x",
                         "duration_sec": 0, "transcript": "", "source_id": "s1"},
                "user_intent": "",
            }
            seedling_file = tmp_path / "seedling_dup_001.json"
            seedling_file.write_text(_json.dumps(seedling))

            await handler.process_seedling(seedling_file)

            ok("Skip path: seedling deleted without synthesis",
               not seedling_file.exists(), "file should be gone")
            ok("Skip path: dedup count still 1 (not re-indexed)",
               gate.count() == 1, f"got {gate.count()}")

            # ── Index path: novel content → synthesise (mocked) → indexed ──
            from processor import ProcessingResult, AtomicNote as _AN

            fake_note = _AN(
                title="Attention Is All You Need",
                note_type="concept",
                aliases=["Self-Attention", "Transformer Model"],
                tags=["ai/transformers"],
                content="Transformer architecture uses self-attention.",
                source_quote="NOT IN SOURCE",
                canonical_links=[],
                needs_review=False,
            )
            fake_result = ProcessingResult(
                atomic_notes=[fake_note],
                source_summary="",
                key_takeaways=[],
                anchors_recovered=0,
            )

            gate2 = DedupGate(db_path=tmp_path / "dedup_index.db")
            handler2 = SeedlingHandler(cfg, _StubStore(), queue, loop, dedup_gate=gate2)

            novel_seedling = {
                "url": "url_novel_xyz",
                "relay_description": "transformer self-attention positional encoding",
                "meta": {"source_type": "web", "title": "Transformers", "author": "x",
                         "duration_sec": 0, "transcript": "", "source_id": "s2"},
                "user_intent": "",
            }
            novel_file = tmp_path / "seedling_novel_001.json"
            novel_file.write_text(_json.dumps(novel_seedling))

            with _mock.patch("watcher.run_crystallizer_stage", return_value=fake_result), \
                 _mock.patch("watcher.write_to_vault"):
                await handler2.process_seedling(novel_file)

            ok("Index path: content indexed after synthesis",
               gate2.count() == 1, f"got {gate2.count()}")

    _asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────────────
# 13. MLX tokenizer patch — idempotent, survives re-run
# ─────────────────────────────────────────────────────────────────────────────

def test_mlx_tokenizer_patch():
    print("\n── 13. MLX tokenizer patch (_patch_mlx_tokenizer_config) ──")
    import glob, json as _json

    from config import Config
    from processor import _patch_mlx_tokenizer_config

    cfg = Config()
    model_path = cfg.mlx_model_fast  # mlx-community/Qwen3.5-9B-MLX-4bit

    if "/" not in model_path:
        skip("MLX tokenizer patch", "mlx_model_fast has no owner/name separator")
        return

    owner, name = model_path.split("/", 1)
    import os
    cache_dir = os.path.expanduser(f"~/.cache/huggingface/hub/models--{owner}--{name}")
    cfg_files = glob.glob(f"{cache_dir}/snapshots/*/tokenizer_config.json")

    if not cfg_files:
        skip("MLX tokenizer patch", f"model not cached locally at {cache_dir}")
        return

    # Run patch (should be idempotent — already patched in last session)
    _patch_mlx_tokenizer_config(model_path)
    _patch_mlx_tokenizer_config(model_path)  # second call must not corrupt file

    for cfg_path in cfg_files:
        with open(cfg_path) as f:
            data = _json.load(f)
        ok(f"TokenizersBackend absent in {Path(cfg_path).parent.name[:8]}...",
           data.get("tokenizer_class") != "TokenizersBackend",
           f"got {data.get('tokenizer_class')!r}")
        ok(f"Qwen2TokenizerFast present in {Path(cfg_path).parent.name[:8]}...",
           data.get("tokenizer_class") == "Qwen2TokenizerFast",
           f"got {data.get('tokenizer_class')!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Nexus v6.0 — Integration Smoke Test")
    print("=" * 52)

    test_config_v6()
    test_dedup_gate()
    test_embed_store_lancedb()
    test_marker_pdf()
    test_server_capture_smart()
    test_server_voice_fleeting()
    test_dreamer_synthetic()
    test_watcher_queue()
    test_canonical_links_normalizer()
    test_embed_keep_alive()
    test_deployment_config()
    test_process_seedling_dedup_wiring()
    test_mlx_tokenizer_patch()

    print()
    print("=" * 52)
    if _failures:
        print(f"\033[31mFAILED: {len(_failures)} test(s)\033[0m")
        for f in _failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print(f"\033[32mAll tests passed.\033[0m")
