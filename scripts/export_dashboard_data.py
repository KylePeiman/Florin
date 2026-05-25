#!/usr/bin/env python3
"""Export dashboard data to docs/data.json for the GH Pages static site.

Usage:
    python scripts/export_dashboard_data.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── ticker / event helpers (duplicated from dashboard.py, no Streamlit dep) ──

_SYMBOLS: dict[str, str] = {
    "BTCD": "BTC Daily", "BTCW": "BTC Weekly",
    "SOLE": "SOL", "ETHE": "ETH", "XRPE": "XRP",
    "DOGE": "DOGE", "AVAX": "AVAX", "LINK": "LINK",
}
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_TYPE_LABELS: dict[str, str] = {
    "LAST_SECOND": "Last-Second",
    "PREDICTION":  "Prediction",
    "BINARY":      "Binary Arb",
    "SERIES":      "Series Arb",
    "CROSS":       "Cross-Arb",
    "DIRECTIONAL": "Directional",
}


def _fmt_type(raw: str) -> str:
    return _TYPE_LABELS.get(raw.upper(), raw.title())


def _parse_kalshi_date(s: str) -> str:
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})(\d{2})", s)
    if not m:
        return s
    yy, mon, day, hr = m.groups()
    month_num = _MONTHS.get(mon, 1)
    try:
        dt = datetime(2000 + int(yy), month_num, int(day), int(hr), 0)
        return dt.strftime("%b %d %I:%M %p").replace(" 0", " ")
    except Exception:
        return s


def _describe_event(ticker: str, event_name: str | None = None) -> str:
    if event_name:
        return event_name[:60] + ("…" if len(event_name) > 60 else "")
    raw = ticker.removeprefix("CROSS_")
    m = re.match(r"KX([A-Z]+)-(\d{2}[A-Z]{3}\d{4})", raw)
    if not m:
        return raw
    sym, date_str = m.groups()
    label = _SYMBOLS.get(sym, sym)
    return f"{label} · {_parse_kalshi_date(date_str)}"


def _series_event_ticker(ticker: str) -> str:
    return re.sub(r"-B\d+$", "", ticker)


def _is_live_session(session) -> bool:
    if "LIVE" in (session.log_path or "").upper():
        return True
    return "[LIVE" in _read_log_header(session)


def _read_log_header(session, chars: int = 600) -> str:
    try:
        with open(session.log_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(chars)
    except Exception:
        return ""


def _session_strategy(session) -> str:
    header = _read_log_header(session)
    parts = []
    if "+LAST-SEC" in header or any(p.arb_type == "last_second" for p in session.positions):
        parts.append("Last-Sec")
    if "+PREDICTION" in header or any(
        p.arb_type is None and p.ev and p.ev > 0 for p in session.positions
    ):
        parts.append("Prediction")
    if not parts:
        if any(p.arb_type in ("series", "binary", "cross") for p in session.positions):
            parts.append("Arb (legacy)")
    return " · ".join(parts) if parts else "—"


# ── exporters ────────────────────────────────────────────────────────────────

def export_sessions(db) -> tuple[list[dict], list[dict]]:
    from src.storage.models import SimSession
    rows = db.query(SimSession).order_by(SimSession.created_at.desc()).all()
    sim_rows: list[dict] = []
    live_rows: list[dict] = []
    for s in rows:
        total = s.total_value_cents()
        initial = s.initial_bankroll_cents
        pnl_cents = total - initial
        pnl_pct = pnl_cents / initial * 100 if initial else 0.0
        record = {
            "id": s.id,
            "started": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "—",
            "status": s.status,
            "strategy": _session_strategy(s),
            "balance": round(total / 100, 4),
            "pnl": round(pnl_cents / 100, 4),
            "gain_pct": round(pnl_pct, 2),
            "trades": s.total_trades,
            "w": s.won,
            "l": s.lost,
            "v": s.voided,
            "pool_id": getattr(s, "pool_id", None),
            "worker_index": getattr(s, "worker_index", None),
        }
        (live_rows if _is_live_session(s) else sim_rows).append(record)
    return sim_rows, live_rows


def export_open_positions(db) -> tuple[list[dict], list[dict]]:
    from src.storage.models import SimPosition, SimSession
    positions = (
        db.query(SimPosition)
        .join(SimSession, SimPosition.session_id == SimSession.id)
        .filter(SimPosition.status == "open", SimSession.status == "running")
        .order_by(SimPosition.created_at.desc())
        .all()
    )

    series_groups: dict[str, list] = defaultdict(list)
    singles: list = []
    for p in positions:
        if p.arb_type == "series":
            series_groups[f"{p.session_id}|{_series_event_ticker(p.ticker)}"].append(p)
        else:
            singles.append(p)

    sim_rows: list[dict] = []
    live_rows: list[dict] = []

    for _key, legs in series_groups.items():
        rep = legs[0]
        row = {
            "session": rep.session_id,
            "type": "Series Arb",
            "event": _describe_event(_series_event_ticker(rep.ticker)),
            "contracts": rep.contracts,
            "cost": round(sum(l.cost_cents for l in legs) / 100, 4),
            "entered": rep.created_at.strftime("%m-%d %H:%M") if rep.created_at else "—",
        }
        (live_rows if rep.live else sim_rows).append(row)

    for p in singles:
        arb_type = p.arb_type or "prediction"
        event_name = None
        if arb_type == "cross" and p.legs:
            kalshi_leg = next(
                (l for l in p.legs if l.get("source") == "kalshi"), p.legs[0]
            )
            event_name = kalshi_leg.get("event_name")
        row = {
            "session": p.session_id,
            "type": _fmt_type(arb_type),
            "event": _describe_event(p.ticker.removeprefix("CROSS_"), event_name),
            "contracts": p.contracts,
            "cost": round(p.cost_cents / 100, 4),
            "entered": p.created_at.strftime("%m-%d %H:%M") if p.created_at else "—",
        }
        (live_rows if p.live else sim_rows).append(row)

    return sim_rows, live_rows


def export_trade_history(db, limit: int = 500) -> tuple[list[dict], list[dict]]:
    from src.storage.models import SimPosition
    positions = (
        db.query(SimPosition)
        .filter(SimPosition.status != "open")
        .order_by(SimPosition.settled_at.desc())
        .limit(limit)
        .all()
    )

    series_groups: dict[str, list] = defaultdict(list)
    singles: list = []
    for p in positions:
        if p.arb_type == "series":
            series_groups[f"{p.session_id}|{_series_event_ticker(p.ticker)}"].append(p)
        else:
            singles.append(p)

    sim_arbs: list[dict] = []
    live_arbs: list[dict] = []

    for _key, legs in series_groups.items():
        rep = legs[0]
        total_cost = sum(l.cost_cents for l in legs) / 100
        total_pnl = sum((l.pnl_cents or 0) for l in legs) / 100
        statuses = {l.status for l in legs}
        net_result = (
            "VOIDED" if statuses == {"voided"} else ("WON" if total_pnl > 0 else "LOST")
        )
        arb = {
            "result": net_result,
            "type": "Series Arb",
            "event": _describe_event(_series_event_ticker(rep.ticker)),
            "contracts": rep.contracts,
            "cost": round(total_cost, 4),
            "pnl": round(total_pnl, 4),
            "roi": round(total_pnl / total_cost * 100 if total_cost else 0, 2),
            "settled": rep.settled_at.strftime("%m-%d %H:%M") if rep.settled_at else "—",
            "session": rep.session_id,
            "legs": [
                {
                    "ticker": l.ticker,
                    "side": l.side.upper(),
                    "entry_cents": round(l.entry_price_cents, 1),
                    "contracts": l.contracts,
                    "result": l.status.upper(),
                    "pnl": round((l.pnl_cents or 0) / 100, 4),
                }
                for l in sorted(legs, key=lambda l: l.ticker)
            ],
        }
        (live_arbs if rep.live else sim_arbs).append(arb)

    for p in singles:
        arb_type = p.arb_type or "prediction"
        event_name = None
        if arb_type == "cross" and p.legs:
            kalshi_leg = next(
                (l for l in p.legs if l.get("source") == "kalshi"), p.legs[0]
            )
            event_name = kalshi_leg.get("event_name")
        cost = p.cost_cents / 100
        pnl = (p.pnl_cents or 0) / 100
        raw_legs = p.legs or [
            {"ticker": p.ticker, "side": p.side, "price_cents": p.entry_price_cents}
        ]
        arb = {
            "result": p.status.upper(),
            "type": _fmt_type(arb_type),
            "event": _describe_event(p.ticker.removeprefix("CROSS_"), event_name),
            "contracts": p.contracts,
            "cost": round(cost, 4),
            "pnl": round(pnl, 4),
            "roi": round(pnl / cost * 100 if cost else 0, 2),
            "settled": p.settled_at.strftime("%m-%d %H:%M") if p.settled_at else "—",
            "session": p.session_id,
            "legs": [
                {
                    "ticker": l.get("ticker", p.ticker),
                    "side": l.get("side", p.side).upper(),
                    "entry_cents": round(l.get("price_cents", p.entry_price_cents), 1),
                    "contracts": p.contracts,
                    "result": p.status.upper(),
                    "pnl": round(pnl, 4),
                }
                for l in raw_legs
            ],
        }
        (live_arbs if p.live else sim_arbs).append(arb)

    sim_arbs.sort(key=lambda x: x["settled"], reverse=True)
    live_arbs.sort(key=lambda x: x["settled"], reverse=True)
    return sim_arbs, live_arbs


# ── Gist upload ──────────────────────────────────────────────────────────────

def push_to_gist(content: str) -> bool:
    """PATCH the Gist with updated data.json content. Returns True on success."""
    import urllib.request
    from config.settings import settings

    token = settings.GH_GIST_TOKEN
    gist_id = settings.GH_GIST_ID
    if not token or not gist_id:
        return False

    payload = json.dumps({"files": {"data.json": {"content": content}}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


# ── pool summaries ────────────────────────────────────────────────────────────

def export_pool_summaries(db) -> dict:
    from src.storage.models import SimSession
    rows = db.query(SimSession).filter(SimSession.pool_id.isnot(None)).all()
    pools: dict[int, list] = {}
    for s in rows:
        pools.setdefault(s.pool_id, []).append(s)

    sim_pools: list[dict] = []
    live_pools: list[dict] = []
    for pool_id, sessions in pools.items():
        total_trades = sum(s.total_trades for s in sessions)
        workers_active = sum(1 for s in sessions if s.status == "running")
        initial = sum(s.initial_bankroll_cents for s in sessions)
        current = sum(s.total_value_cents() for s in sessions)
        # Skip ghost pools (crash-loops that produced no trades and no P&L)
        if total_trades == 0 and current == initial and workers_active > 100:
            continue
        entry = {
            "pool_id": pool_id,
            "workers_total": len(sessions),
            "workers_active": workers_active,
            "initial_dollars": round(initial / 100, 2),
            "value_dollars": round(current / 100, 2),
            "pnl_dollars": round((current - initial) / 100, 4),
            "total_trades": total_trades,
            "won": sum(s.won for s in sessions),
            "lost": sum(s.lost for s in sessions),
        }
        is_live = any(_is_live_session(s) for s in sessions)
        (live_pools if is_live else sim_pools).append(entry)

    sim_pools.sort(key=lambda x: x["pool_id"], reverse=True)
    live_pools.sort(key=lambda x: x["pool_id"], reverse=True)
    return {"sim": sim_pools, "live": live_pools}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    from src.storage.db import get_session

    db = get_session()

    balance_dollars = None
    try:
        from src.fetchers.kalshi import KalshiFetcher
        balance_dollars = round(KalshiFetcher().get_balance() / 100, 2)
    except Exception as exc:
        print(f"  [warn] Could not fetch Kalshi balance: {exc}")

    sim_sessions, live_sessions = export_sessions(db)
    sim_open, live_open = export_open_positions(db)
    sim_hist, live_hist = export_trade_history(db)
    pool_summaries = export_pool_summaries(db)

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "balance_dollars": balance_dollars,
        "sessions": {"sim": sim_sessions, "live": live_sessions},
        "open_positions": {"sim": sim_open, "live": live_open},
        "trade_history": {"sim": sim_hist, "live": live_hist},
        "pool_summary": pool_summaries,
    }

    content = json.dumps(data, indent=2)

    out_path = ROOT / "docs" / "data.json"
    out_path.write_text(content, encoding="utf-8")

    ok = push_to_gist(content)

    print(f"Wrote {out_path}" + (" + Gist updated" if ok else " (Gist skipped — no token)"))
    print(f"  generated_at : {data['generated_at']}")
    print(f"  balance      : {'$' + str(balance_dollars) if balance_dollars is not None else 'n/a'}")
    print(f"  sessions     : {len(sim_sessions)} sim, {len(live_sessions)} live")
    print(f"  open pos     : {len(sim_open)} sim, {len(live_open)} live")
    print(f"  history      : {len(sim_hist)} sim, {len(live_hist)} live")
    print(f"  pools        : {len(pool_summaries['sim'])} sim, {len(pool_summaries['live'])} live")


if __name__ == "__main__":
    main()
