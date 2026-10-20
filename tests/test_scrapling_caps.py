"""The registry of Scrapling capabilities must stay TRUE: entry points resolve, upstream symbols exist, no upstream
symbol appeared or vanished unreviewed, and docs/SCRAPLING.md is exactly what the generator prints.

Upstream drift tests need an installed scrapling (>=0.5 for the snapshot); they skip, loudly, otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scraperx import scrapling_caps as caps

_DOC = Path(__file__).resolve().parent.parent / "docs" / "SCRAPLING.md"


def _need_scrapling(min_version: tuple[int, ...] = ()):
    pytest.importorskip("scrapling")
    if caps._version_tuple(caps.installed_version()) < min_version:
        pytest.skip(f"needs scrapling >= {'.'.join(map(str, min_version))}, have {caps.installed_version()}")


def test_keys_unique_and_statuses_valid():
    keys = [c.key for c in caps.CAPABILITIES]
    assert len(keys) == len(set(keys)), "duplicate capability key"
    valid = {caps.WIRED, caps.PASSTHROUGH, caps.PARTIAL, caps.GAP, caps.REMOVED, caps.NOT_ADOPTED}
    assert {c.status for c in caps.CAPABILITIES} <= valid
    assert {c.area for c in caps.CAPABILITIES} <= {"fetch", "parse", "crawl", "ai", "devex"}


def test_wired_and_partial_capabilities_name_an_entry_point():
    """A status that promises a scraperx API must point at it; gaps and refusals must not pretend to."""
    for c in caps.CAPABILITIES:
        if c.status in (caps.WIRED, caps.PASSTHROUGH, caps.PARTIAL):
            assert c.scraperx, f"{c.key} is {c.status} but names no scraperx entry point"
        if c.status in (caps.GAP, caps.NOT_ADOPTED, caps.REMOVED):
            assert c.scraperx is None, f"{c.key} is {c.status} but names an entry point"


def test_every_scraperx_entry_point_resolves():
    """No scrapling needed: scraperx modules import it lazily. A renamed function fails HERE, not in a user's run."""
    for c in caps.CAPABILITIES:
        if c.scraperx:
            assert caps._resolve(c.scraperx) is not None, c.key


def test_upstream_symbols_we_map_exist_in_the_installed_scrapling():
    _need_scrapling()
    res = caps.check_upstream()
    assert res["scraperx_unresolved"] == []
    assert res["upstream_missing"] == [], f"upstream removed/renamed something we rely on: {res['upstream_missing']}"


def test_no_unreviewed_upstream_symbols():
    """Upstream added a public class/function: read what it is, update CAPABILITIES, re-snapshot (see the doc)."""
    _need_scrapling((0, 5))
    new = caps.check_upstream()["new_public"]
    assert new == [], (
        f"scrapling exposes new public symbols nobody has reviewed: {new}. Add or adjust a Capability, then "
        "`python -m scraperx.scrapling_caps --snapshot` into KNOWN_PUBLIC."
    )


def test_removed_capabilities_are_really_gone_on_05_and_the_positive_control_holds():
    _need_scrapling((0, 5))
    import scrapling.fetchers as f

    assert not hasattr(f, "DynamicFetcher"), "registry says DynamicFetcher was removed in 0.5"
    assert hasattr(f, "StealthyFetcher"), "positive control: the class we do use must be there"


def test_capture_xhr_really_raises_on_05():
    """Why scraperx feature-detects: on 0.5 the old kwarg is a TypeError, not a silent no-op."""
    _need_scrapling((0, 5))
    from scrapling.fetchers import StealthySession

    with pytest.raises(TypeError, match="capture_xhr"):
        StealthySession(capture_xhr="/api/")
    StealthySession(record_requests=True)  # positive control: the replacement constructs


def test_markdown_helpers_we_lean_on_still_exist():
    """page_markdown.html_to_markdown uses Convertor internals; a rename must fail here, not ship unsanitised text."""
    _need_scrapling((0, 5))
    from scrapling.core.shell import Convertor

    for name in ("_sanitize_for_ai", "_strip_noise_tags", "_convert_to_markdown"):
        assert hasattr(Convertor, name), name


def test_docs_file_is_exactly_the_generated_table():
    assert _DOC.exists(), "run: python -m scraperx.scrapling_caps --markdown > docs/SCRAPLING.md"
    assert _DOC.read_text(encoding="utf-8") == caps.render_markdown(), (
        "docs/SCRAPLING.md is stale: python -m scraperx.scrapling_caps --markdown > docs/SCRAPLING.md"
    )


def test_cli_reports_clean_on_this_install():
    if caps.installed_version() is None:
        pytest.skip("scrapling not installed")
    assert caps.main([]) in (0, 1)  # prints; exit 1 only when drift exists, asserted precisely above
