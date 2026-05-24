"""
Live simulation engine — last-second price convergence + optional headline prediction.

Strategy:
  1. Every tick: settle resolved positions, run last-second sniper.
  2. Every interval: fetch near-term Kalshi markets (to feed last-second cache),
     optionally run Claude headline prediction scanner.

Usage:
    python -m src.cli --simulate                    # last-second on by default
    python -m src.cli --simulate --prediction       # + headline trades
    python -m src.cli --simulate --no-last-second   # market fetch only (for prediction)
"""
from __future__ import annotations

import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

_RUNNING = True


def _handle_sigint(sig, frame):
    global _RUNNING
    _RUNNING = False
    print("\n  [SIM] Stopping after this cycle completes...")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(log_file, msg: str, also_print: bool = True):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if also_print:
        print(line)
    log_file.write(line + "\n")
    log_file.flush()


# ---------------------------------------------------------------------------
# Near-term market fetch
# ---------------------------------------------------------------------------

def _event_close_time(event: dict) -> datetime | None:
    raw = event.get("close_time") or event.get("expiration_time")
    if not raw:
        for m in event.get("markets") or []:
            raw = m.get("close_time") or m.get("expiration_time")
            if raw:
                break
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_near_term_markets(fetcher, categories: list[str], within_minutes: int):
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=within_minutes)
    events = fetcher.get_events_raw(categories=categories)

    # Collect with close_time so we can sort soonest-first
    near: list[tuple[datetime, list, str]] = []
    for event in events:
        close_time = _event_close_time(event)
        # Allow markets closing up to 30s in the past (last-second scanner may still
        # have open positions; also avoids dropping them right as the window opens)
        if close_time is None or close_time < now - timedelta(seconds=30) or close_time > cutoff:
            continue
        parsed = fetcher._parse_event(event)
        if parsed:
            mins = (close_time - now).total_seconds() / 60
            label = (
                f"{event.get('event_ticker','?')} closes {close_time.strftime('%H:%M UTC')}"
                f" ({mins:.0f}min)"
            )
            near.append((close_time, parsed, label))

    near.sort(key=lambda x: x[0])  # soonest first

    near_markets = []
    seen_events: list[str] = []
    for _, markets, label in near:
        near_markets.extend(markets)
        seen_events.append(label)
    return near_markets, seen_events


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def _settle_open_positions(db: Session, session_id: int, fetcher, log_file) -> int:
    from src.storage.models import SimPosition, SimSession

    open_positions = (
        db.query(SimPosition)
        .filter(SimPosition.session_id == session_id, SimPosition.status == "open")
        .all()
    )
    settled_count = 0
    now = datetime.now(timezone.utc)
    sim = db.get(SimSession, session_id)

    for pos in open_positions:
        try:
            mkt = fetcher.get_market_status(pos.ticker)
        except Exception as exc:
            _log(log_file, f"  [ERR] fetch {pos.ticker}: {exc}")
            continue

        mkt_status = mkt.get("status", "")
        mkt_result = mkt.get("result") or ""  # normalize None → ""
        if mkt_status not in ("finalized", "settled", "closed") or mkt_result not in ("yes", "no"):
            continue

        if mkt_result == pos.side:
            outcome = "won"
            pnl = pos.contracts * 100.0 - pos.cost_cents
        else:
            outcome = "lost"
            pnl = -pos.cost_cents

        pos.status = outcome
        pos.result = mkt_result
        pos.pnl_cents = round(pnl, 2)
        pos.settled_at = now

        if outcome == "won":
            sim.current_bankroll_cents += pos.cost_cents + pnl
            sim.won += 1
        else:
            sim.lost += 1

        label = "WON " if outcome == "won" else "LOST"
        _log(log_file,
            f"  SETTLE {label} {pos.ticker} ({pos.side.upper()}) "
            f"| P&L={pnl:+.0f}c  bankroll=${sim.current_bankroll_cents/100:.4f}"
        )
        settled_count += 1

    db.commit()
    return settled_count


# ---------------------------------------------------------------------------
# Live order placement
# ---------------------------------------------------------------------------

def _place_live_legs(fetcher, legs: list[dict], count: int, log_file) -> tuple[list[str], int] | None:
    """
    Place IOC limit buy orders for each leg.
    Returns (order_ids, total_filled) if at least 1 contract filled, or None if nothing filled.
    Partial fills are accepted — IOC cancels any unfilled remainder automatically.
    legs: [{"ticker": str, "side": str, "price_cents": int}]
    """
    placed: list[tuple[str, str, int]] = []  # (order_id, ticker, filled)

    for leg in legs:
        try:
            order = fetcher.place_order(
                ticker=leg["ticker"],
                side=leg["side"],
                price_cents=int(leg["price_cents"]),
                count=count,
            )
        except Exception as exc:
            _log(log_file, f"  [LIVE] order FAILED {leg['ticker']} ({leg['side']}): {exc}")
            return None if not placed else ([oid for oid, _, _ in placed], sum(f for _, _, f in placed))

        order_id = order.get("order_id") or order.get("id", "")
        status = order.get("status", "")
        filled = order.get("fill_count") or order.get("filled_count", 0)

        # Kalshi sometimes returns fill_count: None on executed orders — trust status
        if status in ("executed", "filled") and not filled:
            filled = count

        if filled > 0:
            _log(log_file,
                f"  [LIVE] filled  {leg['ticker']} ({leg['side']}) x{filled}/{count}"
                f" @ {leg['price_cents']}¢  order={order_id}"
            )
            placed.append((order_id, leg["ticker"], filled))
        else:
            _log(log_file,
                f"  [LIVE] no fill {leg['ticker']} ({leg['side']}) x{count}"
                f" @ {leg['price_cents']}¢  order={order_id}"
            )
            return None if not placed else ([oid for oid, _, _ in placed], sum(f for _, _, f in placed))

    total_filled = sum(f for _, _, f in placed)
    return ([oid for oid, _, _ in placed], total_filled)


# ---------------------------------------------------------------------------
# Position entry helpers
# ---------------------------------------------------------------------------

def _enter_last_second_bet(
    db, sim, open_keys, entry: dict, contracts: int, log_file,
    fetcher=None, live: bool = False
):
    """Enter a last-second YES or NO bet on a single Kalshi bucket."""
    import json
    from src.storage.models import SimPosition

    mkt = entry["market"]
    ticker = mkt.id
    side = entry.get("side", "yes")
    # Key includes side so we can hold both YES and NO on different buckets
    open_key = f"{ticker}_{side}"
    if open_key in open_keys:
        return

    # Streaming path sets "ask_cents" directly; scan path uses "yes_ask_cents"/"no_ask_cents"
    ask_cents = entry.get("ask_cents")
    if ask_cents is None:
        ask_cents = entry.get("yes_ask_cents") if side == "yes" else entry.get("no_ask_cents")
    if ask_cents is None:
        return
    total_cost = ask_cents * contracts
    bankroll = sim.current_bankroll_cents
    if total_cost > bankroll or total_cost < 1:
        return

    order_ids = None
    if live and fetcher is not None:
        # Place YES orders at 99¢ to sweep the full order book — any seller at
        # any price ≤99¢ will fill. NO orders keep the scanned ask price.
        order_price = 99 if side == "yes" else ask_cents
        leg_dicts = [{"ticker": ticker, "side": side, "price_cents": order_price}]
        result = _place_live_legs(fetcher, leg_dicts, contracts, log_file)
        if result is None:
            _log(log_file, f"  [LIVE] last-second skipped (no fill) {ticker} {side.upper()}")
            return
        order_ids, filled = result
        if filled < contracts:
            _log(log_file, f"  [LIVE] partial fill {filled}/{contracts} contracts — recording actual fill")
        contracts = filled
        total_cost = ask_cents * contracts

    pos = SimPosition(
        session_id=sim.id, ticker=ticker, side=side,
        entry_price_cents=ask_cents, cost_cents=total_cost,
        contracts=contracts, ev=0.0, arb_type="last_second",
        live=1 if live else 0,
        order_ids=json.dumps(order_ids) if order_ids else None,
    )
    pos.legs = [{"ticker": ticker, "side": side, "price_cents": ask_cents}]
    db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(open_key)

    mode_tag = "[LIVE] " if live else ""
    _log(log_file,
        f"  BUY  last-second {mode_tag}{ticker} {side.upper()} @ {ask_cents}¢ x{contracts}"
        f"  | spot={entry['spot_price']:.4f} ({entry['kraken_pair']})"
        f"  | closes_in={entry['seconds_to_close']:.0f}s"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
    )
    return pos



def _enter_prediction_bet(db, sim, open_keys, opp, max_position_pct, log_file):
    from src.storage.models import SimPosition

    market = opp["market"]
    direction = opp["direction"]  # "yes" or "no"
    ticker = market.id
    pos_key = f"PRED_{ticker}_{direction}"
    if pos_key in open_keys:
        return

    # Risk guardrail 1: max 3 open prediction positions per session
    pred_count = db.query(SimPosition).filter(
        SimPosition.session_id == sim.id,
        SimPosition.status == "open",
        SimPosition.arb_type.is_(None),
    ).count()
    if pred_count >= 3:
        return

    # Risk guardrail 2: skip if bankroll < 50% of initial (capital preservation)
    if sim.current_bankroll_cents < sim.initial_bankroll_cents * 0.50:
        return

    sel = next((s for s in market.selections if s.name.lower() == direction), None)
    if sel is None:
        return

    bankroll = sim.current_bankroll_cents
    size_pct = min(float(opp.get("suggested_size_pct", 0.02)), max_position_pct)
    price_cents = round(100.0 / sel.odds, 1)
    contracts = max(1, int(bankroll * size_pct // price_cents))
    total_cost = contracts * price_cents
    if total_cost > bankroll or total_cost < 1:
        return

    pos = SimPosition(
        session_id=sim.id, ticker=ticker, side=direction,
        entry_price_cents=price_cents, cost_cents=total_cost,
        contracts=contracts, ev=opp["confidence"] / 100.0, arb_type=None,
        live=0,
    )
    db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(pos_key)
    terms_str = ", ".join(opp.get("shared_terms", [])[:5])
    _log(log_file,
        f"  BUY  prediction  {ticker} {direction.upper()} @ {price_cents:.1f}c"
        f" | conf={opp['confidence']}%  terms=[{terms_str}]"
        f" | {contracts}x cost={total_cost:.0f}c"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
        f"\n            {opp.get('reasoning', '')[:100]}"
    )


# ---------------------------------------------------------------------------
# Balance reconciliation
# ---------------------------------------------------------------------------

def _reconcile_balance(db, sim, fetcher, log_file) -> None:
    """
    Fetch the actual Kalshi balance and sync the DB bankroll to match.
    Only called when no positions are open, so locked funds don't skew the diff.
    Logs a warning if the discrepancy exceeds $0.05 (manual trades, untracked fills, etc).
    """
    try:
        actual_cents = fetcher.get_balance()
        db_cents = sim.current_bankroll_cents
        diff = actual_cents - db_cents
        if abs(diff) < 1:
            return  # in sync, no action needed
        warning = "  *** DISCREPANCY > $0.05 — possible manual trade or untracked fill ***" if abs(diff) > 5 else ""
        _log(log_file,
            f"  [RECONCILE] Kalshi=${actual_cents/100:.2f}  DB=${db_cents/100:.2f}"
            f"  diff={diff/100:+.2f}  → syncing"
            + (f"\n{warning}" if warning else "")
        )
        sim.current_bankroll_cents = actual_cents
        db.commit()
    except Exception as exc:
        _log(log_file, f"  [RECONCILE] failed: {exc}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _wait_interruptible(seconds: int, price_event=None) -> bool:
    """
    Wait up to `seconds` for either a price update or a shutdown signal.
    If price_event is provided (streaming active), returns as soon as any
    price changes rather than sleeping the full interval.
    """
    if price_event is not None:
        deadline = time.time() + seconds
        while _RUNNING:
            remaining = deadline - time.time()
            if remaining <= 0:
                return True
            fired = price_event.wait(timeout=min(remaining, 1.0))
            if fired:
                price_event.clear()
                return True
        return False
    else:
        for _ in range(seconds):
            if not _RUNNING:
                return False
            time.sleep(1)
        return True


def run_live_simulation(
    db: Session,
    initial_bankroll_usd: float = 5.00,
    interval_seconds: int = 5,
    settle_interval_seconds: int = 5,
    categories: list[str] | None = None,
    max_position_pct: float = 0.10,
    near_term_minutes: int = 60,
    logs_dir: str = "logs",
    resume_session_id: int | None = None,
    use_live_orders: bool = False,
    use_last_second: bool = True,
    ls_entry_window: int = 300,  # default matches last_second.ENTRY_WINDOW_SECONDS
    ls_min_yes_cents: int = 70,
    ls_max_yes_cents: int = 98,
    ls_edge_buffer_pct: float = 0.15,
    ls_stability_window_s: int = 15,
    ls_stability_threshold_pct: float = 0.003,
    ls_min_no_cents: int = 3,
    ls_max_no_cents: int = 40,
    ls_directional_margin_pct: float = 0.003,
    use_prediction: bool = False,
    use_streaming: bool = True,
) -> None:
    """
    Last-second price convergence sniper + optional headline prediction trades.

    Every tick (settle_interval_seconds):
      A.  Settle resolved positions.
      A2. Last-second scanner — buy YES on bucket containing stable Kraken spot price
          in the final ls_entry_window seconds before close.

    Every interval (interval_seconds):
      B.  Fetch near-term Kalshi markets (updates last-second cache).
      B2. Prediction trades — Claude reviews headline signals and approves directional bets.
    """
    global _RUNNING
    _RUNNING = True
    signal.signal(signal.SIGINT, _handle_sigint)

    from src.fetchers.kalshi import KalshiFetcher
    from src.storage.models import SimSession, SimPosition

    # Last-second strategy state
    _ls_trackers: dict = {}           # kraken_pair → PriceTracker
    _ls_markets_cache: dict = {}      # ticker → Market; additive, evict only after close+30s
    _ls_entered_tickers: set = set()      # event keys already entered this close-time cycle
    _ls_diag_file = None              # separate verbose diagnostic log
    _kalshi_subscribed: set = set()   # tickers currently subscribed via WS

    # WebSocket streaming (replaces REST polling for prices)
    stream_mgr = None
    if use_last_second and use_streaming:
        try:
            from src.streaming.manager import StreamManager
            stream_mgr = StreamManager()
            stream_mgr.start()
        except Exception as exc:
            import sys
            print(f"  [STREAMING] failed to start ({exc}), falling back to REST polling", file=sys.stderr)
            stream_mgr = None

    target_categories = categories or ["Crypto", "Economics", "Financials"]
    Path(logs_dir).mkdir(exist_ok=True)
    initial_cents = round(initial_bankroll_usd * 100, 2)

    if resume_session_id:
        sim = db.get(SimSession, resume_session_id)
        if sim is None:
            raise ValueError(f"Session {resume_session_id} not found")
        log_path = sim.log_path
    else:
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = str(Path(logs_dir) / f"sim_{ts_str}.log")
        sim = SimSession(
            initial_bankroll_cents=initial_cents,
            current_bankroll_cents=initial_cents,
            status="running",
            log_path=log_path,
        )
        db.add(sim)
        db.commit()

    session_id = sim.id

    try:
        fetcher = KalshiFetcher()
    except Exception as exc:
        raise RuntimeError(f"Cannot init Kalshi fetcher: {exc}")

    news_fetcher = None
    reviewer = None
    if use_prediction:
        from src.fetchers.news import NewsFetcher
        from src.engine.prediction import ClaudeReviewer
        try:
            news_fetcher = NewsFetcher()
            reviewer = ClaudeReviewer()
        except RuntimeError as exc:
            import sys
            print(f"  [PREDICTION] disabled: {exc}", file=sys.stderr)

    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ls_diag_dir = str(Path(logs_dir) / "ls_diag")
    Path(ls_diag_dir).mkdir(exist_ok=True)
    ls_diag_path = str(Path(ls_diag_dir) / f"ls_{Path(log_path).stem}.log")

    with open(log_path, "a", encoding="utf-8") as log_file, \
         open(ls_diag_path, "a", encoding="utf-8") as _ls_diag_file:
        mode_parts = ["LIVE" if use_live_orders else "SIM"]
        if use_last_second:
            mode_parts.append("+LAST-SEC")
        if stream_mgr is not None:
            mode_parts.append("+WS")
        if use_prediction and news_fetcher is not None:
            mode_parts.append("+PREDICTION")
        mode_label = " ".join(mode_parts)
        _log(log_file, "=" * 65)
        _log(log_file,
            f"  SESSION {session_id}  [{mode_label}]  bankroll=${initial_bankroll_usd:.2f}"
            f"  |  scan={interval_seconds}s  tick={settle_interval_seconds}s"
        )
        _log(log_file, f"  categories={target_categories}  |  near_term={near_term_minutes}min")
        if use_last_second:
            _log(log_file,
                f"  [LAST-SECOND] entry_window={ls_entry_window}s"
                f"  yes=[{ls_min_yes_cents},{ls_max_yes_cents}]¢"
                f"  no=[{ls_min_no_cents},{ls_max_no_cents}]¢"
                f"  edge_buf={ls_edge_buffer_pct:.0%}"
                f"  stability={ls_stability_window_s}s/<{ls_stability_threshold_pct:.1%}"
            )
            _log(log_file, f"  [LAST-SECOND] diag log: {ls_diag_path}")
        # Opening reconciliation (live mode, new sessions only)
        if use_live_orders and not resume_session_id:
            try:
                actual_cents = fetcher.get_balance()
                prev = (
                    db.query(SimSession)
                    .filter(SimSession.id != session_id, SimSession.status == "stopped")
                    .order_by(SimSession.id.desc())
                    .first()
                )
                prev_end = prev.current_bankroll_cents if prev else None
                adj = actual_cents - initial_cents
                sim.initial_bankroll_cents = actual_cents
                sim.current_bankroll_cents = actual_cents
                sim.opening_adjustment_cents = (actual_cents - prev_end) if prev_end is not None else 0.0
                db.commit()
                _log(log_file,
                    f"  [RECONCILE] opening balance: Kalshi=${actual_cents/100:.2f}"
                    + (f"  prev_session_end=${prev_end/100:.2f}  gap={sim.opening_adjustment_cents/100:+.2f}" if prev_end is not None else "")
                    + (f"  *** untracked gap ***" if prev_end is not None and abs(sim.opening_adjustment_cents) > 5 else "")
                )
            except Exception as exc:
                _log(log_file, f"  [RECONCILE] opening check failed: {exc}")

        _log(log_file, "=" * 65)
        _ls_diag_file.write(
            f"=== LS DIAG  session={session_id}  window={ls_entry_window}s"
            f"  yes_ask=[{ls_min_yes_cents},{ls_max_yes_cents}]¢"
            f"  edge_buf={ls_edge_buffer_pct:.0%}"
            f"  stability={ls_stability_window_s}s/<{ls_stability_threshold_pct:.1%} ===\n"
            f"  diag log: {ls_diag_path}\n"
        )
        _ls_diag_file.flush()

        last_scan_at = 0.0
        last_log_scan_at = 0.0
        tick = 0

        while _RUNNING:
            tick += 1
            now = datetime.now(timezone.utc)
            now_ts = now.timestamp()

            # ----------------------------------------------------------------
            # A. Last-second scanner — ENTRY FIRST (every tick when enabled)
            # ----------------------------------------------------------------
            if use_last_second and _ls_markets_cache:
                from src.engine.last_second import (
                    scan_last_second_opportunities,
                    update_price_trackers,
                    kraken_pair_for_market,
                )
                cache_values = list(_ls_markets_cache.values())

                # Determine which markets are relevant to what just changed
                if stream_mgr is not None:
                    triggered_pairs, triggered_tickers = stream_mgr.cache.pop_triggered()
                    # A Kalshi ticker update → include all markets sharing that pair
                    # so adjacent buckets are also checked
                    for mkt in cache_values:
                        if mkt.id in triggered_tickers:
                            p = kraken_pair_for_market(mkt)
                            if p:
                                triggered_pairs.add(p)
                    # Filter to only markets relevant to what changed
                    if triggered_pairs:
                        scan_markets = [
                            m for m in cache_values
                            if kraken_pair_for_market(m) in triggered_pairs
                        ]
                    else:
                        scan_markets = []  # nothing changed, skip scan
                else:
                    # No streaming — scan everything every tick
                    scan_markets = cache_values
                    triggered_pairs = set()

                if scan_markets:
                    pairs_needed: set[str] = {
                        p for m in scan_markets
                        if (p := kraken_pair_for_market(m)) is not None
                    }

                    # Update price trackers
                    ts_str = now.strftime("%H:%M:%S")
                    _ls_diag_file.write(
                        f"\n[{ts_str}] tick={tick}  triggered_pairs={sorted(triggered_pairs)}"
                        f"  scan={len(scan_markets)}/{len(_ls_markets_cache)} markets\n"
                    )

                    if stream_mgr is not None:
                        from src.engine.last_second import PriceTracker
                        for pair in pairs_needed:
                            if pair not in _ls_trackers:
                                _ls_trackers[pair] = PriceTracker()
                            ws_price = stream_mgr.cache.get_spot(pair)
                            if ws_price is not None:
                                _ls_trackers[pair].record(ws_price)
                        price_results = {p: _ls_trackers[p].latest() for p in pairs_needed}
                    else:
                        price_results = update_price_trackers(_ls_trackers, pairs_needed)

                    for pair, price in price_results.items():
                        tracker = _ls_trackers.get(pair)
                        obs = tracker.observation_count() if tracker else 0
                        stable = tracker.is_stable(ls_stability_window_s, ls_stability_threshold_pct) if tracker else False
                        _ls_diag_file.write(
                            f"  kraken {pair}: price={price}  obs={obs}  stable={stable}\n"
                        )

                    # Decision trace for each market in scope (buckets + directional 15M)
                    bucket_count = 0
                    for mkt in scan_markets:
                        floor_s = mkt.metadata.get("floor_strike")
                        cap_s = mkt.metadata.get("cap_strike")
                        if floor_s is None:
                            continue
                        bucket_count += 1
                        is_directional = cap_s is None
                        pair = kraken_pair_for_market(mkt)
                        yes_ask = mkt.selections[0].metadata.get("yes_ask") if mkt.selections else None
                        tracker = _ls_trackers.get(pair) if pair else None
                        spot = tracker.latest() if tracker else None
                        stable = tracker.is_stable(ls_stability_window_s, ls_stability_threshold_pct) if tracker else False
                        secs = (mkt.starts_at - now).total_seconds() if mkt.starts_at else None

                        if secs is None:
                            decision = "SKIP: no close time"
                        elif secs <= 0:
                            decision = f"SKIP: already closed ({secs:.0f}s ago)"
                        elif secs > ls_entry_window:
                            decision = f"wait: {secs:.0f}s to close (window={ls_entry_window}s)"
                        elif spot is None:
                            decision = "REJECT: no spot price from Kraken"
                        elif not stable:
                            obs = tracker.observation_count() if tracker else 0
                            decision = f"REJECT: price unstable (obs={obs} in {ls_stability_window_s}s)"
                        elif is_directional:
                            pct = (spot - float(floor_s)) / float(floor_s)
                            no_ask = mkt.selections[0].metadata.get("no_ask") if mkt.selections else None
                            if abs(pct) < ls_directional_margin_pct:
                                decision = f"REJECT: too close to floor (pct={pct:.4f}, need ±{ls_directional_margin_pct})"
                            elif pct >= ls_directional_margin_pct:
                                if yes_ask is None:
                                    decision = "REJECT: yes_ask is None"
                                elif yes_ask < ls_min_yes_cents:
                                    decision = f"REJECT: yes_ask={yes_ask}¢ < min={ls_min_yes_cents}¢"
                                elif yes_ask > ls_max_yes_cents:
                                    decision = f"REJECT: yes_ask={yes_ask}¢ > max={ls_max_yes_cents}¢"
                                elif f"{mkt.id.rsplit('-', 1)[0]}_yes" in _ls_entered_tickers:
                                    decision = "SKIP: already entered this cycle"
                                else:
                                    decision = f">>> ENTER directional YES yes_ask={yes_ask}¢ spot={spot} pct={pct:.4f}"
                            else:  # pct <= -ls_directional_margin_pct
                                if no_ask is None:
                                    decision = "REJECT: no_ask is None"
                                elif no_ask < ls_min_no_cents:
                                    decision = f"REJECT: no_ask={no_ask}¢ < min={ls_min_no_cents}¢"
                                elif no_ask > ls_max_no_cents:
                                    decision = f"REJECT: no_ask={no_ask}¢ > max={ls_max_no_cents}¢"
                                elif f"{mkt.id.rsplit('-', 1)[0]}_no" in _ls_entered_tickers:
                                    decision = "SKIP: already entered this cycle"
                                else:
                                    decision = f">>> ENTER directional NO no_ask={no_ask}¢ spot={spot} pct={pct:.4f}"
                        elif not (float(floor_s) <= spot < float(cap_s)):
                            decision = f"REJECT: spot={spot} outside [{floor_s}, {cap_s})"
                        else:
                            bw = float(cap_s) - float(floor_s)
                            buf = ls_edge_buffer_pct * bw
                            mf = spot - float(floor_s)
                            mc = float(cap_s) - spot
                            if mf < buf:
                                decision = f"REJECT: edge-fail floor margin={mf:.5f} < buffer={buf:.5f}"
                            elif mc < buf:
                                decision = f"REJECT: edge-fail cap margin={mc:.5f} < buffer={buf:.5f}"
                            elif yes_ask is None:
                                decision = "REJECT: yes_ask is None"
                            elif yes_ask < ls_min_yes_cents:
                                decision = f"REJECT: yes_ask={yes_ask}¢ < min={ls_min_yes_cents}¢"
                            elif yes_ask > ls_max_yes_cents:
                                decision = f"REJECT: yes_ask={yes_ask}¢ > max={ls_max_yes_cents}¢"
                            elif f"{mkt.id.rsplit('-', 1)[0]}_yes" in _ls_entered_tickers:
                                decision = "SKIP: already entered this cycle"
                            else:
                                decision = f">>> ENTER yes_ask={yes_ask}¢ spot={spot} margin_floor={mf:.5f} margin_cap={mc:.5f}"

                        _ls_diag_file.write(
                            f"  {mkt.id} | {secs:.0f}s | floor={floor_s} cap={cap_s} "
                            f"yes_ask={yes_ask}¢ pair={pair} | {decision}\n"
                        )

                    if bucket_count == 0:
                        _ls_diag_file.write("  (no bucket/directional markets with floor_strike in scope)\n")
                    _ls_diag_file.flush()

                    ls_entries = scan_last_second_opportunities(
                        scan_markets, _ls_trackers, now,
                        entry_window_seconds=ls_entry_window,
                        min_yes_cents=ls_min_yes_cents,
                        max_yes_cents=ls_max_yes_cents,
                        min_no_cents=ls_min_no_cents,
                        max_no_cents=ls_max_no_cents,
                        edge_buffer_pct=ls_edge_buffer_pct,
                        stability_window_s=ls_stability_window_s,
                        stability_threshold_pct=ls_stability_threshold_pct,
                        directional_margin_pct=ls_directional_margin_pct,
                    )

                    # Log rejection reasons when markets are in-window but nothing qualifies
                    if not ls_entries:
                        in_window = [
                            m for m in scan_markets
                            if m.starts_at and 0 < (m.starts_at - now).total_seconds() <= ls_entry_window
                        ]
                        if in_window:
                            reject_lines = []
                            seen_assets: set[str] = set()
                            for mkt in in_window:
                                pair = kraken_pair_for_market(mkt)
                                if pair in seen_assets:
                                    continue
                                seen_assets.add(pair or mkt.id)
                                tracker = _ls_trackers.get(pair) if pair else None
                                spot = tracker.latest() if tracker else None
                                stable = tracker.is_stable(ls_stability_window_s, ls_stability_threshold_pct) if tracker else False
                                floor_s = mkt.metadata.get("floor_strike")
                                cap_s = mkt.metadata.get("cap_strike")
                                secs = (mkt.starts_at - now).total_seconds()
                                if spot is None:
                                    reason = "no spot price"
                                elif not stable:
                                    obs = tracker.observation_count() if tracker else 0
                                    reason = f"unstable (obs={obs})"
                                elif floor_s is not None and cap_s is not None:
                                    try:
                                        bw = float(cap_s) - float(floor_s)
                                        buf = ls_edge_buffer_pct * bw
                                        mf = spot - float(floor_s)
                                        mc = float(cap_s) - spot
                                        if not (float(floor_s) <= spot < float(cap_s)):
                                            reason = f"spot={spot} outside bucket [{floor_s},{cap_s})"
                                        elif mf < buf:
                                            reason = f"edge-fail floor: {mf:.4f} < {buf:.4f} (need {buf - mf:.4f} more)"
                                        elif mc < buf:
                                            reason = f"edge-fail cap: {mc:.4f} < {buf:.4f} (need {buf - mc:.4f} more)"
                                        else:
                                            yes_ask = mkt.selections[0].metadata.get("yes_ask") if mkt.selections else None
                                            reason = f"ask={yes_ask}¢ out of range [{ls_min_yes_cents},{ls_max_yes_cents}]"
                                    except (TypeError, ValueError):
                                        reason = "parse error"
                                else:
                                    reason = "directional: no edge"
                                reject_lines.append(
                                    f"    {pair or mkt.id}  spot={spot}  {secs:.0f}s  → {reason}"
                                )
                            _log(log_file,
                                f"  [LS] {len(in_window)} in-window market(s), no entries:\n"
                                + "\n".join(reject_lines)
                            )

                    for entry in ls_entries:
                        ticker = entry["market"].id
                        side = entry.get("side", "yes")
                        event_key = ticker.rsplit("-", 1)[0]
                        entry_key = f"{event_key}_{side}"
                        if entry_key in _ls_entered_tickers:
                            continue  # already entered this event — block all second entries
                        # Freshen ask from WS cache (it just fired, so this is fresh)
                        if stream_mgr is not None:
                            ws_ask = stream_mgr.cache.get_yes_ask(ticker)
                            if ws_ask is not None and stream_mgr.cache.yes_ask_age(ticker) < 10:
                                entry = dict(entry)
                                if side == "yes":
                                    live_ask = int(round(ws_ask))
                                    in_range = ls_min_yes_cents <= live_ask <= ls_max_yes_cents
                                else:
                                    live_ask = int(round(100 - ws_ask))
                                    in_range = ls_min_no_cents <= live_ask <= ls_max_no_cents
                                entry["ask_cents"] = live_ask
                                if not in_range:
                                    _log(log_file,
                                        f"  [LS-WS] skip {ticker} {side.upper()} live ask={live_ask}¢ out of range"
                                    )
                                    continue
                        db.refresh(sim)
                        ask_cents = entry.get("ask_cents") or (
                            entry.get("yes_ask_cents") if side == "yes" else entry.get("no_ask_cents")
                        )
                        if ask_cents >= 95:
                            alloc = sim.current_bankroll_cents * 0.375
                        elif ask_cents >= 90:
                            alloc = sim.current_bankroll_cents * 0.25
                        elif ask_cents >= 85:
                            alloc = sim.current_bankroll_cents * 0.125
                        else:
                            alloc = min(sim.current_bankroll_cents * 0.025, ask_cents * 5)
                        contracts = max(1, int(alloc // ask_cents))
                        # Cap high-ask trades at 1 contract to limit tail risk
                        if ask_cents >= 90:
                            contracts = min(contracts, 1)
                        ls_open_keys: set[str] = {
                            f"{p.ticker}_{p.side}" for p in db.query(SimPosition).filter(
                                SimPosition.session_id == session_id,
                                SimPosition.status == "open",
                            ).all()
                        }
                        new_pos = _enter_last_second_bet(
                            db, sim, ls_open_keys, entry, contracts, log_file,
                            fetcher=fetcher, live=use_live_orders,
                        )
                        if new_pos is not None:
                            _ls_entered_tickers.add(entry_key)
                        db.commit()

            # ----------------------------------------------------------------
            # B. Settle open positions (after entry check)
            # ----------------------------------------------------------------
            newly_settled = _settle_open_positions(db, session_id, fetcher, log_file)
            if newly_settled:
                db.refresh(sim)
                locked_now = sum(
                    p.cost_cents for p in db.query(SimPosition).filter(
                        SimPosition.session_id == session_id,
                        SimPosition.status == "open",
                    ).all()
                )
                _log(log_file,
                    f"  SETTLED {newly_settled}"
                    f"  liquid=${sim.current_bankroll_cents/100:.4f}"
                    f"  locked=${locked_now/100:.4f}"
                    f"  total=${(sim.current_bankroll_cents + locked_now)/100:.4f}"
                )

                # Reconcile DB bankroll against actual Kalshi balance (live only,
                # and only when no positions are locked so comparison is clean)
                if use_live_orders and locked_now == 0:
                    _reconcile_balance(db, sim, fetcher, log_file)
                    db.refresh(sim)

                # Prune entry keys for events where all markets have closed.
                # Keys are now event-level ("KXBTC-26MAR2507_yes"), so compare
                # against the set of open event prefixes, not full tickers.
                open_event_set = {
                    m.id.rsplit("-", 1)[0]
                    for m in _ls_markets_cache.values()
                    if m.starts_at and m.starts_at > now
                }
                expired = {
                    k for k in _ls_entered_tickers
                    if k.rsplit("_", 1)[0] not in open_event_set
                }
                _ls_entered_tickers -= expired

            # ----------------------------------------------------------------
            # B. Full market scan (every interval_seconds)
            # ----------------------------------------------------------------
            if now_ts - last_scan_at >= interval_seconds:
                last_scan_at = now_ts
                # Fetch and merge into the additive cache every 5s (silent)
                try:
                    markets, seen_events = _fetch_near_term_markets(
                        fetcher, target_categories, near_term_minutes
                    )
                    # Additive merge: add/update new markets, evict only those
                    # closed >30s ago so markets aren't dropped mid-entry-window
                    prev_ids = set(_ls_markets_cache.keys())
                    for m in markets:
                        _ls_markets_cache[m.id] = m
                    evict_before = now - timedelta(seconds=30)
                    for tid in [k for k, m in _ls_markets_cache.items()
                                if m.starts_at and m.starts_at < evict_before]:
                        del _ls_markets_cache[tid]
                    cache_changed = set(_ls_markets_cache.keys()) != prev_ids
                except Exception as exc:
                    _log(log_file, f"  ERROR fetching markets: {exc}")
                    markets, seen_events = [], []
                    cache_changed = False

                # Print the SCAN header once per minute or when market list changes
                if now_ts - last_log_scan_at >= 60 or cache_changed:
                    last_log_scan_at = now_ts
                    _log(log_file, "")
                    _log(log_file,
                        f"-- SCAN  tick={tick}  {now.strftime('%H:%M:%S UTC')} --------------------"
                    )
                    db.refresh(sim)
                    liquid = sim.current_bankroll_cents
                    open_positions = db.query(SimPosition).filter(
                        SimPosition.session_id == session_id,
                        SimPosition.status == "open",
                    ).all()
                    locked_now = sum(p.cost_cents for p in open_positions)
                    _log(log_file,
                        f"  liquid=${liquid/100:.4f}  locked=${locked_now/100:.4f}"
                        f"  total=${(liquid + locked_now)/100:.4f}  |  open={len(open_positions)}"
                    )
                    if markets:
                        _log(log_file, f"  {len(markets)} markets across {len(seen_events)} event(s):")
                        for ev_info in seen_events[:8]:
                            _log(log_file, f"    - {ev_info}")
                        if len(seen_events) > 8:
                            _log(log_file, f"    ... and {len(seen_events) - 8} more")
                    else:
                        _log(log_file, f"  No near-term markets found in {target_categories}.")

                # Update Kalshi WS subscriptions to match cache (additive)
                if stream_mgr is not None and use_last_second:
                    new_tickers = set(_ls_markets_cache.keys())
                    to_sub = new_tickers - _kalshi_subscribed
                    to_unsub = _kalshi_subscribed - new_tickers
                    if to_sub:
                        stream_mgr.subscribe_kalshi(to_sub)
                        _kalshi_subscribed |= to_sub
                    if to_unsub:
                        stream_mgr.unsubscribe_kalshi(to_unsub)
                        _kalshi_subscribed -= to_unsub
                    if to_sub or to_unsub:
                        _log(log_file,
                            f"  [WS] kalshi +{len(to_sub)}/-{len(to_unsub)} tickers"
                            f"  (total subscribed: {len(_kalshi_subscribed)})"
                        )

                # --------------------------------------------------------
                # B2. Prediction trades (Claude + headline signals)
                # --------------------------------------------------------
                if use_prediction and news_fetcher is not None:
                    from src.engine.prediction import scan_prediction_opportunities
                    try:
                        pred_opps = scan_prediction_opportunities(
                            markets, news_fetcher, reviewer
                        )
                        if pred_opps:
                            _log(log_file, f"  [PREDICTION] {len(pred_opps)} approved signal(s)")
                        pred_open_keys: set[str] = {
                            p.ticker for p in db.query(SimPosition).filter(
                                SimPosition.session_id == session_id,
                                SimPosition.status == "open",
                            ).all()
                        }
                        for opp in pred_opps:
                            if not _RUNNING:
                                break
                            _enter_prediction_bet(
                                db, sim, pred_open_keys, opp, max_position_pct, log_file
                            )
                    except Exception as exc:
                        _log(log_file, f"  [PREDICTION] scan error: {exc}")

                db.commit()

            # ----------------------------------------------------------------
            # C. Stop if bankrupt with no open positions
            # ----------------------------------------------------------------
            db.refresh(sim)
            if sim.current_bankroll_cents <= 0:
                open_count = db.query(SimPosition).filter(
                    SimPosition.session_id == session_id,
                    SimPosition.status == "open",
                ).count()
                if open_count == 0:
                    _log(log_file, "  Bankroll depleted and no open positions -- stopping.")
                    break

            if _RUNNING:
                price_event = stream_mgr.cache.update_event if stream_mgr is not None else None
                _wait_interruptible(settle_interval_seconds, price_event)

        # Shutdown
        if stream_mgr is not None:
            stream_mgr.stop()
        db.refresh(sim)
        sim.status = "stopped"
        sim.stopped_at = datetime.now(timezone.utc)
        db.commit()

        locked = sum(
            p.cost_cents for p in db.query(SimPosition).filter(
                SimPosition.session_id == session_id,
                SimPosition.status == "open",
            ).all()
        )
        total_value = sim.current_bankroll_cents + locked
        pnl = total_value - sim.initial_bankroll_cents

        _log(log_file, "")
        _log(log_file, "=" * 65)
        _log(log_file, f"  SESSION {session_id} STOPPED  ticks={tick}")
        _log(log_file, f"  Initial bankroll : ${sim.initial_bankroll_cents/100:.4f}")
        _log(log_file, f"  Liquid           : ${sim.current_bankroll_cents/100:.4f}")
        _log(log_file, f"  Locked (open)    : ${locked/100:.4f}")
        _log(log_file, f"  Total value      : ${total_value/100:.4f}")
        _log(log_file, f"  Unrealised P&L   : ${pnl/100:+.4f}")
        _log(log_file, f"  Trades opened    : {sim.total_trades}")
        _log(log_file, f"  W / L / V        : {sim.won} / {sim.lost} / {sim.voided}")
        _log(log_file, f"  Log file         : {log_path}")
        _log(log_file, "=" * 65)

        # Push fresh snapshot to Gist so the dashboard clears stopped sessions
        try:
            from scripts.export_dashboard_data import main as _export_main
            _export_main()
            _log(log_file, "  [GIST] Dashboard data pushed.", also_print=False)
        except Exception as _exc:
            _log(log_file, f"  [GIST] Push failed: {_exc}", also_print=False)
