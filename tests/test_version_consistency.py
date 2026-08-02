"""Version bookkeeping must agree across all three surfaces.

WHY THIS EXISTS (2026-08-02 incident)
-------------------------------------
Version discipline lapsed after 1.7.0 (2026-04-26). Fourteen commits — seven of
them feature drops shipping six new modules — went out under the *same* version
string. Nothing could detect it, so nothing did:

  * ``pyproject.toml`` said       1.7.0
  * ``CHANGELOG.md`` last release said 1.4.3
  * the code shipped six modules beyond both

The consequence was not cosmetic. The pip-installed copy of ``scraperx`` was a
strict SUBSET of this source — missing ``scrapling_stealth`` (Cloudflare
bypass), ``reddit``, ``silent_video_ocr``, ``docs_crawler``,
``fingerprint_audit`` and ``method_telemetry`` — while reporting an identical
version number. Six documented capabilities were silently unavailable for
weeks, the project wiki advertised them the whole time, and sessions concluded
"scraperx can't do X" when the truth was "this install never received X".

A version number that does not move when the code moves is worse than no
version number: it actively asserts a falsehood that tooling trusts.

These tests are cheap, offline, and make the next lapse a red test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import scraperx

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_CHANGELOG = _ROOT / "CHANGELOG.md"

# Matches a Keep-a-Changelog release heading: "## [1.8.0] — 2026-08-02".
# Deliberately NOT anchored to the dash style — em-dash and hyphen both occur.
_RELEASE_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def _pyproject_version() -> str:
    text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no top-level version = \"...\" line"
    return m.group(1)


def _changelog_versions() -> list[str]:
    return _RELEASE_HEADING.findall(_CHANGELOG.read_text(encoding="utf-8"))


def test_pyproject_matches_dunder_version():
    """``pip`` trusts pyproject; importers trust ``__version__``. They must agree."""
    assert _pyproject_version() == scraperx.__version__, (
        f"pyproject.toml={_pyproject_version()} but "
        f"scraperx.__version__={scraperx.__version__}. Bump BOTH in the same commit."
    )


def test_changelog_documents_the_shipped_version():
    """The shipped version must have a CHANGELOG entry.

    This is the assertion that would have caught the 2026-08-02 drift: six
    modules shipped with no release heading to name them.
    """
    versions = _changelog_versions()
    assert versions, "CHANGELOG.md contains no '## [x.y.z]' release headings"
    shipped = _pyproject_version()
    assert shipped in versions, (
        f"version {shipped} ships but has no '## [{shipped}]' section in "
        f"CHANGELOG.md (newest documented: {versions[0]}). "
        "A feature that is not in the changelog is a feature nobody can discover."
    )


def test_newest_changelog_entry_is_the_shipped_version():
    """The newest release heading must BE the shipped version, not trail it.

    Guards the specific 2026-08-02 shape: CHANGELOG frozen at 1.4.3 while
    pyproject had already moved to 1.7.0.
    """
    versions = _changelog_versions()
    assert versions[0] == _pyproject_version(), (
        f"newest CHANGELOG release is [{versions[0]}] but the package ships "
        f"{_pyproject_version()}. The changelog is behind the code."
    )


@pytest.mark.parametrize(
    "module",
    [
        "scrapling_stealth",
        "reddit",
        "silent_video_ocr",
        "docs_crawler",
        "fingerprint_audit",
        "method_telemetry",
    ],
)
def test_shipped_modules_are_importable(module):
    """The six modules that were missing from the 2026-08-02 install.

    An install that cannot import these is the broken-subset state. Note this
    passes trivially when run from the repo root (cwd shadowing) — its value is
    in CI and in a clean checkout, where the installed package is what answers.
    """
    __import__(f"scraperx.{module}")


def test_changelog_versions_are_ordered_newest_first():
    """Keep-a-Changelog order. A misordered file makes 'newest' meaningless."""
    def key(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    versions = _changelog_versions()
    parsed = [key(v) for v in versions]
    assert parsed == sorted(parsed, reverse=True), (
        f"CHANGELOG release headings are not newest-first: {versions}"
    )
