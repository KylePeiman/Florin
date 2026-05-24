"""Weather strategy loop — scan NWS-edge markets, enter positions, settle.

Continuously scans Kalshi weather markets for NWS forecast edge,
enters positions when edge exceeds a threshold, settles them when
the market resolves, and hourly rechecks open positions to scale up
or exit based on updated forecasts.

Usage (via CLI or direct call)::

    run_weather_strategy(live=False, bankroll_cents=500)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import IO, Any

from src.fetchers.kalshi import KalshiFetcher
from src.storage.db import get_session
from src.storage.models import SimPosition, SimSession
from src.weather.scanner import scan_weather_markets

RECHECK_INTERVAL = 3600  # re-evaluate open positions every hour


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(f: IO[str], msg: str) -> None:
    """Write a timestamped line to both stdout and the log file.

    Args:
        f: Open log file handle.
        msg: Message to log.
    """
    line = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line)
    f.write(line + "\n")
    f.flush()


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def _settle_positions(
    open_positions: list[SimPosition],
    fetcher: KalshiFetcher,
    session: SimSession,
    db: Any,
    log_file: IO[str],
) -> list[SimPosition]:
    """Settle resolved positions and return those still open.

    For each open position, polls the Kalshi market status.  If the
    market has resolved to ``"yes"`` or ``"no"``, the position is
    settled and the session bankroll / win-loss counters are updated.

    Args:
        open_positions: List of ``SimPosition`` objects with
            ``status == "open"``.
        fetcher: Authenticated ``KalshiFetcher`` for market lookups.
        session: The parent ``SimSession`` whose bankroll to update.
        db: SQLAlchemy session for persistence.
        log_file: Open log file handle.

    Returns:
        A list of positions that remain open (unresolved).
    """
    still_open: list[SimPosition] = []
    now = datetime.now(timezone.utc)

    for pos in open_positions:
        try:
            mkt = fetcher.get_market_status(pos.ticker)
        except Exception as exc:
            _log(log_file, f"  [ERR] fetch {pos.ticker}: {exc}")
            still_open.append(pos)
            continue

        result = mkt.get("result") or ""
        if result not in ("yes", "no"):
            still_open.append(pos)
            continue

        # Determine outcome based on position side vs market result.
        if result == "yes":
            won = pos.side == "yes"
        else:
            won = pos.side == "no"

        if won:
            pnl_cents = (
                (100 - pos.entry_price_cents) * pos.contracts
            )
            pos.status = "won"
            pos.pnl_cents = pnl_cents
            session.current_bankroll_cents += pos.cost_cents + pnl_cents
            session.won += 1
        else:
            pnl_cents = -pos.cost_cents
            pos.status = "lost"
            pos.pnl_cents = pnl_cents
            session.lost += 1

        pos.result = result
        pos.settled_at = now

        label = "WON " if won else "LOST"
        _log(
            log_file,
            f"  SETTLE {label} {pos.ticker} ({pos.side.upper()}) "
            f"| P&L={pnl_cents:+.0f}c  "
            f"bankroll=${session.current_bankroll_cents / 100:.4f}",
        )
        db.commit()

    return still_open


# ---------------------------------------------------------------------------
# Position exit
# ---------------------------------------------------------------------------

def _exit_position(
    pos: SimPosition,
    exit_price_cents: int,
    session: SimSession,
    fetcher: KalshiFetcher,
    live: bool,
    db: Any,
    log_file: IO[str],
) -> bool:
    """Sell out of an open weather position at the current implied bid.

    For live sessions, places an IOC sell order and returns False (hold)
    if it doesn't fill.  For sim sessions, always succeeds.

    Args:
        pos: The open ``SimPosition`` to exit.
        exit_price_cents: Implied bid price in cents (``100 - opposite_ask``).
        session: The parent ``SimSession`` whose bankroll to update.
        fetcher: Authenticated ``KalshiFetcher`` for live sell orders.
        live: If ``True``, place a real Kalshi sell order.
        db: SQLAlchemy session for persistence.
        log_file: Open log file handle.

    Returns:
        ``True`` if the position was exited, ``False`` if the sell did not fill.
    """
    exit_price_cents = max(1, exit_price_cents)
    contracts = pos.contracts

    if live and pos.live:
        try:
            order = fetcher.place_order(
                ticker=pos.ticker,
                side=pos.side,
                price_cents=exit_price_cents,
                count=contracts,
                action="sell",
            )
            filled = order.get("fill_count", 0)
            status = order.get("status", "")
            if status in ("executed", "filled") and not filled:
                filled = contracts
            if filled == 0:
                _log(log_file, f"  [EXIT] {pos.ticker} sell not filled — holding")
                return False
            contracts = filled
        except Exception as exc:
            _log(log_file, f"  [EXIT ERR] {pos.ticker}: {exc} — holding")
            return False

    recovered = exit_price_cents * contracts
    pnl_cents = recovered - pos.cost_cents

    pos.status = "exited"
    pos.pnl_cents = pnl_cents
    pos.settled_at = datetime.now(timezone.utc)
    session.current_bankroll_cents += recovered
    if pnl_cents >= 0:
        session.won += 1
    else:
        session.lost += 1
    db.commit()

    _log(
        log_file,
        f"  EXIT {pos.ticker} ({pos.side.upper()}) @ {exit_price_cents}c x{contracts}"
        f" | P&L={pnl_cents:+.0f}c  bankroll=${session.current_bankroll_cents / 100:.4f}",
    )
    return True


# ---------------------------------------------------------------------------
# Position recheck (hourly)
# ---------------------------------------------------------------------------

def _recheck_positions(
    open_positions: list[SimPosition],
    fetcher: KalshiFetcher,
    session: SimSession,
    db: Any,
    log_file: IO[str],
    live: bool,
    min_edge: float,
    scale_up_threshold: float,
) -> tuple[list[SimPosition], list[dict]]:
    """Re-evaluate open weather positions against fresh NWS + Kalshi data.

    For each open weather position, fetches the current NWS probability and
    Kalshi ask prices.  Exits positions where the edge has flipped past
    ``min_edge`` in the wrong direction.  Returns scale-up opportunities for
    positions where the edge has grown by at least ``scale_up_threshold``.

    Args:
        open_positions: Currently open positions for this session.
        fetcher: Authenticated ``KalshiFetcher``.
        session: The parent ``SimSession``.
        db: SQLAlchemy session.
        log_file: Open log file handle.
        live: Whether to place real Kalshi orders when exiting.
        min_edge: Minimum edge used for initial entry.
        scale_up_threshold: How much the edge must grow beyond ``entry_edge``
            to trigger a scale-up (e.g. 0.10 = 10 percentage points).

    Returns:
        A tuple of ``(still_open, scale_up_opps)`` where ``scale_up_opps``
        are opportunity dicts (same shape as ``scan_weather_markets`` output)
        ready to pass to ``_enter_position``.
    """
    weather_positions = [p for p in open_positions if p.arb_type == "weather"]
    if not weather_positions:
        return open_positions, []

    _log(log_file, f"Rechecking {len(weather_positions)} open weather position(s)...")

    try:
        # min_edge=0 so we get fresh data for all markets, even ones below threshold
        all_markets = scan_weather_markets(fetcher, min_edge=0.0)
    except Exception as exc:
        _log(log_file, f"  [RECHECK ERR] scan failed: {exc}")
        return open_positions, []

    market_lookup: dict[str, dict] = {opp["market"].id: opp for opp in all_markets}

    still_open = list(open_positions)
    scale_up_opps: list[dict] = []

    for pos in weather_positions:
        current = market_lookup.get(pos.ticker)
        if current is None:
            _log(log_file, f"  [RECHECK] {pos.ticker} not in scan (expired?) — skipping")
            continue

        nws_prob: float = current["nws_prob"]
        kalshi_prob: float = current["kalshi_prob"]
        yes_ask: int = current["market"].selections[0].metadata.get("yes_ask", 0)
        no_ask: int = current["market"].selections[0].metadata.get("no_ask", 0)

        # Edge in our direction: positive = still favourable, negative = flipped
        if pos.side == "yes":
            current_edge = nws_prob - kalshi_prob
            exit_price = max(1, 100 - no_ask) if no_ask > 0 else 1
        else:
            current_edge = kalshi_prob - nws_prob
            exit_price = max(1, 100 - yes_ask) if yes_ask > 0 else 1

        entry_edge = pos.entry_edge or 0.0

        _log(
            log_file,
            f"  [RECHECK] {pos.ticker} side={pos.side} "
            f"entry_edge={entry_edge:.2f} current_edge={current_edge:+.2f} "
            f"nws={nws_prob:.2f} kalshi={kalshi_prob:.2f}",
        )

        # Edge has flipped against us past min_edge — exit
        if current_edge < -min_edge:
            _log(log_file, f"  [RECHECK] {pos.ticker} edge flipped — EXITING")
            exited = _exit_position(pos, exit_price, session, fetcher, live, db, log_file)
            if exited:
                still_open = [p for p in still_open if p.id != pos.id]
            continue

        # Edge grown enough in our direction and side still matches — scale up
        if current_edge >= entry_edge + scale_up_threshold and current["side"] == pos.side:
            _log(
                log_file,
                f"  [RECHECK] {pos.ticker} edge grew {entry_edge:.2f} → {current_edge:.2f} — SCALE UP",
            )
            scale_up_opps.append(current)

    return still_open, scale_up_opps


# ---------------------------------------------------------------------------
# Position entry
# ---------------------------------------------------------------------------

def _enter_position(
    opp: dict[str, Any],
    session: SimSession,
    fetcher: KalshiFetcher,
    live: bool,
    db: Any,
    log_file: IO[str],
) -> None:
    """Open a new weather position from a scanner opportunity.

    Calculates contract count (2.5 % of bankroll per position),
    creates a ``SimPosition``, deducts cost from the session
    bankroll, and optionally places a real Kalshi order.

    Args:
        opp: Opportunity dict from ``scan_weather_markets`` with
            keys ``market``, ``side``, ``ask_cents``.
        session: Active ``SimSession``.
        fetcher: Authenticated ``KalshiFetcher`` for live orders.
        live: If ``True``, place a real Kalshi order.
        db: SQLAlchemy session.
        log_file: Open log file handle.
    """
    ask_cents: int = opp["ask_cents"]
    if ask_cents <= 0:
        _log(log_file, f"  SKIP {opp['market'].id}: ask_cents={ask_cents}")
        return

    contracts = max(
        1,
        int(session.current_bankroll_cents * 0.025 / ask_cents),
    )
    cost_cents = contracts * ask_cents

    if cost_cents > session.current_bankroll_cents:
        _log(
            log_file,
            f"  SKIP {opp['market'].id}: cost={cost_cents}c "
            f"> bankroll={session.current_bankroll_cents:.0f}c",
        )
        return

    ticker = opp["market"].id
    side = opp["side"]

    position = SimPosition(
        session_id=session.id,
        ticker=ticker,
        side=side,
        entry_price_cents=ask_cents,
        cost_cents=cost_cents,
        contracts=contracts,
        arb_type="weather",
        status="open",
        live=int(live),
        entry_nws_prob=opp.get("nws_prob"),
        entry_edge=opp.get("edge"),
    )
    db.add(position)

    session.current_bankroll_cents -= cost_cents
    session.total_trades += 1
    db.commit()

    # Place real order if running live.
    if live:
        try:
            order = fetcher.place_order(
                ticker=ticker,
                side=side,
                price_cents=ask_cents,
                count=contracts,
            )
            order_id = order.get("order_id") or order.get("id", "")
            filled = order.get("fill_count") or order.get(
                "filled_count", 0
            )
            status = order.get("status", "")

            if status in ("executed", "filled") and not filled:
                filled = contracts

            if filled > 0:
                position.order_ids = json.dumps([order_id])
                _log(
                    log_file,
                    f"  [LIVE] filled {ticker} ({side}) "
                    f"x{filled}/{contracts} @ {ask_cents}c "
                    f"order={order_id}",
                )
            else:
                _log(
                    log_file,
                    f"  [LIVE] no fill {ticker} ({side}) "
                    f"x{contracts} @ {ask_cents}c — reverting",
                )
                # Revert bankroll and mark position voided.
                session.current_bankroll_cents += cost_cents
                session.total_trades -= 1
                position.status = "voided"
                session.voided += 1
                db.commit()
                return

            db.commit()
        except Exception as exc:
            _log(
                log_file,
                f"  [LIVE] order FAILED {ticker} ({side}): {exc} "
                f"— reverting",
            )
            session.current_bankroll_cents += cost_cents
            session.total_trades -= 1
            position.status = "voided"
            session.voided += 1
            db.commit()
            return

    _log(
        log_file,
        f"  ENTER {ticker} side={side} ask={ask_cents}c "
        f"x{contracts} cost={cost_cents}c "
        f"bankroll=${session.current_bankroll_cents / 100:.4f}",
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_weather_strategy(
    live: bool,
    bankroll_cents: float,
    interval_seconds: int = 3600,
    min_edge: float = 0.05,
    scale_up_threshold: float = 0.10,
    session_id: int | None = None,
) -> None:
    """Run the weather-edge strategy in a continuous loop.

    Scans Kalshi weather markets for NWS forecast edge, enters
    positions when edge exceeds ``min_edge``, and settles them
    when the underlying market resolves.

    Args:
        live: If ``True``, place real Kalshi orders; otherwise
            paper-trade only.
        bankroll_cents: Starting bankroll in cents (ignored when
            resuming via ``session_id``).
        interval_seconds: Seconds between new-opportunity scans.
            Defaults to 3600 (hourly), matching the NWS update cadence.
            Settlement checks run every 30 s regardless.
        min_edge: Minimum absolute probability edge to enter a
            position.
        scale_up_threshold: How much the edge must grow beyond its
            value at entry to trigger adding contracts (default 0.10).
            Also used to detect scale-down: position is exited when
            edge flips past ``min_edge`` in the wrong direction.
        session_id: If provided, resume an existing ``SimSession``
            instead of creating a new one.

    Raises:
        ValueError: If ``session_id`` refers to a session that is
            not in ``"running"`` status.
    """
    db = get_session()
    fetcher = KalshiFetcher()

    # ---- Session setup ---------------------------------------------------
    if session_id is not None:
        session = db.get(SimSession, session_id)
        if session is None:
            raise ValueError(
                f"SimSession {session_id} not found"
            )
        if session.status != "running":
            raise ValueError(
                f"SimSession {session_id} status is "
                f"'{session.status}', expected 'running'"
            )
        log_path = session.log_path
    else:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        log_path = f"logs/weather_{timestamp}.log"
        os.makedirs("logs", exist_ok=True)

        session = SimSession(
            initial_bankroll_cents=bankroll_cents,
            current_bankroll_cents=bankroll_cents,
            status="running",
            log_path=log_path,
        )
        db.add(session)
        db.commit()

    # open_positions: only current session — used for settlement and recheck.
    open_positions: list[SimPosition] = (
        db.query(SimPosition)
        .filter(
            SimPosition.session_id == session.id,
            SimPosition.status == "open",
        )
        .all()
    )

    # entered_tickers: all sessions — prevents re-entering a ticker that was
    # already traded in a prior session that crashed and restarted.
    all_open_weather: list[SimPosition] = (
        db.query(SimPosition)
        .filter(
            SimPosition.status == "open",
            SimPosition.arb_type == "weather",
        )
        .all()
    )
    entered_tickers: set[str] = {p.ticker for p in all_open_weather}

    last_recheck_at: float = 0.0
    mode_label = "LIVE" if live else "SIM"

    # Align first scan to :25 past the hour (NWS posts ~15-20 min after the hour).
    now_dt = datetime.now(timezone.utc)
    mins_past = now_dt.minute + now_dt.second / 60.0
    if mins_past < 25:
        secs_until_offset = (25 - mins_past) * 60
    else:
        secs_until_offset = (60 - mins_past + 25) * 60
    # Treat "within 90 seconds of :25" as scan-immediately.
    if secs_until_offset > 90:
        last_scan_at = time.time() - (interval_seconds - secs_until_offset)
    else:
        last_scan_at = 0.0  # fire on first loop iteration

    log_file = open(log_path, "a", encoding="utf-8")
    try:
        next_scan_dt = datetime.fromtimestamp(
            last_scan_at + interval_seconds, tz=timezone.utc
        )
        _log(
            log_file,
            f"Weather strategy started [{mode_label}] "
            f"session={session.id} "
            f"bankroll=${session.current_bankroll_cents / 100:.2f} "
            f"interval={interval_seconds}s "
            f"min_edge={min_edge} "
            f"scale_up_threshold={scale_up_threshold} "
            f"| first scan at {next_scan_dt.strftime('%H:%M')} UTC",
        )

        while True:
            # -- Settle resolved positions ---------------------------------
            open_positions = _settle_positions(
                open_positions, fetcher, session, db, log_file,
            )

            now = time.time()

            # -- Hourly recheck of open positions -------------------------
            if now - last_recheck_at >= RECHECK_INTERVAL and open_positions:
                open_positions, scale_up_opps = _recheck_positions(
                    open_positions, fetcher, session, db, log_file,
                    live, min_edge, scale_up_threshold,
                )
                for opp in scale_up_opps:
                    market_id = opp["market"].id
                    # Only scale up once per ticker — skip if already scaled
                    if market_id in entered_tickers:
                        continue
                    _enter_position(opp, session, fetcher, live, db, log_file)
                    entered_tickers.add(market_id)
                    new_pos = (
                        db.query(SimPosition)
                        .filter(
                            SimPosition.session_id == session.id,
                            SimPosition.ticker == market_id,
                            SimPosition.status == "open",
                        )
                        .order_by(SimPosition.id.desc())
                        .first()
                    )
                    if new_pos is not None:
                        open_positions.append(new_pos)
                last_recheck_at = now

            # -- Scan for new opportunities --------------------------------
            if now - last_scan_at >= interval_seconds:
                _log(log_file, "Scanning weather markets...")
                try:
                    opportunities = scan_weather_markets(fetcher, min_edge)
                except Exception as exc:
                    _log(log_file, f"  [ERR] scan failed: {exc}")
                    opportunities = []

                for opp in opportunities:
                    market_id = opp["market"].id
                    if market_id not in entered_tickers:
                        _enter_position(
                            opp, session, fetcher, live, db, log_file,
                        )
                        entered_tickers.add(market_id)
                        new_pos = (
                            db.query(SimPosition)
                            .filter(
                                SimPosition.session_id == session.id,
                                SimPosition.ticker == market_id,
                                SimPosition.status == "open",
                            )
                            .first()
                        )
                        if new_pos is not None:
                            open_positions.append(new_pos)

                last_scan_at = now
                _log(
                    log_file,
                    f"  Open positions: {len(open_positions)} | "
                    f"Bankroll: ${session.current_bankroll_cents / 100:.4f}",
                )

            time.sleep(30)

    except KeyboardInterrupt:
        _log(log_file, "Shutting down (KeyboardInterrupt)...")
    finally:
        session.status = "stopped"
        session.stopped_at = datetime.utcnow()
        db.commit()

        total_pnl = (
            session.current_bankroll_cents
            - session.initial_bankroll_cents
        )
        _log(
            log_file,
            f"Session {session.id} stopped. "
            f"Bankroll: ${session.current_bankroll_cents / 100:.4f} "
            f"| P&L: {total_pnl:+.0f}c "
            f"| Trades: {session.total_trades} "
            f"| W={session.won} L={session.lost} "
            f"V={session.voided}",
        )
        log_file.close()
