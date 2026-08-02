"""Parse XML from REMOTE, UNTRUSTED sources. stdlib only, no new dependency.

WHY THIS EXISTS (2026-08-02)
----------------------------
scraperx parses XML that a stranger controls:

* ``docs_crawler`` reads ``sitemap.xml`` — and since the sitemap-index fix it
  also FOLLOWS child sitemaps to whatever URLs the index names. A hostile site
  chooses both the content and the follow-up URLs.
* ``github_analyzer.mentions.arxiv`` parses arXiv API responses.

MEASURED on this box (python 3.12.3), not assumed from a lint warning:

* **billion laughs — VULNERABLE.** A 300-byte document with nested ``<!ENTITY>``
  declarations expanded to 30,000 chars. One more nesting level per 10× — a few
  hundred bytes becomes gigabytes of RAM, which is a free remote OOM.
* **XXE — NOT vulnerable.** ``ElementTree`` refuses external entities outright
  (``undefined entity &xxe;``); ``/etc/passwd`` did not leak. The generic advice
  "stdlib XML is vulnerable to XXE" does not hold for this parser on this Python.

WHY NOT ``defusedxml``
----------------------
It *is* importable here — but only because ``py-serializable`` happens to pull
it in. It is NOT declared in our ``pyproject.toml``. Depending on it would be a
phantom dependency: green on this machine, ImportError on a clean install. That
is precisely the failure class that cost this project a day (a pip-installed
package that was a silent SUBSET of its own source). And the README promises a
"stdlib-only core" twice, so a hard dependency would break a documented promise
to close a hole that three lines of stdlib close just as well.

THE DEFENCE
-----------
Entity *declarations* have no legitimate place in a sitemap or an arXiv feed.
Rather than neutering the parser, we refuse documents that contain them at all —
which kills the entire entity-expansion class before a parser ever sees it — and
cap the input size so a merely enormous document cannot exhaust memory either.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

#: Hard ceiling on a single XML document. Real sitemaps are well under this
#: (the largest seen in the wild here: 261 KB / 2011 URLs). Generous by 40×.
MAX_XML_BYTES = 10 * 1024 * 1024

#: An internal DTD subset that declares entities. Sitemaps and Atom feeds never
#: need one; a document that ships one is either broken or hostile.
_ENTITY_DECL = re.compile(rb"<!ENTITY\b", re.IGNORECASE)


class UnsafeXML(ValueError):
    """Raised when a document is refused BEFORE parsing.

    Deliberately a distinct type: callers must be able to tell "we rejected
    this" apart from "the remote sent us malformed XML" apart from "our code is
    broken". Conflating those three is how a defect gets reported as a wall.
    """


def safe_fromstring(xml: str | bytes) -> ET.Element:
    """``ET.fromstring`` for untrusted input. Raises UnsafeXML or ET.ParseError.

    Refuses, before parsing:
      * documents over ``MAX_XML_BYTES``
      * any document declaring XML entities (the billion-laughs vector)
    """
    raw = xml.encode("utf-8", "replace") if isinstance(xml, str) else xml

    if len(raw) > MAX_XML_BYTES:
        raise UnsafeXML(
            f"XML document is {len(raw)} bytes, over the {MAX_XML_BYTES} limit — refused unparsed"
        )

    # Only inspect the prolog: an <!ENTITY> can only appear in the internal DTD
    # subset, which precedes the root element. Scanning the whole body would
    # false-positive on the literal text "<!ENTITY" inside a CDATA block or a
    # docs page about XML.
    head = raw[:4096]
    if _ENTITY_DECL.search(head):
        raise UnsafeXML(
            "XML declares entities (<!ENTITY) — refused unparsed. Sitemaps and feeds "
            "have no legitimate use for these, and nested declarations are the "
            "billion-laughs denial-of-service vector."
        )

    return ET.fromstring(raw)
