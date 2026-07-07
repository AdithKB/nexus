# Engineering Skills & Techniques: Nexus Pipeline

This document catalogs the specialized knowledge and technical workarounds developed during the creation of the AgenticPKM (Nexus) pipeline.

## 1. iOS Shortcuts: Data-Driven Development
Shortcuts are typically built via a GUI, but for Nexus, we treated them as **binary data structures** to enable programmatic generation and advanced logic.

*   **SQLite Tapping:** On macOS, shortcuts are stored in `~/Library/Shortcuts/Shortcuts.sqlite`. We used SQL queries to extract raw logic from the `ZDATA` (BLOB) column.
*   **Binary Plist Manipulation:** Used Python's `plistlib` to deconstruct the internal dictionary of a shortcut.
    *   **Control Flow:** Discovered that `is.workflow.actions.choosefrommenu` is the universal identifier for the start, items, and end of a menu, differentiated strictly by `WFControlFlowMode` (`0`=open, `1`=item, `2`=close).
    *   **Variable Binding:** Implemented `WFURLAggrandizement` to ensure Share Sheet inputs are correctly resolved to URL strings before being passed to an API.
*   **Cryptographic Signing:** Bypassed macOS security restrictions using the `/usr/bin/shortcuts sign` CLI. This allows programmatically generated `.shortcut` files to be imported onto iOS devices without "Unsigned Shortcut" errors.
*   **Robust POST Logic:** Developed the "JSON Value Mapping" pattern inside the `downloadurl` action to ensure iPhone-to-Server handoffs never send empty payloads.

## 2. LLM Orchestration: The Two-Pass Pipeline
To solve the conflict between **high token costs** and **hallucination risks**, we implemented a split-responsibility architecture.

*   **Pass 1 (The Eyes):** Uses `gemini-2.5-flash-lite` at `MEDIA_RESOLUTION_LOW` (66 tokens/frame). Its persona is a "Literal Court Reporter." This saves ~80% on video token costs.
*   **Pass 2 (The Brain):** Uses the full `gemini-2.5-flash` in **Text-Only mode**. By keeping the smarter model "blind" to the actual video, we prevent it from "recognizing" faces or entities and injecting extrinsic training knowledge not present in the source.
*   **Implicit Caching:** Optimized prompt order by prepending the stable `AGENTS.md` instructions to the System Prompt. This allows Google to cache the prefix, resulting in a 75-90% discount on input tokens.

## 3. Advanced Prompt Engineering & Governance
We moved beyond basic instructions to a **Strict Alignment** framework.

*   **Mirror Persona:** Command-level instruction forbidding "language smoothing" or paraphrasing. The LLM must mirror the source's exact terminology (e.g., "Data Pipelines" vs "Data Storage").
*   **Evidence Check Loop:** A mandatory "Proof of Work" section in the LLM's internal thinking field. It must list every statistic or date and find its verbatim match in the transcript before it is allowed to write the final note.
*   **Recency Bias Steering:** Strategically injecting `user_intent` overrides at the very end of the user prompt to ensure the model prioritizes specific user commands over general vault rules.
*   **PTCF Framework:** Structured the global `SYSTEM_PROMPT` using Persona, Task, Context, and Format boundaries.
*   **Prompt-Alignment vs. Fine-Tuning (The Choice):** We intentionally avoided weight-based fine-tuning in favor of **Dynamic In-Context Learning (ICL)**. 
    *   **Reasoning:** Fine-tuning creates a "frozen" mental model that is expensive to update. By using a sophisticated system prompt and RAG (Retrieval-Augmented Generation), the pipeline remains "live"—we can update the vault's logic or the LLM's personality in seconds by editing `AGENTS.md` without triggering a $100+ training run.
    *   **Data Veracity:** Prompt-based constraints (the "Evidence Check Loop") provide better hallucination control than fine-tuning, as we can force the model to cite its sources, which a fine-tuned model often fails to do when "dreaming" based on its weights.

## 4. DevOps & Pipeline Resilience
Operating on a remote DigitalOcean droplet required advanced SSH and logging workarounds.

*   **SSH Key Recovery:** Restored access to a "locked" droplet by using the DigitalOcean **Recovery Console (VNC)** to manually rebuild the `.ssh/authorized_keys` file when standard TTY input was blocked.
*   **doctl Integration:** Leveraged the DigitalOcean CLI to bridge the gap when standard `ssh` failed, using `doctl compute ssh` for secure file transfers and service restarts.
*   **Non-Buffered Logging:** Overcame `uvicorn` output buffering by using `logging.warning` and `flush=True` prints to ensure diagnostic data reached `journalctl` in real-time.

## 5. UX & Knowledge Graph Architecture
Focused on making the vault "pop" visually while maintaining a clean, high-density graph.

*   **MOC Emoji Anchors:** Built a deterministic mapping logic in `vault.py` to prepend emojis to Map of Content titles. This creates visual "Sun" nodes in the Obsidian Graph View that related notes orbit around.
*   **Shannon Entropy (H) Tracking:** Implemented real-time tracking of relationship type distribution. This provides a mathematical warning signal if the graph becomes too "flat" or if the model starts using catch-all categories like "prerequisite" too often.
*   **Obsidian Bases:** Decoupled operational tracking (Inbox, Review Queue) from the semantic layer by installing `.base` JSON templates for the Obsidian Bases plugin (v1.9+).
*   **Mac-Off Bridge:** Orchestrated a sync flow using DigitalOcean Spaces and a local Mac `launchd` service that wakes the Mac via **Power Nap** to pull notes into iCloud every 60 seconds.
