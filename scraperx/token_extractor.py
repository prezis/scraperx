"""Extract cryptocurrency token mentions from tweet text."""

import re
from dataclasses import dataclass


@dataclass
class TokenMention:
    symbol: str  # e.g. "SOL", "WIF", "BONK"
    mention_type: str  # 'cashtag' ($SOL), 'text_match' (mentioned by name)
    token_address: str | None = None  # Solana mint address if known
    confidence: float = 1.0  # 1.0 for cashtag, 0.7 for text match


# Well-known Solana tokens to match (case-insensitive in text)
KNOWN_TOKENS = {
    "SOL": None,  # native, no mint needed
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
}

# Tokens to IGNORE (too common, not useful as signals)
IGNORE_TOKENS = {"USD", "BTC", "ETH", "THE", "FOR", "AND", "NOT", "ALL", "NEW", "NOW", "HOT", "TOP", "APE"}


def extract_token_mentions(text: str) -> list[TokenMention]:
    """Extract token mentions from tweet text.

    Finds:
    1. $CASHTAGS (e.g. $SOL, $WIF) — high confidence
    2. Known token names mentioned in text — lower confidence

    Returns deduplicated list sorted by confidence (highest first).
    """
    mentions = {}  # symbol -> TokenMention (dedup by symbol)

    # 1. Find $CASHTAGS
    cashtags = re.findall(r"\$([A-Z]{2,10})\b", text.upper())
    for tag in cashtags:
        if tag in IGNORE_TOKENS:
            continue
        mentions[tag] = TokenMention(
            symbol=tag,
            mention_type="cashtag",
            token_address=KNOWN_TOKENS.get(tag),
            confidence=1.0,
        )

    # 2. Find known token names in text (case-insensitive, word boundary)
    text_upper = text.upper()
    for token, address in KNOWN_TOKENS.items():
        if token in IGNORE_TOKENS or token in mentions:
            continue
        # Require word boundary match
        pattern = r"\b" + re.escape(token) + r"\b"
        if re.search(pattern, text_upper):
            mentions[token] = TokenMention(
                symbol=token,
                mention_type="text_match",
                token_address=address,
                confidence=0.7,
            )

    return sorted(mentions.values(), key=lambda m: m.confidence, reverse=True)
