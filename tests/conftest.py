"""Shared test fixtures.

Method-telemetry isolation: reddit's adaptive tier ordering reads/writes
``~/.scraperx/method-telemetry.jsonl`` by default. Tests must NEVER touch
(or be influenced by) the real per-machine ledger — a dev box where TIER 1
is genuinely blocked would otherwise reorder tiers under the tier-ladder
tests and make them nondeterministic. Point the ledger at a per-test tmpdir
via the env var seam that ``scraperx.method_telemetry`` resolves at call
time.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_method_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SCRAPERX_METHOD_TELEMETRY_PATH", str(tmp_path / "method-telemetry.jsonl")
    )
