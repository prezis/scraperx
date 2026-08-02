"""The untrusted-XML guard — proven against the payload that actually worked.

Measured on this box (python 3.12.3) BEFORE writing any of this, because a lint
warning is not evidence:

  billion laughs -> ET.fromstring PARSED it, expanding 300 bytes to 30,000 chars.
                    One more nesting level per 10x. REAL.
  XXE            -> ET REFUSED it ("undefined entity &xxe;"). /etc/passwd did not
                    leak. The generic "stdlib XML is XXE-vulnerable" advice does
                    not hold for this parser here.

So exactly one of the two flagged attacks was real, and the fix targets that one.
"""

from __future__ import annotations

import pytest

from scraperx._safe_xml import MAX_XML_BYTES, UnsafeXML, safe_fromstring

# The exact document that expanded to 30,000 chars through bare ET.fromstring.
BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<urlset><url><loc>&lol4;</loc></url></urlset>"""

XXE = """<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<urlset><url><loc>&xxe;</loc></url></urlset>"""

GOOD_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
</urlset>"""


def test_billion_laughs_is_refused_unparsed():
    """THE test. This payload demonstrably worked against bare ET.fromstring."""
    with pytest.raises(UnsafeXML, match="ENTITY"):
        safe_fromstring(BILLION_LAUGHS)


def test_xxe_payload_also_refused():
    """Refused for declaring entities, not because ET would have leaked.

    ET already blocks external entities on this Python; the guard catches this
    one anyway because it declares an entity at all. Belt and braces, and it
    means the defence does not depend on ET's behaviour staying that way.
    """
    with pytest.raises(UnsafeXML, match="ENTITY"):
        safe_fromstring(XXE)


def test_ordinary_sitemap_still_parses():
    root = safe_fromstring(GOOD_SITEMAP)
    locs = [e.text for e in root.iter() if e.tag.endswith("loc")]
    assert locs == ["https://example.com/a"]


def test_bytes_input_works():
    assert safe_fromstring(GOOD_SITEMAP.encode("utf-8")) is not None


def test_oversized_document_is_refused():
    payload = "<urlset>" + "<url><loc>x</loc></url>" * 10 + "</urlset>"
    big = payload + " " * (MAX_XML_BYTES + 1)
    with pytest.raises(UnsafeXML, match="over the"):
        safe_fromstring(big)


def test_entity_word_in_page_content_is_not_a_false_positive():
    """A docs page ABOUT XML must not be refused for saying '<!ENTITY'.

    The scan is limited to the prolog, where an internal DTD subset can legally
    appear — not the body, where the string is just text.
    """
    doc = ("<?xml version='1.0'?><urlset><url><loc>https://example.com/a</loc>"
           "<note>Never write &lt;!ENTITY in your sitemap</note></url></urlset>")
    # Pad the body so the literal lands well past the 4 KB prolog window.
    doc = doc.replace("<note>", "<note>" + "padding " * 700)
    root = safe_fromstring(doc)
    assert root is not None


def test_malformed_xml_still_raises_parse_error_not_unsafe():
    """A broken feed and a hostile feed are different events — keep them apart."""
    import xml.etree.ElementTree as ET

    with pytest.raises(ET.ParseError):
        safe_fromstring("<urlset><url>")


def test_sitemap_parser_survives_a_hostile_document():
    """End-to-end: the crawler must degrade to 'no URLs', never expand the bomb."""
    from scraperx import docs_crawler as dc

    assert dc._parse_sitemap(BILLION_LAUGHS) == []
    assert dc._is_sitemap_index(BILLION_LAUGHS) is False
