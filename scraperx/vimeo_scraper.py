"""
Draft: scraperx/scraperx/vimeo_scraper.py (NEW FILE).

CRITICAL FIX from GPU draft: Use stdlib `urllib.request` — NOT `requests`.
scraperx is stdlib-only per policy.

Also imports VTT parser + whisper helpers from `scraperx.youtube_scraper`.
Future refactor (Agent 3 task): extract these into `scraperx._transcript_common`.
For now, import them directly to avoid blocking this work.

API mirrors YouTubeScraper: get_metadata(url), get_transcript(url, force_whisper=, max_duration_minutes=, referer=)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Accepted URL forms for Vimeo — return (id, optional_hash)
VIMEO_URL_RE = re.compile(
    r"(?:player\.)?vimeo\.com/(?:video/|videos/|channels/[^/]+/|showcase/\d+/video/|event/)?(?P<id>\d+)(?:/(?P<hash>[a-f0-9]+))?",
    re.IGNORECASE,
)

VIMEO_HOST_ALLOWLIST = {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class VimeoResult:
    """Result of Vimeo transcription. Mirrors YouTubeResult shape for cross-provider use."""
    provider: str = "vimeo"
    video_id: str = ""
    title: str = ""
    author: str = ""
    duration_seconds: float = 0.0
    canonical_url: str = ""
    transcript: str = ""
    transcript_method: str = ""  # "text_tracks" | "whisper_faster" | "whisper_cli"
    text_tracks_language: str = ""
    source_page_url: Optional[str] = None
    referer: Optional[str] = None
    raw_config: dict = field(default_factory=dict, repr=False)


def parse_vimeo_url(url: str) -> tuple[str, Optional[str]]:
    """Extract (video_id, optional_unlisted_hash) from any Vimeo URL variant."""
    m = VIMEO_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a valid Vimeo URL: {url}")
    return m.group("id"), m.group("hash")


def _http_get(url: str, timeout: int = 15, referer: Optional[str] = None) -> bytes:
    """Stdlib GET with optional Referer (required for embed-domain-locked videos)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,*/*;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_json(url: str, timeout: int = 15, referer: Optional[str] = None) -> dict:
    body = _http_get(url, timeout=timeout, referer=referer).decode("utf-8", errors="replace")
    return json.loads(body)


def _is_vimeo_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in VIMEO_HOST_ALLOWLIST
    except Exception:
        return False


def _fetch_oembed(url: str, timeout: int = 15) -> dict:
    """Fetch Vimeo oEmbed for public metadata (unauth)."""
    endpoint = f"https://vimeo.com/api/oembed.json?url={quote_plus(url)}"
    return _http_get_json(endpoint, timeout=timeout)


def _fetch_player_config(video_id: str, timeout: int = 15, referer: Optional[str] = None) -> dict:
    """Fetch unauthenticated player config JSON — the goldmine for text_tracks.

    Endpoint: https://player.vimeo.com/video/{id}/config
    Works for public + unlisted-with-hash videos. Private/password/embed-locked
    may 403/404 without correct Referer.
    """
    endpoint = f"https://player.vimeo.com/video/{video_id}/config"
    return _http_get_json(endpoint, timeout=timeout, referer=referer)


def _parse_duration(raw_cfg: dict) -> float:
    try:
        return float(raw_cfg.get("video", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _select_text_track(raw_cfg: dict, preferred_lang: str = "en") -> Optional[dict]:
    """Pick best text_track from player config. Prefers English, accepts any."""
    tracks = raw_cfg.get("request", {}).get("text_tracks") or []
    if not isinstance(tracks, list):
        return None
    # Prefer exact lang match, then any lang starting with preferred
    for t in tracks:
        if isinstance(t, dict) and t.get("lang") == preferred_lang:
            return t
    for t in tracks:
        if isinstance(t, dict) and (t.get("lang") or "").startswith(preferred_lang):
            return t
    # Any track
    for t in tracks:
        if isinstance(t, dict) and t.get("url"):
            return t
    return None


def _download_vtt(track_url: str, timeout: int = 15, referer: Optional[str] = None) -> str:
    return _http_get(track_url, timeout=timeout, referer=referer).decode("utf-8", errors="replace")


def _ytdlp_download_audio(video_url: str, out_dir: str, referer: Optional[str] = None, timeout: int = 600) -> Optional[str]:
    """Use yt-dlp to download audio-only from Vimeo. Returns path to audio file or None."""
    out_tpl = os.path.join(out_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "-o", out_tpl,
        "--quiet",
    ]
    if referer:
        cmd.extend(["--referer", referer])
    cmd.append(video_url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.warning("yt-dlp failed (%s): %s", result.returncode, result.stderr[:200])
            return None
        # Find the produced file
        for f in os.listdir(out_dir):
            if f.endswith(".mp3"):
                return os.path.join(out_dir, f)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out downloading %s", video_url)
        return None
    except FileNotFoundError:
        logger.warning("yt-dlp not found in PATH — cannot download Vimeo audio")
        return None


class VimeoScraper:
    """Fetch Vimeo metadata + transcripts.

    Transcript fallback chain:
      1. Player config `request.text_tracks[]` (creator-uploaded VTT, rare)
      2. yt-dlp → audio → faster-whisper (GPU preferred) → whisper CLI fallback
    """

    def __init__(self):
        pass

    def get_metadata(self, url: str) -> dict:
        """Returns oEmbed metadata dict. Raises on invalid URL or unavailable video."""
        if not _is_vimeo_url(url):
            raise ValueError(f"Not a Vimeo URL: {url}")
        video_id, _hash = parse_vimeo_url(url)
        try:
            oembed = _fetch_oembed(url)
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            raise RuntimeError(f"oEmbed fetch failed for {url}: {e}") from e
        return {
            "video_id": video_id,
            "title": oembed.get("title", ""),
            "author_name": oembed.get("author_name", ""),
            "duration": oembed.get("duration", 0),
            "thumbnail_url": oembed.get("thumbnail_url", ""),
            "upload_date": oembed.get("upload_date", ""),
            "html": oembed.get("html", ""),
            "canonical_url": f"https://vimeo.com/{video_id}",
        }

    def get_transcript(
        self,
        url: str,
        force_whisper: bool = False,
        max_duration_minutes: int = 120,
        referer: Optional[str] = None,
    ) -> VimeoResult:
        """Mirror YouTubeScraper.get_transcript() API."""
        if not _is_vimeo_url(url):
            raise ValueError(f"Not a Vimeo URL: {url}")

        video_id, _hash = parse_vimeo_url(url)
        canonical_url = f"https://vimeo.com/{video_id}"

        # Fetch player config (goldmine)
        try:
            cfg = _fetch_player_config(video_id, referer=referer)
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"Vimeo player config fetch failed ({video_id}): {e}. "
                f"For embed-domain-locked videos, pass referer= matching the embedder page."
            ) from e

        title = cfg.get("video", {}).get("title", "") or ""
        author = (cfg.get("video", {}).get("owner", {}) or {}).get("name", "") or ""
        duration = _parse_duration(cfg)

        result = VimeoResult(
            video_id=video_id,
            title=title,
            author=author,
            duration_seconds=duration,
            canonical_url=canonical_url,
            source_page_url=referer,
            referer=referer,
            raw_config=cfg,
        )

        if duration > max_duration_minutes * 60:
            logger.warning("Vimeo %s duration %.0fs exceeds cap %dmin — proceeding anyway",
                           video_id, duration, max_duration_minutes)

        # --- Stage 1: text_tracks if available and not forced to whisper ---
        if not force_whisper:
            track = _select_text_track(cfg)
            if track and track.get("url"):
                try:
                    vtt_content = _download_vtt(track["url"], referer=referer)
                    # Import _parse_vtt lazily to avoid circular imports
                    from scraperx.youtube_scraper import _parse_vtt
                    transcript_text = _parse_vtt(vtt_content)
                    if transcript_text.strip():
                        result.transcript = transcript_text
                        result.transcript_method = "text_tracks"
                        result.text_tracks_language = track.get("lang", "")
                        return result
                except Exception as e:
                    logger.warning("text_tracks parse failed, falling through to whisper: %s", e)

        # --- Stage 2: yt-dlp → whisper ---
        with tempfile.TemporaryDirectory(prefix="scraperx_vimeo_") as tmpdir:
            audio_path = _ytdlp_download_audio(canonical_url, tmpdir, referer=referer)
            if not audio_path:
                raise RuntimeError(
                    f"Vimeo transcript failed for {video_id}: yt-dlp could not download audio "
                    f"(embed-domain-locked? pass referer=)"
                )
            try:
                from scraperx.youtube_scraper import (
                    _detect_whisper_backend,
                    _transcribe_faster_whisper,
                    _transcribe_whisper_cli,
                )
            except ImportError as e:
                raise RuntimeError(f"youtube_scraper whisper helpers unavailable: {e}") from e

            backend = _detect_whisper_backend()
            if backend == "faster":
                result.transcript = _transcribe_faster_whisper(audio_path)
                result.transcript_method = "whisper_faster"
            else:
                result.transcript = _transcribe_whisper_cli(audio_path)
                result.transcript_method = "whisper_cli"

        return result
