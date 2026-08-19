"""
Source extractors: YouTube, Web (Jina Reader), PDF (PyMuPDF), Instagram, Podcast, Twitter.
Author: Adith K B

Unified interface: extract_source(url, config) → SourceMeta
Routes automatically based on URL pattern.
"""
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

from config import Config


# ── Data ──────────────────────────────────────────────────────


@dataclass
class SourceMeta:
    """Extracted source metadata + content."""
    title: str
    url: str
    source_type: str           # youtube | web | pdf | instagram | podcast | twitter
    author: str = ""
    duration_sec: int = 0
    transcript: str = ""
    description: str = ""
    publish_date: str = ""
    source_id: str = ""        # deterministic 8-char hash for dedup
    video_path: str = ""       # local temp file path for uploaded video (Instagram, etc.)
    chapters: list[dict] = None # [{"start": 0, "end": 10, "title": "Intro"}]
    transcript_quality: dict = None  # populated by _score_transcript for audio-based sources


# ── Source ID ─────────────────────────────────────────────────


def _compute_source_id(url: str) -> str:
    """
    Generate a deterministic 8-char hex hash from a normalized URL.
    Ensures that mobile, desktop, and tracked URLs for the same source 
    produce the same ID.
    """
    key = url.strip()
    
    # 1. Normalize YouTube
    vid_id = _extract_video_id(key)
    if vid_id:
        # Canonical form: https://www.youtube.com/watch?v=11CHARID
        key = f"https://www.youtube.com/watch?v={vid_id}"
    else:
        # 2. General Normalization
        # - Lowercase (for case-insensitive host/scheme)
        # - Strip tracking parameters (si, feature, utm_*)
        try:
            from urllib.parse import urlunparse, urlencode, parse_qsl
            u = urlparse(key.lower())
            # List of tracking params to strip
            trash = {'si', 'feature', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'}
            q = [(k, v) for k, v in parse_qsl(u.query) if k not in trash]
            # Reconstruct without trash
            key = urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), u.fragment))
            # Strip trailing slash
            key = key.rstrip("/")
        except:
            # Fallback to basic cleaning
            key = re.sub(r'^(https?://)www\.', r'\1', key.lower()).rstrip("/")
            
    return hashlib.sha256(key.encode()).hexdigest()[:8]


# ── Router ────────────────────────────────────────────────────


_PODCAST_DOMAINS = [
    "open.spotify.com/episode", "open.spotify.com/show",
    "podcasts.apple.com",
    "soundcloud.com",
    "anchor.fm", "podcasters.spotify.com",
    "buzzsprout.com", "transistor.fm",
    "pocketcasts.com",
    "overcast.fm", "castro.fm",
    "podbean.com", "libsyn.com",
]

_TWITTER_DOMAINS = ["twitter.com", "x.com", "t.co"]


def extract_source(url: str, config: Config) -> SourceMeta:
    """
    Detect source type and dispatch to the correct extractor.
    """
    lower = url.lower().strip()

    if any(x in lower for x in ["youtube.com", "youtu.be", "yt.be"]):
        return extract_youtube(url, config=config)
    elif "instagram.com" in lower:
        return _extract_instagram(url, apify_api_key=getattr(config, "apify_api_key", ""))
    elif any(d in lower for d in _TWITTER_DOMAINS):
        return _extract_twitter(url, config)
    elif any(d in lower for d in _PODCAST_DOMAINS):
        return _extract_podcast(url, config)
    elif (lower.startswith("file://")
          or re.search(r"\.pdf(\?.*)?$", lower)):
        # Local files (file://, /path/to/file.pdf) and remote PDF URLs
        return extract_pdf(url, config)
    else:
        return extract_web(url, config)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  YOUTUBE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_video_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _get_yt_metadata(url: str) -> dict:
    # Try yt-dlp first (most complete)
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            
            # Extract chapters: [{"start": 0.0, "end": 10.0, "title": "..."}]
            raw_chapters = info.get("chapters", [])
            chapters = []
            if raw_chapters:
                for c in raw_chapters:
                    chapters.append({
                        "start": c.get("start_time", 0),
                        "end": c.get("end_time", 0),
                        "title": c.get("title", "Untitled Chapter")
                    })

            return {
                "title": info.get("title", "Untitled"),
                "author": info.get("uploader", info.get("channel", "")),
                "duration_sec": info.get("duration", 0),
                "description": info.get("description", "")[:500],
                "publish_date": info.get("upload_date", ""),
                "chapters": chapters,
            }
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    # Fallback: scrape oEmbed API (no auth needed, no bot detection)
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        resp = requests.get(oembed_url, timeout=10)
        if resp.ok:
            info = resp.json()
            return {
                "title": info.get("title", "Untitled"),
                "author": info.get("author_name", ""),
                "duration_sec": 0,
                "description": "",
                "publish_date": "",
            }
    except Exception:
        pass

    # Fallback: scrape page HTML for og:title and og:description
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        title_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', resp.text)
        desc_m = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', resp.text)
        return {
            "title": title_m.group(1) if title_m else "YouTube Video",
            "author": "",
            "duration_sec": 0,
            "description": (desc_m.group(1) if desc_m else "")[:500],
            "publish_date": "",
        }
    except Exception:
        pass

    return {"title": "YouTube Video", "author": "", "duration_sec": 0}


def _try_transcript_api(video_id: str) -> Optional[str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id)
        lines = []
        for entry in transcript:
            text = (entry.text.strip() if hasattr(entry, "text")
                    else str(entry.get("text", "")).strip())
            if text:
                lines.append(text)
        return " ".join(lines) if lines else None
    except Exception:
        return None


def _try_ytdlp_subs(url: str) -> Optional[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                [
                    "yt-dlp", "--write-auto-sub", "--write-sub",
                    "--sub-lang", "en", "--sub-format", "vtt",
                    "--skip-download", "--no-warnings",
                    "-o", f"{tmpdir}/%(id)s.%(ext)s", url,
                ],
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

        vtt_files = list(Path(tmpdir).glob("*.vtt"))
        if not vtt_files:
            return None

        lines, prev = [], ""
        for line in vtt_files[0].read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("WEBVTT") or "-->" in line:
                continue
            if re.match(r"^\d+$", line):
                continue
            line = re.sub(r"<[^>]+>", "", line)
            if line and line != prev:
                lines.append(line)
                prev = line
        return " ".join(lines)


def _download_audio(url: str, max_size_mb: int = 24) -> Optional[Path]:
    """
    Download audio from any yt-dlp-supported URL to a compressed temp file.

    Target: 32kbps mono MP3 at 16kHz — ~14MB/hour of audio.
    This keeps most episodes under Groq's 25MB file size limit.

    Returns the Path to the temp MP3, or None if download fails.
    The caller is responsible for deleting the temp file.
    """
    tmpdir = tempfile.mkdtemp()
    out_template = str(Path(tmpdir) / "audio.%(ext)s")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-x",                         # extract audio only
                "--audio-format", "mp3",       # convert to MP3
                "--audio-quality", "32K",      # very compressed: ~14MB/hour
                "--postprocessor-args",        # force 16kHz mono for smaller files
                "ffmpeg:-ar 16000 -ac 1",
                "--no-warnings",
                "-o", out_template,
                url,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return None

        mp3_files = list(Path(tmpdir).glob("*.mp3"))
        if not mp3_files:
            return None

        mp3 = mp3_files[0]
        size_mb = mp3.stat().st_size / (1024 * 1024)
        if size_mb > max_size_mb:
            print(f"[AUDIO] File too large ({size_mb:.1f}MB > {max_size_mb}MB), skipping Groq", flush=True)
            mp3.unlink(missing_ok=True)
            return None

        return mp3

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _transcribe_groq(audio_path: Path, api_key: str) -> Optional[str]:
    """
    Transcribe an audio file using Groq's Whisper API (whisper-large-v3).

    Groq runs Whisper at ~280x realtime — a 1-hour podcast transcribes in ~13s.
    Returns the full transcript text, or None on failure.

    Supports: MP3, M4A, WAV, FLAC, OGG, OPUS (max 25MB).
    """
    if not api_key:
        return None

    try:
        with open(audio_path, "rb") as f:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, f, "audio/mpeg")},
                data={
                    "model": "whisper-large-v3",
                    "response_format": "json",
                    # omit language → auto-detect
                },
                timeout=120,
            )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            return text if len(text) > 50 else None
    except Exception as e:
        print(f"[GROQ-WHISPER] Transcription failed: {e}", flush=True)
        return None
    finally:
        # Clean up temp audio file
        try:
            audio_path.unlink(missing_ok=True)
            audio_path.parent.rmdir()
        except Exception:
            pass


def _try_groq_whisper(url: str, config: Config) -> Optional[str]:
    """
    Full pipeline: download audio → Groq Whisper transcription.
    Returns transcript string or None if anything fails.
    """
    if not config.groq_api_key:
        return None

    print(f"[GROQ-WHISPER] Downloading audio for transcription...", flush=True)
    audio_path = _download_audio(url)
    if not audio_path:
        return None

    size_mb = audio_path.stat().st_size / (1024 * 1024)
    print(f"[GROQ-WHISPER] Transcribing {size_mb:.1f}MB audio...", flush=True)
    transcript = _transcribe_groq(audio_path, config.groq_api_key)
    if transcript:
        print(f"[GROQ-WHISPER] Got {len(transcript)} chars", flush=True)
    return transcript


def extract_voice_memo(audio_path: Path, config: "Config") -> "SourceMeta":
    """
    Transcribe a voice memo (M4A/WAV/MP3/OGG) via Groq Whisper and return a
    SourceMeta ready for the full Stage 1 → Stage 2 synthesis pipeline.

    Reuses _transcribe_groq — the same engine used for podcast transcription
    (~280× realtime, auto-detects language, 25 MB max).
    """
    from datetime import datetime as _dt

    if not config.groq_api_key:
        raise ValueError("[VOICE] groq_api_key is required for voice memo transcription")

    size_mb = audio_path.stat().st_size / (1024 * 1024)
    print(f"[VOICE] Transcribing {size_mb:.1f}MB via Groq Whisper...", flush=True)

    transcript = _transcribe_groq(audio_path, config.groq_api_key)
    if not transcript:
        raise ValueError("[VOICE] Groq Whisper returned an empty transcript")

    transcript = _clean_transcript(transcript)
    quality = _score_transcript(transcript)
    _log_transcript_quality(quality, tag="VOICE")

    now = _dt.now()
    return SourceMeta(
        url="",
        source_type="voice_memo",
        title=f"Voice Memo {now.strftime('%Y-%m-%d %H:%M')}",
        author="Self",
        duration_sec=0,
        transcript=transcript,
        transcript_quality=quality,
        source_id=f"voice_{now.strftime('%Y%m%d_%H%M%S')}",
    )


def _clean_transcript(text: str) -> str:
    text = re.sub(r"\[Music\]|\[Applause\]|\[Laughter\]", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _score_transcript(text: str) -> dict:
    """
    Pure-Python transcript quality scorer. No dependencies beyond stdlib.

    Three signals for auto-caption artifact detection:
      1. punctuation_density — sentence-ending chars per word. Auto-generated
         YouTube captions often have near-zero punctuation.
      2. ttr — type-token ratio on a capped 500-word window (length-normalised).
         Repetition loops score < 0.2; normal speech scores 0.4–0.6.
      3. repetition_ratio — fraction of 4-grams appearing more than once.
         Looping subs repeat the same phrases; natural speech rarely does.

    is_poor_quality requires 2+ signals to fail — single-signal false positives
    from legitimate repetitive content (speeches, drills) are suppressed.
    """
    from collections import Counter
    words = re.findall(r'\b\w+\b', text.lower())
    n = len(words)
    if n < 50:
        return {"punctuation_density": 1.0, "ttr": 1.0, "repetition_ratio": 0.0, "is_poor_quality": False}

    # Signal 1: sentence-ending punctuation density
    punct_count = sum(1 for c in text if c in '.!?')
    punctuation_density = punct_count / n

    # Signal 2: vocabulary diversity on capped window (prevents length bias)
    sample = words[:500]
    ttr = len(set(sample)) / len(sample)

    # Signal 3: 4-gram repetition ratio
    ngrams = [tuple(words[i:i+4]) for i in range(n - 3)]
    counts = Counter(ngrams)
    repeated = sum(1 for c in counts.values() if c > 1)
    repetition_ratio = repeated / len(counts) if counts else 0.0

    flags = [
        punctuation_density < 0.05,  # <5 sentence-ending marks per 100 words
        ttr < 0.35,                   # vocabulary diversity below 35%
        repetition_ratio > 0.20,      # >20% of 4-grams repeat
    ]
    is_poor_quality = sum(flags) >= 2

    return {
        "punctuation_density": round(punctuation_density, 4),
        "ttr": round(ttr, 4),
        "repetition_ratio": round(repetition_ratio, 4),
        "is_poor_quality": is_poor_quality,
    }


def _log_transcript_quality(tq: dict, tag: str = "EXTRACTOR") -> None:
    """Print the standard low-quality-transcript warning if tq flags it."""
    if not tq["is_poor_quality"]:
        return
    print(
        f"[{tag}] Low-quality transcript detected "
        f"(punct={tq['punctuation_density']:.3f}, ttr={tq['ttr']:.3f}, "
        f"repetition={tq['repetition_ratio']:.3f})",
        flush=True,
    )


def extract_youtube(url: str, config: Config | None = None) -> SourceMeta:
    """
    Extract transcript + metadata from YouTube.

    Transcript priority:
      1. YouTube Transcript API (official captions / auto-subs)
      2. yt-dlp subtitle download (.vtt)
      3. Groq Whisper transcription (direct audio — best for no-caption videos)
      4. Video description (fallback for Shorts)
      5. Title + author (last resort)
    """
    video_id = _extract_video_id(url)
    if not video_id:
        raise ValueError(f"Cannot parse YouTube video ID from: {url}")

    canonical = f"https://www.youtube.com/watch?v={video_id}"
    meta = _get_yt_metadata(canonical)

    transcript = _try_transcript_api(video_id)
    if not transcript:
        transcript = _try_ytdlp_subs(canonical)
    if not transcript and config:
        # Groq Whisper: download audio → transcribe (much better than captions for technical content)
        transcript = _try_groq_whisper(canonical, config)
    if not transcript:
        # Fallback to description for caption-less videos (e.g. Shorts)
        desc = meta.get("description", "").strip()
        if desc:
            transcript = desc
        else:
            # Last resort: use title + author as minimal content
            title = meta.get("title", "").strip()
            author = meta.get("author", "").strip()
            if title and title != "YouTube Video":
                transcript = f"{title}" + (f" by {author}" if author else "")
            else:
                raise RuntimeError(
                    f"No transcript, description, or metadata available for {canonical}."
                )

    cleaned = _clean_transcript(transcript)
    tq = _score_transcript(cleaned)
    _log_transcript_quality(tq)
    return SourceMeta(
        title=meta["title"],
        url=canonical,
        source_type="youtube",
        author=meta.get("author", ""),
        duration_sec=meta.get("duration_sec", 0),
        transcript=cleaned,
        description=meta.get("description", ""),
        publish_date=meta.get("publish_date", ""),
        source_id=_compute_source_id(canonical),
        chapters=meta.get("chapters"),
        transcript_quality=tq,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INSTAGRAM (yt-dlp native support)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _clean_instagram_url(url: str) -> str:
    """Strip Instagram tracking params (igsh, img_index, etc.) that cause yt-dlp failures.
    Keeps only the canonical reel/post path: https://www.instagram.com/reel/SHORTCODE/
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    # Reconstruct with no query string or fragment
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", "", "", ""))


def _rapidapi_fetch_instagram(clean_url: str, rapidapi_key: str) -> dict:
    """
    Fetch Instagram reel video URL + caption via RapidAPI instagram120 /links endpoint.
    Returns a normalised dict with keys: videoUrl, caption, title.

    Response shape from the API:
      [{"urls": [{"url": "https://...mp4", "extension": "mp4"}],
        "meta": {"title": "caption text...", "sourceUrl": "..."}}]
    """
    import urllib.request
    import json as _json

    payload = _json.dumps({"url": clean_url}).encode("utf-8")
    req = urllib.request.Request(
        "https://instagram120.p.rapidapi.com/api/instagram/links",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-rapidapi-host": "instagram120.p.rapidapi.com",
            "x-rapidapi-key": rapidapi_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list) or not data:
                return {}
            item = data[0]
            urls = item.get("urls", [])
            video_url = next(
                (u["url"] for u in urls if u.get("extension") == "mp4"),
                urls[0]["url"] if urls else "",
            )
            caption = item.get("meta", {}).get("title", "")
            return {"videoUrl": video_url, "caption": caption}
    except Exception as e:
        print(f"[INSTAGRAM] RapidAPI error: {e}", flush=True)
    return {}


def _apify_fetch_instagram(clean_url: str, apify_key: str) -> dict:
    """
    Fallback: Apify instagram-scraper actor.
    Returns partial metadata (caption via description field) even when video is restricted.
    """
    import urllib.request
    import json as _json

    actor = "shu8hvrXbJbY3Eb9W"
    endpoint = (
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        f"?token={apify_key}&timeout=90&format=json"
    )
    payload = _json.dumps({
        "directUrls": [clean_url],
        "resultsType": "posts",
        "resultsLimit": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=100) as resp:
            items = _json.loads(resp.read().decode("utf-8"))
            if items and isinstance(items, list):
                return items[0]
    except Exception as e:
        print(f"[INSTAGRAM] Apify error: {e}", flush=True)
    return {}


def _download_video_to_tempfile(video_url: str, suffix: str = ".mp4") -> str:
    """Download a video URL to a tempfile. Returns the path; raises if the download is empty."""
    import urllib.request as _ur
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    _ur.urlretrieve(video_url, tmp.name)
    if Path(tmp.name).stat().st_size == 0:
        Path(tmp.name).unlink(missing_ok=True)
        raise ValueError("downloaded file is empty")
    return tmp.name


def _extract_instagram(url: str, apify_api_key: str = "") -> SourceMeta:
    """
    Instagram Reels adapter — downloads the actual video for Gemini visual analysis.

    Flow:
      1. RapidAPI instagram120 /links — returns direct MP4 URL + full caption. Primary path.
      2. Apify instagram-scraper — fallback for metadata/caption when RapidAPI fails.
         Returns partial data (no videoUrl) when Instagram restricts access.
      3. yt-dlp — last resort, blocked on VPS datacenter IPs without cookies.
      4. Text-only — caption from Apify description field, still produces good notes.

    The downloaded video path is stored in SourceMeta.video_path so processor.py
    can upload it to the Gemini File API and run the two-pass visual pipeline.
    The temp file is deleted by processor.py after the Gemini File API upload completes.
    """
    clean_url = _clean_instagram_url(url)
    rapidapi_key = os.getenv("RAPIDAPI_KEY", "")

    video_path = ""
    caption = ""

    # ── Attempt 1: RapidAPI (managed sessions, currently working) ──
    if rapidapi_key:
        print("[INSTAGRAM] Trying RapidAPI...", flush=True)
        rapid_meta = _rapidapi_fetch_instagram(clean_url, rapidapi_key)
        video_url = rapid_meta.get("videoUrl", "")
        caption = rapid_meta.get("caption", "")
        if video_url:
            try:
                video_path = _download_video_to_tempfile(video_url)
                size = Path(video_path).stat().st_size
                print(f"[INSTAGRAM] RapidAPI download OK: {size // 1024}KB", flush=True)
            except Exception as e:
                print(f"[INSTAGRAM] RapidAPI video download error: {e}, trying Apify", flush=True)
        else:
            print("[INSTAGRAM] RapidAPI returned no videoUrl, trying Apify", flush=True)

    # ── Attempt 2: Apify (metadata fallback — video often restricted) ──
    apify_meta = {}
    if not video_path and apify_api_key:
        print("[INSTAGRAM] Trying Apify scraper...", flush=True)
        apify_meta = _apify_fetch_instagram(clean_url, apify_api_key)
        video_url = apify_meta.get("videoUrl", "")
        if video_url and not video_path:
            try:
                video_path = _download_video_to_tempfile(video_url)
                size = Path(video_path).stat().st_size
                print(f"[INSTAGRAM] Apify download OK: {size // 1024}KB", flush=True)
            except Exception as e:
                print(f"[INSTAGRAM] Apify video download error: {e}", flush=True)
        elif not video_url:
            print("[INSTAGRAM] Apify returned no videoUrl, trying yt-dlp", flush=True)

    # ── Attempt 2: yt-dlp (fallback — usually blocked on VPS) ──
    if not video_path:
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            tmp.close()
            cmd = [
                "yt-dlp",
                "--format", "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]/best",
                "--merge-output-format", "mp4",
                "--output", tmp.name,
                "--no-playlist", "--quiet", "--no-warnings",
                clean_url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode == 0 and Path(tmp.name).exists() and Path(tmp.name).stat().st_size > 0:
                video_path = tmp.name
                print(f"[INSTAGRAM] yt-dlp download OK: {Path(tmp.name).stat().st_size // 1024}KB", flush=True)
            else:
                print(f"[INSTAGRAM] yt-dlp failed (rc={result.returncode}), text-only fallback", flush=True)
                Path(tmp.name).unlink(missing_ok=True)
        except Exception as e:
            print(f"[INSTAGRAM] yt-dlp error: {e}, text-only fallback", flush=True)

    # ── Metadata: prefer Apify fields, fill gaps from yt-dlp ──
    yt_meta = _get_yt_metadata(clean_url)

    # Author
    author = (
        apify_meta.get("ownerFullName")
        or apify_meta.get("ownerUsername")
        or yt_meta.get("author", "")
    )

    # Caption / transcript — prefer RapidAPI (full clean text), fallback to Apify fields
    if not caption:
        caption = apify_meta.get("caption") or apify_meta.get("text", "")
    if not caption:
        # Apify restricted_page: caption lands in description as
        # "N likes, N comments - username on date: \"actual caption text\""
        raw_desc = apify_meta.get("description", "")
        if raw_desc and ': "' in raw_desc:
            caption = raw_desc.split(': "', 1)[-1].rstrip('"')
        elif raw_desc:
            caption = raw_desc
    transcript = _try_ytdlp_subs(clean_url) or caption or yt_meta.get("description", "No captions available.")

    # Duration
    duration = (
        apify_meta.get("videoDuration")
        or apify_meta.get("duration")
        or yt_meta.get("duration_sec", 0)
    )
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0

    # Title: build from caption, strip hashtags
    raw_caption = caption or html.unescape(yt_meta.get("title", ""))
    clean = re.sub(r'^.*?on Instagram\s*[:\-–]?\s*["\u201c]?', "", raw_caption, flags=re.IGNORECASE).strip().strip('""\u201d')
    clean = re.sub(r"\s*#\S+", "", clean).strip().strip('"\u201d.,')
    # Normalize internal whitespace (newlines → space) before truncating,
    # otherwise rsplit(" ") cuts at the last space before a newline mid-sentence
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > 80:
        clean = clean[:80].rsplit(" ", 1)[0].strip().strip('.,;:')
    title = clean if len(clean) > 5 else (f"{author} — Instagram Reel" if author else "Instagram Reel")

    return SourceMeta(
        title=title, url=url, source_type="instagram",
        author=author, duration_sec=duration,
        transcript=transcript, description=caption or yt_meta.get("description", ""),
        source_id=_compute_source_id(url), video_path=video_path,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PODCAST (yt-dlp — Spotify, Apple Podcasts, SoundCloud, etc.)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_podcast(url: str, config: Config) -> SourceMeta:
    """
    Podcast extractor using yt-dlp + Groq Whisper for transcription.

    Transcript priority:
      1. yt-dlp subtitle download (some platforms serve transcripts)
      2. Groq Whisper (whisper-large-v3): download audio → transcribe
         This is the primary path for most podcasts — vastly better than
         show notes alone and works for any platform yt-dlp can download from.
      3. Show description / notes (fallback)
      4. Title stub (last resort)

    Note: Spotify requires authentication for full playback. yt-dlp can
    usually extract metadata and audio for most other platforms.
    """
    meta = _get_yt_metadata(url)

    # Try yt-dlp subtitles first (fastest — no download needed)
    transcript = _try_ytdlp_subs(url)

    # Primary transcription: Groq Whisper (direct audio)
    if not transcript:
        transcript = _try_groq_whisper(url, config)

    # Fallback: show description / notes
    if not transcript:
        description = meta.get("description", "").strip()
        if description and len(description) > 100:
            transcript = description
        else:
            transcript = meta.get("title", "Podcast Episode")

    cleaned = _clean_transcript(transcript)
    tq = _score_transcript(cleaned)
    _log_transcript_quality(tq)
    return SourceMeta(
        title=meta.get("title", "Podcast Episode"),
        url=url,
        source_type="podcast",
        author=meta.get("author", ""),
        duration_sec=meta.get("duration_sec", 0),
        transcript=cleaned,
        description=meta.get("description", "")[:500],
        publish_date=meta.get("publish_date", ""),
        source_id=_compute_source_id(url),
        transcript_quality=tq,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TWITTER / X (Jina Reader with thread assembly)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_twitter(url: str, config: Config) -> SourceMeta:
    """
    Twitter/X extractor. Jina Reader handles Twitter well — it assembles
    threads and returns the full text. We set source_type="twitter" so
    the processor can give tailored instructions (threads are structured
    differently from articles).

    Normalize t.co → the original tweet URL for dedup.
    """
    result = _jina_extract(url, config)

    if not result:
        result = _bs4_fallback(url)

    if not result:
        raise RuntimeError(
            f"Could not extract tweet/thread from {url}. "
            "The account may be private or the URL may be invalid."
        )

    title, author, content = result

    # Jina often returns "on Twitter: \"...\"" as title — clean it up
    if not title or title.lower().startswith("x (formerly"):
        # Extract first meaningful line as title
        first_line = content.split("\n")[0].strip()
        title = first_line[:80] + ("..." if len(first_line) > 80 else "")

    return SourceMeta(
        title=title or "Twitter Thread",
        url=url,
        source_type="twitter",
        author=author,
        transcript=content,
        source_id=_compute_source_id(url),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WEB (Jina Reader → BS4 fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _jina_extract(url: str, config: Config) -> Optional[tuple[str, str, str]]:
    """
    Jina Reader API: returns clean markdown from any URL.
    Handles JS-rendered SPAs, paywalls, ads — one HTTP call.
    Returns (title, author, markdown) or None on failure.
    """
    headers = {"Accept": "application/json"}
    if config.jina_api_key:
        headers["Authorization"] = f"Bearer {config.jina_api_key}"

    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers=headers,
            timeout=config.jina_timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            inner = data.get("data", {})
            title = inner.get("title", "")
            content = inner.get("content", "")
            # Jina doesn't always return author
            author = inner.get("author", "")
            if content and len(content) > 100:
                return title, author, content
    except Exception:
        pass
    return None


def _bs4_fallback(url: str) -> Optional[tuple[str, str, str]]:
    """Fallback: BeautifulSoup + markdownify for when Jina is down."""
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify as md
    except ImportError:
        return None

    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                )
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"]
    elif soup.title:
        title = soup.title.get_text(strip=True)

    author = ""
    for meta in soup.find_all("meta"):
        if meta.get("name", "").lower() in ("author", "article:author"):
            author = meta.get("content", "")
            break

    for sel in ["nav", "header", "footer", "aside", ".sidebar",
                ".comments", ".ad", "script", "style", "noscript"]:
        for el in soup.select(sel):
            el.decompose()

    content_el = soup.find("article") or soup.find("main") or soup.find("body")
    if not content_el:
        return None

    markdown = md(str(content_el), heading_style="ATX",
                  strip=["img", "figure", "figcaption"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    if len(markdown) < 100:
        return None
    return title or "Untitled", author, markdown


def extract_web(url: str, config: Config) -> SourceMeta:
    """Extract article content from a web URL."""
    # Primary: Jina Reader API
    result = _jina_extract(url, config)

    # Fallback: BS4
    if not result:
        result = _bs4_fallback(url)

    if not result:
        raise RuntimeError(
            f"Could not extract content from {url}. "
            "The page may be JS-rendered, paywalled, or empty."
        )

    title, author, content = result

    # Truncate very long articles
    if len(content) > 80000:
        content = content[:40000] + "\n\n[...TRUNCATED...]\n\n" + content[-40000:]

    return SourceMeta(
        title=title,
        url=url,
        source_type="web",
        author=author,
        transcript=content,
        source_id=_compute_source_id(url),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PDF (PyMuPDF with page-limit + chunking)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ── Marker singleton (Tier 0 PDF extractor) ──────────────────

_marker_converter = None   # lazy-loaded on first PDF extraction


def _try_marker(path) -> str | None:
    """
    Tier 0 PDF extraction via Marker (IBM/VikParuchuri).
    2026 SOTA: 95.67% accuracy on structured PDFs vs 86.71% for pymupdf4llm.

    Lazy-loads PdfConverter + model dict on first call (~300 MB, cached to disk).
    Returns markdown string or None if marker-pdf is not installed / extraction fails.
    """
    global _marker_converter
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
    except ImportError:
        return None  # marker-pdf not installed — silent fall-through to Tier 1

    if _marker_converter is None:
        print("[PDF] Initialising Marker models (first-time ~300 MB download)...", flush=True)
        _marker_converter = PdfConverter(artifact_dict=create_model_dict())
        print("[PDF] Marker ready.", flush=True)

    rendered = _marker_converter(str(path))
    text, _, _ = text_from_rendered(rendered)
    return text if text and len(text.strip()) >= 100 else None


def _resolve_pdf_path(url_or_path: str, _tmpdir_holder: list | None = None) -> Path:
    """
    Resolve a PDF source to a local Path, downloading if necessary.

    For remote http(s) URLs: downloads to a temp file (caller must manage cleanup).
    For file:// URLs and local paths: resolves normally.

    _tmpdir_holder: if provided (a list), a TemporaryDirectory object is appended
    so the caller controls lifetime. Otherwise caller should pass a list and keep it
    alive for the duration of PDF access.
    """
    from urllib.parse import unquote, urlparse as up
    parsed = up(url_or_path)

    if parsed.scheme in ("http", "https"):
        # Download remote PDF to a temp file
        td = tempfile.TemporaryDirectory()
        if _tmpdir_holder is not None:
            _tmpdir_holder.append(td)
        suffix = Path(parsed.path).suffix or ".pdf"
        dest = Path(td.name) / f"remote{suffix}"
        resp = requests.get(
            url_or_path,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
            stream=True,
        )
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        return dest

    if parsed.scheme == "file":
        return Path(unquote(parsed.path))

    return Path(url_or_path).expanduser().resolve()


def extract_pdf(url: str, config: Config) -> SourceMeta:
    """
    Extract text from a PDF (local file, file:// URL, or remote https:// URL)
    with page-limit and chunking.

    Prevents 100-page papers from blowing out the LLM context window:
      1. Hard cap at config.pdf_max_pages (default 40)
      2. Truncate to config.pdf_chunk_chars (default 60000 chars)
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError(
            "PyMuPDF required for PDF extraction. Install: pip install pymupdf"
        )

    tmpdir_holder: list = []
    path = _resolve_pdf_path(url, _tmpdir_holder=tmpdir_holder)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    full_text = None
    used_markdown = False

    # ── Get PDF metadata via PyMuPDF (always fast, no models) ─
    try:
        doc = fitz.open(str(path))
        meta_raw = doc.metadata or {}
        title = (meta_raw.get("title", "").strip()
                 or path.stem.replace("-", " ").replace("_", " ").title())
        author = meta_raw.get("author", "")
        total_pages = len(doc)
        pages_to_read = min(total_pages, config.pdf_max_pages)
        doc.close()
    except Exception as _fitz_meta_err:
        title = path.stem.replace("-", " ").title()
        author = ""
        total_pages = 0
        pages_to_read = config.pdf_max_pages
    finally:
        # tmpdir cleanup happens at the end — hold off until text extracted
        pass

    try:
        # ── Tier 0: Marker — VLM layout detection (best for complex PDFs) ──
        # 2026 SOTA: 95.67% heuristic accuracy vs pymupdf4llm's 86.71%.
        # Handles multi-column academic papers, tables, equations, code blocks.
        # Lazy singleton — models downloaded on first call (~300 MB, cached).
        # Falls through to Tier 1 if marker-pdf not installed or extraction fails.
        if getattr(config, "marker_enabled", True):
            try:
                md = _try_marker(path)
                if md and len(md) >= 100:
                    full_text = md
                    used_markdown = True
                    print(
                        f"[PDF] Marker: {len(full_text):,} chars (Tier 0 structure-aware)",
                        flush=True,
                    )
            except Exception as _marker_err:
                print(f"[PDF] Marker failed ({_marker_err!r}), trying Tier 1...", flush=True)

        # ── Tier 1: pymupdf4llm — fast structure-aware markdown ────────────
        if not full_text:
            try:
                doc = fitz.open(str(path))
                import pymupdf4llm
                md = pymupdf4llm.to_markdown(
                    doc,
                    pages=list(range(pages_to_read)),
                    write_images=False,
                    embed_images=False,
                )
                doc.close()
                if md and len(md) >= 50:
                    full_text = md
                    used_markdown = True
                    print(
                        f"[PDF] pymupdf4llm: {pages_to_read}/{total_pages} pages "
                        f"→ {len(full_text):,} chars (Tier 1 structure-aware)",
                        flush=True,
                    )
            except ImportError:
                try: doc.close()
                except Exception: pass
                print("[PDF] pymupdf4llm unavailable, using plain text.", flush=True)
            except Exception as _pm_err:
                try: doc.close()
                except Exception: pass
                print(f"[PDF] pymupdf4llm failed ({_pm_err!r}), using plain text.", flush=True)

        # ── Tier 2: plain text via PyMuPDF ────────────────────────────────
        if not full_text:
            doc = fitz.open(str(path))
            pages = []
            for i in range(min(pages_to_read, len(doc))):
                text = doc[i].get_text("text").strip()
                if text:
                    pages.append(f"[Page {i+1}]\n{text}")
            doc.close()
            full_text = "\n\n".join(pages)

    finally:
        for td in tmpdir_holder:
            td.cleanup()

    # Normalise whitespace — preserve markdown structure for pymupdf4llm output
    # (collapsing spaces destroys table cell alignment and code block indentation)
    if used_markdown:
        full_text = re.sub(r"\n{4,}", "\n\n\n", full_text)
    else:
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)
        full_text = re.sub(r"[ \t]+", " ", full_text)

    if not full_text or len(full_text) < 50:
        raise RuntimeError(
            f"Could not extract text from {path.name}. "
            "The PDF may be scanned/image-only (needs OCR)."
        )

    # Chunk-limit: truncate to prevent context window blowout
    if len(full_text) > config.pdf_chunk_chars:
        half = config.pdf_chunk_chars // 2
        full_text = (
            full_text[:half]
            + f"\n\n[...TRUNCATED — showing {pages_to_read} of {total_pages} pages...]\n\n"
            + full_text[-half:]
        )

    return SourceMeta(
        title=title,
        url=url,  # use original URL (not temp path) for dedup and source note
        source_type="pdf",
        author=author,
        transcript=full_text.strip(),
        description=f"{total_pages} pages, extracted {pages_to_read}",
        source_id=_compute_source_id(url),
    )
