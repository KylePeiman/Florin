"""Weather edge scanner — compares Kalshi weather market prices to NWS forecasts.

Fetches Kalshi weather markets closing today, parses each title into
structured fields (city, metric, threshold, direction), pulls the
corresponding NOAA/NWS forecast, and identifies markets where the
NWS-implied probability diverges from the Kalshi price by at least
``min_edge``.
"""

from __future__ import annotations

import datetime
import math
from typing import Any

from src.fetchers.kalshi import KalshiFetcher
from src.weather.market_parser import parse_weather_market
from src.weather.noaa import get_forecast

# Known Kalshi daily weather series tickers (high temp, low temp, precip, wind).
# These markets have no API category, so we fetch by series directly.
_WEATHER_SERIES: list[str] = [
    "KXHIGHAUS", "KXHIGHCHI", "KXHIGHDEN", "KXHIGHLAX", "KXHIGHMIA",
    "KXHIGHNY", "KXHIGHPHIL", "KXHIGHTATL", "KXHIGHTBOS", "KXHIGHTDAL",
    "KXHIGHTDC", "KXHIGHTHOU", "KXHIGHTLV", "KXHIGHTMIN", "KXHIGHTNOLA",
    "KXHIGHTOKC", "KXHIGHTPHX", "KXHIGHTPHX", "KXHIGHTPHX", "KXHIGHTSATX",
    "KXHIGHTPHX", "KXHIGHTSEA", "KXHIGHTSFO",
    "KXLOWTAUS", "KXLOWTCHI", "KXLOWTDEN", "KXLOWTLAX", "KXLOWTMIA",
    "KXLOWTNYC", "KXLOWTPHIL",
]


def get_nws_probability(
    parsed: dict[str, Any],
    forecast: list[dict[str, Any]],
) -> float | None:
    """Convert an NWS hourly forecast to a 0-1 probability for a parsed market.

    Uses all available hourly data to produce a smooth, continuous probability
    rather than a coarse bucket-based heuristic:

    - **Precip**: highest single-hour PoP across the day (best proxy for
      "will it rain at all today").
    - **High / low temp**: sigmoid centered on the forecast extreme vs the
      threshold; sigma adapts to the daily temperature range so a narrow
      forecast is treated as higher-confidence than a wide one.
    - **Wind**: sigmoid using the peak hourly wind against the threshold,
      with a fixed sigma of 3 mph.
    - **Bucket**: fraction of hours inside the bucket, blended with a
      sigmoid on how far the peak is from the nearest edge.

    Args:
        parsed: Output of ``parse_weather_market`` with keys ``date``,
            ``metric``, ``threshold``, and ``direction``.
        forecast: List of hourly forecast period dicts as returned by
            ``get_forecast``, each containing ``time``, ``temp_f``,
            ``precip_pct``, and ``wind_mph``.

    Returns:
        A float in [0, 1] representing the estimated probability that
        the market condition is met, or ``None`` if no forecast data
        matches the target date.
    """
    if not forecast:
        return None

    target_date: datetime.date = parsed["date"]
    metric: str = parsed["metric"]
    threshold: float = parsed["threshold"]
    direction: str = parsed["direction"]

    # Filter forecast periods to the target date.
    day_periods = [
        p for p in forecast if p["time"].date() == target_date
    ]
    if not day_periods:
        return None

    if metric == "precip":
        # Max hourly PoP is the best single-day proxy: if any hour is
        # likely to produce precip, the day probably sees precip.
        max_pop = max(p["precip_pct"] for p in day_periods)
        # Blend with average to avoid over-counting brief shower windows.
        avg_pop = sum(p["precip_pct"] for p in day_periods) / len(day_periods)
        prob = (0.6 * max_pop + 0.4 * avg_pop) / 100.0
        return prob if direction == "above" else 1.0 - prob

    if metric == "high_temp":
        temps = [p["temp_f"] for p in day_periods]
        observed = max(temps)
        # Sigma adapts to daily temperature spread: a tight forecast
        # (all hours similar) warrants a sharper probability curve.
        daily_range = max(temps) - min(temps)
        sigma = max(daily_range / 6.0, 1.5)
        diff = observed - threshold
        raw = _sigmoid(diff, sigma)
        return raw if direction == "above" else 1.0 - raw

    if metric == "low_temp":
        temps = [p["temp_f"] for p in day_periods]
        observed = min(temps)
        daily_range = max(temps) - min(temps)
        sigma = max(daily_range / 6.0, 1.5)
        diff = observed - threshold
        raw = _sigmoid(diff, sigma)
        return raw if direction == "above" else 1.0 - raw

    if metric == "wind":
        # Use peak hourly wind as the relevant extreme.
        observed = max(p["wind_mph"] for p in day_periods)
        raw = _sigmoid(observed - threshold, sigma=3.0)
        return raw if direction == "above" else 1.0 - raw

    if direction == "bucket":
        lo = parsed.get("threshold_low", threshold - 0.5)
        hi = parsed.get("threshold_high", threshold + 0.5)
        temps = [p["temp_f"] for p in day_periods]
        peak = max(temps)
        # Fraction of hours inside the bucket — direct data-driven signal.
        hours_inside = sum(1 for t in temps if lo <= t < hi)
        frac_inside = hours_inside / len(temps)
        # Sigmoid on how far the peak is from nearest bucket edge.
        margin = min(peak - lo, hi - peak)
        edge_conf = _sigmoid(margin, sigma=2.0)
        if lo <= peak < hi:
            return 0.5 * frac_inside + 0.5 * edge_conf
        else:
            return 0.5 * frac_inside + 0.5 * (1.0 - edge_conf)

    return None


def _sigmoid(diff: float, sigma: float = 2.0) -> float:
    """Logistic function mapping a signed difference to a [0, 1] probability.

    ``_sigmoid(0)`` → 0.50; positive diff → above 0.50; negative → below.

    Args:
        diff: Signed difference (observed − threshold).
        sigma: Scale factor; larger values produce a softer curve.
            Typical values: 2.0 for temperature (°F), 3.0 for wind (mph).

    Returns:
        Float in (0, 1).
    """
    return 1.0 / (1.0 + math.exp(-diff / sigma))


def scan_weather_markets(
    fetcher: KalshiFetcher,
    min_edge: float = 0.05,
    min_nws_prob: float = 0.35,
) -> list[dict[str, Any]]:
    """Fetch Kalshi weather markets and find NWS edge opportunities.

    Retrieves all open Kalshi markets, filters to weather markets
    closing today, and compares the Kalshi-implied probability to the
    NWS forecast probability. Returns only markets where the absolute
    edge exceeds ``min_edge``.

    Args:
        fetcher: An authenticated ``KalshiFetcher`` instance.
        min_edge: Minimum absolute probability edge to include in
            results. Defaults to 0.05 (5 percentage points).

    Returns:
        A list of dicts, each containing:
            - ``market``: The original ``Market`` object.
            - ``side``: ``"yes"`` or ``"no"``.
            - ``ask_cents``: The ask price in cents for the chosen side.
            - ``kalshi_prob``: The Kalshi-implied probability
              (``ask_cents / 100``).
            - ``nws_prob``: The NWS-derived probability.
            - ``edge``: ``abs(nws_prob - kalshi_prob)``.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    # Daily weather markets close at ~midnight ET = ~05:00 UTC next day.
    # Treat any market closing within the next 30 hours as "today's" market.
    window_end = now + datetime.timedelta(hours=30)
    opportunities: list[dict[str, Any]] = []

    try:
        markets = fetcher.get_markets_by_series(_WEATHER_SERIES)
    except Exception as exc:
        print(f"[weather] failed to fetch markets: {exc}")
        return opportunities

    for market in markets:
        try:
            # Skip markets with no selections or zero yes_ask.
            if not market.selections:
                continue

            yes_ask: int = market.selections[0].metadata.get(
                "yes_ask", 0
            )
            no_ask: int = market.selections[0].metadata.get("no_ask", 0)

            if yes_ask == 0:
                continue

            # Only include markets that are currently open and closing within 30h.
            close_time = market.starts_at
            if close_time is None:
                continue

            if not (now < close_time <= window_end):
                continue

            # Parse market title for weather fields.
            parsed = parse_weather_market(market)
            if parsed is None:
                continue

            # Fetch NWS forecast for the market's city.
            forecast = get_forecast(parsed["lat"], parsed["lon"])
            nws_prob = get_nws_probability(parsed, forecast)
            if nws_prob is None:
                continue

            kalshi_prob = yes_ask / 100.0

            if nws_prob == kalshi_prob:
                continue

            edge = abs(nws_prob - kalshi_prob)
            if edge < min_edge:
                continue

            # Require minimum NWS confidence on the favored side to avoid
            # entering YES trades where NWS is only marginally above 0.
            if nws_prob > kalshi_prob and nws_prob < min_nws_prob:
                continue
            if nws_prob < kalshi_prob and (1.0 - nws_prob) < min_nws_prob:
                continue

            if nws_prob > kalshi_prob:
                side = "yes"
                ask_cents = yes_ask
            else:
                side = "no"
                ask_cents = no_ask

            if ask_cents == 0:
                continue

            opportunities.append(
                {
                    "market": market,
                    "side": side,
                    "ask_cents": ask_cents,
                    "kalshi_prob": kalshi_prob,
                    "nws_prob": nws_prob,
                    "edge": edge,
                }
            )

        except Exception as exc:
            print(f"[weather] error processing {market.id}: {exc}")
            continue
    return opportunities
