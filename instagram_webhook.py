"""
instagram_webhook.py — Instagram DM capture bot: signature verification and
payload parsing for Meta's webhook deliveries.

Share a reel to a dedicated bot Instagram account → Meta POSTs the event here
(wired up in server.py's /webhooks/instagram routes) → the extracted URL feeds
into the exact same _process_job() full-cloud path the iOS Shortcut's Cloud
Capture option already uses. See docs/architecture.md §2g for the full flow.
"""
import hashlib
import hmac
import re

import httpx

# Matches instagram.com/reel/<code>, /reels/<code>, or /p/<code>, with or
# without trailing query params — used as the plain-text fallback when a
# share arrives as text rather than a structured attachment.
_IG_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+/?[^\s\"]*")


def verify_signature(raw_body: bytes, signature_header: str, app_secret: str) -> bool:
    """
    HMAC-SHA256 over the raw POST body against the X-Hub-Signature-256
    header, using the Meta app secret. This is what proves a request
    actually came from Meta — never process a payload that fails this.
    """
    if not app_secret or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256="):])


def extract_messaging_events(payload: dict) -> list[dict]:
    """
    Flatten entry[].messaging[] out of a Meta webhook payload. Defensive —
    returns [] rather than raising on any unexpected top-level shape, since
    a malformed/unexpected payload should never crash the webhook handler.
    """
    events = []
    for entry in payload.get("entry", []) or []:
        for ev in entry.get("messaging", []) or []:
            events.append(ev)
    return events


def extract_shared_url(messaging_event: dict) -> str | None:
    """
    Pull a shared reel/post URL out of one messaging event.

    Primary: message.attachments[].payload.url — covers the `ig_post`
    attachment type (the Oct 2025 Meta change that replaced the legacy
    `share` type) AND the legacy type AND whatever Meta renames it to next,
    since this checks for a url field rather than gating on an exact type
    string (Meta has already renamed this once; don't assume it won't again).

    Fallback: regex-scan message.text for a raw instagram.com URL, in case
    a share ever arrives as plain pasted text instead of a structured
    attachment.

    Both paths validate against _IG_URL_RE (a real reel/reels/p permalink),
    not a loose "instagram.com" substring check — an image attachment's
    payload.url is commonly a CDN asset link like
    https://scontent.cdninstagram.com/... (contains the literal substring
    "instagram.com" inside "cdninstagram.com"), which is a media file, not a
    capturable post permalink; a substring check would wrongly accept it.

    Returns None (never raises) when nothing shareable is found — e.g. a
    text-only DM, a sticker reply, or a non-permalink attachment.
    """
    message = messaging_event.get("message", {}) or {}

    for att in message.get("attachments", []) or []:
        url = (att.get("payload") or {}).get("url")
        if url and _IG_URL_RE.match(url):
            return url

    text = message.get("text", "") or ""
    m = _IG_URL_RE.search(text)
    if m:
        return m.group(0)

    return None


async def send_ack_reply(recipient_id: str, text: str, access_token: str) -> None:
    """
    Phase 2 / optional — gated by config.instagram_ack_reply_enabled
    (default False). Confirms receipt back to the sender, matching Nylo's
    UX. Must be sent within Meta's 24h messaging window that opens when the
    user shares to the bot. Best-effort: logs and returns on failure, never
    raises — an ack-reply failure must never affect the actual capture,
    which has already completed (or failed) independently by the time this
    is called.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://graph.instagram.com/v21.0/me/messages",
                params={"access_token": access_token},
                json={"recipient": {"id": recipient_id}, "message": {"text": text}},
            )
            if resp.status_code >= 400:
                print(f"[IG_WEBHOOK] Ack reply failed ({resp.status_code}): {resp.text}", flush=True)
    except Exception as e:
        print(f"[IG_WEBHOOK] Ack reply failed: {e}", flush=True)
