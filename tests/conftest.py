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

import socket

import pytest

# Hosts the tests use as stand-in public sites. smart_fetch's SSRF guard resolves every host and refuses one that does
# not resolve, so in a sandbox without DNS (`unshare -n`, the fleet's test rule) 30 cascade tests failed with
# "refusing to fetch private/loopback host: 'example.com'" while passing with network (2026-10-28). These names get a
# public address when real DNS fails; nothing connects to it (the legs are monkeypatched, and without network a
# connection would fail anyway). Private and loopback names keep resolving for real, so the SSRF tests still bite.
_STAND_IN_PUBLIC_HOSTS = ("example.com", "example.org", "example.net", "intel.arkm.com")
_STAND_IN_ADDRESS = "93.184.215.14"


@pytest.fixture(autouse=True)
def _isolated_method_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SCRAPERX_METHOD_TELEMETRY_PATH", str(tmp_path / "method-telemetry.jsonl")
    )


@pytest.fixture(autouse=True)
def _stand_in_dns_without_network(monkeypatch):
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        try:
            return real(host, *args, **kwargs)
        except socket.gaierror:
            if isinstance(host, str) and host.lower().rstrip(".") in _STAND_IN_PUBLIC_HOSTS:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_STAND_IN_ADDRESS, 0))]
            raise

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
