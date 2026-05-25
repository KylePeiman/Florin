# Strategies

Deep-dive on the two active trading strategies: how each works, what data it uses, entry conditions, position sizing, and settlement.

---

## Table of Contents

- [Last-Second Convergence](#last-second-convergence)
- [Arbitrage (Binary + Series)](#arbitrage-binary--series)
- [Dropped Strategies](#dropped-strategies)

---

## Last-Second Convergence

### How it works

Kalshi hourly crypto series divide each asset's price into a set of mutually exclusive, collectively exhaustive price-range buckets (e.g. BTC $84,000–$85,000, $85,000–$86,000, etc.). Each bucket is a binary YES/NO market. The winning bucket is determined by the **CF Benchmarks Real-Time Index (RTI)**: a 60-second equally-weighted average of qualifying trades in the final minute before close.

If spot price has been stable and well inside a single bucket for the last 15+ seconds, the 60-second average will almost certainly land in that same bucket. The market may still be pricing YES at 70–98¢ rather than 99¢ because other traders are not yet confident or haven't acted — giving a small but reliable edge.

The edge disappears quickly as the market adjusts. Entry must happen within the first seconds after the window opens.

### Data sources

- **Kraken WebSocket** (`wss://ws.kraken.com/v2`): real-time ask prices for BTC, ETH, SOL, XRP, DOGE. No authentication required. Updates `PriceCache` on every tick.
- **Kalshi WebSocket** (`wss://api.elections.kalshi.com/trade-api/ws/v2`): RSA-PSS authenticated. Subscribes to `orderbook_delta` for near-term market tickers. Provides live YES ask prices that override stale REST values before entry.
- **REST polling fallback**: When streaming is disabled (as in pool mode), prices are fetched via REST on each scan interval. Sufficient for arb discovery and last-second entries when running many parallel workers.

Both WebSocket feeds run on background threads. The main loop blocks on a `threading.Event` that fires whenever either feed writes a new price.

### Asset-to-pair mapping

| Kalshi series prefix | Kraken pair |
|---|---|
| `KXBTC` | `XBTUSD` |
| `KXETH` | `ETHUSD` |
| `KXSOL` | `SOLUSD` |
| `KXXRP` | `XRPUSD` |
| `KXDOGE` | `DOGEUSD` |

### Entry conditions

All conditions must be satisfied simultaneously on a given price tick:

1. **Entry window**: Market closes within `--ls-entry-window` seconds (default: 120).
2. **Bucket match**: Kraken spot price falls within a specific bucket's `floor_strike`–`cap_strike` range.
3. **Edge buffer**: Spot is at least `--ls-edge-buffer` (default: 15%) of bucket width from both edges. Avoids settlement risk when spot is near a boundary.
4. **Stability**: Spot has moved less than `--ls-stability-threshold` (default: 0.3%) over the last `--ls-stability-window` (default: 15) seconds.
5. **YES trade price**: YES ask is between `--ls-min-yes` (default: 70¢) and `--ls-max-yes` (default: 98¢).
6. **NO trade price**: For buckets where spot is clearly outside, NO ask is between `--ls-min-no` (default: 3¢) and `--ls-max-no` (default: 40¢).

If a fresh YES ask is available from the Kalshi WebSocket (less than 10 seconds old), it overrides the stale REST value before the price range check.

### Position sizing

Sizing is capped at `--max-position` (default: 10%) of current liquid bankroll. Minimum is 1 contract (1¢). A single position per market ticker is enforced — no doubling up if the window stays open.

### Settlement

After each tick's entry check, the engine polls `GET /markets/{ticker}` for all open positions. A position settles only when:
- Market `status` is `finalized`, `settled`, or `closed`
- `result` is `"yes"` or `"no"` — an empty string is treated as unresolved

On a YES win: bankroll increases by `(100 - entry_price_cents) * contracts / 100`.  
On a YES loss: the staked amount is already deducted; no further change.

### Optimal settings (from live sim logs)

Derived from sessions 13–22 (2026-03-21 through 2026-03-24). Best result: session 22 grew from $1.42 → $4.66 (+$3.24, ~228%) over ~31 hours.

```bash
python -m src.cli live --simulate --last-second \
  --ls-max-yes 99 \
  --ls-stability-window 8
```

| Flag | Optimal | Default | Reason |
|---|---|---|---|
| `--ls-entry-window` | 120 | 120 | Wider window (300s) lets in too many borderline entries |
| `--ls-min-yes` | 70 | 70 | No change needed |
| `--ls-max-yes` | **99** | 98 | Capping at 92¢ rejected high-confidence bucket contracts |
| `--ls-edge-buffer` | 0.15 | 0.15 | Dropping to 8% caused losses near bucket edges |
| `--ls-stability-window` | **8** | 15 | 8s is fast enough; 15s misses valid opportunities |
| `--ls-stability-threshold` | 0.003 | 0.003 | No change needed |

**What to avoid**: a wide entry window (300s), loose edge buffer (8%), and low min_yes (60¢) together caused a $4.75 loss in a single session — entering too early on volatile 15M candles that moved against the position before settlement.

### Key implementation files

- `src/engine/last_second.py` — `PriceTracker`, `find_matching_bucket()`, `scan_last_second_opportunities()`
- `src/engine/live_sim.py` — main loop calling the scanner and settling positions
- `src/streaming/manager.py` — `StreamManager` wiring both WebSocket feeds to `PriceCache`
- `src/streaming/price_cache.py` — thread-safe `PriceCache` with `update_event`

---

## Arbitrage (Binary + Series)

### How it works

Kalshi prices occasionally allow risk-free (or near risk-free) profit by simultaneously buying contracts across a market or series.

**Binary arb**: A single Kalshi market where `yes_ask + no_ask < 100¢`. Buying 1 YES and 1 NO contract costs less than 100¢ and always pays out exactly 100¢ regardless of outcome.

**Series arb**: A price-range series (e.g. all hourly BTC buckets) where the sum of YES asks across all legs is less than 100¢. Exactly one leg always resolves YES, so buying 1 YES on every leg pays out 100¢. This is risk-free only when the series is **collectively exhaustive** — every possible outcome is covered by a liquid leg.

### Exhaustiveness check (critical)

A series arb is marked `guaranteed=True` only when:

```
len(liquid_legs) == total_markets_in_event
```

Where `total_markets_in_event` is the raw market count from the API **before** any bid/ask filtering. If some buckets have no quotes (illiquid), those buckets are gaps — spot could settle in an uncovered bucket, causing a total loss on all positions. The live engine only auto-enters `guaranteed=True` series arbs.

### Entry and sizing

Arb positions are entered as limit orders on Kalshi. If any leg fails to fill within 2 seconds, all placed legs are cancelled and the position is skipped entirely. This prevents partial fills that would create unhedged directional exposure.

Sizing: each arb uses the same `--max-position` cap as last-second trades.

### Settlement

Settlement is polled in the same loop as last-second positions. A series arb position settles when any one leg resolves — the winning leg covers all costs plus profit, losing legs each expire worthless.

### Key implementation files

- `src/engine/arbitrage.py` — `scan_binary_arb()`, `scan_series_arb()`, `opportunities_to_sim()`
- `src/engine/live_sim.py` — enforces `guaranteed=True` before entering series arbs

---

## Dropped Strategies

### Weather market edge (NWS vs Kalshi)

Compared NOAA/NWS hourly forecast probabilities against Kalshi-implied prices for temperature, precipitation, and wind markets. When the NWS-derived probability diverged from the Kalshi price by more than a configurable edge, a position was entered on the favored side.

Dropped because: the NWS API data is coarse (hourly forecasts, ~27 supported cities), and Kalshi weather markets are thinly traded — fills were unreliable and the edge was inconsistent. All `src/weather/` code has been removed.

### 15-minute directional crypto bets (Claude agent)

The legacy agent mode used Claude to evaluate Kalshi crypto markets and place directional bets based on macro reasoning and news. Abandoned after consistently negative results — crypto prediction markets are too efficient for agent-based directional edge. Code preserved in `src/engine/agent_mode.py` but not called from the active simulation loop.

### EV recommendation engine

An earlier approach: compute EV across all Kalshi markets using Kelly criterion sizing and store recommendations for later settlement tracking. Replaced by the arb-only and last-second strategies. The `run` / `simulate run` / `evaluate` command group is preserved for historical tracking.
