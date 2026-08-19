#!/usr/bin/env python3
"""
reflector_tier1.py — Structural Audit for the Nexus Vault.
Scans for broken links, orphan nodes, and tag violations.
"""
import re
import sys
import argparse
import asyncio
import json
from pathlib import Path
from collections import Counter, defaultdict

# Configuration
VAULT_PATH = Path("/Users/adithkb/Library/Mobile Documents/iCloud~md~obsidian/Documents/Nexus")
CAPTURES_DIR = VAULT_PATH / "Captures"
SOURCES_DIR = VAULT_PATH / "Sources"
MOCS_DIR = VAULT_PATH / "MOCs"
AGENTS_MD = VAULT_PATH / "AGENTS.md"

# Late import to avoid circularity if called from other modules
try:
    from embed_store import EmbedStore
    from config import Config
except ImportError:
    EmbedStore = None
    Config = None

def get_taxonomy():
    """Extract valid domains from AGENTS.md."""
    if not AGENTS_MD.exists():
        return []
    text = AGENTS_MD.read_text()
    match = re.search(r'## 2\. Tag Taxonomy.*?domain/\*\*:(.*?)\.', text, re.S)
    if not match:
        return []
    domains = [d.strip().replace('`', '') for d in match.group(1).split(',')]
    return domains

def run_audit(verbose=False):
    print(f"--- Nexus Vault Audit: {VAULT_PATH.name} ---")
    
    notes = list(CAPTURES_DIR.glob("*.md"))
    valid_names = {n.stem for n in notes}
    valid_names.update({n.stem for n in (VAULT_PATH / "Sources").glob("*.md")})
    valid_names.update({n.stem for n in (VAULT_PATH / "MOCs").glob("*.md")})
    valid_domains = get_taxonomy()

    # A wikilink also resolves via any note's `aliases:` frontmatter — Obsidian's
    # own resolution rule. Checking filenames alone over-reports broken links for
    # every note legitimately referenced by an alias rather than its exact title.
    valid_link_targets = set(valid_names)
    _alias_re = re.compile(r'^aliases:\s*\[(.*?)\]\s*$', re.MULTILINE)
    for f in list(notes) + list((VAULT_PATH / "Sources").glob("*.md")) + list((VAULT_PATH / "MOCs").glob("*.md")):
        try:
            head = f.read_text()[:2000]
        except Exception:
            continue
        m = _alias_re.search(head)
        if m:
            valid_link_targets.update(a.strip() for a in m.group(1).split(",") if a.strip())
    
    broken_links = []
    orphans = set(valid_names)
    tag_violations = []
    edges = []  # (source, target, rel_type)
    
    tag_pattern = re.compile(r'^[a-z0-9\-_]+(/[a-z0-9\-_]+)*$')

    for note_file in notes:
        if note_file.name.startswith("_Index"):
            continue
            
        content = note_file.read_text()

        # Strip fenced and inline code spans before link scanning — code samples
        # (e.g. `x = torch.tensor([[1.0, 2.0]])`) contain literal "[[...]]" that
        # isn't a wikilink and would otherwise be misreported as broken.
        link_scan_content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        link_scan_content = re.sub(r'`[^`\n]*`', '', link_scan_content)

        # 1. Check Links & Extract Edges
        # Matches [[Title]] — *rel_type*
        typed_matches = re.finditer(r'\[\[(.+?)\]\] — \*(\w+)\*', link_scan_content)
        for m in typed_matches:
            target = m.group(1).split('|')[0].split('#')[0].strip()
            rel_type = m.group(2)
            edges.append((note_file.stem, target, rel_type))

        links = re.findall(r'\[\[(.*?)\]\]', link_scan_content)
        for link in links:
            target = link.split('|')[0].split('#')[0].strip()
            if target and target not in valid_link_targets:
                broken_links.append((note_file.name, target))
            if target in orphans:
                orphans.remove(target)
        
        # 2. Check Tags
        if "tags:" in content:
            tags_match = re.search(r'tags: \[(.*?)\]', content)
            if tags_match:
                tags = [t.strip() for t in tags_match.group(1).split(',')]
                for tag in tags:
                    if not tag_pattern.match(tag):
                        tag_violations.append((note_file.name, tag, "format"))
                    elif valid_domains and tag.startswith("domain/"):
                        suffix = tag.split("/", 1)[1] if "/" in tag else ""
                        if suffix not in valid_domains:
                            tag_violations.append((note_file.name, tag, "domain"))

    # 3. Check for Circular Prerequisites
    logical_loops = []
    for i, (a, b, rel) in enumerate(edges):
        if rel == "prerequisite":
            # Check if b also lists a as prerequisite
            if any(eb == a and ea == b and er == "prerequisite" for ea, eb, er in edges[i+1:]):
                logical_loops.append((a, b))

    # Report
    print(f"\nNotes Scanned: {len(notes)}")
    print(f"Edges Found: {len(edges)}")
    
    if logical_loops:
        print(f"\n[!] Logical Loops Found ({len(logical_loops)}):")
        for a, b in logical_loops:
            print(f"  - [[{a}]] <-> [[{b}]] (Mutual Prerequisite)")
    else:
        print("\n[✓] No logical loops found.")
    
    if broken_links:
        print(f"\n[!] Broken Links ({len(broken_links)}):")
        for note, target in broken_links[:10]:
            print(f"  - {note} -> [[{target}]]")
    else:
        print("\n[✓] No broken links found.")

    if tag_violations:
        print(f"\n[!] Tag Violations ({len(tag_violations)}):")
        for note, tag, reason in tag_violations[:10]:
            print(f"  - {note}: {tag} ({reason})")
    else:
        print("\n[✓] All tags follow AGENTS.md taxonomy.")

    # Filter orphans (ignore MOCs and Sources for this simple check)
    real_orphans = [o for o in orphans if (CAPTURES_DIR / f"{o}.md").exists() and not o.startswith("_Index")]
    if real_orphans:
        print(f"\n[!] Orphaned Notes ({len(real_orphans)}):")
        for o in real_orphans[:10]:
            print(f"  - {o}.md")
    else:
        print("\n[✓] No orphaned notes found.")

    print("\n--- Audit Complete ---")
    return broken_links, edges, len(notes), real_orphans


def detect_cycles_iterative(edges: list[tuple[str, str, str]]):
    """
    Non-recursive DFS to detect cycles of any length in the logic graph.
    Efficient for large vaults.
    """
    from collections import defaultdict
    adj = defaultdict(list)
    for s, t, r in edges:
        adj[s].append(t)
        
    print("\n--- Logic Cycle Detection (DFS) ---")
    
    nodes = list(adj.keys())
    visited = set()
    on_stack = set()
    cycles = []
    
    for start_node in nodes:
        if start_node in visited:
            continue
            
        stack = [(start_node, iter(adj[start_node]))]
        visited.add(start_node)
        on_stack.add(start_node)
        
        while stack:
            parent, children = stack[-1]
            try:
                child = next(children)
                if child in on_stack:
                    # Cycle detected!
                    cycle_path = []
                    # Backtrack to find the full loop
                    for node, _ in stack:
                        cycle_path.append(node)
                    cycle_path.append(child)
                    
                    # Only report complex cycles (Length > 2)
                    # Reciprocal pairs (A <-> B) are intentional.
                    if len(cycle_path) > 3: # path includes child twice, so length 3 is A->B->A
                        cycles.append(cycle_path)
                elif child not in visited:
                    visited.add(child)
                    on_stack.add(child)
                    stack.append((child, iter(adj[child])))
            except StopIteration:
                on_stack.remove(parent)
                stack.pop()
                
    if cycles:
        print(f"[!] Detected {len(cycles)} Logical Cycles (Loops):")
        for c in cycles[:5]:
            print(f"  - " + " -> ".join([f"[[{n}]]" for n in c]))
    else:
        print("[✓] Logic graph is a Directed Acyclic Graph (DAG).")


def prune_broken_links(broken_links: list[tuple[str, str]]):
    """
    Surgically remove broken links from note bodies.
    Pillar 3: Active Maintenance.
    """
    print(f"\n--- Pillar 3: Auto-Pruning {len(broken_links)} broken links ---")
    
    notes_to_fix = defaultdict(list)
    for note_file, target in broken_links:
        notes_to_fix[note_file].append(target)
        
    for note_name, targets in notes_to_fix.items():
        note_path = CAPTURES_DIR / note_name
        if not note_path.exists(): continue
        
        content = note_path.read_text()
        original_content = content
        
        for t in targets:
            # Remove line containing the link
            # Pattern: matches a line starting with '- [[' or '  - [[' containing the target
            # and potentially followed by ' — *rel*'
            pattern = rf'^\s*- \[\[{re.escape(t)}\]\]( — \*\w+\*)?.*$'
            content = re.sub(pattern, '', content, flags=re.MULTILINE)
            
        if content != original_content:
            note_path.write_text(content.strip() + "\n")
            print(f"  ✓ Pruned {len(targets)} links from [[{note_name[:-3]}]]")


def detect_broken_hubs(broken_links: list[tuple[str, str]], threshold: int = 5) -> list[str]:
    """Identify 'Conceptual Vacuum States' - non-existent notes with 5+ incoming links."""
    counts = Counter(target for note, target in broken_links)
    hubs = [target for target, count in counts.items() if count >= threshold]
    if hubs:
        print(f"\n[!] Detected {len(hubs)} Broken Hubs (Threshold >= {threshold}):")
        for h in hubs:
            print(f"  - [[{h}]] ({counts[h]} links)")
    return hubs


async def recover_hubs(hubs: list[str], config=None):
    """
    Autonomously resolve broken hubs by synthesizing content from calling notes.
    Zero manual intervention.
    """
    if not hubs or not config:
        return

    from processor import process_source, SourceMeta
    from vault import write_to_vault

    print(f"\n--- Hub Recovery: Synthesizing {len(hubs)} missing hubs ---")
    
    for hub_title in hubs:
        # 1. Gather context from notes that link to this hub
        calling_notes = []
        for md_file in CAPTURES_DIR.glob("*.md"):
            if f"[[{hub_title}]]" in md_file.read_text():
                calling_notes.append(md_file.stem)
        
        # 2. Extract snippets from callers
        context_snippets = []
        for caller in calling_notes[:10]:
            content = (CAPTURES_DIR / f"{caller}.md").read_text()
            # Get first 500 chars for context
            context_snippets.append(f"CONTEXT FROM [[{caller}]]:\n{content[:500]}")
        
        full_context = "\n\n".join(context_snippets)
        
        # 3. Create a SourceMeta for synthesis
        meta = SourceMeta(
            title=f"Synthesis: {hub_title}",
            url=f"internal://hub-recovery/{hub_title.lower().replace(' ', '-')}",
            source_type="theory",
            author="Nexus Auditor",
            transcript=f"SYNTHESIS REQUEST for missing hub: [[{hub_title}]]\n\n{full_context}"
        )
        
        # 4. Process via LLM with synthesis intent
        intent = (
            f"STABILIZE GRAPH: You are creating a missing Theory hub for [[{hub_title}]]. "
            f"Use the provided context from surrounding notes to define this concept. "
            f"Ensure it anchors the existing 'mechanism' notes. "
            f"Tag it with appropriate 'pattern/' tags."
        )
        
        try:
            # Shift to Lite model for recovery to save costs
            original_model = config.gemini_model
            config.gemini_model = config.gemini_lite_model
            
            result = await process_source(meta, config, user_intent=intent)
            
            config.gemini_model = original_model
            # Override title to match exact hub title
            if result.atomic_notes:
                result.atomic_notes[0].title = hub_title
                result.atomic_notes[0].note_type = "theory"
            
            # 5. Write to vault (Automated Reciprocity will kick in here!)
            write_to_vault(result, meta, config)
            print(f"  ✓ Recovered Hub: [[{hub_title}]] (Context from {len(calling_notes)} notes)")
        except Exception as e:
            print(f"  ✗ Failed to recover hub [[{hub_title}]]: {e}")


async def propose_fixes(broken_links: list[tuple[str, str]], config=None):
    """
    For each broken link, use semantic search to find the closest 
    existing note and suggest a correction.
    """
    if not EmbedStore or not config:
        return
        
    store = EmbedStore(api_key=config.gemini_api_key)
    if store.count() == 0:
        return

    print(f"\n[?] Proposing Fixes for {len(broken_links)} broken links...")
    
    for note_name, target in broken_links[:10]:
        # Search for target title in vault
        results = await store.query_relevant(target, k=1)
        if results:
            best = results[0]
            print(f"  - {note_name}: [[{target}]] -> Suggest [[{best['title']}]] (Similarity: {best['similarity']})")


async def harmonize_taxonomy(config=None):
    """
    Identify redundant or overlapping pattern/ tags and consolidate 
    them into the Master Taxonomy from AGENTS.md.
    """
    if not config:
        return

    master_domains = get_taxonomy()
    # Hard-coded master patterns from system instructions
    master_patterns = {"optimization", "bottleneck", "feedback-loop", "trade-off", "representation_learning"}
    
    print("\n--- Pillar 3: Taxonomy Harmonization Sweep ---")
    
    notes = list(CAPTURES_DIR.glob("*.md"))
    changes_made = 0
    
    for note_file in notes:
        content = note_file.read_text()
        if "tags: [" not in content:
            continue
            
        tags_match = re.search(r'tags: \[(.*?)\]', content)
        if not tags_match:
            continue
            
        tags = [t.strip() for t in tags_match.group(1).split(',')]
        new_tags = []
        modified = False
        
        for tag in tags:
            if tag.startswith("pattern/"):
                p_name = tag.split("/", 1)[1]
                # Check for near-matches (e.g. feedback vs feedback-loop)
                if p_name == "feedback":
                    tag = "pattern/feedback-loop"
                    modified = True
                elif p_name == "efficiency":
                    tag = "pattern/optimization"
                    modified = True
                elif p_name not in master_patterns:
                    # Generic catch-all for now
                    pass
            new_tags.append(tag)
            
        if modified:
            new_tags_str = ", ".join(new_tags)
            new_content = content.replace(f"tags: [{tags_match.group(1)}]", f"tags: [{new_tags_str}]")
            note_file.write_text(new_content)
            changes_made += 1
            print(f"  ✓ Harmonized tags in [[{note_file.stem}]]")

    if changes_made == 0:
        print("[✓] Taxonomy is already harmonized.")
    else:
        print(f"[✓] Harmonized {changes_made} notes.")


async def refactor_noisy_hubs(config=None, edges: list = None):
    """
    Identify nodes with >12 connections and autonomously execute a split.
    Creates new Sub-MOCs and redistributes links to maintain high-signal clustering.
    """
    if not config or not edges:
        return

    print("\n--- Module B: Automated Fractal Hub Refactoring ---")
    
    outgoing = Counter(s for s, t, r in edges)
    # Threshold for noise is 12 links
    noisy_nodes = [n for n, c in outgoing.items() if c > 12]
    
    if not noisy_nodes:
        print("[✓] No noisy hubs detected (>12).")
        return

    from processor import _gemini_generate, SourceMeta
    from vault import write_to_vault, slugify
    
    for hub in noisy_nodes:
        children = [t for s, t, r in edges if s == hub]
        print(f"\n[!] Hub [[{hub}]] is noisy ({len(children)} links). Automating split...")
        
        # 1. Request structured JSON split from LLM
        context = "\n".join([f"- [[{c}]]" for c in children])
        prompt = (
            f"REFactor HUB: The node [[{hub}]] has too many links.\n"
            f"CURRENT LINKS:\n{context}\n\n"
            f"TASK: Group these links into 2-3 logical sub-themes.\n"
            f"OUTPUT: A JSON object with this schema: \n"
            f'{{"sub_hubs": [{{"title": "Sub-Hub Title", "notes": ["Note Title 1", "Note Title 2"]}}]}}\n'
            f"Keep titles declarative and specific. Respond ONLY with JSON."
        )
        
        try:
            raw_response, _ = await _gemini_generate(
                prompt, "You are a vault architect. Output JSON only.", 
                config.gemini_api_key, config.gemini_model, 0.1, json_mode=True
            )
            
            # Clean response
            raw_response = re.sub(r"```json\s*", "", raw_response).strip(" `")
            data = json.loads(raw_response)
            
            for sub in data.get("sub_hubs", []):
                sub_title = sub["title"]
                member_notes = sub["notes"]
                
                print(f"  ▸ Creating Sub-MOC: [[{sub_title}]] ({len(member_notes)} notes)")
                
                # 2. Create the Sub-MOC file
                moc_path = MOCS_DIR / f"{slugify(sub_title)}.md"
                member_links = "\n".join([f"- [[{n}]]" for n in member_notes])
                moc_content = f"""---
type: moc
status: evergreen
parent: [[{hub}]]
---

# {sub_title}

This sub-MOC was autonomously generated to refactor the noisy [[{hub}]] node.

## Member Notes
{member_links}

## Connections
- [[{hub}]] — *context_for*
"""
                moc_path.write_text(moc_content, encoding="utf-8")
                
                # 3. Update Member Notes (Migrate Link)
                for member in member_notes:
                    member_path = CAPTURES_DIR / f"{slugify(member)}.md"
                    if member_path.exists():
                        content = member_path.read_text(encoding="utf-8")
                        # Replace the noisy parent link with the specific sub-hub link
                        old_link = f"[[{hub}]]"
                        new_link = f"[[{sub_title}]]"
                        if old_link in content:
                            new_content = content.replace(old_link, new_link)
                            member_path.write_text(new_content, encoding="utf-8")
                
                # 4. Update Noisy Parent to link to the new Sub-MOC
                parent_path = CAPTURES_DIR / f"{slugify(hub)}.md"
                if not parent_path.exists():
                    parent_path = MOCS_DIR / f"{slugify(hub)}.md"
                
                if parent_path.exists():
                    p_content = parent_path.read_text(encoding="utf-8")
                    sub_link = f"- [[{sub_title}]] — *refinement*"
                    if sub_title not in p_content:
                        if "## Connections" in p_content:
                            p_content = p_content.replace("## Connections", f"## Connections\n{sub_link}")
                        else:
                            p_content = p_content.rstrip() + f"\n\n## Connections\n{sub_link}\n"
                        parent_path.write_text(p_content, encoding="utf-8")

            print(f"  ✓ Refactoring complete for [[{hub}]].")
                
        except Exception as e:
            print(f"  ✗ Automated refactoring failed for [[{hub}]]: {e}")

async def gap_analysis(config=None, edges: list[tuple[str, str, str]] = None):
    """
    Identify potential 'analogous_to' links between unlinked notes
    with high semantic similarity, especially across different domains.
    """
    if not EmbedStore or not config:
        return
        
    store = EmbedStore(api_key=config.gemini_api_key)
    if store.count() == 0:
        return

    print("\n--- Map of the Gaps: Potential Cross-Domain Links ---")
    
    # Existing links to skip
    linked_pairs = set()
    if edges:
        for s, t, r in edges:
            linked_pairs.add(tuple(sorted((s, t))))

    # Get semantic clusters
    clusters = store.find_all_related_pairs(auto_threshold=0.82, suggest_threshold=0.75)
    potential = clusters["auto"] + clusters["suggest"]
    
    # Filter for unlinked and cross-domain
    unlinked_potential = [p for p in potential if tuple(sorted((p[0], p[1]))) not in linked_pairs]
    
    # We'll need tags to identify domains
    notes_data = {n['title']: n for n in store.get_all_as_index()}
    
    cross_domain_suggestions = []
    for a, b, sim in unlinked_potential:
        tags_a = set(notes_data.get(a, {}).get('tags', []))
        tags_b = set(notes_data.get(b, {}).get('tags', []))
        
        # Check if they share a root domain (first segment of slash tag)
        domains_a = {t.split("/")[0] for t in tags_a if "/" in t}
        domains_b = {t.split("/")[0] for t in tags_b if "/" in t}
        
        # If they don't share ANY root domains, it's a cross-domain candidate
        if domains_a and domains_b and not (domains_a & domains_b):
            cross_domain_suggestions.append((a, b, sim, domains_a, domains_b))

    if cross_domain_suggestions:
        for a, b, sim, da, db in cross_domain_suggestions[:10]:
            print(f"  - [POTENTIAL ANALOGY] ({sim})")
            print(f"    [[{a}]] ({da})")
            print(f"    [[{b}]] ({db})")
    else:
        print("[✓] No obvious cross-domain gaps found at current thresholds.")

def verify_logic_edges(edges: list[tuple[str, str, str]]):
    """
    Advanced logic check: 
    1. If A evidence_for B, check if B should reference A.
    2. If A contradicts B, check if B also contradicts A (Bidirectionality).
    3. If A analogous_to B, check for Bidirectionality.
    """
    print("\n--- Logic Edge Verification ---")

    edge_map = {(s, t): r for s, t, r in edges}
    violations = []
    rel_counts = Counter(r for s, t, r in edges)

    print(f"Relationship Distribution:")
    for rel, count in rel_counts.items():
        print(f"  - {rel}: {count}")

    for (s, t), r in edge_map.items():
        if r in ("contradiction", "analogous_to", "analogy"):
            if (t, s) not in edge_map:
                violations.append(f"  - Missing bidirectional {r}: [[{s}]] <-> [[{t}]]")

        if r == "evidence_for" or r == "solves_problem":
            # Check if target acknowledges the link (informational only)
            if (t, s) not in edge_map:
                pass

    if violations:
        for v in violations[:10]:
            print(v)
    else:
        print("[✓] Logic edges are consistent.")


def compute_vault_health(notes_scanned: int, edges_found: int, orphans: int, edges: list):
    """
    Calculate and display the Nexus Health Dashboard.
    Based on PKM Best Practices: Density, Entropy, and Small-World properties.
    """
    print("\n" + "━" * 60)
    print("  NEXUS HEALTH DASHBOARD")
    print("━" * 60)

    density = edges_found / notes_scanned if notes_scanned > 0 else 0
    orphan_rate = (orphans / notes_scanned) * 100 if notes_scanned > 0 else 0

    # 1. Logic Density (Target: 1.5 - 2.5)
    density_status = "OPTIMAL" if 1.5 <= density <= 2.5 else "LOW" if density < 1.5 else "NOISY"
    print(f"▸ Logic Density: {density:.2f} [{density_status}]")

    # 2. Orphan Rate (Target: < 5%)
    orphan_status = "HEALTHY" if orphan_rate < 5 else "CRITICAL" if orphan_rate > 20 else "WARNING"
    print(f"▸ Orphan Rate:   {orphan_rate:.1f}% [{orphan_status}]")

    # 3. Shannon Entropy (Diversity of connections)
    # Using reciprocal logic to estimate complexity
    rel_types = Counter(r for s, t, r in edges)
    import math
    total = sum(rel_types.values())
    if total > 0:
        entropy = -sum((c/total) * math.log2(c/total) for c in rel_types.values())
        entropy_status = "HIGH (Serendipitous)" if entropy > 2.0 else "MODERATE"
        print(f"▸ Graph Entropy: {entropy:.2f} [{entropy_status}]")

    # 4. Noisy Nodes (Over-linked)
    outgoing = Counter(s for s, t, r in edges)
    noisy = [n for n, c in outgoing.items() if c > 10]
    if noisy:
        print(f"▸ Noisy Nodes:   {len(noisy)} (>10 links) - Consider refactoring.")
        for n in noisy[:3]:
            print(f"    - [[{n}]]")
    else:
        print(f"▸ Noisy Nodes:   0 [CLEAN]")

    print("━" * 60)


async def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--fix", action="store_true", help="Propose semantic fixes for broken links")
    parser.add_argument("--gap", action="store_true", help="Perform cross-domain gap analysis")
    parser.add_argument("--recover", action="store_true", help="Autonomously recover broken hubs")
    parser.add_argument("--harmonize", action="store_true", help="Consolidate redundant pattern tags")
    parser.add_argument("--prune", action="store_true", help="Surgically remove broken links from note bodies")
    parser.add_argument("--split", action="store_true", help="Propose sub-clusters for noisy hubs")
    args = parser.parse_args()

    broken_links, edges, notes_count, real_orphans = run_audit(args.verbose)
    
    if edges:
        verify_logic_edges(edges)
        detect_cycles_iterative(edges)
        compute_vault_health(notes_count, len(edges), len(real_orphans), edges)
        
    cfg = Config() if Config else None
    
    if args.prune and broken_links:
        prune_broken_links(broken_links)

    if args.fix and broken_links:
        if cfg:
            await propose_fixes(broken_links, cfg)
        else:
            print("Config not available for fixes.")

    if args.gap:
        if cfg:
            await gap_analysis(cfg, edges)
        else:
            print("Config not available for gap analysis.")

    if args.recover and broken_links:
        hubs = detect_broken_hubs(broken_links)
        if hubs and cfg:
            await recover_hubs(hubs, cfg)
        elif not cfg:
            print("Config not available for hub recovery.")

    if args.harmonize:
        if cfg:
            await harmonize_taxonomy(cfg)
        else:
            print("Config not available for taxonomy harmonization.")

    if args.split:
        if cfg:
            await refactor_noisy_hubs(cfg, edges)
        else:
            print("Config not available for hub refactoring.")

if __name__ == "__main__":
    asyncio.run(main())
