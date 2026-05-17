"""
scraperx/silent_video_ocr.py — frame-OCR transcription for silent videos.

When a video has no audio stream (e.g. Twitter screen-recording demos), neither
captions nor Whisper can produce a transcript. This module fills the gap by
sampling N evenly-spaced frames via PyAV, running tesseract OCR on each, and
assembling a markdown narrative.

Designed as the third leg of scraperx's video-transcription cascade:
    captions → whisper → silent_video_ocr

The motivating incident (2026-05-17): a tweet at
https://x.com/gitlawb/status/2055992174358274431 contained a silent demo of
Hermes Agent + OpenGateway. The model name `mimo-v2.5-pro` was on-screen in the
TUI footer but Whisper failed because there was no audio. Frame OCR recovers
the on-screen text in 6 frames at 1 fps with deterministic tesseract output
(no LLM hallucination risk).

Public API:
    transcribe_silent_video(url_or_path, n_frames=12, lang="eng") -> SilentVideoResult

The function downloads remote URLs to a temp file, opens with PyAV, samples
frames at uniform stride, OCRs each, and returns timestamped text + assembled
full_text. PyAV detects "no audio stream" by checking that container.streams.audio
is empty; if a video DOES have audio, this function still works but the caller
should prefer the whisper path for that case.

Dependencies (declare in pyproject.toml [silent-video] extra):
    av >= 14.0       (already transitive of faster_whisper)
    Pillow >= 10.0   (already transitive of av)
    pytesseract >= 0.3.13

System dep: tesseract binary (`apt install tesseract-ocr` / brew install tesseract).
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Soft imports — silent_video_ocr is opt-in
try:
    import av
    HAS_PYAV = True
except ImportError:
    HAS_PYAV = False

try:
    import pytesseract
    from PIL import Image
    HAS_OCR = True
except ImportError:
    HAS_OCR = False


class SilentVideoNotAvailable(RuntimeError):
    """Raised when PyAV or pytesseract is not installed.

    Install with: `pip install 'scraperx[silent-video]'` (adds pyav + pytesseract).
    The system `tesseract` binary must also be on PATH.
    """


@dataclass
class FrameOCR:
    """One sampled frame with its OCR text."""
    index: int             # frame index in source stream
    timestamp_sec: float   # wall-clock timestamp from video start
    text: str              # tesseract output, raw (NOT post-processed)
    image_path: Optional[str] = None  # if save_frames=True, path to PNG


@dataclass
class SilentVideoResult:
    """Result of OCR-transcribing a silent video."""
    source: str                       # original URL or path
    duration_sec: float               # total video duration
    has_audio: bool                   # was there any audio stream (sanity flag)
    fps: float                        # source frame rate
    total_frames: int                 # total video frames
    n_frames_sampled: int             # how many we OCR'd
    frames: list[FrameOCR] = field(default_factory=list)
    full_text: str = ""               # all frame texts joined with \n--- frame N @ Xs ---\n
    summary: str = ""                 # one-line summary (first 200 chars of full_text)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "duration_sec": self.duration_sec,
            "has_audio": self.has_audio,
            "fps": self.fps,
            "total_frames": self.total_frames,
            "n_frames_sampled": self.n_frames_sampled,
            "full_text": self.full_text,
            "summary": self.summary,
            "frames": [
                {"index": f.index, "timestamp_sec": f.timestamp_sec,
                 "text": f.text, "image_path": f.image_path}
                for f in self.frames
            ],
        }


def _download_to_temp(url: str, suffix: str = ".mp4", timeout: int = 60) -> str:
    """Download URL to a temp file. Caller owns cleanup."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme for download: {parsed.scheme}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) scraperx/silent_video_ocr"
    })
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="scraperx-svo-")
    os.close(fd)
    try:
        # Stream to disk via copyfileobj (no full-file buffering — avoids OOM on >100MB videos).
        with urllib.request.urlopen(req, timeout=timeout) as r:
            with open(path, "wb") as f:
                shutil.copyfileobj(r, f, length=64 * 1024)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def transcribe_silent_video(
    url_or_path: str,
    n_frames: int = 12,
    lang: str = "eng",
    tesseract_psm: int = 6,
    save_frames: bool = False,
    frames_dir: Optional[str] = None,
) -> SilentVideoResult:
    """OCR a silent video by sampling N evenly-spaced frames.

    Args:
        url_or_path: HTTP(S) URL or local file path. Remote URLs are downloaded
            to a temp file which is auto-deleted unless save_frames is True.
        n_frames: how many frames to sample. 12 = roughly 1 every 3.5 sec for a
            40-sec demo video. Higher = more text recovery, slower runtime.
        lang: tesseract language code(s). Default `eng`. Use `eng+chi_sim` for
            mixed-language content (requires `tesseract-ocr-chi-sim` package).
        tesseract_psm: page segmentation mode. 6 = "uniform block of text",
            best for full-screen TUI captures. 3 = auto, slower. 11 = sparse text.
        save_frames: if True, keep extracted PNGs in frames_dir.
        frames_dir: directory to save frames (created if missing). Defaults to
            a temp dir; required if save_frames is True.

    Returns:
        SilentVideoResult with timestamped OCR text per frame + full_text.

    Raises:
        SilentVideoNotAvailable: if PyAV or pytesseract are not installed.
        FileNotFoundError: if the local file doesn't exist.
        av.error.InvalidDataError: if the video file is malformed.
    """
    if not HAS_PYAV or not HAS_OCR:
        missing = []
        if not HAS_PYAV: missing.append("av")
        if not HAS_OCR: missing.append("pytesseract+Pillow")
        raise SilentVideoNotAvailable(
            f"silent_video_ocr requires {', '.join(missing)} — "
            f"install with `pip install 'scraperx[silent-video]'`"
        )

    # Resolve source
    cleanup_path: Optional[str] = None
    if url_or_path.startswith(("http://", "https://")):
        logger.info("downloading silent video: %s", url_or_path)
        local_path = _download_to_temp(url_or_path)
        cleanup_path = local_path if not save_frames else None
    else:
        local_path = url_or_path
        if not os.path.exists(local_path):
            raise FileNotFoundError(local_path)

    # Frame save dir
    if save_frames:
        frames_dir = frames_dir or tempfile.mkdtemp(prefix="scraperx-svo-frames-")
        Path(frames_dir).mkdir(parents=True, exist_ok=True)

    container = None
    try:
        container = av.open(local_path)
        video_stream = container.streams.video[0]
        has_audio = len(container.streams.audio) > 0
        duration_sec = (
            float(video_stream.duration * video_stream.time_base)
            if video_stream.duration else 0.0
        )
        fps = float(video_stream.average_rate) if video_stream.average_rate else 0.0
        total_frames = int(video_stream.frames or 0)

        if total_frames <= 0:
            logger.warning("video has no frames metadata; decoding to count")
            # rare codec branch — count by iterating (cheap for short clips).
            # Re-open so the decoder cursor starts fresh.
            total_frames = sum(1 for _ in container.decode(video=0))
            container.close()
            container = av.open(local_path)
            video_stream = container.streams.video[0]

        n_frames = max(1, min(n_frames, total_frames))
        target_indices = sorted({int(total_frames * i / n_frames) for i in range(n_frames)})

        # Sample frames at the target indices
        frames: list[FrameOCR] = []
        idx_set = set(target_indices)
        seen = 0
        for i, frame in enumerate(container.decode(video=0)):
            if i in idx_set:
                pil_img = frame.to_image()
                # PTS-based timestamp preferred; fall back to fps-derived; else 0.
                if frame.pts is not None:
                    ts = float(frame.pts * video_stream.time_base)
                elif fps > 0:
                    ts = i / fps
                else:
                    ts = 0.0
                image_path: Optional[str] = None
                if save_frames and frames_dir:
                    image_path = os.path.join(frames_dir, f"frame_{i:06d}.png")
                    pil_img.save(image_path)
                text = pytesseract.image_to_string(
                    pil_img, lang=lang, config=f"--psm {tesseract_psm}"
                ).strip()
                frames.append(FrameOCR(
                    index=i, timestamp_sec=ts, text=text, image_path=image_path
                ))
                seen += 1
                if seen >= len(target_indices):
                    break

        # Assemble full text
        parts = []
        for f in frames:
            parts.append(f"--- frame {f.index} @ {f.timestamp_sec:.1f}s ---")
            parts.append(f.text or "(blank)")
        full_text = "\n".join(parts)
        summary = (full_text.replace("\n", " ").strip()[:200] + "…") if len(full_text) > 200 else full_text

        return SilentVideoResult(
            source=url_or_path,
            duration_sec=duration_sec,
            has_audio=has_audio,
            fps=fps,
            total_frames=total_frames,
            n_frames_sampled=len(frames),
            frames=frames,
            full_text=full_text,
            summary=summary,
        )
    finally:
        # Guarantee container close even if PyAV iteration / OCR raises.
        if container is not None:
            try:
                container.close()
            except Exception:
                pass
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass


def has_audio_stream(local_path: str) -> bool:
    """Probe whether a video file has any audio stream. Used by callers as a
    pre-flight to decide between whisper and silent_video_ocr.

    Returns True on error (conservative — let whisper try and fail).
    """
    if not HAS_PYAV:
        return True
    try:
        container = av.open(local_path)
        has = len(container.streams.audio) > 0
        container.close()
        return has
    except Exception as e:
        logger.warning("has_audio_stream probe failed: %s", e)
        return True
