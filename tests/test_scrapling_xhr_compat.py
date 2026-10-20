"""XHR capture across the scrapling 0.4 -> 0.5 break, both worlds, with fakes (the real browser run is
tests/live/scrapling_live_probe.py).

0.4.x: ``capture_xhr=<pattern>`` kwarg, results on ``page.captured_xhr``.
0.5  : ``capture_xhr`` raises TypeError; ask for ``record_requests=True`` and read ``session.network``.
``scraperx.scrapling_stealth._has_network_history`` is the one switch; these tests flip it.
"""

from __future__ import annotations

import pytest

import scraperx.scrapling_stealth as ss


class _Rec:
    def __init__(self, url, rtype, body=b"{}"):
        self.url, self.meta, self.body, self.status = url, {"resource_type": rtype}, body, 200


class _Net:
    """Mimics scrapling 0.5's NetworkRecorder: ids never repeat; search honours url_pattern and after_id."""

    def __init__(self):
        self.last_id = 0
        self.records: dict[int, _Rec] = {}
        self.search_calls: list[dict] = []

    def add(self, rec):
        self.last_id += 1
        self.records[self.last_id] = rec

    def search(self, url_pattern=None, after_id=0, limit=100, **_):
        import re

        self.search_calls.append({"url_pattern": url_pattern, "after_id": after_id, "limit": limit})
        return [r for i, r in self.records.items() if i > after_id and (url_pattern is None or re.search(url_pattern, r.url))]


class _Page:
    status = 200
    body = b"<html>ok</html>"


class _FakeSession05:
    """StealthySession as 0.5 builds it. Rejects capture_xhr like upstream does."""

    instances: list["_FakeSession05"] = []

    def __init__(self, **kw):
        if "capture_xhr" in kw:
            raise TypeError("capture_xhr was removed; use record_requests=True and session.network.")
        self.kw = kw
        self.network = _Net()
        self.closed = False
        self.started = False
        self.saved = None
        _FakeSession05.instances.append(self)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def fetch(self, url):
        # every fetch makes an xhr + an image request
        self.network.add(_Rec(url.rstrip("/") + "/api/a.json", "xhr"))
        self.network.add(_Rec(url.rstrip("/") + "/pixel.png", "image"))
        self.network.add(_Rec(url.rstrip("/") + "/api/b.json", "fetch"))
        return _Page()

    def save_state(self, path):
        self.saved = path

    def load_state(self, path):
        self.loaded = path


class _FakeFetcher:
    @classmethod
    def _generate_parser_arguments(cls):
        return {"huge_tree": True}

    last_kwargs: dict = {}

    @classmethod
    def fetch(cls, url, **kw):
        cls.last_kwargs = kw

        class P(_Page):
            captured_xhr = [_Rec(url + "/api/legacy.json", "xhr")]

        return P()


@pytest.fixture
def world05(monkeypatch):
    _FakeSession05.instances.clear()
    monkeypatch.setattr(ss, "_has_network_history", lambda: True)
    monkeypatch.setattr(ss, "_get_stealthy_session", lambda: _FakeSession05)
    monkeypatch.setattr(ss, "_get_stealthy_fetcher", lambda: _FakeFetcher)


@pytest.fixture
def world04(monkeypatch):
    monkeypatch.setattr(ss, "_has_network_history", lambda: False)
    monkeypatch.setattr(ss, "_get_stealthy_fetcher", lambda: _FakeFetcher)


def test_05_one_shot_xhr_uses_record_requests_and_never_capture_xhr(world05):
    content, status, xhrs = ss.fetch_stealth_xhr("https://x.test", r"/api/")
    s = _FakeSession05.instances[0]
    assert "capture_xhr" not in s.kw and s.kw["record_requests"] is True
    assert s.kw["selector_config"]["huge_tree"] is True, "a raw session needs the huge_tree injection"
    assert (content, status) == ("<html>ok</html>", 200)
    assert [x.url for x in xhrs] == ["https://x.test/api/a.json", "https://x.test/api/b.json"]
    assert s.started and s.closed


def test_05_images_are_not_returned_as_xhr(world05):
    _, _, xhrs = ss.fetch_stealth_xhr("https://x.test", r".")  # pattern matches the png too
    assert all(x.meta["resource_type"] in ("xhr", "fetch") for x in xhrs)
    assert not any(x.url.endswith(".png") for x in xhrs)


def test_05_session_closes_even_when_the_fetch_raises(world05, monkeypatch):
    def boom(self, url):
        raise RuntimeError("navigation timeout")

    monkeypatch.setattr(_FakeSession05, "fetch", boom)
    with pytest.raises(RuntimeError, match="navigation timeout"):
        ss.fetch_stealth_xhr("https://x.test", "/api/")
    assert _FakeSession05.instances[0].closed


def test_05_session_fetch_xhr_returns_only_the_last_fetchs_traffic(world05):
    with ss.stealth_session(capture_xhr=r"/api/") as s:
        _, _, first = s.fetch_xhr("https://x.test")
        _, _, second = s.fetch_xhr("https://y.test")
    assert [x.url for x in first] == ["https://x.test/api/a.json", "https://x.test/api/b.json"]
    assert [x.url for x in second] == ["https://y.test/api/a.json", "https://y.test/api/b.json"], "must not accumulate"
    assert _FakeSession05.instances[0].network.search_calls[1]["after_id"] == 3, "second call asks only after id 3"


def test_05_session_without_capture_pattern_still_refuses_fetch_xhr(world05):
    with ss.stealth_session() as s:
        with pytest.raises(ValueError, match="capture_xhr"):
            s.fetch_xhr("https://x.test")


def test_04_legacy_path_still_passes_capture_xhr_and_reads_captured_xhr(world04):
    content, status, xhrs = ss.fetch_stealth_xhr("https://x.test", r"/api/")
    assert _FakeFetcher.last_kwargs["capture_xhr"] == r"/api/"
    assert "record_requests" not in _FakeFetcher.last_kwargs
    assert [x.url for x in xhrs] == ["https://x.test/api/legacy.json"]


def test_state_methods_delegate_and_fail_clearly_on_old_scrapling(world05):
    with ss.stealth_session() as s:
        s.save_state("/tmp/x.json")
        s.load_state("/tmp/x.json")
    fake = _FakeSession05.instances[0]
    assert fake.saved == "/tmp/x.json" and fake.loaded == "/tmp/x.json"

    class Old:
        pass

    h = ss.StealthSessionHandle(Old())
    with pytest.raises(RuntimeError, match="scrapling>=0.5"):
        h.save_state("/tmp/x.json")
    with pytest.raises(RuntimeError, match="scrapling>=0.5"):
        h.load_state("/tmp/x.json")


def test_has_network_history_feature_detects_the_installed_scrapling():
    pytest.importorskip("scrapling")
    from importlib.metadata import version

    major_minor = tuple(int(p) for p in version("scrapling").split(".")[:2])
    assert ss._has_network_history() == (major_minor >= (0, 5))
