"""`scraperx doctor` — system diagnostic for users.

Checks:
- Python version
- Core stdlib availability (always pass)
- Optional dependencies: imagehash, Pillow, faster-whisper, beautifulsoup4, twscrape
- Optional system tools: yt-dlp, whisper CLI, ffmpeg
- GPU: NVIDIA CUDA via nvidia-smi, Apple Metal via platform detection
- Ollama running on localhost:11434 (for users who want local LLM routing)

Prints human-readable report with actionable install hints.
Exit code 0 always (diagnostic only, not a failure signal).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

CHECK = "✓"
CROSS = "✗"
WARN = "!"


def _check_import(name: str) -> tuple[bool, str | None]:
    try:
        import_module(name)
        try:
            return True, version(name.replace("_", "-"))
        except PackageNotFoundError:
            return True, None
    except ImportError:
        return False, None


def _check_binary(name: str) -> str | None:
    """Return version string if binary is on PATH, else None."""
    path = shutil.which(name)
    if not path:
        return None
    try:
        r = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else path
    except (subprocess.TimeoutExpired, OSError):
        return path


def _detect_gpu() -> dict:
    """Return info about available GPU acceleration."""
    info: dict = {"nvidia": None, "apple_metal": False, "usable": False}

    # NVIDIA CUDA via nvidia-smi
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                lines = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
                info["nvidia"] = lines
                info["usable"] = True
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Apple Metal (Metal Performance Shaders available on macOS arm64 / intel with AMD)
    if platform.system() == "Darwin":
        info["apple_metal"] = True
        info["usable"] = True

    return info


def _detect_ollama() -> dict:
    """Check if Ollama is running on localhost:11434. Not required — optional local LLM."""
    info: dict = {"running": False, "models": [], "endpoint": "http://localhost:11434"}
    try:
        import json
        from urllib.error import URLError
        from urllib.request import urlopen

        with urlopen("http://localhost:11434/api/tags", timeout=2) as resp:
            data = json.loads(resp.read())
            info["running"] = True
            info["models"] = [m.get("name", "") for m in data.get("models", [])]
    except (URLError, OSError, Exception):
        pass
    return info


def run_doctor(as_json: bool = False) -> int:
    """Print diagnostic report. Returns exit code 0."""
    # Collect
    py_version = sys.version.split()[0]
    gpu = _detect_gpu()
    ollama = _detect_ollama()

    optionals = {
        "imagehash": _check_import("imagehash"),
        "PIL": _check_import("PIL"),
        "faster_whisper": _check_import("faster_whisper"),
        "bs4": _check_import("bs4"),
        "twscrape": _check_import("twscrape"),
    }

    binaries = {
        "yt-dlp": _check_binary("yt-dlp"),
        "whisper": _check_binary("whisper"),
        "ffmpeg": _check_binary("ffmpeg"),
        "nvidia-smi": _check_binary("nvidia-smi"),
    }

    if as_json:
        import json

        report = {
            "python": py_version,
            "platform": f"{platform.system()} {platform.machine()}",
            "gpu": gpu,
            "ollama": ollama,
            "optional_libraries": {k: {"installed": v[0], "version": v[1]} for k, v in optionals.items()},
            "system_tools": {k: {"present": bool(v), "info": v} for k, v in binaries.items()},
        }
        print(json.dumps(report, indent=2))
        return 0

    # Human-readable report
    print("scraperx doctor — system diagnostic\n")
    print(f"Python:   {py_version}")
    print(f"Platform: {platform.system()} {platform.machine()}")
    print()

    # GPU section
    print("GPU acceleration:")
    if gpu["nvidia"]:
        for ln in gpu["nvidia"]:
            print(f"  {CHECK} NVIDIA CUDA: {ln}")
    elif gpu["apple_metal"]:
        print(f"  {CHECK} Apple Metal available (macOS)")
    else:
        print(f"  {CROSS} No GPU detected — transcription will use CPU (slow but works)")
        print(f"    {WARN} For fast whisper: install CUDA or run on Apple Silicon")
    print()

    # Ollama (optional)
    print("Ollama (optional, for local LLM routing via local-ai-mcp):")
    if ollama["running"]:
        print(f"  {CHECK} Running at {ollama['endpoint']} — {len(ollama['models'])} model(s) loaded")
        for m in ollama["models"][:5]:
            print(f"    • {m}")
    else:
        print(f"  {CROSS} Not running at {ollama['endpoint']}")
        print(f"    {WARN} Install: https://ollama.com  →  then `ollama pull qwen3:4b` (fast classifier)")
        print(f"    {WARN} Optional — scraperx works without Ollama")
    print()

    # Optional libraries
    print("Optional libraries:")
    hints = {
        "imagehash": "pip install scraperx[vision]      # perceptual avatar hashing",
        "PIL": "(installed with [vision])",
        "faster_whisper": "pip install scraperx[whisper]     # GPU transcription (4x faster)",
        "bs4": "pip install scraperx[video-discovery]   # robust embed scanning",
        "twscrape": "pip install scraperx[twscrape]    # account-backed X scraping (opt-in)",
    }
    for pkg, (ok, ver) in optionals.items():
        if ok:
            v = f" ({ver})" if ver else ""
            print(f"  {CHECK} {pkg}{v}")
        else:
            hint = hints.get(pkg, "")
            print(f"  {CROSS} {pkg:16} — {hint}")
    print()

    # System tools
    print("System tools:")
    bin_hints = {
        "yt-dlp": "apt install yt-dlp / brew install yt-dlp  — needed for Vimeo/YouTube whisper path",
        "whisper": "pip install openai-whisper  — fallback CLI if faster-whisper unavailable",
        "ffmpeg": "apt install ffmpeg / brew install ffmpeg  — audio extraction for whisper",
        "nvidia-smi": "install NVIDIA drivers  — required to detect CUDA GPU",
    }
    for name, info in binaries.items():
        if info:
            print(f"  {CHECK} {name}: {info[:80]}")
        else:
            hint = bin_hints.get(name, "")
            print(f"  {CROSS} {name:12} — {hint}")
    print()

    # Summary + recommendations
    print("Summary:")
    has_whisper_gpu = optionals["faster_whisper"][0] and gpu["usable"]
    has_any_whisper = optionals["faster_whisper"][0] or binaries["whisper"]
    has_vision = optionals["imagehash"][0] and optionals["PIL"][0]

    if has_whisper_gpu:
        print(f"  {CHECK} Fast transcription ready (faster-whisper + GPU)")
    elif has_any_whisper:
        print(f"  {WARN} Transcription available but will use CPU — consider GPU for long videos")
    else:
        print(f"  {CROSS} No transcription backend — install: pip install scraperx[whisper]")

    if has_vision:
        print(f"  {CHECK} Avatar impersonation detection ready (pHash)")
    else:
        print(f"  {WARN} Avatar matching falls back to SHA256 — install: pip install scraperx[vision]")

    if ollama["running"]:
        print(
            f"  {CHECK} Local LLM routing available — scraperx can be paired with local-ai-mcp for zero-token workflows"
        )
    else:
        print(
            f"  {WARN} No local LLM — scraperx works standalone, but pairing with Ollama + local-ai-mcp saves API costs"
        )

    print()
    print("Full install (all optionals):")
    print('  pip install "scraperx[vision,video-discovery,whisper] @ git+https://github.com/prezis/scraperx.git"')
    print()
    print("For local-ai-mcp pairing (optional):")
    print("  https://github.com/prezis/local-ai-mcp")

    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="scraperx doctor", description="System diagnostic for scraperx")
    parser.add_argument("_cmd", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable")
    args = parser.parse_args()
    return run_doctor(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
