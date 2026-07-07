"""
Nexus MCP Server — Model Context Protocol bridge for Obsidian Vault.
Exposes semantic search, note retrieval, and agentic research tools.

Author: Adith K B
Version: 1.6 (April 2026)
"""
import asyncio
import re
import json
import hashlib
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

import httpx
from mcp.server import Server
import mcp.types as types
from mcp.server.stdio import stdio_server

from embed_store import EmbedStore
from config import Config
from vault import slugify, write_to_vault, check_duplicate
from processor import process_source
from extractors import extract_source, SourceMeta

# ── Initialization ───────────────────────────────────────────

config = Config()
# Ensure store uses local Qwen3 embeddings
store = EmbedStore()

# Robustness: Validate vault path on startup
if not config.vault_path.exists():
    import sys
    print(f"CRITICAL ERROR: Vault path not found at {config.vault_path}. Check config.py.", file=sys.stderr)

app = Server("nexus-vault")

# ── Helpers ──────────────────────────────────────────────────

def _load_note_title(title: str) -> Optional[Path]:
    """Resolve a note title to a file path with fuzzy matching support."""
    # 1. Exact slugified match
    clean_title = title.strip("[]")
    filename = f"{slugify(clean_title)}.md"
    
    for subdir in ["Captures", "MOCs", "Sources"]:
        path = config.vault_path / subdir / filename
        if path.exists():
            return path
            
    # 2. Case-insensitive search (Robustness upgrade)
    for subdir in ["Captures", "MOCs", "Sources"]:
        dir_path = config.vault_path / subdir
        if not dir_path.exists():
            continue
        for f in dir_path.glob("*.md"):
            if f.stem.lower() == slugify(clean_title).lower():
                return f
                
    return None

def _parse_connections(note_path: Path) -> List[Dict]:
    """Parse ## Connections section from a note file."""
    if not note_path.exists():
        return []
    
    try:
        content = note_path.read_text(encoding="utf-8")
        # Match ## Connections section until next header or end of file
        match = re.search(r"## Connections\n(.*?)(?:\n##|\n---|$)", content, re.DOTALL)
        if not match:
            return []
        
        connections_text = match.group(1)
        connections = []
        # Match lines like: - [[Title]] — relation
        # or - [[Title]] — *relation*
        pattern = r"-\s*\[\[(.*?)\]\]\s*—\s*\*?([\w_]+)\*?"
        for title, rel in re.findall(pattern, connections_text):
            connections.append({"title": title, "rel": rel})
        return connections
    except Exception:
        return []

def _traverse_graph(title: str, current_hop: int, max_hops: int, visited: set) -> List[Dict]:
    """Recursively follow links up to max_hops."""
    if current_hop >= max_hops:
        return []
    
    note_path = _load_note_title(title)
    if not note_path:
        return []
        
    connections = _parse_connections(note_path)
    results = []
    for conn in connections:
        conn_title = conn["title"]
        if conn_title in visited:
            continue
        
        visited.add(conn_title)
        # Traverse deeper
        children = _traverse_graph(conn_title, current_hop + 1, max_hops, visited)
        results.append({
            "title": conn_title, 
            "rel": conn["rel"], 
            "children": children
        })
    return results

def _format_graph_output(tree: Dict, indent: int = 2) -> str:
    """Recursively format the traversal tree."""
    output = ""
    for child in tree.get("children", []):
        prefix = " " * indent + "└─ "
        output += f"{prefix}{child['rel']} → [[{child['title']}]]\n"
        output += _format_graph_output(child, indent + 5)
    return output

def _collect_all_nodes(tree: Dict, nodes: set):
    """Gather all unique titles from a traversal tree."""
    nodes.add(tree["title"])
    for child in tree.get("children", []):
        _collect_all_nodes(child, nodes)

def _log_action(action: str, details: str):
    """Log action to ~/.nexus-sync.log."""
    log_path = Path.home() / ".nexus-sync.log"
    timestamp = datetime.now().isoformat()
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{action}] {timestamp} | {details}\n")
    except Exception:
        pass

async def _notify_updated(uri: str = None):
    """Emit notifications/resources/updated if session is available."""
    try:
        if hasattr(app, "request_context") and hasattr(app.request_context, "session"):
            await app.request_context.session.send_resource_updated(uri=uri)
    except Exception:
        pass

async def _extract_async(url: str, config: Config) -> SourceMeta:
    """Run blocking extract_source in an executor to prevent event loop stalls."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, extract_source, url, config)

# ── Resources ────────────────────────────────────────────────

@app.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    """List vault notes as browsable resources."""
    resources = []
    meta_files = {"_INDEX.md", "CLAUDE.md", "GEMINI.md"}
    
    for subdir in ["Captures", "MOCs", "Sources"]:
        dir_path = config.vault_path / subdir
        if not dir_path.exists():
            continue
            
        for f in dir_path.glob("*.md"):
            if f.name in meta_files:
                continue
                
            # URI: nexus://Subdir/Stem
            uri = f"nexus://{subdir}/{f.stem}"
            resources.append(types.Resource(
                uri=uri,
                name=f.stem.replace("-", " ").replace("_", " "),
                mimeType="text/markdown"
            ))
    return resources

@app.read_resource()
async def handle_read_resource(uri: str) -> list[types.TextResourceContents]:
    """Read content of a vault note resource."""
    if not uri.startswith("nexus://"):
        raise ValueError(f"Invalid URI scheme: {uri}")
        
    path_parts = uri[len("nexus://"):].split("/", 1)
    if len(path_parts) != 2:
        raise ValueError(f"Invalid URI format: {uri}")
        
    subdir, stem = path_parts
    if subdir not in ["Captures", "MOCs", "Sources"]:
        raise ValueError(f"Invalid vault subdirectory: {subdir}")
        
    file_path = config.vault_path / subdir / f"{stem}.md"
    if not file_path.exists():
        raise ValueError(f"Resource not found: {uri}")
        
    content = file_path.read_text(encoding="utf-8")
    return [types.TextResourceContents(
        uri=uri,
        text=content,
        mimeType="text/markdown"
    )]

# ── Tools ─────────────────────────────────────────────────────

@app.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="search_vault",
            description="Semantically search your Obsidian vault for relevant concepts, theories, and notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The research topic or question to search for."},
                    "k": {"type": "integer", "description": "Number of results to return (default 7).", "default": 7}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="read_note",
            description="Retrieve the full content of a specific note from your vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The exact title of the note."}
                },
                "required": ["title"]
            }
        ),
        types.Tool(
            name="list_recent_captures",
            description="List the most recently created or updated notes in your vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max number of notes to show.", "default": 10}
                }
            }
        ),
        types.Tool(
            name="graph_search",
            description="Multi-hop graph traversal of the vault relation graph. Returns how concepts connect across notes, not just which notes are relevant.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The research topic or question to start the graph traversal from."},
                    "hops": {"type": "integer", "description": "Number of traversal hops (default 2).", "default": 2},
                    "k": {"type": "integer", "description": "Number of seed notes to start from (default 5).", "default": 5}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="inspect_graph_schema",
            description="Inspect the vault's knowledge graph schema — returns all relation types used in ## Connections sections with frequency counts. Use this before graph_search to know what edge labels exist in this vault.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="research_to_vault",
            description="Perform web research on a URL and automatically crystallize it into atomic notes in your vault using the Nexus v4.2 pipeline.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to research (web article, documentation, etc.)."},
                    "user_intent": {"type": "string", "description": "Optional: Specific goal for the research.", "default": "capture key insights"}
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="research_topic",
            description="Perform multi-source research on a topic. Provide URLs directly, or provide search_query for autonomous source discovery. Synthesizes knowledge sequentially for cross-source linking.",
            inputSchema={
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "List of URLs to research (max 4). Optional if search_query is provided."},
                    "search_query": {"type": "string", "description": "Auto-discover sources via Jina search. Used when urls is empty."},
                    "topic_intent": {"type": "string", "description": "The core research goal or topic name."},
                    "max_notes_per_source": {"type": "integer", "description": "Limit notes created per URL (default 5).", "default": 5},
                    "generate_moc": {"type": "boolean", "description": "Create a grouping MOC (default true).", "default": True},
                    "generate_synthesis": {"type": "boolean", "description": "Create a synthesis note after all sources (default false). Marked needs_review.", "default": False}
                },
                "required": ["topic_intent"]
            }
        ),
        types.Tool(
            name="create_note",
            description="Manually create a new atomic note in the vault with proper frontmatter and semantic indexing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Declarative sentence title (Title Case)."},
                    "content": {"type": "string", "description": "The 2-4 sentence note body."},
                    "note_type": {"type": "string", "description": "Note category (e.g. theory, mechanism, concept).", "default": "concept"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Hierarchical tags (lowercase/slash)."},
                    "aliases": {"type": "array", "items": {"type": "string"}, "description": "Search aliases for the note."},
                    "source": {"type": "string", "description": "URL or note title this insight came from."}
                },
                "required": ["title", "content"]
            }
        )
    ]

@app.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """Handle tool execution."""
    if name == "search_vault":
        query = arguments.get("query")
        k = arguments.get("k", 7)
        results = await store.query_relevant(query, k=k)
        
        if not results:
            return [types.TextContent(type="text", text="No relevant notes found.")]
            
        output = "### Semantic Search Results:\n"
        for r in results:
            output += f"- **[[{r['title']}]]** (sim: {r['similarity']:.2f})\n"
            output += f"  *Tags: {', '.join(r['tags']) if r['tags'] else 'none'}*\n"
            
        return [types.TextContent(type="text", text=output)]

    elif name == "read_note":
        title = arguments.get("title")
        path = _load_note_title(title)

        if not path or not path.exists():
            return [types.TextContent(type="text", text=f"Error: Note '[[{title}]]' not found in vault.")]

        content = path.read_text(encoding="utf-8")
        return [types.TextContent(type="text", text=f"### Note: [[{title}]]\n\n{content}")]

    elif name == "list_recent_captures":
        limit = arguments.get("limit", 10)
        captures_dir = config.vault_path / "Captures"
        
        if not captures_dir.exists():
            return [types.TextContent(type="text", text="Error: Captures directory not found.")]
            
        # Get all .md files, sorted by mtime
        files = sorted(
            captures_dir.glob("*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )[:limit]
        
        output = f"### Last {len(files)} Captures:\n"
        for f in files:
            output += f"- [[{f.stem}]]\n"
            
        return [types.TextContent(type="text", text=output)]

    elif name == "graph_search":
        query = arguments.get("query")
        hops = arguments.get("hops", 2)
        k = arguments.get("k", 5)
        
        # Step 1: Find seed notes
        results = await store.query_relevant(query, k=k)
        if not results:
            return [types.TextContent(type="text", text="No seed notes found for the query.")]
            
        all_unique_nodes = set()
        all_results_data = []
        
        for r in results:
            seed_title = r["title"]
            visited = {seed_title}
            tree = {"title": seed_title, "children": _traverse_graph(seed_title, 0, hops, visited)}
            _collect_all_nodes(tree, all_unique_nodes)
            
            graph_text = _format_graph_output(tree)
            all_results_data.append({
                "title": seed_title,
                "similarity": r["similarity"],
                "text": graph_text
            })

        # Base raw output construction
        raw_output = f"### Graph Search: \"{query}\"\n\n"
        for data in all_results_data:
            raw_output += f"Seed: [[{data['title']}]] (sim: {data['similarity']:.2f})\n"
            if data['text']:
                raw_output += data['text']
            else:
                raw_output += "  *(No connections found)*\n"
            raw_output += "\n"

        # Optional Sampling Capability
        if len(all_unique_nodes) > 10:
            try:
                sampler = None
                if hasattr(app, "create_message"):
                    sampler = app.create_message
                elif hasattr(app, "request_context") and hasattr(app.request_context, "session"):
                    sampler = app.request_context.session.create_message
                
                if sampler:
                    response = await sampler(
                        messages=[
                            types.SamplingMessage(
                                role="user",
                                content=types.TextContent(
                                    type="text",
                                    text=f"Graph traversal found {len(all_unique_nodes)} connected notes. Reply 'summarize' to get cluster summaries, or 'raw' to get the full traversal tree."
                                )
                            )
                        ],
                        max_tokens=20
                    )
                    
                    resp_text = getattr(response.content, "text", "") or ""
                    if "summarize" in resp_text.lower():
                        rel_groups = {}
                        for node_title in all_unique_nodes:
                            n_path = _load_note_title(node_title)
                            if n_path:
                                conns = _parse_connections(n_path)
                                for c in conns:
                                    rel = c["rel"]
                                    if rel not in rel_groups:
                                        rel_groups[rel] = set()
                                    rel_groups[rel].add(c["title"])
                        
                        summary = "### Graph Summary (Condensed)\n\n"
                        for rel, targets in rel_groups.items():
                            summary += f"- **{rel}**: {', '.join([f'[[{t}]]' for t in list(targets)[:5]])}"
                            if len(targets) > 5:
                                summary += f" (+{len(targets)-5} more)"
                            summary += "\n"
                        return [types.TextContent(type="text", text=summary.strip())]
            except (AttributeError, NotImplementedError, TypeError):
                pass  # Sampling not supported by this client — fallback to raw

        return [types.TextContent(type="text", text=raw_output.strip())]

    elif name == "inspect_graph_schema":
        captures_dir = config.vault_path / "Captures"
        if not captures_dir.exists():
            return [types.TextContent(type="text", text="Error: Captures directory not found.")]
        
        relation_counts = {}
        total_notes = 0
        total_edges = 0
        meta_files = {"_INDEX.md", "CLAUDE.md", "GEMINI.md"}
        
        for f in captures_dir.glob("*.md"):
            if f.name in meta_files:
                continue
            total_notes += 1
            connections = _parse_connections(f)
            for conn in connections:
                rel = conn["rel"]
                relation_counts[rel] = relation_counts.get(rel, 0) + 1
                total_edges += 1
        
        sorted_rels = sorted(relation_counts.items(), key=lambda x: x[1], reverse=True)
        
        table = "| relation_type | count |\n| :--- | :--- |\n"
        for rel, count in sorted_rels:
            table += f"| {rel} | {count} |\n"
            
        output = f"### Vault Graph Schema\n\n{table}\n"
        output += f"**Total Notes Scanned:** {total_notes}\n"
        output += f"**Total Edges Found:** {total_edges}"
        
        return [types.TextContent(type="text", text=output)]

    elif name == "research_to_vault":
        url = arguments.get("url")
        user_intent = arguments.get("user_intent", "capture key insights")
        
        try:
            # Step 1: Extract
            meta = extract_source(url, config)

            # Fetch vault index before synthesis so forward_links resolve correctly
            vault_index = store.get_all_as_index()

            # Step 2: Process (Stage 1 + Stage 2)
            result = await process_source(meta, config, vault_index=vault_index, user_intent=user_intent, embed_store=store)

            # Step 3: Write to Vault
            created_files = write_to_vault(result, meta, config, vault_index=vault_index)
            
            # Step 4: Index immediately
            notes_to_index = []
            for note in result.atomic_notes:
                notes_to_index.append({
                    "title": note.title,
                    "aliases": note.aliases or [],
                    "tags": note.tags or [],
                    "note_type": note.note_type,
                    "content": note.content
                })
            await store.upsert_notes(notes_to_index)
            
            # Step 5: Log
            _log_action("MCP research_to_vault", f"url={url} | notes_created={len(result.atomic_notes)}")
            
            # Step 6: Notify
            await _notify_updated()
            
            # Step 7: Summary
            titles = [n.title for n in result.atomic_notes]
            needs_review = [n.title for n in result.atomic_notes if n.needs_review]
            
            model_used = "quality path (Gemini)" if config.llm_provider == "gemini" else f"{config.llm_provider} path"
            anchors = getattr(result, "anchors_recovered", 0)
            
            output = f"✅ Research complete for '{meta.title}'.\n"
            output += f"Created {len(titles)} notes using {model_used}.\n"
            output += f"Anchors verified: {anchors}\n"
            if needs_review:
                output += f"⚠️ Notes needing review: {', '.join([f'[[{t}]]' for t in needs_review])}\n"
            output += "\nNotes created:\n"
            for t in titles:
                output += f"- [[{t}]]\n"
                
            return [types.TextContent(type="text", text=output)]
        except Exception as e:
            return [types.TextContent(type="text", text=f"❌ Research failed: {str(e)}")]

    elif name == "research_topic":
        urls = (arguments.get("urls") or [])[:4]
        search_query = arguments.get("search_query", "").strip()
        topic_intent = arguments.get("topic_intent")
        max_notes = arguments.get("max_notes_per_source", 5)
        generate_moc = arguments.get("generate_moc", True)
        generate_synthesis = arguments.get("generate_synthesis", False)

        # Optimization 4: HTTPX for discovery
        if not urls and search_query:
            if not config.jina_api_key:
                return [types.TextContent(type="text", text="❌ No URLs provided and JINA_API_KEY is not set. Please provide explicit URLs or set config.jina_api_key.")]
            try:
                import urllib.parse
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(
                        f"https://r.jina.ai/search?q={urllib.parse.quote(search_query)}",
                        headers={
                            "Authorization": f"Bearer {config.jina_api_key}",
                            "Accept": "application/json",
                            "X-Return-Format": "json"
                        }
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    
                urls = [item["url"] for item in data.get("data", [])[:4] if item.get("url")]
                if not urls:
                    return [types.TextContent(type="text", text="❌ Jina search returned no results. Try providing explicit URLs.")]
            except Exception as e:
                return [types.TextContent(type="text", text=f"❌ Auto-discovery failed: {str(e)}. Please provide explicit URLs.")]

        if not urls:
            return [types.TextContent(type="text", text="❌ Provide either 'urls' or 'search_query' for auto-discovery.")]

        # Optimization 3: Deduplication check before extraction
        if config.dedup_enabled:
            urls = [u for u in urls if not check_duplicate(u, config)]
            if not urls:
                 return [types.TextContent(type="text", text="✅ All sources in this topic are already present in your vault.")]

        completed = []
        failed = []
        all_created_notes = {}  # URL -> List[titles]
        synthesis_note_data = [] # List of (title, first_sentence)

        # Optimization 1: Parallel Stage 1 Extraction
        metas = await asyncio.gather(*[_extract_async(u, config) for u in urls], return_exceptions=True)

        # Temporarily override config notes density; restore unconditionally when done
        old_max_notes, old_min_notes = config.max_notes, config.min_notes
        config.max_notes = max_notes
        config.min_notes = max(2, max_notes - 2)
        
        moc_path = None
        moc_title = None
        
        try:
            # Optimization 2: Incremental Indexing (fetch once)
            vault_index = store.get_all_as_index()
            
            for url, meta in zip(urls, metas):
                if isinstance(meta, Exception):
                    failed.append({"url": url, "error": str(meta)})
                    continue
                    
                try:
                    # Stage 2: Synthesis (Sequential for Thermal Safety + Compounding)
                    result = await process_source(meta, config, vault_index=vault_index, user_intent=topic_intent, embed_store=store)

                    # Write to vault
                    write_to_vault(result, meta, config, vault_index=vault_index)

                    # Index new notes immediately and update incremental index
                    notes_to_index = []
                    for note in result.atomic_notes:
                        note_entry = {
                            "title": note.title,
                            "aliases": note.aliases or [],
                            "tags": note.tags or [],
                            "note_type": note.note_type,
                            "content": note.content
                        }
                        notes_to_index.append(note_entry)
                        
                        # Optimization 2: Merge incrementally into running index
                        vault_index.append({
                            "title": note.title,
                            "aliases": note.aliases or [],
                            "tags": note.tags or [],
                            "note_type": note.note_type
                        })
                        
                        # Capture data for synthesis
                        first_sentence = note.content.split(".")[0].strip() if "." in note.content else note.content.strip()
                        synthesis_note_data.append((note.title, first_sentence))
                        
                    await store.upsert_notes(notes_to_index)

                    completed.append(url)
                    all_created_notes[url] = [n.title for n in result.atomic_notes]

                except Exception as e:
                    failed.append({"url": url, "error": str(e)})

            # 5. Optional MOC generation
            if generate_moc and completed:
                moc_title = f"MOC - Topic Research - {topic_intent}"
                moc_file = config.get_moc_dir() / f"{slugify(moc_title)}.md"

                moc_content = f"---\ntype: moc\ntags: [research, {slugify(topic_intent)}]\ndate: {datetime.now().strftime('%Y-%m-%d')}\n---\n\n"
                moc_content += f"# {topic_intent}\n\n"
                moc_content += "## Findings Overview\n"

                for url in completed:
                    notes = all_created_notes[url]
                    moc_content += f"\n### Source: {url}\n"
                    for t in notes:
                        moc_content += f"- [[{t}]]\n"

                moc_file.write_text(moc_content, encoding="utf-8")
                moc_path = moc_file

        finally:
            config.max_notes = old_max_notes
            config.min_notes = old_min_notes

        # Change 3: Synthesis Note
        synthesis_title = None
        if generate_synthesis and completed:
            try:
                synthesis_prompt = f"You are summarizing facts already captured in a Zettelkasten vault. Do NOT add any facts not present in the notes listed below. Write a 3-5 sentence synthesis of what these notes collectively say about: {topic_intent}\n\nNotes:\n"
                for title, first_sentence in synthesis_note_data:
                    synthesis_prompt += f"- [[{title}]]: {first_sentence}.\n"
                synthesis_prompt += f"\nRules: Only use information from these notes. If notes contradict each other, state the contradiction explicitly. Do not add training knowledge."
                
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{config.ollama_base_url}/api/generate",
                        json={
                            "model": config.ollama_model_fast,
                            "prompt": synthesis_prompt,
                            "stream": False
                        }
                    )
                    resp.raise_for_status()
                    synthesis_text = resp.json().get("response", "").strip()
                
                if synthesis_text:
                    synthesis_title = f"Synthesis - {topic_intent}"
                    synthesis_path = config.get_notes_dir() / f"{slugify(synthesis_title)}.md"
                    synthesis_content = f"---\ntype: synthesis\nneeds_review: true\nsource: \"synthesis of {len(completed)} sources\"\ndate: {datetime.now().strftime('%Y-%m-%d')}\n---\n\n# {synthesis_title}\n\n{synthesis_text}\n"
                    synthesis_path.write_text(synthesis_content, encoding="utf-8")
                    
                    # Add to MOC if generated
                    if moc_path:
                        with open(moc_path, "a", encoding="utf-8") as f:
                            f.write(f"- [[{synthesis_title}]]\n")
            except Exception as e:
                print(f"Synthesis failed: {e}")

        # Change 2: Auto-taxonomy
        parent_moc_titles = []
        if moc_path and moc_title:
            try:
                moc_results = await store.query_relevant(topic_intent, k=10)
                moc_dir = config.vault_path / "MOCs"
                for r in moc_results:
                    if r["similarity"] < 0.75:
                        continue
                    candidate_path = _load_note_title(r["title"])
                    if candidate_path and candidate_path.parent == moc_dir and candidate_path != moc_path:
                        parent_moc_titles.append(r["title"])
                        
                if parent_moc_titles:
                    part_of_section = "\n\n## Part Of\n"
                    for pt in parent_moc_titles[:2]:
                        part_of_section += f"- [[{pt}]]\n"
                    with open(moc_path, "a", encoding="utf-8") as f:
                        f.write(part_of_section)
                        
                    for pt in parent_moc_titles[:2]:
                        parent_path = _load_note_title(pt)
                        if parent_path and parent_path.exists():
                            existing = parent_path.read_text(encoding="utf-8")
                            if f"[[{moc_title}]]" not in existing:
                                backlink = f"\n- [[{moc_title}]] *(auto-linked)*\n"
                                with open(parent_path, "a", encoding="utf-8") as f:
                                    f.write(backlink)
            except Exception:
                pass 

        # 6. Audit log and notifications
        _log_action("MCP research_topic", f"intent={topic_intent} | sources={len(completed)}/{len(urls)} | notes={sum(len(v) for v in all_created_notes.values())}")
        await _notify_updated()

        output = f"📋 Topic Research Summary: {topic_intent}\n"
        output += f"Sources Success: {len(completed)}/{len(urls)}\n"
        if moc_path and moc_title:
            output += f"📁 Created MOC: [[{moc_title}]]\n"
        if synthesis_title:
            output += f"📝 Created Synthesis: [[{synthesis_title}]]\n"
        if parent_moc_titles:
            output += f"🔗 Linked to parent MOCs: {', '.join([f'[[{t}]]' for t in parent_moc_titles[:2]])}\n"

        if failed:
            output += "\n❌ Failures:\n"
            for item in failed:
                output += f"- {item['url']}: {item['error']}\n"

        output += "\n✅ Notes Created:\n"
        for url, notes in all_created_notes.items():
            output += f"\nFrom: {url}\n"
            for t in notes:
                output += f"- [[{t}]]\n"

        return [types.TextContent(type="text", text=output)]

    elif name == "create_note":
        title = arguments.get("title")
        content = arguments.get("content")
        note_type = arguments.get("note_type", "concept")
        tags = arguments.get("tags") or []
        aliases = arguments.get("aliases") or []
        source = arguments.get("source", "")
        
        tag_pattern = re.compile(r"^[a-z][a-z0-9/_-]*$")
        bad_tags = [t for t in tags if not tag_pattern.match(t)]
        if bad_tags:
            return [types.TextContent(type="text", text=f"❌ Tag validation failed. Bad tags: {', '.join(bad_tags)}. Tags must be lowercase/slash/hierarchy.")]

        filename = f"{slugify(title)}.md"
        filepath = config.vault_path / "Captures" / filename
        if filepath.exists():
            return [types.TextContent(type="text", text=f"❌ Error: Note '[[{title}]]' already exists. Use a different title.")]

        today = datetime.now().strftime("%Y-%m-%d")
        summary = content.split(".")[0].strip()[:120]
        if summary and not summary.endswith("."):
            summary += "."
            
        aliases_yaml = "[" + ", ".join([f'"{a}"' for a in aliases]) + "]"
        tags_yaml = "[" + ", ".join([f'"{t}"' for t in tags]) + "]"
        
        keywords = ", ".join(aliases)
        
        note_body = f"""---
type: atomic
note_type: {note_type}
aliases: {aliases_yaml}
tags: {tags_yaml}
source: "{source}"
date: {today}
summary: "{summary}"
---

# {title}

**Keywords**: {keywords}

{content}

## Connections
"""
        try:
            filepath.write_text(note_body, encoding="utf-8")
            
            await store.upsert_notes([{
                "title": title,
                "aliases": aliases,
                "tags": tags,
                "note_type": note_type,
                "content": content
            }])
            
            _log_action("MCP create_note", f"title={title}")
            await _notify_updated()
            
            return [types.TextContent(type="text", text=f"✅ Note '[[{title}]]' created at {filepath}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"❌ Creation failed: {str(e)}")]

    else:
        raise ValueError(f"Unknown tool: {name}")

# ── Main ──────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())
