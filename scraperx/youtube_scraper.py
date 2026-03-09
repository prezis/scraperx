"""YouTube video scraper — download + transcribe via yt-dlp + whisper.

Pipeline:
  1. yt-dlp: download audio (mp3) from YouTube URL
  2. whisper: transcribe audio to text
  3. Return transcript + metadata

Supports:
  - YouTube URLs (youtube.com, youtu.be)
  - Auto-captions extraction (fast, no whisper needed)
  - Whisper transcription fallback (slower, requires whisper)

Usage:
    from scraperx.youtube_scraper import YouTubeScraper
    scraper = YouTubeScraper()
    result = scraper.get_transcript("https://www.youtube.com/watch?v=...")
    print(result.title, result.transcript[:500])
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

logger = logging.getLogger(__name__)

YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/)"
    r"(?P<id>[a-zA-Z0-9_-]{11})"
)

DEFAULT_TRANSCRIPT_DIR = os.path.join(
    os.path.expanduser('~'),
    '.scraperx', 'transcripts'
)


@dataclass
class YouTubeResult:
    """Parsed YouTube video data with transcript."""
    video_id: str
    title: str
    channel: str
    duration_seconds: int = 0
    transcript: str = ""
    transcript_method: str = ""  # 'auto_captions' or 'whisper'
    audio_path: Optional[str] = None
    transcript_path: Optional[str] = None
    metadata: dict = field(default_factory=dict, repr=False)


def parse_youtube_url(url: str) -> str:
    """Extract video ID from YouTube URL."""
    m = YOUTUBE_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a valid YouTube URL: {url}")
    return m.group("id")


class YouTubeScraper:
    """YouTube video scraper with auto-captions + whisper fallback."""

    def __init__(self, *,
                 output_dir: str = DEFAULT_TRANSCRIPT_DIR,
                 whisper_model: str = 'base',
                 language: str = 'en'):
        self.output_dir = output_dir
        self.whisper_model = whisper_model
        self.language = language
        os.makedirs(output_dir, exist_ok=True)

    def get_metadata(self, url: str) -> dict:
        """Fetch video metadata without downloading."""
        cmd = [
            'yt-dlp',
            '--dump-json', '--no-download', url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp metadata failed: {result.stderr[:300]}")
        return json.loads(result.stdout)

    def get_transcript(self, url: str, *,
                       force_whisper: bool = False,
                       max_duration_minutes: int = 120) -> YouTubeResult:
        """Get transcript from YouTube video.

        Strategy:
        1. Try auto-captions first (fast, free)
        2. If no captions -> download audio + whisper

        Args:
            url: YouTube URL
            force_whisper: Skip auto-captions, use whisper directly
            max_duration_minutes: Skip videos longer than this (CPU protection)

        Returns:
            YouTubeResult with transcript
        """
        video_id = parse_youtube_url(url)
        logger.info("Processing YouTube video: %s", video_id)

        # Get metadata
        meta = self.get_metadata(url)
        title = meta.get('title', 'Unknown')
        channel = meta.get('channel', meta.get('uploader', 'Unknown'))
        duration = int(meta.get('duration', 0))

        result = YouTubeResult(
            video_id=video_id,
            title=title,
            channel=channel,
            duration_seconds=duration,
            metadata=meta,
        )

        # Duration guard
        if duration > max_duration_minutes * 60:
            raise ValueError(
                f"Video too long: {duration // 60}min > {max_duration_minutes}min limit. "
                f"Use max_duration_minutes= to override."
            )

        # Strategy 1: Auto-captions (fast)
        if not force_whisper:
            transcript = self._try_auto_captions(url, video_id)
            if transcript:
                result.transcript = transcript
                result.transcript_method = 'auto_captions'
                self._save_transcript(result)
                return result

        # Strategy 2: Download audio + whisper
        logger.info("No auto-captions, using whisper (model=%s)", self.whisper_model)
        audio_path = self._download_audio(url, video_id)
        result.audio_path = audio_path

        transcript = self._whisper_transcribe(audio_path)
        result.transcript = transcript
        result.transcript_method = 'whisper'
        self._save_transcript(result)

        # Cleanup audio after transcription
        try:
            os.remove(audio_path)
        except OSError:
            pass

        return result

    def _try_auto_captions(self, url: str, video_id: str) -> Optional[str]:
        """Try to get auto-generated captions from YouTube."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                'yt-dlp',
                '--write-auto-sub', '--sub-lang', self.language,
                '--skip-download', '--sub-format', 'vtt',
                '-o', os.path.join(tmpdir, video_id),
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            # Look for subtitle file
            for f in os.listdir(tmpdir):
                if f.endswith('.vtt'):
                    vtt_path = os.path.join(tmpdir, f)
                    return self._parse_vtt(vtt_path)

        return None

    def _parse_vtt(self, vtt_path: str) -> str:
        """Parse VTT subtitle file to plain text."""
        with open(vtt_path, 'r', encoding='utf-8') as f:
            content = f.read()

        lines = []
        prev_line = None
        for line in content.split('\n'):
            # Skip VTT headers, timestamps, empty lines
            line = line.strip()
            if not line or line.startswith('WEBVTT') or '-->' in line:
                continue
            if line.startswith('Kind:') or line.startswith('Language:'):
                continue
            # Remove VTT formatting tags
            clean = re.sub(r'<[^>]+>', '', line)
            clean = clean.strip()
            if clean and clean != prev_line:
                lines.append(clean)
                prev_line = clean

        return ' '.join(lines)

    def _download_audio(self, url: str, video_id: str) -> str:
        """Download audio-only via yt-dlp."""
        audio_path = os.path.join(self.output_dir, f'{video_id}.mp3')
        cmd = [
            'yt-dlp',
            '-f', 'bestaudio', '-x', '--audio-format', 'mp3',
            '-o', audio_path, url,
        ]
        logger.info("Downloading audio: %s", video_id)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp audio download failed: {result.stderr[:300]}")

        # yt-dlp may add extension
        if not os.path.exists(audio_path):
            # Check for auto-renamed file
            for f in os.listdir(self.output_dir):
                if f.startswith(video_id) and f.endswith('.mp3'):
                    audio_path = os.path.join(self.output_dir, f)
                    break

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found after download: {audio_path}")

        return audio_path

    def _whisper_transcribe(self, audio_path: str) -> str:
        """Transcribe audio using whisper."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                'whisper', audio_path,
                '--model', self.whisper_model,
                '--language', self.language,
                '--output_format', 'txt',
                '-o', tmpdir,
            ]
            logger.info("Transcribing with whisper model=%s", self.whisper_model)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                raise RuntimeError(f"Whisper failed: {result.stderr[:300]}")

            # Read transcript
            for f in os.listdir(tmpdir):
                if f.endswith('.txt'):
                    txt_path = os.path.join(tmpdir, f)
                    with open(txt_path, 'r', encoding='utf-8') as fh:
                        return fh.read().strip()

        raise RuntimeError("Whisper produced no output file")

    def _save_transcript(self, result: YouTubeResult):
        """Save transcript to disk."""
        path = os.path.join(self.output_dir, f'{result.video_id}.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"Title: {result.title}\n")
            f.write(f"Channel: {result.channel}\n")
            f.write(f"Duration: {result.duration_seconds // 60}min\n")
            f.write(f"Method: {result.transcript_method}\n")
            f.write(f"---\n\n")
            f.write(result.transcript)
        result.transcript_path = path
        logger.info("Transcript saved: %s", path)
