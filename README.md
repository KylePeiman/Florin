# Kalshi Sniper

> A Python CLI application that trades Kalshi prediction markets using real-time price data, last-second convergence, and micro-arbitrage detection. Supports paper trading and live order placement, with a parallelized worker-pool mode for capital efficiency.

## Table of Contents

- [Overview](#overview)
- [Strategies](#strategies)
- [Worker Pool](#worker-pool)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [Architecture](#architecture)
- [Dashboard](#dashboard)
- [Development](#development)

---

## Overview

Kalshi Sniper scans Kalshi prediction markets continuously and executes trades when a quantifiable edge is found. Two active strategies run in every session:

- **Last-second convergence** — enters the bucket containing a stable Kraken spot price in the final 120 seconds before a crypto series closes
- **Micro-arbitrage** — detects binary and series arbs where the sum of YES asks is less than 100¢

Both strategies work in paper-trade (simulation) mode or with real Kalshi orders. Sessions, positions, and P&L are persisted in a local SQLite database and published to a [GitHub Pages dashboard](https://kylepeiman.github.io/Betting-App/).

The **worker pool** is the recommended way to run: instead of one session with a large bankroll, it spawns `floor(bankroll / $1)` parallel $1 sessions. As each worker compounds to $10 it resets and funds new workers, increasing throughput proportionally to total capital.

---

## Strategies

### Last-Second Convergence

Kalshi hourly crypto series divide each asset's price into mutually exclusive buckets (e.g. BTC $84,000–$85,000). Each bucket resolves on the CF Benchmarks 60-second equally-weighted average in the final minute before close. If the Kraken spot price has been stable and well inside a bucket for the preceding 15+ seconds, the settlement average will almost certainly land in the same bucket — yet the YES ask may still be 70–98¢, providing a measurable edge.

Entry conditions (all must be true on a given price tick):
- Market closes within `--ls-entry-window` seconds (default: 120)
- Spot price falls within a bucket's `floor_strike`–`cap_strike` range
- Spot is at least `--ls-edge-buffer` (default: 15%) of bucket width from both edges
- Spot has moved less than `--ls-stability-threshold` (0.3%) over the last `--ls-stability-window` (15s)
- YES ask is between `--ls-min-yes` (70¢) and `--ls-max-yes` (98¢)

See [docs/strategies.md](docs/strategies.md) for full entry logic, sizing, optimal settings, and settlement details.

### Arbitrage (Binary + Series)

Scans for riskless arbitrage across Kalshi markets:

- **Binary arb**: A single market where `yes_ask + no_ask < 100`. Buying both sides guarantees 100¢ payout regardless of outcome.
- **Series arb**: A price-range series where `sum(yes_asks) < 100`. Risk-free only when the series is collectively exhaustive — `guaranteed=True` requires the number of liquid legs to equal the total raw market count before any bid/ask filtering. Partial-coverage series are never auto-entered.

See [docs/strategies.md](docs/strategies.md) for the exhaustiveness logic and sizing rules.

---

## Worker Pool

The pool mode addresses a core limitation of single large sessions: each arb or last-second opportunity is tiny (cents per contract), so a $50 session can't deploy capital any faster than a $1 session — it just holds more idle cash.

**How it works:**
1. Start with `--pool --bankroll N` — spawns `floor(N)` parallel workers, each starting with $1
2. Every worker runs its own independent scan loop (no shared state beyond the DB)
3. When a worker's total value (liquid + locked) reaches $10, the supervisor graduates it — stops it, banks the $9 profit, and spawns enough new $1 workers to match the new `floor(total_capital / $1)` target
4. Capital compounds: $5 → 5 workers → if each grows to $10 → bank $45 → 45 workers

```bash
# Start a pool with $10 — spawns 10 parallel $1 workers
python -m src.cli live --simulate --pool --bankroll 10

# Same with real orders (prompts for confirmation)
python -m src.cli live --live --pool --bankroll 10
```

**Notes:**
- Pool mode always uses REST polling (not WebSocket streaming) — opening N simultaneous WS connections causes HTTP 429 rate-limit storms
- Each worker writes its own log file: `logs/pool{id}_w{idx}_{ts}.log`
- The supervisor checks every 2 seconds for graduations and spawns

---

## Getting Started

```bash
# 1. Clone the repository
git clone <repo-url>
cd Betting-App

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env — at minimum set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH

# 4. Run a paper-trade pool session ($5 = 5 parallel workers)
python -m src.cli live --simulate --pool --bankroll 5
```

Get Kalshi API credentials from [kalshi.com/profile/api-keys](https://kalshi.com/profile/api-keys). Download the PEM private key file and set `KALSHI_PRIVATE_KEY_PATH` to its absolute path.

---

## Configuration

All settings are loaded from `.env` via `python-dotenv`. Copy `.env.example` to `.env` and fill in the values below.

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | *(required)* | API key ID from your Kalshi profile |
| `KALSHI_PRIVATE_KEY_PATH` | *(required)* | Absolute path to the downloaded PEM private key |
| `KALSHI_CATEGORIES` | *(all)* | Comma-separated category filter, e.g. `Crypto,Economics`. Leave blank to fetch all. |
| `DATABASE_URL` | `sqlite:///betting_app.db` | SQLAlchemy database URL. Swap to Postgres via `postgresql://...` |
| `ANTHROPIC_API_KEY` | *(optional)* | Required only for `--prediction` mode (Claude headline trades) |
| `NEWS_API_KEY` | *(optional)* | Required only for `--prediction` mode (NewsAPI headlines) |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model used by prediction mode |
| `MIN_EV_THRESHOLD` | `0.005` | Minimum expected value threshold for the legacy recommendation engine |
| `DEFAULT_SOURCES` | `kalshi` | Default data sources for the legacy `run` command |
| `GH_GIST_TOKEN` | *(optional)* | GitHub token for publishing live session data to the dashboard Gist |
| `GH_GIST_ID` | *(preset)* | GitHub Gist ID for the live dashboard data feed |

---

## CLI Reference

### Root command — `python -m src.cli`

Quickest way to start a session. Uses sensible defaults for all strategy parameters.

```bash
python -m src.cli --simulate                       # paper trade, last-second on
python -m src.cli --live                           # real orders, auto-detects Kalshi balance
python -m src.cli --simulate --prediction          # paper trade + Claude headline trades
```

| Flag | Default | Description |
|---|---|---|
| `--simulate` | — | Paper trade — no real orders placed |
| `--live` | — | Place real orders on Kalshi |
| `--bankroll FLOAT` | `5.00` (sim) / account balance (live) | Starting bankroll in USD |
| `--last-second` / `--no-last-second` | on | Enable/disable last-second convergence strategy |
| `--streaming` / `--no-streaming` | on | Use WebSocket feeds; `--no-streaming` falls back to REST polling |
| `--prediction` | off | Enable Claude + NewsAPI headline prediction trades |

---

### `live` — full options

```bash
python -m src.cli live --simulate [OPTIONS]
python -m src.cli live --live [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--simulate` / `--live` | *(required)* | Paper trade or real orders |
| `--bankroll FLOAT` | `5.00` / account balance | Starting bankroll in USD |
| `--pool` | off | Worker pool mode — see [Worker Pool](#worker-pool) |
| `--interval INT` | `15` | Seconds between full REST market list scans |
| `--settle-interval INT` | `5` | Seconds between settlement polls while idle |
| `--categories TEXT` | `Crypto,Economics,Financials` | Comma-separated Kalshi categories to scan |
| `--near-term INT` | `60` | Only consider markets closing within this many minutes |
| `--max-position FLOAT` | `0.10` | Max fraction of bankroll per single position |
| `--logs-dir TEXT` | `logs` | Directory for session log files |
| `--resume INT` | — | Resume an existing session by ID (incompatible with `--pool`) |
| `--last-second` / `--no-last-second` | on | Enable/disable last-second strategy |
| `--streaming` / `--no-streaming` | on | Enable/disable WebSocket streaming (disabled automatically in pool mode) |
| `--prediction` | off | Enable headline prediction trades |
| `--ls-entry-window INT` | `120` | Seconds before close to begin monitoring for entries |
| `--ls-min-yes INT` | `70` | Minimum YES ask in cents to enter a YES trade |
| `--ls-max-yes INT` | `98` | Maximum YES ask in cents to enter a YES trade |
| `--ls-min-no INT` | `3` | Minimum NO ask in cents to enter a NO trade |
| `--ls-max-no INT` | `40` | Maximum NO ask in cents to enter a NO trade |
| `--ls-edge-buffer FLOAT` | `0.15` | Fraction of bucket width spot must be from both edges |
| `--ls-stability-window INT` | `15` | Seconds of price history used for stability check |
| `--ls-stability-threshold FLOAT` | `0.003` | Max allowed price movement in stability window (0.3%) |
| `--ls-directional-margin FLOAT` | `0.003` | Min pct spot must be above/below floor_strike for directional entries |

**Examples:**

```bash
# Worker pool — 5 parallel $1 workers, each resets at $10
python -m src.cli live --simulate --pool --bankroll 5

# Single session, narrow entry window, bigger bankroll
python -m src.cli live --simulate --ls-entry-window 60 --bankroll 20

# Real orders with live balance auto-detection
python -m src.cli live --live

# Resume a previous session
python -m src.cli live --live --resume 4

# REST polling only (no WebSocket)
python -m src.cli live --simulate --no-streaming
```

---

### `simulate` — paper-trade management

```bash
# List all live simulation sessions with P&L
python -m src.cli simulate sessions [--limit 10]

# List legacy simulated bets
python -m src.cli simulate list [--status open|settled|expired] [--limit 30]

# Auto-settle resolved legacy paper-trade bets
python -m src.cli simulate settle [--quiet]

# Show aggregate performance report for legacy paper trades
python -m src.cli simulate report
```

---

### `arb` — arbitrage scanning

```bash
# One-shot scan, print results — no trades placed
python -m src.cli arb scan [--categories TEXT] [--min-profit FLOAT] [--type all|binary|series]

# Record current arb opportunities as simulated trades
python -m src.cli arb simulate

# Auto-settle resolved arb simulations
python -m src.cli arb settle [--quiet]

# List recorded arb simulations
python -m src.cli arb list [--status open|won|lost|voided] [--limit 30]

# Aggregate P&L report
python -m src.cli arb report
```

---

### `cross-arb` — Kalshi vs Polymarket cross-platform arbitrage

```bash
python -m src.cli cross-arb scan [--categories TEXT] [--min-profit FLOAT] [--min-match FLOAT] [--show-unmatched]
```

---

### `recommendations` — legacy recommendation engine

```bash
python -m src.cli run [--mode compute|agent] [--period week|month] [--categories TEXT] [--min-ev FLOAT]
python -m src.cli recommendations list [--limit 20] [--status pending|settled]
python -m src.cli recommendations show <ID>
python -m src.cli recommendations settle <ID> --result win|loss|void
python -m src.cli evaluate [--from YYYY-MM-DD] [--to YYYY-MM-DD]
```

---

### Utility scripts

```bash
# Export dashboard data to docs/data.json and push to Gist
python scripts/export_dashboard_data.py

# Wipe all sessions, positions, and records (prompts for confirmation)
python scripts/clear_db.py
```

---

## Architecture

```
src/
├── cli.py                       # Click CLI — all command groups and options
├── fetchers/
│   ├── base.py                  # BaseFetcher ABC, Market/Selection dataclasses
│   ├── kalshi.py                # Kalshi REST API — RSA-PSS auth, market fetch, order placement
│   ├── polymarket.py            # Polymarket read + order placement (Polygon)
│   ├── crypto_prices.py         # Kraken REST price fetch (streaming fallback)
│   └── news.py                  # NewsAPI client for headline prediction trades
├── engine/
│   ├── live_sim.py              # Main simulation loop — entry, settlement, bankroll tracking
│   ├── worker_pool.py           # WorkerPool supervisor — parallel $1 sessions, graduation logic
│   ├── last_second.py           # Last-second strategy: PriceTracker, bucket matching, scanner
│   ├── arbitrage.py             # Binary + series arb detection, exhaustiveness check
│   ├── cross_arb.py             # Cross-platform arb: Kalshi vs Polymarket
│   ├── prediction.py            # Headline signal detection + Claude review
│   ├── pipeline.py              # Legacy recommendation engine entry point
│   ├── compute_mode.py          # Legacy EV + Kelly sizing
│   └── agent_mode.py            # Legacy Claude multi-turn tool loop (unused)
├── streaming/
│   ├── price_cache.py           # Thread-safe cache: spot prices + yes_ask, update_event
│   ├── kraken_ws.py             # Kraken WebSocket client (public, no auth)
│   ├── kalshi_ws.py             # Kalshi WebSocket client (RSA-PSS authenticated)
│   └── manager.py               # StreamManager — starts/stops both feeds
├── storage/
│   ├── models.py                # ORM: SimSession, SimPosition, ArbSimulation, Recommendation, etc.
│   └── db.py                    # SQLAlchemy session factory + additive auto-migration
└── evaluator/
    └── performance.py           # Historical recommendation performance evaluation
config/
└── settings.py                  # All env vars loaded via python-dotenv
scripts/
├── export_dashboard_data.py     # Export DB → docs/data.json + push to GitHub Gist
└── clear_db.py                  # Wipe all DB records (with confirmation prompt)
docs/
├── strategies.md                # Deep-dive: entry conditions, sizing, settlement, optimal settings
└── kalshi-auth.md               # RSA-PSS auth, sign string format, header names, common errors
```

### Key files

| File | Purpose |
|---|---|
| `src/cli.py` | All CLI commands. Entry point for every user-facing action. |
| `src/engine/live_sim.py` | The main loop — fetching, last-second scanning, settlement, bankroll. Accepts `stop_event` for pool-worker control. |
| `src/engine/worker_pool.py` | Supervisor thread — spawns workers, checks $10 graduation threshold, banks capital, controls shutdown. |
| `src/engine/last_second.py` | `PriceTracker`, `find_matching_bucket()`, per-tick entry decision logic. |
| `src/engine/arbitrage.py` | `scan_binary_arb()`, `scan_series_arb()`, exhaustiveness check. |
| `src/fetchers/kalshi.py` | Every Kalshi API call: market list, event fetch, order placement, balance, market status. |
| `src/streaming/manager.py` | Manages the Kraken + Kalshi WebSocket threads and the shared `PriceCache`. |
| `src/storage/models.py` | All ORM models — `SimSession` now carries `pool_id` and `worker_index` for pool tracking. |
| `config/settings.py` | Single source of truth for all env vars and their defaults. |

### Data model notes

`SimSession` tracks one trading session. Fields added for pool support:
- `pool_id` — integer timestamp linking all workers in a pool run
- `worker_index` — 0-based index within the pool (for log file naming and identification)

`SimPosition` tracks individual positions within a session, with `live` flag to separate real from paper trades.

---

## Dashboard

The live dashboard is published at **[kylepeiman.github.io/Betting-App](https://kylepeiman.github.io/Betting-App/)** — a static GitHub Pages site that fetches data from a GitHub Gist updated on every `git push`.

### Updating the dashboard manually

```bash
python scripts/export_dashboard_data.py
```

This writes `docs/data.json` locally and PATCHes the Gist (requires `GH_GIST_TOKEN` in `.env`). The dashboard auto-refreshes every 30 seconds.

### What the dashboard shows

- **Portfolio hero** — total capital across running sessions, all-time P&L, win rate, live Kalshi balance
- **Worker Pools** — one row per pool run with aggregate workers/capital/P&L; scoped to the active Live or Simulated tab
- **Sessions** — one grouped row per pool (click to expand individual workers), then solo sessions below
- **Open Positions** — current locked positions from active sessions
- **Trade History** — settled trades with per-leg detail, cumulative P&L chart, strategy breakdown bar chart

---

## Development

### Running tests

```bash
python -m pytest tests/
python -m pytest tests/test_last_second.py    # last-second strategy unit tests
```

### Adding a new strategy

1. Create `src/<strategy_name>/` with its own module files.
2. Do not modify `live_sim.py` or other running-strategy files — add a new loop function.
3. Add a CLI command group in `src/cli.py`.
4. Add any new env vars to `config/settings.py` and `.env.example`.

### Adding a new fetcher

1. Create `src/fetchers/your_source.py`.
2. Implement `BaseFetcher` — `.get_markets()` and `.get_odds()`.
3. Register in `FETCHER_MAP` in `src/engine/pipeline.py`.
4. Add API key settings to `config/settings.py` and `.env.example`.

### Kalshi categories

Pass any of these to `--categories` (comma-separated, title-case):

| Category | Notes |
|---|---|
| `Crypto` | Hourly BTC/ETH/SOL/XRP/DOGE price-range buckets — primary last-second target |
| `Economics` | Macro indicator series |
| `Financials` | Index and rate series |
| `Companies` | Earnings and stock price series |
| `Politics` | Election and policy markets |
| `Sports`, `Entertainment` | Event-driven markets |

Default: `Crypto,Economics,Financials`
