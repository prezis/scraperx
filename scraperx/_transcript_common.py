"""Shared transcription helpers — extracted from youtube_scraper so vimeo_scraper
(and future wistia/jwplayer scrapers) can reuse them WITHOUT instance-method coupling.

Previously `YouTubeScraper._parse_vtt(self, vtt_path: str)` was only usable as a
method. `VimeoScraper` imported it and called it with a VTT string (not path),
which crashes at runtime. This module exposes pure functions that both callers
use correctly.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import tempfile
from typing import Literal

logger = logging.getLogger(__name__)

# --- VTT parsing (pure) ---


def parse_vtt_content(vtt_content: str) -> str:
    """Convert VTT subtitle content (string) to plain text.

    Accepts the VTT file content directly. Callers: YouTubeScraper reads from
    disk and passes content; VimeoScraper downloads via HTTP and passes content.
    """
    lines: list[str] = []
    prev_line: str | None = None
    for raw_line in vtt_content.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        if line.startswith("Kind:") or line.startswith("Language:"):
            continue
        # Remove VTT formatting tags like <c>, <v Speaker>, <00:00:01.000>
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean and clean != prev_line:
            lines.append(clean)
            prev_line = clean
    return " ".join(lines)


def parse_vtt_file(vtt_path: str) -> str:
    """Thin wrapper: read VTT from path + parse."""
    with open(vtt_path, encoding="utf-8") as f:
        return parse_vtt_content(f.read())


# --- Backend detection (pure) ---

WhisperBackend = Literal["faster-whisper", "whisper-cli", "none"]


def detect_whisper_backend() -> WhisperBackend:
    """Best available whisper backend. Priority: faster-whisper > whisper CLI > none."""
    try:
        from faster_whisper import WhisperModel  # noqa: F401

        return "faster-whisper"
    except ImportError:
        pass
    try:
        result = subprocess.run(["whisper", "--help"], capture_output=True, timeout=5)
        if result.returncode == 0:
            return "whisper-cli"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "none"


def detect_gpu_for_whisper() -> tuple[str, str]:
    """Return (device, compute_type) for faster-whisper based on host GPU availability."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            free_mb = int(result.stdout.strip().split("\n")[0])
            if free_mb >= 1000:
                return "cuda", "float16"
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    if platform.system() == "Darwin":
        return "auto", "int8"
    return "cpu", "int8"


# --- Transcription (pure, no class coupling) ---


def transcribe_faster_whisper(
    audio_path: str,
    *,
    model: str = "base",
    device: str | None = None,
    compute_type: str | None = None,
    language: str = "en",
    beam_size: int = 3,
) -> str:
    """Transcribe audio via faster-whisper. Caller provides model + compute config."""
    from faster_whisper import WhisperModel

    if device is None or compute_type is None:
        dev, ct = detect_gpu_for_whisper()
        device = device or dev
        compute_type = compute_type or ct

    logger.info("faster-whisper: model=%s device=%s compute=%s", model, device, compute_type)
    wm = WhisperModel(model, device=device, compute_type=compute_type)
    segments, info = wm.transcribe(
        audio_path,
        language=language if language != "auto" else None,
        beam_size=beam_size,
    )
    lines = [f"[{seg.start:.0f}s] {seg.text.strip()}" for seg in segments]
    del wm  # free GPU memory
    logger.info("faster-whisper: transcribed %.0fs audio, %d segments", info.duration, len(lines))
    return "\n".join(lines)


def transcribe_whisper_cli(
    audio_path: str,
    *,
    model: str = "base",
    language: str = "en",
) -> str:
    """Transcribe audio via whisper CLI. Universal fallback."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            "whisper",
            audio_path,
            "--model",
            model,
            "--language",
            language,
            "--output_format",
            "txt",
            "--output_dir",
            tmpdir,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(f"whisper CLI failed: {result.stderr[:300]}")
        # whisper writes <basename>.txt to output_dir
        basename = os.path.splitext(os.path.basename(audio_path))[0]
        txt_path = os.path.join(tmpdir, f"{basename}.txt")
        if not os.path.exists(txt_path):
            # Find the produced file (whisper may sanitize filename)
            txt_files = [f for f in os.listdir(tmpdir) if f.endswith(".txt")]
            if not txt_files:
                raise RuntimeError("whisper produced no output file")
            txt_path = os.path.join(tmpdir, txt_files[0])
        with open(txt_path, encoding="utf-8") as f:
            return f.read().strip()


def transcribe_audio(
    audio_path: str,
    *,
    model: str = "base",
    language: str = "en",
) -> tuple[str, WhisperBackend]:
    """Auto-pick backend + transcribe. Returns (transcript, backend_used)."""
    backend = detect_whisper_backend()
    if backend == "faster-whisper":
        return transcribe_faster_whisper(audio_path, model=model, language=language), backend
    if backend == "whisper-cli":
        return transcribe_whisper_cli(audio_path, model=model, language=language), backend
    raise RuntimeError(
        "No whisper backend available. Install one:\n"
        "  pip install faster-whisper  (recommended, GPU-accelerated)\n"
        "  pip install openai-whisper  (fallback, CLI-based)"
    )
