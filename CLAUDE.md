# CLAUDE.md — Betting App

## Project Overview
Python CLI application that scans Kalshi prediction markets for arbitrage opportunities, executes real or paper trades, and tracks performance over time.

## Rules
- Claude should only work in the Betting-App directory. Claude should never work in any directory that is in any parent folders.
- Any code changes must be committed to GitHub with a clear description of what was changed and why.

## Architecture
- `src/fetchers/kalshi.py` — Kalshi REST API: market fetch, RSA-PSS auth, order placement
- `src/engine/arbitrage.py` — binary + series arb detection with exhaustiveness check
- `src/engine/live_sim.py` — continuous arb loop: scan → enter → settle → bankroll tracking
- `src/engine/simulator.py` — one-shot EV paper-trade simulator (legacy)
- `src/engine/pipeline.py` — recommendation engine entry point (legacy)
- `src/storage/models.py` — ORM: SimSession, SimPosition, ArbSimulation, Recommendation, etc.
- `src/storage/db.py` — SQLAlchemy session factory + auto-migration for new columns
- `src/cli.py` — Click CLI

## Key Commands
```bash
# Primary: arb scanner
python -m src.cli live --simulate                  # paper trade, $5 default bankroll
python -m src.cli live --live                      # real orders, auto-detects balance
python -m src.cli live --live --resume <id>        # resume a session

# Session inspection
python -m src.cli simulate sessions                # list sessions with P&L
python -m src.cli arb scan                        # one-shot scan, no trades

# Legacy recommendation engine
python -m src.cli run --mode compute --period week
python -m src.cli run --mode agent --period week
python -m src.cli evaluate
python -m src.cli recommendations list
python -m src.cli recommendations settle <id> --result win
```

## Kalshi Auth
- Base URL: `https://api.elections.kalshi.com/trade-api/v2`
- Sign string: `{timestamp_ms}{METHOD_UPPER}/trade-api/v2{/path}` (full path including prefix)
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- RSA-PSS SHA-256, `salt_length=PSS.DIGEST_LENGTH`
- Price fields: new API uses `yes_ask_dollars` (float, dollars); `_parse_selections` handles both formats

## Arb Scanner — Critical Logic
- Series arb is only `guaranteed=True` when `len(liquid_legs) == total_markets_in_event`
- `total_markets_in_event` is the **raw** market count from the API **before** bid/ask filtering
- This prevents partial-coverage positions where illiquid/unquoted buckets cause losses
- Only enter series arbs where `guaranteed=True` (enforced in `live_sim.py`)

## Order Placement
- `POST /portfolio/orders` — body: `{ticker, action:"buy", type:"limit", side, count, yes_price or no_price}`
- Response status: `"executed"` (not `"filled"`); fill count field: `fill_count` (not `filled_count`)
- If any leg fails to fill within 2s, all placed legs are cancelled and position is skipped
- Balance: `GET /portfolio/balance` → `{"balance": <int cents>}`

## Adding a New Fetcher
1. Create `src/fetchers/your_source.py`
2. Implement `BaseFetcher` (`.get_markets()` and `.get_odds()`)
3. Register in `FETCHER_MAP` in `src/engine/pipeline.py`
4. Add API key to `config/settings.py` and `.env.example`

## Models
- `SimSession` — a live/sim run with bankroll tracking
- `SimPosition` — individual position within a session (supports `live`, `order_ids` fields)
- `ArbSimulation` — one-shot arb record (used by `arb simulate/settle/report`)
- `Recommendation` — legacy EV recommendation
- `Outcome` / `EvaluationReport` — legacy settlement and reporting
