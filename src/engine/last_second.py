"""
Last-second price convergence strategy for Kalshi hourly crypto bucket markets.

Strategy:
  In the final ENTRY_WINDOW_SECONDS before a Kalshi hourly crypto series closes:
    1. Fetch real-time Kraken spot price for the underlying asset.
    2. Find which price-range bucket the spot price falls in.
    3. Verify price has been stable (< STABILITY_THRESHOLD_PCT movement)
       for at least STABILITY_WINDOW_S seconds.
    4. Verify spot price is at least EDGE_BUFFER_PCT of bucket width away from
       both edges (avoids settlement risk when price is near a boundary).
    5. If the bucket's YES ask is between MIN_YES_CENTS and MAX_YES_CENTS, buy YES.

Why this works:
  Kalshi crypto series resolve via the CF Benchmarks Real-Time Index (RTI), which
  is computed as the 60-second equally-weighted average of qualifying trades in the
  final minute. If spot has been stable and well-inside a bucket for 60+ seconds,
  the 60s average will almost certainly land in that same bucket — yet the YES ask
  may still be 80-90¢ rather than 99¢, giving a small edge.

  The edge disappears quickly as the market adjusts, so we need very fast entry
  right at the start of the window.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Defaults (all overridable via function params)
# ---------------------------------------------------------------------------

ENTRY_WINDOW_SECONDS: int = 120         # enter within this many seconds of close
MIN_YES_CENTS: int = 70                 # minimum YES ask (market must find it likely)
MAX_YES_CENTS: int = 98                 # maximum YES ask (99c excluded: no room for profit)
MIN_NO_CENTS: int = 3                   # minimum NO ask for NO trades (avoid zero-edge)
MAX_NO_CENTS: int = 40                  # maximum NO ask for NO trades (avoid high-risk)
STABILITY_WINDOW_S: int = 15           # look-back window for stability check
STABILITY_THRESHOLD_PCT: float = 0.003  # max allowed price move in window (0.3%)
EDGE_BUFFER_PCT: float = 0.15           # must be >15% of bucket width from each edge
DIRECTIONAL_MARGIN_PCT: float = 0.003   # spot must be >0.3% above/below floor_strike for 15M markets

# Kalshi series_ticker prefix → Kraken trading pair
_PREFIX_TO_KRAKEN: dict[str, str] = {
    "KXBTC": "XBTUSD",
    "KXETH": "ETHUSD",
    "KXSOL": "SOLUSD",
    "KXXRP": "XRPUSD",
    "KXDOGE": "DOGEUSD",
}


# ---------------------------------------------------------------------------
# PriceTracker
# ---------------------------------------------------------------------------

class PriceTracker:
    """Rolling price history for a single Kraken trading pair."""

    def __init__(self, max_age_seconds: int = 120) -> None:
        self._history: list[tuple[float, float]] = []  # (unix_ts, price)
        self._max_age = max_age_seconds

    def record(self, price: float) -> None:
        """Append a new price observation and prune stale entries."""
        now = time.time()
        self._history.append((now, price))
        cutoff = now - self._max_age
        self._history = [(t, p) for t, p in self._history if t >= cutoff]

    def latest(self) -> float | None:
        """Return the most recent price, or None if no observations."""
        return self._history[-1][1] if self._history else None

    def is_stable(
        self,
        window_seconds: int = STABILITY_WINDOW_S,
        max_move_pct: float = STABILITY_THRESHOLD_PCT,
    ) -> bool:
        """
        Return True if price has NOT moved more than max_move_pct (fraction)
        over the last window_seconds. Requires at least 2 observations in window.
        """
        if not self._history:
            return False
        now = time.time()
        cutoff = now - window_seconds
        recent = [p for t, p in self._history if t >= cutoff]
        if len(recent) < 2:
            return False
        lo, hi = min(recent), max(recent)
        if lo == 0:
            return False
        return (hi - lo) / lo <= max_move_pct

    def observation_count(self) -> int:
        return len(self._history)

    def age_seconds(self) -> float:
        """Seconds since the first recorded observation (0 if empty)."""
        if not self._history:
            return 0.0
        return time.time() - self._history[0][0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def kraken_pair_for_market(market: Any) -> str | None:
    """Return the Kraken pair for a Market based on its series_ticker, or None."""
    series_ticker: str = market.metadata.get("series_ticker", "")
    for prefix, pair in _PREFIX_TO_KRAKEN.items():
        if series_ticker.startswith(prefix):
            return pair
    return None


def find_matching_bucket(
    markets: list,
    spot_price: float,
    edge_buffer_pct: float = EDGE_BUFFER_PCT,
) -> tuple[Any, int] | None:
    """
    Given a list of Market objects (same asset / same close-time), find the one
    whose [floor_strike, cap_strike) range contains spot_price with sufficient
    margin from both edges.

    Returns (market, yes_ask_cents) or None if:
      - No bucket contains spot_price
      - Spot is too close to an edge (within edge_buffer_pct * bucket_width)
      - No YES selection with a valid ask price
    """
    for mkt in markets:
        floor_s = mkt.metadata.get("floor_strike")
        cap_s = mkt.metadata.get("cap_strike")
        if floor_s is None or cap_s is None:
            continue

        # Convert to float in case they're stored as strings
        try:
            floor_s = float(floor_s)
            cap_s = float(cap_s)
        except (TypeError, ValueError):
            continue

        if not (floor_s <= spot_price < cap_s):
            continue

        bucket_width = cap_s - floor_s
        if bucket_width <= 0:
            continue

        margin_from_floor = spot_price - floor_s
        margin_from_cap = cap_s - spot_price
        buffer = edge_buffer_pct * bucket_width
        if margin_from_floor < buffer or margin_from_cap < buffer:
            return None  # Too close to an edge; settlement risk too high

        yes_sel = next((s for s in mkt.selections if s.name == "Yes"), None)
        if yes_sel is None:
            continue
        yes_ask = yes_sel.metadata.get("yes_ask")
        if yes_ask is None or yes_ask <= 0:
            continue

        return (mkt, int(yes_ask))

    return None


def find_no_opportunities(
    markets: list,
    spot_price: float,
    edge_buffer_pct: float = EDGE_BUFFER_PCT,
    min_no_cents: int = MIN_NO_CENTS,
    max_no_cents: int = MAX_NO_CENTS,
) -> list[tuple[Any, int]]:
    """
    Find buckets where the spot price is clearly OUTSIDE (not near an edge),
    and the NO ask is within the tradeable range.

    Returns list of (market, no_ask_cents) for each qualifying bucket.

    Edge check for NO trades: spot must be at least edge_buffer_pct * bucket_width
    away from the nearest edge of the bucket (cap or floor, whichever is closer).
    This ensures the settlement price can't easily drift into the bucket.
    """
    results = []
    for mkt in markets:
        floor_s = mkt.metadata.get("floor_strike")
        cap_s = mkt.metadata.get("cap_strike")
        if floor_s is None or cap_s is None:
            continue
        try:
            floor_s = float(floor_s)
            cap_s = float(cap_s)
        except (TypeError, ValueError):
            continue

        # Skip if spot is INSIDE this bucket — that's a YES trade candidate
        if floor_s <= spot_price < cap_s:
            continue

        bucket_width = cap_s - floor_s
        if bucket_width <= 0:
            continue

        buffer = edge_buffer_pct * bucket_width
        if spot_price < floor_s:
            distance_to_bucket = floor_s - spot_price
        else:  # spot_price >= cap_s
            distance_to_bucket = spot_price - cap_s

        if distance_to_bucket < buffer:
            continue  # Too close to the bucket edge — settlement risk

        no_sel = next((s for s in mkt.selections if s.name == "No"), None)
        if no_sel is None:
            continue
        no_ask = no_sel.metadata.get("no_ask")
        if no_ask is None or no_ask <= 0:
            continue

        no_ask_cents = int(no_ask)
        if not (min_no_cents <= no_ask_cents <= max_no_cents):
            continue

        results.append((mkt, no_ask_cents))

    return results


def find_directional_opportunity(
    market,
    spot_price: float,
    margin_pct: float = DIRECTIONAL_MARGIN_PCT,
    min_yes_cents: int = MIN_YES_CENTS,
    max_yes_cents: int = MAX_YES_CENTS,
    min_no_cents: int = MIN_NO_CENTS,
    max_no_cents: int = MAX_NO_CENTS,
) -> dict | None:
    """
    Handle 15-minute directional markets: floor_strike present, no cap_strike.

    These resolve YES if the final RTI >= floor_strike (the starting price from
    30 minutes earlier).  If the spot has moved far enough in one direction with
    a stable price, we can trade with edge:
      - spot >= floor_strike * (1 + margin_pct)  →  YES (price held above starting level)
      - spot <= floor_strike * (1 - margin_pct)  →  NO  (price held below starting level)

    Returns {"side": "yes"|"no", "yes_ask_cents"|"no_ask_cents": int} or None.
    """
    floor_s = market.metadata.get("floor_strike")
    cap_s = market.metadata.get("cap_strike")
    # Only applies to directional markets (floor only, no cap)
    if floor_s is None or cap_s is not None:
        return None
    try:
        floor_s = float(floor_s)
    except (TypeError, ValueError):
        return None
    if floor_s <= 0:
        return None

    pct_from_floor = (spot_price - floor_s) / floor_s

    if pct_from_floor >= margin_pct:
        # Spot comfortably above floor — YES likely
        yes_sel = next((s for s in market.selections if s.name == "Yes"), None)
        if yes_sel is None:
            return None
        yes_ask = yes_sel.metadata.get("yes_ask")
        if yes_ask is None or not (min_yes_cents <= int(yes_ask) <= max_yes_cents):
            return None
        return {"side": "yes", "yes_ask_cents": int(yes_ask)}

    if pct_from_floor <= -margin_pct:
        # Spot comfortably below floor — NO likely
        no_sel = next((s for s in market.selections if s.name == "No"), None)
        if no_sel is None:
            return None
        no_ask = no_sel.metadata.get("no_ask")
        if no_ask is None or not (min_no_cents <= int(no_ask) <= max_no_cents):
            return None
        return {"side": "no", "no_ask_cents": int(no_ask)}

    return None  # too close to floor_strike — settlement risk


def update_price_trackers(
    trackers: dict[str, PriceTracker],
    pairs_needed: set[str],
) -> dict[str, float | None]:
    """
    Fetch the current Kraken spot price for each pair in pairs_needed,
    record it in the corresponding tracker (creating one if absent),
    and return {pair: price | None}.
    Silently skips pairs that fail to fetch.
    """
    from src.fetchers.crypto_prices import get_current_price

    results: dict[str, float | None] = {}
    for pair in pairs_needed:
        if pair not in trackers:
            trackers[pair] = PriceTracker()
        try:
            price = get_current_price(pair)
            trackers[pair].record(price)
            results[pair] = price
        except Exception:
            results[pair] = None
    return results


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

def scan_last_second_opportunities(
    near_markets: list,
    trackers: dict[str, PriceTracker],
    now: datetime,
    entry_window_seconds: int = ENTRY_WINDOW_SECONDS,
    min_yes_cents: int = MIN_YES_CENTS,
    max_yes_cents: int = MAX_YES_CENTS,
    min_no_cents: int = MIN_NO_CENTS,
    max_no_cents: int = MAX_NO_CENTS,
    edge_buffer_pct: float = EDGE_BUFFER_PCT,
    stability_window_s: int = STABILITY_WINDOW_S,
    stability_threshold_pct: float = STABILITY_THRESHOLD_PCT,
    directional_margin_pct: float = DIRECTIONAL_MARGIN_PCT,
) -> list[dict]:
    """
    Scan near_markets for last-second price convergence opportunities.

    Returns YES entries (spot inside bucket) and NO entries (spot clearly outside).

    Each entry dict has:
        side           — "yes" or "no"
        market         — the target Market object
        ask_cents      — price to pay (yes_ask or no_ask, in cents)
        kraken_pair    — e.g. "XBTUSD"
        spot_price     — current spot price
        closes_at      — market expiry datetime
        seconds_to_close
        tracker_obs    — number of price observations in tracker
    """
    # Group markets by (kraken_pair, close_time_iso) — same asset + same expiry
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for mkt in near_markets:
        pair = kraken_pair_for_market(mkt)
        if pair is None:
            continue
        if mkt.starts_at is None:
            continue
        seconds_to_close = (mkt.starts_at - now).total_seconds()
        if not (0 < seconds_to_close <= entry_window_seconds):
            continue
        close_key = mkt.starts_at.isoformat()
        groups[(pair, close_key)].append(mkt)

    if not groups:
        return []

    entries: list[dict] = []
    for (pair, close_key), mkt_list in groups.items():
        tracker = trackers.get(pair)
        if tracker is None or tracker.latest() is None:
            continue

        # Stability check — applies to both YES and NO trades
        if not tracker.is_stable(stability_window_s, stability_threshold_pct):
            continue

        spot = tracker.latest()
        seconds_to_close = None

        # YES: buy the bucket containing spot
        yes_result = find_matching_bucket(mkt_list, spot, edge_buffer_pct)
        if yes_result is not None:
            mkt, yes_ask = yes_result
            if min_yes_cents <= yes_ask <= max_yes_cents:
                seconds_to_close = (mkt.starts_at - now).total_seconds()
                entries.append({
                    "side": "yes",
                    "market": mkt,
                    "yes_ask_cents": yes_ask,
                    "kraken_pair": pair,
                    "spot_price": spot,
                    "closes_at": mkt.starts_at,
                    "seconds_to_close": seconds_to_close,
                    "tracker_obs": tracker.observation_count(),
                })

        # NO: buy NO on every bucket the spot is clearly outside of
        no_results = find_no_opportunities(mkt_list, spot, edge_buffer_pct, min_no_cents, max_no_cents)
        for mkt, no_ask in no_results:
            secs = (mkt.starts_at - now).total_seconds()
            entries.append({
                "side": "no",
                "market": mkt,
                "no_ask_cents": no_ask,
                "kraken_pair": pair,
                "spot_price": spot,
                "closes_at": mkt.starts_at,
                "seconds_to_close": secs,
                "tracker_obs": tracker.observation_count(),
            })

        # Directional (15M markets): floor_strike only, no cap_strike
        for mkt in mkt_list:
            dir_result = find_directional_opportunity(
                mkt, spot,
                margin_pct=directional_margin_pct,
                min_yes_cents=min_yes_cents, max_yes_cents=max_yes_cents,
                min_no_cents=min_no_cents, max_no_cents=max_no_cents,
            )
            if dir_result is not None:
                secs = (mkt.starts_at - now).total_seconds()
                entries.append({
                    **dir_result,
                    "market": mkt,
                    "kraken_pair": pair,
                    "spot_price": spot,
                    "closes_at": mkt.starts_at,
                    "seconds_to_close": secs,
                    "tracker_obs": tracker.observation_count(),
                })

    return entries
