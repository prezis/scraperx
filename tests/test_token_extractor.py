"""Tests for token_extractor module."""

from scraperx.token_extractor import (
    extract_token_mentions,
)


class TestCashtagExtraction:
    def test_single_cashtag_sol(self):
        result = extract_token_mentions("Just bought $SOL!")
        assert len(result) == 1
        assert result[0].symbol == "SOL"
        assert result[0].mention_type == "cashtag"
        assert result[0].confidence == 1.0

    def test_multiple_cashtags(self):
        result = extract_token_mentions("Loading up on $SOL $WIF $BONK")
        symbols = {m.symbol for m in result}
        assert symbols == {"SOL", "WIF", "BONK"}
        assert all(m.mention_type == "cashtag" for m in result)

    def test_cashtag_case_insensitive_input(self):
        result = extract_token_mentions("bought $sol today")
        assert len(result) == 1
        assert result[0].symbol == "SOL"


class TestTextMatchExtraction:
    def test_known_token_text_match(self):
        result = extract_token_mentions("bought some bonk today")
        assert len(result) == 1
        assert result[0].symbol == "BONK"
        assert result[0].mention_type == "text_match"
        assert result[0].confidence == 0.7

    def test_text_match_requires_word_boundary(self):
        result = extract_token_mentions("orbital mechanics")
        symbols = {m.symbol for m in result}
        assert "ORCA" not in symbols


class TestPriorityAndDedup:
    def test_cashtag_priority_over_text_match(self):
        """When same symbol appears as cashtag and in text, cashtag wins."""
        result = extract_token_mentions("$BONK is great, bonk to the moon")
        bonk_mentions = [m for m in result if m.symbol == "BONK"]
        assert len(bonk_mentions) == 1
        assert bonk_mentions[0].mention_type == "cashtag"
        assert bonk_mentions[0].confidence == 1.0

    def test_deduplication_same_cashtag_twice(self):
        result = extract_token_mentions("$SOL $SOL $SOL moon")
        sol_mentions = [m for m in result if m.symbol == "SOL"]
        assert len(sol_mentions) == 1

    def test_confidence_ordering(self):
        """Cashtags (1.0) should come before text matches (0.7)."""
        result = extract_token_mentions("$WIF and also check out bonk")
        assert len(result) == 2
        assert result[0].confidence >= result[1].confidence
        assert result[0].mention_type == "cashtag"
        assert result[1].mention_type == "text_match"


class TestIgnoreTokens:
    def test_ignore_usd(self):
        result = extract_token_mentions("$USD price going up")
        assert len(result) == 0

    def test_ignore_btc_eth(self):
        result = extract_token_mentions("$BTC $ETH are pumping")
        assert len(result) == 0

    def test_ignore_common_words(self):
        result = extract_token_mentions("$THE $FOR $AND $NOT $ALL")
        assert len(result) == 0

    def test_ignore_does_not_affect_valid_tokens(self):
        result = extract_token_mentions("$USD is boring, $SOL is king")
        assert len(result) == 1
        assert result[0].symbol == "SOL"


class TestEdgeCases:
    def test_empty_text(self):
        assert extract_token_mentions("") == []

    def test_no_tokens(self):
        assert extract_token_mentions("just a normal tweet about nothing") == []

    def test_single_char_not_matched(self):
        """$A should not match (min 2 chars)."""
        result = extract_token_mentions("$A is not a token")
        assert len(result) == 0


class TestTokenAddress:
    def test_known_token_address_populated(self):
        result = extract_token_mentions("$BONK moon")
        assert result[0].token_address == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

    def test_sol_has_no_address(self):
        result = extract_token_mentions("$SOL")
        assert result[0].token_address is None

    def test_unknown_cashtag_has_no_address(self):
        result = extract_token_mentions("$PEPE to the moon")
        assert result[0].symbol == "PEPE"
        assert result[0].token_address is None

    def test_text_match_address_populated(self):
        result = extract_token_mentions("orca is pumping hard")
        assert result[0].symbol == "ORCA"
        assert result[0].token_address == "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE"
