"""
Kraken WebSocket v2 client — public feed, no auth required.

Streams real-time trade data for crypto pairs and writes last-trade prices to PriceCache.
Using the 'trade' channel (not 'ticker') because trade fires on every executed trade,
giving many observations per second for liquid pairs. The 'ticker' channel only fires
when the best bid/ask changes, which on these pairs was ~1 update per 20s — not enough
observations for the 15-second stability window to ever see >= 2 points.

Kraken WS v2 docs: https://docs.kraken.com/api/docs/websocket-v2/trade
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .price_cache import PriceCache

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.kraken.com/v2"

# Kraken WS v2 symbol format uses "/" separator (e.g. "BTC/USD")
_PAIR_TO_SYMBOL: dict[str, str] = {
    "XBTUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "SOLUSD": "SOL/USD",
    "XRPUSD": "XRP/USD",
    "DOGEUSD": "DOGE/USD",
}
# Reverse map for parsing incoming messages
_SYMBOL_TO_PAIR: dict[str, str] = {v: k for k, v in _PAIR_TO_SYMBOL.items()}

RECONNECT_DELAY_S = 5


class KrakenWsClient:
    """
    Runs in a background thread. Connects to Kraken WS v2, subscribes to
    the 'ticker' channel for all configured pairs, and writes ask prices
    to the shared PriceCache.
    """

    def __init__(self, cache: "PriceCache", pairs: set[str] | None = None) -> None:
        self._cache = cache
        self._pairs = pairs or set(_PAIR_TO_SYMBOL.keys())
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="kraken-ws", daemon=True
        )
        self._thread.start()
        logger.info("[KrakenWS] started (pairs: %s)", sorted(self._pairs))

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[KrakenWS] stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_loop())
        finally:
            loop.close()

    async def _connect_loop(self) -> None:
        import websockets

        symbols = [_PAIR_TO_SYMBOL[p] for p in self._pairs if p in _PAIR_TO_SYMBOL]
        subscribe_msg = json.dumps({
            "method": "subscribe",
            "params": {
                "channel": "trade",
                "symbol": symbols,
            },
        })

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    logger.info("[KrakenWS] connected")
                    await ws.send(subscribe_msg)
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle(raw)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("[KrakenWS] disconnected (%s), reconnecting in %ds", exc, RECONNECT_DELAY_S)
                await asyncio.sleep(RECONNECT_DELAY_S)

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Kraken WS v2 trade messages:
        # {"channel": "trade", "type": "update", "data": [{"symbol": "BTC/USD", "price": 66500.0, ...}]}
        if msg.get("channel") != "trade":
            return
        for item in msg.get("data", []):
            symbol = item.get("symbol", "")
            price = item.get("price")
            if price is not None:
                pair = _SYMBOL_TO_PAIR.get(symbol)
                if pair:
                    self._cache.set_spot(pair, float(price))
