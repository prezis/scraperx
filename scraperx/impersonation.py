"""
Impersonation / scam detection for X/Twitter replies.

Detects scammers who impersonate popular accounts by:
- Typosquatting handles (e.g., "elonn_musk" vs "elonmusk")
- Copying display names and avatars
- Posting scam content (crypto scams, phishing links)

No external dependencies — uses only stdlib (difflib, re, dataclasses).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ImpersonationCheck:
    """Result of an impersonation check on a single tweet/reply."""
    is_suspect: bool
    confidence: float          # 0.0–1.0
    reasons: list[str] = field(default_factory=list)
    handle_similarity: float = 0.0
    name_similarity: float = 0.0
    avatar_match: bool = False
    has_scam_content: bool = False


# ---------------------------------------------------------------------------
# Common substitution pairs used by typosquatters
# ---------------------------------------------------------------------------

_SUBSTITUTIONS: list[tuple[str, str]] = [
    ("l", "1"),
    ("i", "1"),
    ("o", "0"),
    ("rn", "m"),
    ("vv", "w"),
    ("cl", "d"),
    ("nn", "m"),
]


def _normalize_handle(handle: str) -> str:
    """Lowercase, strip leading @, remove underscores/dots for comparison."""
    h = handle.lower().lstrip("@")
    h = h.replace("_", "").replace(".", "")
    return h


def _apply_substitutions(text: str) -> str:
    """Replace common typosquat substitutions so similar strings converge."""
    t = text.lower()
    for a, b in _SUBSTITUTIONS:
        t = t.replace(a, b)
        t = t.replace(b, a)  # symmetric — collapse both directions to same
    # Deduplicate repeated chars (e.g. "eellon" -> "elon")
    deduped = []
    for ch in t:
        if not deduped or deduped[-1] != ch:
            deduped.append(ch)
    return "".join(deduped)


def handle_similarity(a: str, b: str) -> float:
    """
    Score 0.0–1.0 measuring how similar two handles are.

    Uses both raw SequenceMatcher ratio AND a normalized version that
    accounts for common typosquat substitutions.  Returns the higher
    of the two scores.
    """
    if not a or not b:
        return 0.0

    na = _normalize_handle(a)
    nb = _normalize_handle(b)

    if na == nb:
        return 1.0

    raw_ratio = SequenceMatcher(None, na, nb).ratio()

    # Also compare after substitution normalization
    sa = _apply_substitutions(na)
    sb = _apply_substitutions(nb)
    sub_ratio = SequenceMatcher(None, sa, sb).ratio()

    return max(raw_ratio, sub_ratio)


def name_similarity(a: str, b: str) -> float:
    """Case-insensitive display-name similarity (0.0–1.0)."""
    if not a or not b:
        return 0.0
    la = a.strip().lower()
    lb = b.strip().lower()
    if la == lb:
        return 1.0
    return SequenceMatcher(None, la, lb).ratio()


# ---------------------------------------------------------------------------
# Avatar URL comparison
# ---------------------------------------------------------------------------

def _avatar_urls_match(url_a: str, url_b: str) -> bool:
    """
    Check whether two avatar URLs point to the same image.

    Compares the path component, ignoring size suffixes like ``_normal``,
    ``_bigger``, ``_200x200``, ``_400x400`` that Twitter appends.
    """
    if not url_a or not url_b:
        return False
    if url_a == url_b:
        return True

    # Strip common Twitter size variants for comparison
    size_re = re.compile(r"_(normal|bigger|mini|200x200|400x400|reasonably_small)")
    clean_a = size_re.sub("", url_a.split("?")[0])
    clean_b = size_re.sub("", url_b.split("?")[0])
    return clean_a == clean_b


# ---------------------------------------------------------------------------
# Scam content detection
# ---------------------------------------------------------------------------

_SCAM_PHRASES: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bclaim\s+now\b",
        r"\bclaim\s+your\b",
        r"\bairdrop\b",
        r"\bsend\s+\d.*\bget\b.*\bback\b",
        r"\bDM\s+me\b",
        r"\bfree\s+crypto\b",
        r"\bfree\s+giveaway\b",
        r"\bgiveaway\b.*\bwallet\b",
        r"\bwallet\b.*\bgiveaway\b",
        r"\bconnect\s+(?:your\s+)?wallet\b",
        r"\bwhitelist\s+spot\b",
        r"\bmint\s+now\b",
        r"\blimited\s+time\b",
        r"\bdouble\s+your\b",
        r"\bI'll\s+send\s+back\b",
    ]
]

# Wallet address patterns
_WALLET_RE = re.compile(
    r"(?:"
    r"0x[0-9a-fA-F]{40}"          # Ethereum
    r"|bc1[a-zA-HJ-NP-Z0-9]{25,39}"  # Bitcoin bech32
    r"|[1-9A-HJ-NP-Za-km-z]{32,44}"   # Solana / Base58
    r")"
)

# Known scam/shortener domains
_SCAM_DOMAINS: set[str] = {
    "bit.ly", "tinyurl.com", "t.co", "rb.gy", "cutt.ly",
    "is.gd", "v.gd", "short.io", "ow.ly", "buff.ly",
}

_URL_RE = re.compile(r"https?://([^\s/]+)")

# Emoji spam: count certain "hype" emojis
_HYPE_EMOJIS = re.compile(r"[\U0001F525\U0001F680\U0001F4B0\U0001F4B8\U0001F381\U0001F389\u2728\U0001F31F\U0001F4A5]")


def _detect_scam_content(text: str) -> list[str]:
    """Return list of scam signal descriptions found in *text*."""
    if not text:
        return []

    signals: list[str] = []

    # Phrase matches
    for pat in _SCAM_PHRASES:
        if pat.search(text):
            signals.append(f"scam phrase: {pat.pattern}")
            break  # one phrase signal is enough

    # Wallet addresses
    if _WALLET_RE.search(text):
        signals.append("contains wallet address")

    # Suspicious shortened URLs
    for m in _URL_RE.finditer(text):
        domain = m.group(1).lower()
        if domain in _SCAM_DOMAINS:
            signals.append(f"shortened/suspicious URL domain: {domain}")
            break

    # Emoji spam
    emoji_count = len(_HYPE_EMOJIS.findall(text))
    if emoji_count >= 5:
        signals.append(f"excessive hype emojis ({emoji_count})")

    return signals


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def check_impersonation(
    tweet_author_handle: str,
    tweet_author_name: str,
    tweet_author_avatar: str,
    tweet_text: str,
    real_author_handle: str,
    real_author_name: str,
    real_author_avatar: str,
    confidence_threshold: float = 0.6,
) -> ImpersonationCheck:
    """
    Check whether a reply tweet is from an impersonator of the real author.

    Args:
        tweet_author_handle: Handle of the reply tweet's author.
        tweet_author_name: Display name of the reply tweet's author.
        tweet_author_avatar: Avatar URL of the reply tweet's author.
        tweet_text: Text content of the reply tweet.
        real_author_handle: Handle of the real/original thread author.
        real_author_name: Display name of the real/original thread author.
        real_author_avatar: Avatar URL of the real/original thread author.
        confidence_threshold: Minimum confidence to flag as suspect.

    Returns:
        ImpersonationCheck with detection results.
    """
    reasons: list[str] = []
    identity_scores: list[float] = []

    # --- Exact same account? Not an impersonator. ---
    if _normalize_handle(tweet_author_handle) == _normalize_handle(real_author_handle):
        return ImpersonationCheck(
            is_suspect=False,
            confidence=0.0,
            handle_similarity=1.0,
            name_similarity=1.0,
            avatar_match=True,
            has_scam_content=False,
        )

    # --- Handle similarity ---
    h_sim = handle_similarity(tweet_author_handle, real_author_handle)
    if h_sim >= 0.75:
        reasons.append(
            f"handle typosquat: @{tweet_author_handle} vs @{real_author_handle} "
            f"(similarity {h_sim:.0%})"
        )
        identity_scores.append(h_sim)

    # --- Display name similarity ---
    n_sim = name_similarity(tweet_author_name, real_author_name)
    if n_sim >= 0.80:
        reasons.append(
            f"display name match: \"{tweet_author_name}\" vs \"{real_author_name}\" "
            f"(similarity {n_sim:.0%})"
        )
        identity_scores.append(n_sim * 0.8)  # name alone is weaker signal

    # --- Avatar match ---
    avatar = _avatar_urls_match(tweet_author_avatar, real_author_avatar)
    if avatar:
        reasons.append("avatar URL matches real author")
        identity_scores.append(0.85)

    # --- Scam content ---
    scam_signals = _detect_scam_content(tweet_text)
    has_scam = bool(scam_signals)
    if has_scam:
        reasons.extend(scam_signals)

    # --- Combine scores ---
    # Identity signals (handle/name/avatar) are primary.
    # Scam content alone is a moderate signal; combined with identity it boosts.
    identity_score = max(identity_scores) if identity_scores else 0.0
    if has_scam and identity_score > 0.0:
        # Compound signal: looks like + acts like impersonator
        confidence = min(1.0, identity_score + 0.2)
    elif identity_score > 0.0:
        confidence = identity_score
    elif has_scam:
        confidence = 0.5  # scam content alone is moderate
    else:
        confidence = 0.0

    is_suspect = confidence >= confidence_threshold

    return ImpersonationCheck(
        is_suspect=is_suspect,
        confidence=round(confidence, 3),
        reasons=reasons,
        handle_similarity=round(h_sim, 3),
        name_similarity=round(n_sim, 3),
        avatar_match=avatar,
        has_scam_content=has_scam,
    )
