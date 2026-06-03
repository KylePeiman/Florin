"""
Regression tests for KalshiFetcher._parse_selections.

Context: the last-second and arbitrage strategies buy a single side of a market
and therefore only need that side's ASK. The parser used to require a non-zero
BID *and* ASK on BOTH sides, which silently dropped the whole market for
near-certain (high-confidence YES, no resting NO bid) and far-OTM (no YES bid)
buckets — exactly the markets those strategies target. That left the app unable
to make any trades even when prices were stable.

These tests lock in that a market stays tradeable as long as both sides are
buyable (non-zero ask), regardless of whether a bid is resting.
"""
from src.fetchers.kalshi import KalshiFetcher


def _parse(market: dict):
    # Bypass __init__ (which needs API keys / httpx client) — _parse_selections
    # only reads the passed dict.
    fetcher = KalshiFetcher.__new__(KalshiFetcher)
    return fetcher._parse_selections(market)


def _by_name(selections):
    return {s.name: s for s in selections}


def test_two_sided_book_unchanged():
    """A normal two-sided book still parses into Yes + No selections."""
    sels = _by_name(_parse(dict(yes_bid=83, yes_ask=85, no_bid=13, no_ask=15)))
    assert set(sels) == {"Yes", "No"}
    assert sels["Yes"].metadata["yes_ask"] == 85
    assert sels["No"].metadata["no_ask"] == 15
    # Mid-based implied prob when both bids exist (84/100), as before.
    assert sels["Yes"].metadata["implied_prob"] == 0.84


def test_high_confidence_yes_with_no_no_bid_is_kept():
    """98¢ YES whose NO side has no resting bid must NOT be dropped."""
    sels = _by_name(_parse(dict(yes_bid=96, yes_ask=98, no_bid=0, no_ask=3)))
    assert set(sels) == {"Yes", "No"}
    assert sels["Yes"].metadata["yes_ask"] == 98
    assert sels["No"].metadata["no_ask"] == 3


def test_far_otm_bucket_with_no_yes_bid_is_kept():
    """Far-OTM bucket (YES near-worthless, no YES bid) must survive for NO trades."""
    sels = _by_name(_parse(dict(yes_bid=0, yes_ask=3, no_bid=95, no_ask=98)))
    assert set(sels) == {"Yes", "No"}
    assert sels["No"].metadata["no_ask"] == 98
    # Ask-based implied prob when the bid is missing.
    assert sels["Yes"].metadata["implied_prob"] == 0.03


def test_dollars_fields_supported_with_missing_bid():
    """New *_dollars float fields work, and a zero bid no longer drops the market."""
    sels = _by_name(_parse(dict(
        yes_bid_dollars=0.0, yes_ask_dollars=0.97,
        no_bid_dollars=0.01, no_ask_dollars=0.04,
    )))
    assert set(sels) == {"Yes", "No"}
    assert sels["Yes"].metadata["yes_ask"] == 97


def test_unbuyable_side_is_dropped():
    """If a side has no ask at all, the market is not tradeable and is dropped."""
    assert _parse(dict(yes_bid=10, yes_ask=12, no_bid=85)) == []   # no no_ask
    assert _parse(dict(yes_ask=0, no_ask=99)) == []                # zero yes_ask
    assert _parse(dict()) == []                                    # nothing quoted
