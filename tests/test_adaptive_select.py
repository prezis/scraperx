"""adaptive_select: learn an element once, relocate it after a redesign. Parser-only scrapling is enough."""

from __future__ import annotations

import pytest

pytest.importorskip("scrapling.parser")

from scraperx.adaptive_select import parse  # noqa: E402

V1 = '<html><body><div class="box"><span class="price" id="p">$10</span><span class="old">$12</span></div></body></html>'
# a realistic redesign: same tag and text, new class, no id, new wrapper
V2 = '<html><body><div class="wrapper"><span class="cost" data-v="2">$10</span><span class="was">$12</span></div></body></html>'
URL = "https://shop.example/list"


def test_selector_survives_a_redesign_only_when_adaptive(tmp_path):
    db = str(tmp_path / "a.db")
    learned = parse(V1, url=URL, db_path=db).css(".price", auto_save=True)
    assert len(learned) == 1 and learned[0].text == "$10"

    after = parse(V2, url=URL, db_path=db)
    assert len(after.css(".price")) == 0, "positive control: the plain selector must find nothing after the redesign"
    healed = after.css(".price", adaptive=True)
    assert [h.text for h in healed][:1] == ["$10"]
    assert "old" not in " ".join(h.attrib.get("class", "") for h in healed), "must not relocate to the other span"


def test_a_page_never_saved_cannot_be_relocated(tmp_path):
    page = parse(V2, url=URL, db_path=str(tmp_path / "empty.db"))
    assert len(page.css(".price", adaptive=True)) == 0


def test_default_memory_is_one_file_per_host_even_for_non_public_hosts(tmp_path, monkeypatch):
    """Scrapling collapses every host without a public TLD into one key (.example, localhost, IPs): we must not."""
    import scraperx.adaptive_select as a

    monkeypatch.setattr(a, "DEFAULT_DIR", str(tmp_path))
    parse(V1, url="https://a.example/p").css(".price", auto_save=True)
    other = parse(V2, url="https://b.example/p")
    assert len(other.css(".price", adaptive=True)) == 0, "site B must not inherit site A's learned element"
    assert {f.name for f in tmp_path.iterdir() if f.name.endswith(".db")} == {"a.example.db", "b.example.db"}, "one file per host"
    same = parse(V2, url="https://a.example/p")
    assert len(same.css(".price", adaptive=True)) == 1, "positive control: the SAME host does relocate"


def test_explicit_shared_db_isolates_real_domains(tmp_path):
    db = str(tmp_path / "shared.db")
    parse(V1, url="https://shop-a.com/p", db_path=db).css(".price", auto_save=True)
    assert len(parse(V2, url="https://shop-b.com/p", db_path=db).css(".price", adaptive=True)) == 0
    assert len(parse(V2, url="https://shop-a.com/p", db_path=db).css(".price", adaptive=True)) == 1


def test_non_adaptive_parse_is_a_plain_selector():
    page = parse(V1, adaptive=False)
    assert page.css(".price")[0].text == "$10"
