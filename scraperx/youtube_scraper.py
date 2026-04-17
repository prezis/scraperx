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

logger = logging.getLogger(__name__)

YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/)"
    r"(?P<id>[a-zA-Z0-9_-]{11})"
)

DEFAULT_TRANSCRIPT_DIR = os.path.join(os.path.expanduser("~"), ".scraperx", "transcripts")


@dataclass
class YouTubeResult:
    """Parsed YouTube video data with transcript."""

    video_id: str
    title: str
    channel: str
    duration_seconds: int = 0
    transcript: str = ""
    transcript_method: str = ""  # 'auto_captions' or 'whisper'
    audio_path: str | None = None
    transcript_path: str | None = None
    metadata: dict = field(default_factory=dict, repr=False)


def parse_youtube_url(url: str) -> str:
    """Extract video ID from YouTube URL."""
    m = YOUTUBE_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a valid YouTube URL: {url}")
    return m.group("id")


def _detect_whisper_backend() -> str:
    """Detect best available whisper backend.

    Priority:
    1. faster-whisper (GPU via CUDA/Metal, 4x faster than OpenAI whisper)
    2. whisper CLI (OpenAI whisper, CPU or GPU depending on install)
    3. None — no transcription available
    """
    try:
        from faster_whisper import WhisperModel  # noqa: F401

        return "faster-whisper"
    except ImportError:
        pass
    try:
        result = subprocess.run(["whisper", "--help"], capture_output=True, timeout=5)
        if result.returncode == 0:
            return "whisper-cli"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "none"


def _detect_gpu_for_whisper() -> tuple[str, str]:
    """Detect if GPU is available for whisper inference.

    Returns: (device, compute_type) for faster-whisper.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            free_mb = int(result.stdout.strip().split("\n")[0])
            if free_mb >= 1000:  # Need ~1GB for whisper small, ~2.5GB for medium
                return "cuda", "float16"
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    # macOS Metal
    import platform

    if platform.system() == "Darwin":
        return "auto", "int8"  # faster-whisper auto-detects Metal on macOS
    return "cpu", "int8"


class YouTubeScraper:
    """YouTube video scraper with auto-captions + whisper fallback.

    Automatically detects the best transcription backend:
    - faster-whisper + CUDA GPU → 4x faster, uses GPU VRAM
    - faster-whisper + CPU → still faster than OpenAI whisper
    - whisper CLI → fallback, works everywhere
    """

    def __init__(
        self,
        *,
        output_dir: str = DEFAULT_TRANSCRIPT_DIR,
        whisper_model: str = "base",
        language: str = "en",
        whisper_backend: str | None = None,
    ):
        self.output_dir = output_dir
        self.whisper_model = whisper_model
        self.language = language
        self.whisper_backend = whisper_backend or _detect_whisper_backend()
        self._device, self._compute_type = _detect_gpu_for_whisper()
        os.makedirs(output_dir, exist_ok=True)
        logger.info("Whisper backend: %s, device: %s", self.whisper_backend, self._device)

    def get_metadata(self, url: str) -> dict:
        """Fetch video metadata without downloading."""
        cmd = ["yt-dlp", "--dump-json", "--no-download", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp metadata failed: {result.stderr[:300]}")
        return json.loads(result.stdout)

    def get_transcript(
        self, url: str, *, force_whisper: bool = False, max_duration_minutes: int = 120
    ) -> YouTubeResult:
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
        title = meta.get("title", "Unknown")
        channel = meta.get("channel", meta.get("uploader", "Unknown"))
        duration = int(meta.get("duration", 0))

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
                result.transcript_method = "auto_captions"
                self._save_transcript(result)
                return result

        # Strategy 2: Download audio + whisper
        logger.info("No auto-captions, using whisper (model=%s)", self.whisper_model)
        audio_path = self._download_audio(url, video_id)
        result.audio_path = audio_path

        transcript = self._whisper_transcribe(audio_path)
        result.transcript = transcript
        result.transcript_method = "whisper"
        self._save_transcript(result)

        # Cleanup audio after transcription
        try:
            os.remove(audio_path)
        except OSError:
            pass

        return result

    def _try_auto_captions(self, url: str, video_id: str) -> str | None:
        """Try to get auto-generated captions from YouTube."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                "yt-dlp",
                "--write-auto-sub",
                "--sub-lang",
                self.language,
                "--skip-download",
                "--sub-format",
                "vtt",
                "-o",
                os.path.join(tmpdir, video_id),
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            # Look for subtitle file
            for f in os.listdir(tmpdir):
                if f.endswith(".vtt"):
                    vtt_path = os.path.join(tmpdir, f)
                    return self._parse_vtt(vtt_path)

        return None

    def _parse_vtt(self, vtt_path: str) -> str:
        """Parse VTT subtitle file to plain text."""
        with open(vtt_path, encoding="utf-8") as f:
            content = f.read()

        lines = []
        prev_line = None
        for line in content.split("\n"):
            # Skip VTT headers, timestamps, empty lines
            line = line.strip()
            if not line or line.startswith("WEBVTT") or "-->" in line:
                continue
            if line.startswith("Kind:") or line.startswith("Language:"):
                continue
            # Remove VTT formatting tags
            clean = re.sub(r"<[^>]+>", "", line)
            clean = clean.strip()
            if clean and clean != prev_line:
                lines.append(clean)
                prev_line = clean

        return " ".join(lines)

    def _download_audio(self, url: str, video_id: str) -> str:
        """Download audio-only via yt-dlp."""
        audio_path = os.path.join(self.output_dir, f"{video_id}.mp3")
        cmd = [
            "yt-dlp",
            "-f",
            "bestaudio",
            "-x",
            "--audio-format",
            "mp3",
            "-o",
            audio_path,
            url,
        ]
        logger.info("Downloading audio: %s", video_id)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp audio download failed: {result.stderr[:300]}")

        # yt-dlp may add extension
        if not os.path.exists(audio_path):
            # Check for auto-renamed file
            for f in os.listdir(self.output_dir):
                if f.startswith(video_id) and f.endswith(".mp3"):
                    audio_path = os.path.join(self.output_dir, f)
                    break

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found after download: {audio_path}")

        return audio_path

    def _whisper_transcribe(self, audio_path: str) -> str:
        """Transcribe audio using the best available backend.

        Backends (auto-detected at init):
        - faster-whisper: Python library, GPU-accelerated (CUDA/Metal), 4x faster
        - whisper-cli: OpenAI whisper CLI, universal fallback
        """
        if self.whisper_backend == "faster-whisper":
            return self._transcribe_faster_whisper(audio_path)
        elif self.whisper_backend == "whisper-cli":
            return self._transcribe_whisper_cli(audio_path)
        else:
            raise RuntimeError(
                "No whisper backend available. Install one:\n"
                "  pip install faster-whisper  (recommended, GPU-accelerated)\n"
                "  pip install openai-whisper  (fallback, CLI-based)"
            )

    def _transcribe_faster_whisper(self, audio_path: str) -> str:
        """Transcribe with faster-whisper (GPU-accelerated if available)."""
        from faster_whisper import WhisperModel

        logger.info(
            "Transcribing with faster-whisper model=%s device=%s compute=%s",
            self.whisper_model,
            self._device,
            self._compute_type,
        )
        model = WhisperModel(
            self.whisper_model,
            device=self._device,
            compute_type=self._compute_type,
        )
        segments, info = model.transcribe(
            audio_path,
            language=self.language if self.language != "auto" else None,
            beam_size=3,
        )
        lines = []
        for seg in segments:
            lines.append(f"[{seg.start:.0f}s] {seg.text.strip()}")

        del model  # free GPU memory
        logger.info("Transcribed %.0fs audio, %d segments", info.duration, len(lines))
        return "\n".join(lines)

    def _transcribe_whisper_cli(self, audio_path: str) -> str:
        """Transcribe with OpenAI whisper CLI (universal fallback)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                "whisper",
                audio_path,
                "--model",
                self.whisper_model,
                "--language",
                self.language,
                "--output_format",
                "txt",
                "-o",
                tmpdir,
            ]
            logger.info("Transcribing with whisper CLI model=%s", self.whisper_model)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                raise RuntimeError(f"Whisper CLI failed: {result.stderr[:300]}")

            for f in os.listdir(tmpdir):
                if f.endswith(".txt"):
                    txt_path = os.path.join(tmpdir, f)
                    with open(txt_path, encoding="utf-8") as fh:
                        return fh.read().strip()

        raise RuntimeError("Whisper CLI produced no output file")

    def _save_transcript(self, result: YouTubeResult):
        """Save transcript to disk."""
        path = os.path.join(self.output_dir, f"{result.video_id}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Title: {result.title}\n")
            f.write(f"Channel: {result.channel}\n")
            f.write(f"Duration: {result.duration_seconds // 60}min\n")
            f.write(f"Method: {result.transcript_method}\n")
            f.write("---\n\n")
            f.write(result.transcript)
        result.transcript_path = path
        logger.info("Transcript saved: %s", path)
