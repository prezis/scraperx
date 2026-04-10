"""Tests for scraperx.impersonation module."""
from __future__ import annotations

import pytest

from scraperx.impersonation import (
    ImpersonationCheck,
    check_impersonation,
    handle_similarity,
    name_similarity,
    _avatar_urls_match,
    _detect_scam_content,
    _normalize_handle,
)


# ---------------------------------------------------------------------------
# handle_similarity
# ---------------------------------------------------------------------------

class TestHandleSimilarity:
    def test_identical(self):
        assert handle_similarity("elonmusk", "elonmusk") == 1.0

    def test_identical_case_insensitive(self):
        assert handle_similarity("ElonMusk", "elonmusk") == 1.0

    def test_with_at_prefix(self):
        assert handle_similarity("@elonmusk", "elonmusk") == 1.0

    def test_underscore_ignored(self):
        assert handle_similarity("elon_musk", "elonmusk") == 1.0

    def test_typosquat_l_vs_1(self):
        # "e1onmusk" vs "elonmusk" — should be very similar
        sim = handle_similarity("e1onmusk", "elonmusk")
        assert sim >= 0.8

    def test_typosquat_0_vs_o(self):
        sim = handle_similarity("el0nmusk", "elonmusk")
        assert sim >= 0.8

    def test_typosquat_rn_vs_m(self):
        sim = handle_similarity("elonrnusk", "elonmusk")
        assert sim >= 0.75

    def test_typosquat_doubled_char(self):
        sim = handle_similarity("elonn_musk", "elonmusk")
        assert sim >= 0.8

    def test_completely_different(self):
        sim = handle_similarity("randomuser123", "elonmusk")
        assert sim <= 0.5

    def test_empty_strings(self):
        assert handle_similarity("", "elonmusk") == 0.0
        assert handle_similarity("elonmusk", "") == 0.0
        assert handle_similarity("", "") == 0.0


# ---------------------------------------------------------------------------
# name_similarity
# ---------------------------------------------------------------------------

class TestNameSimilarity:
    def test_identical(self):
        assert name_similarity("Elon Musk", "Elon Musk") == 1.0

    def test_case_insensitive(self):
        assert name_similarity("elon musk", "Elon Musk") == 1.0

    def test_similar(self):
        sim = name_similarity("Elon Musk", "Elon Musk ")
        assert sim >= 0.9

    def test_different(self):
        sim = name_similarity("Elon Musk", "Vitalik Buterin")
        assert sim < 0.5

    def test_empty(self):
        assert name_similarity("", "Elon Musk") == 0.0


# ---------------------------------------------------------------------------
# Avatar URL matching
# ---------------------------------------------------------------------------

class TestAvatarMatch:
    def test_exact_match(self):
        url = "https://pbs.twimg.com/profile_images/123/abc.jpg"
        assert _avatar_urls_match(url, url) is True

    def test_size_variant_ignored(self):
        url_a = "https://pbs.twimg.com/profile_images/123/abc_normal.jpg"
        url_b = "https://pbs.twimg.com/profile_images/123/abc_400x400.jpg"
        assert _avatar_urls_match(url_a, url_b) is True

    def test_different_images(self):
        url_a = "https://pbs.twimg.com/profile_images/123/abc.jpg"
        url_b = "https://pbs.twimg.com/profile_images/456/xyz.jpg"
        assert _avatar_urls_match(url_a, url_b) is False

    def test_empty(self):
        assert _avatar_urls_match("", "https://example.com/a.jpg") is False
        assert _avatar_urls_match("", "") is False


# ---------------------------------------------------------------------------
# Scam content detection
# ---------------------------------------------------------------------------

class TestScamContent:
    def test_clean_text(self):
        signals = _detect_scam_content("Just discussing the latest tech news")
        assert signals == []

    def test_airdrop(self):
        signals = _detect_scam_content("Free airdrop for all holders!")
        assert any("scam phrase" in s for s in signals)

    def test_claim_now(self):
        signals = _detect_scam_content("Claim now before it's too late!")
        assert any("scam phrase" in s for s in signals)

    def test_wallet_address_eth(self):
        signals = _detect_scam_content(
            "Send to 0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        )
        assert any("wallet address" in s for s in signals)

    def test_wallet_address_btc(self):
        signals = _detect_scam_content(
            "Send to bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
        )
        assert any("wallet address" in s for s in signals)

    def test_shortened_url(self):
        signals = _detect_scam_content("Check this out: https://bit.ly/3xyzABC")
        assert any("shortened" in s for s in signals)

    def test_emoji_spam(self):
        signals = _detect_scam_content(
            "\U0001F525\U0001F680\U0001F4B0\U0001F525\U0001F680 Amazing opportunity!"
        )
        assert any("emoji" in s for s in signals)

    def test_empty_text(self):
        assert _detect_scam_content("") == []


# ---------------------------------------------------------------------------
# Full impersonation check
# ---------------------------------------------------------------------------

class TestCheckImpersonation:
    """Integration tests for the main check_impersonation function."""

    def test_same_account_not_suspect(self):
        """Exact same handle = not an impersonator."""
        result = check_impersonation(
            tweet_author_handle="elonmusk",
            tweet_author_name="Elon Musk",
            tweet_author_avatar="https://pbs.twimg.com/a.jpg",
            tweet_text="Great thread!",
            real_author_handle="elonmusk",
            real_author_name="Elon Musk",
            real_author_avatar="https://pbs.twimg.com/a.jpg",
        )
        assert result.is_suspect is False
        assert result.confidence == 0.0

    def test_typosquat_handle_flagged(self):
        """Similar handle + different account = suspect."""
        result = check_impersonation(
            tweet_author_handle="elonn_musk",
            tweet_author_name="Elon Musk",
            tweet_author_avatar="",
            tweet_text="Check my new project",
            real_author_handle="elonmusk",
            real_author_name="Elon Musk",
            real_author_avatar="",
        )
        assert result.is_suspect is True
        assert result.handle_similarity >= 0.75
        assert any("typosquat" in r for r in result.reasons)

    def test_avatar_match_different_handle(self):
        """Same avatar + scam content = strong signal."""
        avatar = "https://pbs.twimg.com/profile_images/123/photo.jpg"
        result = check_impersonation(
            tweet_author_handle="totallylegit99",
            tweet_author_name="Elon Musk",
            tweet_author_avatar=avatar,
            tweet_text="Airdrop claim now! Send 1 ETH get 2 back",
            real_author_handle="elonmusk",
            real_author_name="Elon Musk",
            real_author_avatar=avatar,
        )
        assert result.is_suspect is True
        assert result.avatar_match is True
        assert result.has_scam_content is True

    def test_scam_content_alone_moderate(self):
        """Scam content without identity signals: below default threshold."""
        result = check_impersonation(
            tweet_author_handle="randomuser",
            tweet_author_name="Random User",
            tweet_author_avatar="",
            tweet_text="Free airdrop! Claim now at https://bit.ly/scam",
            real_author_handle="elonmusk",
            real_author_name="Elon Musk",
            real_author_avatar="",
        )
        assert result.has_scam_content is True
        # Scam content alone = 0.5, below default 0.6 threshold
        assert result.confidence <= 0.6

    def test_completely_unrelated(self):
        """Totally different user, no scam = not suspect."""
        result = check_impersonation(
            tweet_author_handle="janedoe",
            tweet_author_name="Jane Doe",
            tweet_author_avatar="https://example.com/jane.jpg",
            tweet_text="I agree with this take!",
            real_author_handle="elonmusk",
            real_author_name="Elon Musk",
            real_author_avatar="https://pbs.twimg.com/elon.jpg",
        )
        assert result.is_suspect is False
        assert result.confidence < 0.3

    def test_custom_threshold(self):
        """Lower threshold catches weaker signals."""
        result = check_impersonation(
            tweet_author_handle="randomuser",
            tweet_author_name="Random User",
            tweet_author_avatar="",
            tweet_text="Free airdrop! Claim now!",
            real_author_handle="elonmusk",
            real_author_name="Elon Musk",
            real_author_avatar="",
            confidence_threshold=0.3,
        )
        assert result.is_suspect is True

    def test_vitalik_typosquat(self):
        """Real-world typosquat example: vikitibuterin vs VitalikButerin."""
        result = check_impersonation(
            tweet_author_handle="vikitibuterin",
            tweet_author_name="Vitalik Buterin",
            tweet_author_avatar="",
            tweet_text="Connect your wallet to claim the airdrop!",
            real_author_handle="VitalikButerin",
            real_author_name="Vitalik Buterin",
            real_author_avatar="",
        )
        assert result.is_suspect is True
        assert result.handle_similarity >= 0.7

    def test_edge_all_empty(self):
        """All empty strings should not crash."""
        result = check_impersonation(
            tweet_author_handle="",
            tweet_author_name="",
            tweet_author_avatar="",
            tweet_text="",
            real_author_handle="",
            real_author_name="",
            real_author_avatar="",
        )
        assert isinstance(result, ImpersonationCheck)
        assert result.is_suspect is False
