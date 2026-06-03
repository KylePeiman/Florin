"""Kalshi fetcher — binary prediction markets (yes/no contracts)."""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import BaseFetcher, Market, Selection
from config.settings import settings


def _load_private_key(pem_path: str):
    """Load an RSA private key from a PEM file."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    with open(pem_path, "rb") as fh:
        return load_pem_private_key(fh.read(), password=None)


def _cents_to_decimal_odds(cents: float) -> float:
    """Convert a Kalshi yes-price in cents (0–99) to decimal odds."""
    if cents <= 0 or cents >= 100:
        return 0.0
    return round(100.0 / cents, 4)


class KalshiFetcher(BaseFetcher):
    """
    Fetches open prediction markets from Kalshi (https://kalshi.com).

    Authentication uses RSA-PSS (SHA-256). You need:
      - KALSHI_API_KEY_ID  — the key ID shown on your Kalshi API-keys page
      - KALSHI_PRIVATE_KEY_PATH — local path to the downloaded PEM private key

    Pricing: Kalshi quotes yes/no prices in cents (1–99). A yes price of 65
    means the market implies a 65 % probability. We expose both YES and NO
    selections with their respective decimal odds so the compute engine can
    run EV analysis on either side.
    """

    name = "kalshi"
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self):
        self.api_key_id = settings.KALSHI_API_KEY_ID
        pem_path = settings.KALSHI_PRIVATE_KEY_PATH
        if not self.api_key_id or not pem_path:
            raise ValueError(
                "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env"
            )
        self._private_key = _load_private_key(pem_path)
        self.category_filter: list[str] = settings.KALSHI_CATEGORIES

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Return RSA-PSS signed request headers for the given method + path.

        Signing string: {timestamp_ms}{METHOD_UPPER}{full_url_path_no_query}
        where full_url_path includes the /trade-api/v2 prefix.
        Headers use kebab-case: KALSHI-ACCESS-KEY, etc.
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        ts = str(int(time.time() * 1000))
        # Sign the full path including /trade-api/v2 prefix, strip query params
        path_no_query = path.split("?")[0]
        sign_path = f"/trade-api/v2{path_no_query}"
        message = (ts + method.upper() + sign_path).encode()
        signature = self._private_key.sign(
            message,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        headers = self._auth_headers("GET", path)
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{self.BASE_URL}{path}",
                params=params or {},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        headers = self._auth_headers("POST", path)
        headers["Content-Type"] = "application/json"
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self.BASE_URL}{path}",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    def _delete(self, path: str) -> Any:
        headers = self._auth_headers("DELETE", path)
        with httpx.Client(timeout=10) as client:
            resp = client.delete(f"{self.BASE_URL}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json()

    def place_order(
        self,
        ticker: str,
        side: str,
        price_cents: int,
        count: int = 1,
        action: str = "buy",
    ) -> dict:
        """
        Place a limit order on Kalshi.
        action: "buy" (default) or "sell".
        Returns the order dict from the API response.
        Raises httpx.HTTPStatusError on failure.
        """
        body = {
            "ticker": ticker,
            "action": action,
            "type": "limit",
            "time_in_force": "immediate_or_cancel",
            "side": side,
            "count": count,
            f"{side}_price": int(price_cents),
        }
        data = self._post("/portfolio/orders", body)
        return data.get("order", data)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by ID."""
        data = self._delete(f"/portfolio/orders/{order_id}")
        return data.get("order", data)

    def get_order(self, order_id: str) -> dict:
        """Fetch current state of a single order."""
        data = self._get(f"/portfolio/orders/{order_id}")
        return data.get("order", data)

    def get_balance(self) -> float:
        """Return available balance in cents."""
        data = self._get("/portfolio/balance")
        val = data.get("balance", 0)
        # API returns either an int (cents) or a nested dict
        if isinstance(val, dict):
            return float(val.get("available_balance_cents", 0))
        return float(val)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_markets(self, categories: list[str] | None = None, **kwargs) -> list[Market]:
        """
        Fetch open events from Kalshi with nested markets.

        Uses GET /events?status=open&with_nested_markets=true and pages
        through all results (cursor-based pagination).

        Args:
            categories: Optional list of category names to filter by
                (e.g. ["Climate and Weather"]). Filters per page to
                avoid fetching the full catalog when only one category
                is needed. Case-insensitive.
        """
        markets: list[Market] = []
        cursor: str | None = None

        # Build effective category filter: caller arg takes priority over global setting.
        active_filter: list[str] = [c.lower() for c in (categories or self.category_filter or [])]

        while True:
            params: dict[str, Any] = {
                "status": "open",
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor

            try:
                data = self._get("/events", params)
            except httpx.HTTPStatusError as exc:
                print(f"  Kalshi HTTP {exc.response.status_code}: {exc}")
                try:
                    print(f"  Response body: {exc.response.text}")
                except Exception:
                    pass
                break
            except httpx.RequestError as exc:
                print(f"  Kalshi network error: {exc}")
                break

            events: list[dict] = data.get("events") or []

            # Apply category filter per page to avoid accumulating unwanted markets.
            if active_filter:
                events = [
                    e for e in events
                    if e.get("category", "").lower() in active_filter
                    or self._map_category(e.get("category", "")).lower() in active_filter
                ]

            for event in events:
                parsed = self._parse_event(event)
                markets.extend(parsed)

            cursor = data.get("cursor") or ""
            if not cursor:
                break

        print(f"  [kalshi] fetched {len(markets)} markets")
        return markets

    def get_markets_by_series(self, series_tickers: list[str]) -> list[Market]:
        """
        Fetch open markets for specific series tickers using GET /markets?series_ticker=X.
        Much faster than get_markets() for targeted fetches — avoids the full catalog.
        """
        markets: list[Market] = []
        for series in series_tickers:
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {
                    "status": "open",
                    "series_ticker": series,
                    "limit": 100,
                }
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self._get("/markets", params)
                except Exception as exc:
                    print(f"  [kalshi] series {series} fetch error: {exc}")
                    break
                for m in data.get("markets") or []:
                    parsed = self._market_from_dict(m, series_ticker=series)
                    if parsed:
                        markets.append(parsed)
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
        return markets

    def get_market_status(self, ticker: str) -> dict:
        """
        Return the raw status dict for a single market ticker.
        Relevant keys: status ("open"|"closed"|"settled"), result ("yes"|"no"|None).
        """
        data = self._get(f"/markets/{ticker}")
        return data.get("market", data)

    def get_odds(self, market_id: str, **kwargs) -> Market:
        """Fetch a single market by its ticker."""
        data = self._get(f"/markets/{market_id}")
        market_data = data.get("market", data)
        m = self._market_from_dict(market_data, event_category=market_data.get("category", ""))
        if m is None:
            raise ValueError(f"Could not parse Kalshi market: {market_id}")
        return m

    def get_events_raw(self, categories: list[str] | None = None) -> list[dict]:
        """
        Page through all open events and return raw event dicts (not parsed Markets).
        Used by the arb scanner which needs to inspect all markets within an event together.
        """
        events: list[dict] = []
        cursor: str | None = None

        while True:
            params: dict[str, Any] = {
                "status": "open",
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor

            try:
                data = self._get("/events", params)
            except httpx.HTTPStatusError as exc:
                print(f"  Kalshi HTTP {exc.response.status_code}: {exc}")
                break
            except httpx.RequestError as exc:
                print(f"  Kalshi network error: {exc}")
                break

            page_events: list[dict] = data.get("events") or []

            # Apply category filter if provided
            if categories:
                filter_lower = [c.lower() for c in categories]
                page_events = [
                    e for e in page_events
                    if e.get("category", "").lower() in filter_lower
                ]

            events.extend(page_events)

            cursor = data.get("cursor") or ""
            if not cursor:
                break

        return events

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_event(self, event: dict) -> list[Market]:
        """Convert a Kalshi event (with nested markets) to our Market objects."""
        nested: list[dict] = event.get("markets") or []
        # Category lives on the event, not the individual market
        event_category: str = event.get("category", "")
        event_title: str = event.get("title", "")
        event_ticker: str = event.get("event_ticker", "")
        series_ticker: str = event.get("series_ticker", "")
        mutually_exclusive: bool = event.get("mutually_exclusive", False)

        # Count markets per close_time so the arb scanner can verify exhaustiveness
        # within a single hourly bucket, not across the entire multi-hour event.
        close_time_counts: dict[str, int] = {}
        for m in nested:
            ct = m.get("close_time") or m.get("expiration_time") or ""
            close_time_counts[ct] = close_time_counts.get(ct, 0) + 1

        result: list[Market] = []
        for m in nested:
            ct = m.get("close_time") or m.get("expiration_time") or ""
            parsed = self._market_from_dict(
                m,
                event_title=event_title,
                event_category=event_category,
                event_ticker=event_ticker,
                series_ticker=series_ticker,
                mutually_exclusive=mutually_exclusive,
                total_markets_in_event=close_time_counts.get(ct, 1),
            )
            if parsed:
                result.append(parsed)
        return result

    def _market_from_dict(
        self,
        m: dict,
        event_title: str = "",
        event_category: str = "",
        event_ticker: str = "",
        series_ticker: str = "",
        mutually_exclusive: bool = False,
        total_markets_in_event: int = 0,
    ) -> Market | None:
        ticker: str = m.get("ticker", "")
        if not ticker:
            return None

        # Skip non-open markets (Kalshi uses "active" for tradeable markets)
        if m.get("status") not in ("open", "active", None):
            return None

        title: str = m.get("title") or event_title or ticker
        # Category is on the event; fall back to market-level if present
        raw_category: str = event_category or m.get("category", "")
        category: str = self._map_category(raw_category)

        # Apply category filter (case-insensitive match against Kalshi's title-case names)
        if self.category_filter:
            filter_lower = [f.lower() for f in self.category_filter]
            if raw_category.lower() not in filter_lower and category.lower() not in filter_lower:
                return None

        # Timestamps
        close_time: str | None = m.get("close_time") or m.get("expiration_time")
        starts_at: datetime | None = None
        if close_time:
            try:
                starts_at = datetime.fromisoformat(
                    close_time.rstrip("Z")
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        selections = self._parse_selections(m)
        if not selections:
            return None

        return Market(
            id=ticker,
            category=category,
            event_name=title,
            starts_at=starts_at,
            selections=selections,
            source=self.name,
            metadata={
                "volume": m.get("volume"),
                "open_interest": m.get("open_interest"),
                "liquidity": m.get("liquidity"),
                "event_ticker": event_ticker,
                "series_ticker": series_ticker,
                "mutually_exclusive": mutually_exclusive,
                "total_markets_in_event": total_markets_in_event,
                "floor_strike": m.get("floor_strike"),
                "cap_strike": m.get("cap_strike"),
            },
        )

    @staticmethod
    def _price_cents(m: dict, field: str) -> int | None:
        """
        Read a price field in cents. Handles both API formats:
          - Old: yes_ask / no_ask / yes_bid / no_bid  (integer cents)
          - New: yes_ask_dollars / no_ask_dollars etc. (float dollars, multiply × 100)
        """
        val = m.get(field)
        if val is not None:
            return int(round(float(val)))
        dollars = m.get(f"{field[:-1]}dollars" if field.endswith("_") else f"{field}_dollars")
        if dollars is None:
            # Try stripping trailing unit suffix and appending _dollars
            dollars = m.get(field + "_dollars")
        if dollars is not None:
            return int(round(float(dollars) * 100))
        return None

    def _parse_selections(self, m: dict) -> list[Selection]:
        """
        Build YES and NO selections from the market's quoted prices.
        Supports both the legacy integer-cents fields and the newer
        *_dollars float fields introduced by Kalshi.

        Both sides must have valid non-zero quotes; markets with only one
        side quoted are skipped because vig-removal requires both to work
        correctly (a single-selection market always yields prob=1.0).
        """
        def _read(field: str) -> int | None:
            # Try integer cents field first
            val = m.get(field)
            if val is not None:
                try:
                    return int(round(float(val)))
                except (TypeError, ValueError):
                    pass
            # Fall back to dollars field
            val = m.get(f"{field}_dollars")
            if val is not None:
                try:
                    return int(round(float(val) * 100))
                except (TypeError, ValueError):
                    pass
            return None

        yes_bid = _read("yes_bid")
        yes_ask = _read("yes_ask")
        no_bid  = _read("no_bid")
        no_ask  = _read("no_ask")

        # To BUY either side you only need a valid ask, so require both sides to
        # be *buyable* (non-zero ask). A missing or zero BID is fine — it's
        # normal for a near-certain outcome (e.g. a 98¢ YES whose NO side has no
        # resting bid, or a far-OTM bucket whose YES side has none).
        #
        # The previous rule required a non-zero bid AND ask on BOTH sides, which
        # silently dropped exactly those high-confidence and far-OTM buckets —
        # precisely the markets the arb and last-second strategies target — so
        # they never reached the scanner. (Dropped buckets are still counted in
        # total_markets_in_event, which also broke series-arb exhaustiveness.)
        if yes_ask is None or no_ask is None:
            return []
        if not (0 < yes_ask < 100 and 0 < no_ask < 100):
            return []

        # Price estimate per side for the legacy EV engine's odds/implied_prob:
        # the mid when a real bid exists, otherwise the ask (the only firm
        # price). When both bids are present this is identical to the old
        # behaviour, so the legacy path is unchanged for normal two-sided books.
        yes_px = (yes_bid + yes_ask) / 2 if (yes_bid and yes_bid > 0) else yes_ask
        no_px  = (no_bid + no_ask) / 2 if (no_bid and no_bid > 0) else no_ask

        yes_odds = _cents_to_decimal_odds(yes_px)
        no_odds = _cents_to_decimal_odds(no_px)

        if yes_odds <= 1.0 or no_odds <= 1.0:
            return []

        return [
            Selection(
                name="Yes",
                odds=yes_odds,
                metadata={
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "implied_prob": round(yes_px / 100, 4),
                },
            ),
            Selection(
                name="No",
                odds=no_odds,
                metadata={
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "implied_prob": round(no_px / 100, 4),
                },
            ),
        ]

    @staticmethod
    def _map_category(raw: str) -> str:
        """Normalise a Kalshi category string to our internal convention."""
        # Actual Kalshi categories (title-case): Sports, Politics, Elections,
        # Entertainment, Economics, Climate and Weather, Crypto,
        # Science and Technology, Companies, Financials, World, Social,
        # Mentions, Health, Transportation
        _MAP = {
            "politics": "politics",
            "elections": "politics/elections",
            "economics": "economics",
            "financials": "economics/financials",
            "companies": "economics/companies",
            "crypto": "crypto",
            "sports": "sports",
            "climate and weather": "weather",
            "entertainment": "entertainment",
            "science and technology": "technology",
            "world": "world",
            "social": "social",
            "health": "health",
            "transportation": "transportation",
            "mentions": "mentions",
        }
        key = raw.lower().strip()
        return _MAP.get(key, key or "other")
