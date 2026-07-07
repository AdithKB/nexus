# Nexus PKM: Local-First GCP "Cloud-Inbox" Migration Blueprint (2026)

## 1. Objective
Migrate the AgenticPKM pipeline from a fixed-cost DigitalOcean infrastructure ($11/mo) to a 100% Google Cloud "Cloud-Inbox" architecture ($0/mo). The cloud will handle **initial capture** and **MOC synthesis**, while the Mac remains the primary **Source of Truth** and **Local RAG engine**.

## 2. Target Architecture (The "Local-First" Stack)

| Component | Current (DigitalOcean + Local) | Future (GCP "Cloud-Inbox") | Why this is better |
| :--- | :--- | :--- | :--- |
| **Compute** | DO Droplet ($6/mo) | **GCP Cloud Run (Services)** | 2M requests free. Scales to zero. Only runs when you capture a link. |
| **Inbox Storage**| DO Spaces ($5/mo) | **GCP Cloud Storage (GCS)** | 5GB "Always Free" Standard storage. Holds notes until Mac pulls them. |
| **Vault Index** | Local JSON (`~/.nexus_jobs.json`) | **GCP Firestore** | 1GB storage free. Stores **only** Note Titles + Aliases (No Content). |
| **The "Second Brain"**| Mac/iCloud Sync | **Mac/iCloud Sync** | **UNTOUCHED.** Your actual `.md` files never live in the cloud. |
| **RAG Engine** | - | **Gemini CLI / Claude Code (Local)** | 100% Private. No latency. No cloud data leakage. |
| **Connectivity** | Public Port 22 (Legacy) | **Tailscale SSH (Zero Trust)** | No exposed ports. No static `authorized_keys`. Identity-based mesh access. |

## 3. The Simplified Sync Loop

Your `vault_writer.py` on your Mac remains the "Gatekeeper" of your knowledge.

### A. The "Pending" Queue (Cloud-to-Mac)
*   **The Flow:** `server.py` writes new `.md` files directly to a **GCS bucket**.
*   **The Pull:** `vault_writer.py` polls the `/pending` endpoint to download and **delete** those files into your local Obsidian vault.
*   **Refactor:** Rewrite `/pending` in `server.py` to use `google-cloud-storage` instead of `boto3`.

### B. The Vault Index (Mac-to-Cloud)
*   **The Flow:** `vault_writer.py` POSTs your vault's **Titles + Aliases + Tags** (but NO note content) to `/index`.
*   **The Storage:** `server.py` writes this to **Firestore** as a "Shadow Index." 
*   **The Privacy:** This ensures that your private note content never leaves your Mac/iCloud ecosystem. The cloud only sees a "Map" of your knowledge for smart linking.
*   **The Purpose:** This "Shadow Index" survives Cloud Run's ephemeral restarts, allowing the AI to link new captures to your existing MOCs even when your Mac is offline.

### C. The Video Download (Instagram/YouTube)
*   **The Flow (2026):** Cloud Run uses **Cloud Storage FUSE** to mount the GCS bucket to `/mnt/gcs_video`.
*   **The Benefit:** `yt-dlp` downloads videos directly to the "Cloud Disk," keeping the server's RAM footprint at $0 cost.

## 4. Migration Phases

### Phase I: The Firestore Index (Data Layer)
- [ ] Refactor `server.py` to save `/index` (Titles/Aliases) to a Firestore `index` collection.
- [ ] Replace `~/.nexus_jobs.json` logic with a Firestore `jobs` collection.

### Phase II: The Storage & Universal Capture (Storage Layer)
- [ ] Refactor `_upload_to_spaces` in `server.py` to use `google-cloud-storage`.
- [ ] Refactor `/pending` endpoint to read/delete from the GCS bucket.
- [ ] Update `extractors.py` to save temp videos to the FUSE-mounted `/mnt/gcs_video`.
- [ ] **Universal Capture Upgrades:**
    - [ ] **GitHub Extractor:** Add logic to pull `README.md` and repository metadata from `github.com` URLs.
    - [ ] **arXiv Auto-PDF:** Detect `arxiv.org/abs/` links and automatically route them to `extract_pdf` for full-paper analysis instead of just abstract scraping.

### Phase III: The Cloud Run Container (Execution Layer)
- [ ] Create a `Dockerfile` with **Deno** (to bypass 2026 YouTube 403 SABR blocking).
- [ ] Install `ffmpeg` and `yt-dlp` (nightly build).
- [ ] Create `deploy.sh` for `gcloud run deploy` with **Startup CPU Boost**.

### Phase IV: Zero-Trust Operations (Security Layer)
- [ ] **Tailscale Integration:** Install Tailscale on droplet (and sidecar for Cloud Run) to eliminate Port 22 exposure.
- [ ] **Keyless SSH:** Enable Tailscale SSH to bypass `authorized_keys` management entirely.
- [ ] **Private Mesh:** Route all `vault_writer.py` traffic over the internal Tailscale IP for secure, non-public relaying.

---
*Last Updated: April 15, 2026*
