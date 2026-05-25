"""Database setup — SQLite via SQLAlchemy, upgrade-ready for Postgres."""
from __future__ import annotations
import threading
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from src.storage.models import Base, SimPosition
from config.settings import settings

_engine = None
_SessionLocal = None


def _migrate(engine):
    """Add new columns to existing tables without dropping data."""
    with engine.connect() as conn:
        new_columns = [
            ("sim_positions", "live", "INTEGER DEFAULT 0"),
            ("sim_positions", "order_ids", "TEXT"),
            ("sim_sessions", "opening_adjustment_cents", "REAL DEFAULT 0.0"),
            ("sim_positions", "entry_nws_prob", "REAL"),
            ("sim_positions", "entry_edge", "REAL"),
            ("sim_sessions", "pool_id", "INTEGER"),
            ("sim_sessions", "worker_index", "INTEGER"),
        ]
        for table, col, col_type in new_columns:
            try:
                conn.execute(__import__("sqlalchemy").text(
                    f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                ))
                conn.commit()
            except Exception:
                pass  # column already exists


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
        )
        Base.metadata.create_all(_engine)
        _migrate(_engine)
    return _engine


def _run_export() -> None:
    """Export dashboard data in a background thread. Errors are swallowed so
    they never affect the trading loop."""
    try:
        from scripts.export_dashboard_data import main as export_main
        export_main()
    except Exception:
        pass


def _before_commit(session) -> None:
    """Stash a flag if SimPosition rows are part of this transaction.
    (session.new/dirty/deleted are cleared before after_commit fires.)"""
    session.info["_export_pending"] = any(
        isinstance(obj, SimPosition)
        for obj in list(session.new) + list(session.dirty) + list(session.deleted)
    )


def _after_commit(session) -> None:
    """After a successful commit, kick off a background export if positions changed."""
    if session.info.pop("_export_pending", False):
        threading.Thread(target=_run_export, daemon=True).start()


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), autocommit=False, autoflush=False)
    session = _SessionLocal()
    event.listen(session, "before_commit", _before_commit)
    event.listen(session, "after_commit", _after_commit)
    return session
