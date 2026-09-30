# Nexus — Agentic PKM Pipeline

Captures URLs/PDFs/voice/text → Gemini extraction (Stage 1) → local Qwen3 synthesis (Stage 2, or Gemini again for the full-cloud path) → atomic Zettelkasten notes written into an Obsidian vault. "Passive Sovereign" design: cloud LLM is a stateless sensor, all reasoning/memory stays local unless the full-cloud escape hatch is used.

**Source of truth for design details:** [`docs/architecture.md`](docs/architecture.md) — pipeline stages, model routing, atomic note schema, MCP tools, all of it. Read that before making non-trivial changes; this file is operational context, not a spec. [`docs/README.md`](docs/README.md) is the quick-start.

## Current infrastructure (as of 2026-09-30)

- **Relay server**: Oracle Cloud Always Free tier, `VM.Standard.A1.Flex` (4 OCPU/24GB), Ubuntu 24.04 ARM, `uk-london-1`. Public IP `132.145.20.175`, reachable at `https://nexus.adithkb.dev`. SSH: `ssh -i ~/.ssh/oracle_nexus ubuntu@132.145.20.175`. Deployed via `deploy/oracle_setup.sh` (or the equivalent manual steps — see that script's comments for the VM-local-iptables gotcha, a common Oracle trap separate from the OCI console's own Security List).
- **OCI CLI**: authenticated from this Mac via `~/.oci/config` + `~/.oci/oci_api_key.pem`. Tenancy `adithkb`.
- **Object storage**: Oracle Object Storage, bucket `nexus-vault`, namespace `lrrjg2lhv2of`. Any S3-compatible provider works via `SPACES_ENDPOINT_URL` in `.env` (see `config.py`) — this used to be DigitalOcean Spaces before a 2026-09-30 migration (droplet was disabled for non-payment).
- **Local daemons** (Mac, via `~/Library/LaunchAgents/`): `com.adithkb.nexus-crystallizer` (watcher.py, Stage 2), `dev.nexus.vault-writer` + `com.adithkb.nexus-index` (vault_writer.py, both run it — one periodic `--once`, one continuous long-poll; mildly redundant but both work), `com.adithkb.nexus-turbo` (optional local model server).

## Known gotchas (hit and fixed this session — don't rediscover these)

- **This repo's path changed** from `~/MSAI/PersonalProjects/...` to `~/Ireland/PersonalProjects/...`. If a LaunchAgent, plist, or shell script silently stops working, check for a stale hardcoded path first — this exact bug broke all local sync daemons for weeks without anyone noticing (only the log's silence gave it away).
- **`GET /pending` on the relay server consumes the queue, it doesn't peek.** Never curl it manually while `vault_writer.py` might also be about to poll it — you'll silently steal its files. Check `/result/{job_id}` instead (idempotent).
- **Non-AWS S3-compatible storage (Oracle, most others) rejects boto3's default chunked-encoding PutObject.** `_spaces_client()` in `server.py` already passes the `botocore.config.Config` fix — don't remove it, uploads fail silently (job reports success, 0 files actually land) without it.
- **Frontmatter YAML must be built through `vault.py`'s `_yaml_list()` and the escaped `summary_line`**, never hand-interpolated — raw Instagram captions routinely contain `?`, `:`, `#`, bare commas, and quotes that break naively-built YAML. Both helpers now escape correctly; if you add a new frontmatter field built from LLM/user text, escape it the same way.
- **Oracle's Ubuntu images have restrictive local iptables by default**, separate from the OCI console's Security List — both must be opened for 80/443, or the server is "running" but completely unreachable with a confusing symptom (Caddy can't complete its ACME challenge).
- **Vault-quality "fixes" need testing before batch-applying.** A naive body-length heuristic for "under-synthesized notes" produced 145 false positives out of 483 (concise ≠ broken); fuzzy-matching for broken-link repair produced wrong retargets. Always dry-run a heuristic against known-good and known-bad examples before writing to 483 real files.
- **`watcher.py`'s filesystem watch only fires on new events — a seedling that already existed before the daemon started won't trigger it.** The one-time startup scan covers most of this, but if that single attempt fails (Ollama not ready yet at login/wake is a real race), nothing retried it before — it just sat there. Fixed by making `_rescan_seedling_backlog()` also run every 5 minutes from the tick loop, not just at startup. Found via a real seedling stuck since 2026-08-24.
- **Local synthesis is currently 14B/Ollama-only — the 9B/MLX fast path is disabled**, not routing-dependent as `docs/architecture.md` §4 used to describe. `mlx_lm` crashes on import on this machine (never root-caused); `watcher.py` hardcodes the quality-tier model for every seedling regardless of content size. Every local capture is slower than the docs implied, but this is a known, intentional lockout, not a new bug.

## Useful commands

```bash
# Vault structural audit (broken links, tag violations, orphans, cycles)
python reflector_tier1.py --verbose

# Local one-shot capture (bypasses server/cloud path entirely)
python capture.py --url "https://..." --provider gemini

# Pull anything queued on the relay server into the local vault right now
python vault_writer.py --once

# Check relay server health
curl https://nexus.adithkb.dev/health
```

## Secrets

Live in `.env` (gitignored) — `GEMINI_API_KEY`, `JINA_API_KEY`, `SPACES_*`. Never committed. The relay server's copy lives at `/home/ubuntu/AgenticPKMApp/.env` on the Oracle VM, referenced via `EnvironmentFile=` in `deploy/obsidian-capture.service`.
