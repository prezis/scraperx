"""Tests for YouTube scraper."""
import json
import os
from unittest.mock import patch, MagicMock
import pytest

from scraperx.youtube_scraper import (
    YouTubeScraper,
    YouTubeResult,
    parse_youtube_url,
)


# --- URL parsing ---

class TestParseYoutubeUrl:
    def test_standard(self):
        assert parse_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short(self):
        assert parse_youtube_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_live(self):
        assert parse_youtube_url("https://youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_no_www(self):
        assert parse_youtube_url("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_invalid(self):
        with pytest.raises(ValueError, match="Not a valid YouTube URL"):
            parse_youtube_url("https://vimeo.com/12345")


# --- Metadata ---

SAMPLE_METADATA = {
    "title": "Test Video",
    "channel": "Test Channel",
    "uploader": "Test Uploader",
    "duration": 300,
}

class TestGetMetadata:
    @patch("scraperx.youtube_scraper.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(SAMPLE_METADATA),
        )
        scraper = YouTubeScraper(output_dir="/tmp/test_transcripts")
        meta = scraper.get_metadata("https://youtube.com/watch?v=dQw4w9WgXcQ")
        assert meta["title"] == "Test Video"
        assert meta["duration"] == 300

    @patch("scraperx.youtube_scraper.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="ERROR: Video unavailable",
        )
        scraper = YouTubeScraper(output_dir="/tmp/test_transcripts")
        with pytest.raises(RuntimeError, match="yt-dlp metadata failed"):
            scraper.get_metadata("https://youtube.com/watch?v=invalid")


# --- VTT parsing ---

SAMPLE_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
Hello world

00:00:02.000 --> 00:00:04.000
Hello world

00:00:04.000 --> 00:00:06.000
This is a <b>test</b> transcript

00:00:06.000 --> 00:00:08.000
With duplicate lines
"""

class TestParseVtt:
    def test_basic(self, tmp_path):
        vtt_file = tmp_path / "test.vtt"
        vtt_file.write_text(SAMPLE_VTT)
        scraper = YouTubeScraper(output_dir=str(tmp_path))
        result = scraper._parse_vtt(str(vtt_file))
        assert "Hello world" in result
        assert "test transcript" in result
        assert "WEBVTT" not in result
        assert "-->" not in result

    def test_deduplication(self, tmp_path):
        vtt_file = tmp_path / "dup.vtt"
        vtt_file.write_text(SAMPLE_VTT)
        scraper = YouTubeScraper(output_dir=str(tmp_path))
        result = scraper._parse_vtt(str(vtt_file))
        # "Hello world" appears twice consecutively in VTT but should be deduped
        assert result.count("Hello world") == 1


# --- Duration guard ---

class TestDurationGuard:
    @patch("scraperx.youtube_scraper.subprocess.run")
    def test_too_long(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "title": "Long Video",
                "channel": "Test",
                "duration": 7201,  # > 120 min default
            }),
        )
        scraper = YouTubeScraper(output_dir="/tmp/test_transcripts")
        with pytest.raises(ValueError, match="Video too long"):
            scraper.get_transcript("https://youtube.com/watch?v=dQw4w9WgXcQ")


# --- Auto-captions ---

class TestAutoCaption:
    @patch("scraperx.youtube_scraper.subprocess.run")
    def test_success(self, mock_run):
        """Test auto-caption path with mocked yt-dlp."""
        # First call: get_metadata
        meta_result = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "Test", "channel": "Ch", "duration": 60}),
        )
        # Second call: write-auto-sub (creates a .vtt file in tmpdir)
        def side_effect_sub(cmd, **kwargs):
            # Find the output dir from -o flag
            for i, arg in enumerate(cmd):
                if arg == "-o":
                    out_path = cmd[i + 1]
                    out_dir = os.path.dirname(out_path)
                    video_id = os.path.basename(out_path)
                    vtt_path = os.path.join(out_dir, f"{video_id}.en.vtt")
                    os.makedirs(out_dir, exist_ok=True)
                    with open(vtt_path, "w") as f:
                        f.write("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello auto caption\n")
                    break
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = [meta_result, side_effect_sub]
        # Second call needs to create the VTT file - use side_effect
        def run_side_effect(cmd, **kwargs):
            if "--dump-json" in cmd:
                return meta_result
            return side_effect_sub(cmd, **kwargs)

        mock_run.side_effect = run_side_effect

        scraper = YouTubeScraper(output_dir="/tmp/test_yt_transcripts")
        result = scraper.get_transcript("https://youtube.com/watch?v=dQw4w9WgXcQ")
        assert result.transcript_method == "auto_captions"
        assert "Hello auto caption" in result.transcript


# --- YouTubeResult dataclass ---

class TestYouTubeResult:
    def test_defaults(self):
        r = YouTubeResult(video_id="abc", title="T", channel="C")
        assert r.duration_seconds == 0
        assert r.transcript == ""
        assert r.transcript_method == ""
        assert r.audio_path is None
