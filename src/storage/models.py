"""SQLAlchemy ORM models."""
from __future__ import annotations
import json
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, Enum
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _now():
    return datetime.now(timezone.utc)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)
    period = Column(String(20), nullable=False)        # "week" | "month"
    mode = Column(String(20), nullable=False)           # "agent" | "compute"
    source = Column(String(50), nullable=False)
    category = Column(String(100), nullable=False)
    event_name = Column(String(255), nullable=False)
    selection = Column(String(255), nullable=False)
    odds = Column(Float, nullable=False)
    stake_units = Column(Float, nullable=False, default=1.0)
    confidence = Column(Float, nullable=False, default=0.5)
    rationale = Column(Text, nullable=False, default="")
    status = Column(String(20), default="pending")      # "pending" | "settled"

    outcome = relationship("Outcome", back_populates="recommendation", uselist=False)

    def __repr__(self):
        return f"<Recommendation id={self.id} event='{self.event_name}' sel='{self.selection}' odds={self.odds}>"


class Outcome(Base):
    __tablename__ = "outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recommendation_id = Column(Integer, ForeignKey("recommendations.id"), nullable=False, unique=True)
    result = Column(String(10), nullable=False)         # "win" | "loss" | "void"
    actual_odds = Column(Float, nullable=True)
    settled_at = Column(DateTime, default=_now)

    recommendation = relationship("Recommendation", back_populates="outcome")


class SimulatedBet(Base):
    """
    A paper-trade bet on a Kalshi market, used for performance tracking and training.
    Auto-settled by polling the Kalshi API once the market resolves.
    """
    __tablename__ = "simulated_bets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)

    # Kalshi market identifiers
    ticker = Column(String(100), nullable=False)
    title = Column(String(255), nullable=False, default="")
    category = Column(String(100), nullable=False, default="")

    # Bet details
    side = Column(String(4), nullable=False)        # "yes" | "no"
    entry_price_cents = Column(Float, nullable=False)  # 1–99
    entry_odds = Column(Float, nullable=False)         # decimal odds
    stake_units = Column(Float, nullable=False, default=1.0)
    kelly_fraction = Column(Float, nullable=False, default=0.0)
    ev = Column(Float, nullable=False, default=0.0)
    confidence = Column(Float, nullable=False, default=0.5)
    rationale = Column(Text, nullable=False, default="")

    # Market expiry (when Kalshi will resolve this market)
    closes_at = Column(DateTime, nullable=True)

    # Settlement
    status = Column(String(10), default="open")    # "open" | "settled" | "expired"
    result = Column(String(6), nullable=True)       # "win" | "loss" | "void" | None
    exit_price_cents = Column(Float, nullable=True)
    pnl_units = Column(Float, nullable=True)        # profit/loss in stake units
    settled_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return (
            f"<SimulatedBet id={self.id} ticker={self.ticker!r} "
            f"side={self.side} @ {self.entry_price_cents}¢ status={self.status}>"
        )


class ArbSimulation(Base):
    """A simulated arbitrage trade across one or more Kalshi market legs."""
    __tablename__ = "arb_simulations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)

    arb_type = Column(String(10), nullable=False)   # "binary" | "series"
    event_ticker = Column(String(100), nullable=False, default="")
    category = Column(String(100), nullable=False, default="")
    title = Column(String(255), nullable=False, default="")

    # JSON list of {"ticker": str, "side": "yes"|"no", "price_cents": int}
    _legs = Column("legs", Text, nullable=False, default="[]")

    total_cost_cents = Column(Float, nullable=False)   # what we pay per set of contracts
    profit_cents = Column(Float, nullable=False)       # guaranteed profit per set (binary) or expected (series)
    profit_pct = Column(Float, nullable=False)         # profit_cents / total_cost_cents
    guaranteed = Column(Integer, nullable=False, default=0)  # 1=risk-free, 0=requires exhaustive series

    closes_at = Column(DateTime, nullable=True)

    # Settlement
    status = Column(String(10), default="open")   # "open" | "won" | "lost" | "voided"
    result_pnl_cents = Column(Float, nullable=True)
    settled_at = Column(DateTime, nullable=True)

    @property
    def legs(self) -> list[dict]:
        import json
        return json.loads(self._legs or "[]")

    @legs.setter
    def legs(self, value: list[dict]):
        import json
        self._legs = json.dumps(value)

    def __repr__(self):
        return (
            f"<ArbSimulation id={self.id} type={self.arb_type} "
            f"event={self.event_ticker!r} profit={self.profit_pct:.1%} status={self.status}>"
        )


class SimSession(Base):
    """A live simulation session with a virtual bankroll."""
    __tablename__ = "sim_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)
    stopped_at = Column(DateTime, nullable=True)

    initial_bankroll_cents = Column(Float, nullable=False)
    current_bankroll_cents = Column(Float, nullable=False)  # liquid (not counting locked)
    status = Column(String(10), default="running")          # "running" | "stopped"
    log_path = Column(String(255), nullable=False, default="")

    total_trades = Column(Integer, default=0)
    won = Column(Integer, default=0)
    lost = Column(Integer, default=0)
    voided = Column(Integer, default=0)
    opening_adjustment_cents = Column(Float, default=0.0, nullable=True)  # gap vs prev session end
    pool_id = Column(Integer, nullable=True)       # groups sessions under one WorkerPool run
    worker_index = Column(Integer, nullable=True)  # 0-based index within the pool

    positions = relationship("SimPosition", back_populates="session")

    def locked_cents(self) -> float:
        return sum(p.cost_cents for p in self.positions if p.status == "open")

    def total_value_cents(self) -> float:
        return self.current_bankroll_cents + self.locked_cents()

    def __repr__(self):
        return (
            f"<SimSession id={self.id} bankroll={self.current_bankroll_cents:.0f}¢ "
            f"status={self.status}>"
        )


class SimPosition(Base):
    """A single open or closed position within a SimSession."""
    __tablename__ = "sim_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)
    session_id = Column(Integer, ForeignKey("sim_sessions.id"), nullable=False)

    ticker = Column(String(100), nullable=False)   # market ticker or event_ticker for series
    side = Column(String(8), nullable=False)        # "yes" | "no" | "yes_no" | "series"
    entry_price_cents = Column(Float, nullable=False)
    cost_cents = Column(Float, nullable=False)      # total cash spent on this position
    contracts = Column(Integer, nullable=False, default=1)
    ev = Column(Float, nullable=False, default=0.0)
    arb_type = Column(String(10), nullable=True)    # None | "binary" | "series"
    # JSON legs for multi-leg positions: [{"ticker": str, "side": str, "price_cents": int}]
    _legs = Column("legs", Text, nullable=True)

    status = Column(String(8), default="open")      # "open" | "won" | "lost" | "voided"
    result = Column(String(10), nullable=True)
    pnl_cents = Column(Float, nullable=True)
    settled_at = Column(DateTime, nullable=True)

    # Weather strategy entry metadata (used for position recheck logic)
    entry_nws_prob = Column(Float, nullable=True)   # NWS probability at entry
    entry_edge = Column(Float, nullable=True)        # abs edge at entry

    # Live trading fields
    live = Column(Integer, default=0)           # 1 = real Kalshi orders were placed
    order_ids = Column(Text, nullable=True)     # JSON list of Kalshi order IDs

    session = relationship("SimSession", back_populates="positions")

    @property
    def legs(self) -> list[dict]:
        return json.loads(self._legs or "[]")

    @legs.setter
    def legs(self, value: list[dict]):
        self._legs = json.dumps(value)

    def __repr__(self):
        return (
            f"<SimPosition id={self.id} ticker={self.ticker!r} "
            f"side={self.side} cost={self.cost_cents:.0f}¢ status={self.status}>"
        )


class EvaluationReport(Base):
    __tablename__ = "evaluation_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    roi = Column(Float, nullable=False)
    hit_rate = Column(Float, nullable=False)
    clv_avg = Column(Float, nullable=True)
    units_profit = Column(Float, nullable=False)
    total_bets = Column(Integer, nullable=False)
    _mode_breakdown = Column("mode_breakdown", Text, default="{}")

    @property
    def mode_breakdown(self) -> dict:
        return json.loads(self._mode_breakdown or "{}")

    @mode_breakdown.setter
    def mode_breakdown(self, value: dict):
        self._mode_breakdown = json.dumps(value)
