"""Unit tests for src.weather.strategy."""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from src.storage.models import SimPosition, SimSession
from src.weather.strategy import (
    _enter_position,
    _log,
    _settle_positions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    session_id: int = 1,
    bankroll: float = 1000.0,
) -> SimSession:
    """Create a minimal SimSession-like object for testing."""
    s = SimSession(
        initial_bankroll_cents=bankroll,
        current_bankroll_cents=bankroll,
        status="running",
        log_path="logs/test.log",
        total_trades=0,
        won=0,
        lost=0,
        voided=0,
    )
    s.id = session_id
    return s


def _make_position(
    ticker: str = "WEATHER-TX-HIGH-92",
    side: str = "yes",
    ask: int = 40,
    contracts: int = 2,
    session_id: int = 1,
) -> SimPosition:
    cost = ask * contracts
    p = SimPosition(
        session_id=session_id,
        ticker=ticker,
        side=side,
        entry_price_cents=ask,
        cost_cents=cost,
        contracts=contracts,
        arb_type="weather",
        status="open",
        live=0,
    )
    p.id = 99
    return p


def _make_opp(
    market_id: str = "WEATHER-TX-HIGH-92",
    side: str = "yes",
    ask_cents: int = 40,
) -> dict:
    market = types.SimpleNamespace(id=market_id)
    return {
        "market": market,
        "side": side,
        "ask_cents": ask_cents,
        "kalshi_prob": ask_cents / 100,
        "nws_prob": 0.85,
        "edge": 0.25,
    }


# ---------------------------------------------------------------------------
# _log tests
# ---------------------------------------------------------------------------

class TestLog:
    def test_writes_to_file_and_prints(self, capsys: pytest.CaptureFixture) -> None:
        buf = StringIO()
        _log(buf, "hello world")
        output = capsys.readouterr().out
        assert "hello world" in output
        assert "hello world" in buf.getvalue()

    def test_timestamp_format(self) -> None:
        buf = StringIO()
        _log(buf, "msg")
        line = buf.getvalue()
        # Should start with [HH:MM:SS]
        assert line.startswith("[")
        assert "]" in line


# ---------------------------------------------------------------------------
# _settle_positions tests
# ---------------------------------------------------------------------------

class TestSettlePositions:
    def test_won_yes_side(self) -> None:
        """Position with side=yes wins when result=yes."""
        pos = _make_position(side="yes", ask=40, contracts=2)
        session = _make_session(bankroll=500.0)
        fetcher = MagicMock()
        fetcher.get_market_status.return_value = {"result": "yes"}
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos], fetcher, session, db, log_file,
        )

        assert still_open == []
        assert pos.status == "won"
        assert pos.pnl_cents == (100 - 40) * 2  # 120
        # Bankroll: 500 + cost(80) + pnl(120) = 700
        assert session.current_bankroll_cents == 500.0 + 80 + 120
        assert session.won == 1

    def test_lost_yes_side(self) -> None:
        """Position with side=yes loses when result=no."""
        pos = _make_position(side="yes", ask=40, contracts=2)
        session = _make_session(bankroll=500.0)
        fetcher = MagicMock()
        fetcher.get_market_status.return_value = {"result": "no"}
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos], fetcher, session, db, log_file,
        )

        assert still_open == []
        assert pos.status == "lost"
        assert pos.pnl_cents == -80  # -cost
        assert session.current_bankroll_cents == 500.0
        assert session.lost == 1

    def test_won_no_side(self) -> None:
        """Position with side=no wins when result=no."""
        pos = _make_position(side="no", ask=30, contracts=3)
        session = _make_session(bankroll=1000.0)
        fetcher = MagicMock()
        fetcher.get_market_status.return_value = {"result": "no"}
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos], fetcher, session, db, log_file,
        )

        assert pos.status == "won"
        pnl = (100 - 30) * 3  # 210
        assert pos.pnl_cents == pnl
        cost = 30 * 3  # 90
        assert session.current_bankroll_cents == 1000.0 + cost + pnl

    def test_lost_no_side(self) -> None:
        """Position with side=no loses when result=yes."""
        pos = _make_position(side="no", ask=30, contracts=3)
        session = _make_session(bankroll=1000.0)
        fetcher = MagicMock()
        fetcher.get_market_status.return_value = {"result": "yes"}
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos], fetcher, session, db, log_file,
        )

        assert pos.status == "lost"
        assert pos.pnl_cents == -(30 * 3)
        assert session.current_bankroll_cents == 1000.0
        assert session.lost == 1

    def test_unresolved_stays_open(self) -> None:
        """Positions with no result stay open."""
        pos = _make_position()
        session = _make_session()
        fetcher = MagicMock()
        fetcher.get_market_status.return_value = {"result": None}
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos], fetcher, session, db, log_file,
        )

        assert len(still_open) == 1
        assert pos.status == "open"

    def test_empty_string_result_stays_open(self) -> None:
        """Empty string result treated as unresolved."""
        pos = _make_position()
        session = _make_session()
        fetcher = MagicMock()
        fetcher.get_market_status.return_value = {"result": ""}
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos], fetcher, session, db, log_file,
        )

        assert len(still_open) == 1

    def test_fetch_error_keeps_open(self) -> None:
        """Network error keeps position open."""
        pos = _make_position()
        session = _make_session()
        fetcher = MagicMock()
        fetcher.get_market_status.side_effect = Exception("timeout")
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos], fetcher, session, db, log_file,
        )

        assert len(still_open) == 1
        assert "ERR" in log_file.getvalue()

    def test_multiple_positions_mixed(self) -> None:
        """Settle a batch with one won, one lost, one unresolved."""
        pos_won = _make_position(ticker="W-1", side="yes", ask=50, contracts=1)
        pos_lost = _make_position(ticker="W-2", side="yes", ask=60, contracts=1)
        pos_open = _make_position(ticker="W-3", side="no", ask=40, contracts=1)

        session = _make_session(bankroll=800.0)
        fetcher = MagicMock()

        def status_lookup(ticker: str) -> dict:
            if ticker == "W-1":
                return {"result": "yes"}
            if ticker == "W-2":
                return {"result": "no"}
            return {"result": None}

        fetcher.get_market_status.side_effect = status_lookup
        db = MagicMock()
        log_file = StringIO()

        still_open = _settle_positions(
            [pos_won, pos_lost, pos_open],
            fetcher, session, db, log_file,
        )

        assert len(still_open) == 1
        assert still_open[0].ticker == "W-3"
        assert session.won == 1
        assert session.lost == 1


# ---------------------------------------------------------------------------
# _enter_position tests
# ---------------------------------------------------------------------------

class TestEnterPosition:
    def test_basic_sim_entry(self) -> None:
        """Paper-trade entry deducts cost and creates position."""
        session = _make_session(bankroll=1000.0)
        opp = _make_opp(ask_cents=40)
        db = MagicMock()
        fetcher = MagicMock()
        log_file = StringIO()

        _enter_position(opp, session, fetcher, False, db, log_file)

        db.add.assert_called_once()
        pos = db.add.call_args[0][0]
        assert isinstance(pos, SimPosition)
        assert pos.ticker == "WEATHER-TX-HIGH-92"
        assert pos.side == "yes"
        assert pos.entry_price_cents == 40
        assert pos.arb_type == "weather"
        assert pos.status == "open"
        assert pos.live == 0
        # contracts = max(1, int(1000 * 0.025 / 40)) = max(1, int(0.625)) = 1
        assert pos.contracts == 1
        assert pos.cost_cents == 40
        assert session.current_bankroll_cents == 960.0
        assert session.total_trades == 1

    def test_skip_when_insufficient_bankroll(self) -> None:
        """Skip entry when cost exceeds bankroll."""
        session = _make_session(bankroll=10.0)
        opp = _make_opp(ask_cents=50)
        db = MagicMock()
        fetcher = MagicMock()
        log_file = StringIO()

        _enter_position(opp, session, fetcher, False, db, log_file)

        # 1 contract at 50c > 10c bankroll -> skip
        assert "SKIP" in log_file.getvalue()
        db.add.assert_not_called()
        assert session.total_trades == 0

    def test_skip_zero_ask(self) -> None:
        """Skip entry when ask_cents is zero."""
        session = _make_session(bankroll=1000.0)
        opp = _make_opp(ask_cents=0)
        db = MagicMock()
        fetcher = MagicMock()
        log_file = StringIO()

        _enter_position(opp, session, fetcher, False, db, log_file)

        assert "SKIP" in log_file.getvalue()
        db.add.assert_not_called()

    def test_multiple_contracts_with_large_bankroll(self) -> None:
        """Large bankroll yields multiple contracts."""
        session = _make_session(bankroll=10000.0)
        opp = _make_opp(ask_cents=50)
        db = MagicMock()
        fetcher = MagicMock()
        log_file = StringIO()

        _enter_position(opp, session, fetcher, False, db, log_file)

        pos = db.add.call_args[0][0]
        # contracts = max(1, int(10000 * 0.025 / 50)) = max(1, 5) = 5
        assert pos.contracts == 5
        assert pos.cost_cents == 250

    def test_live_order_filled(self) -> None:
        """Live mode places order and records order_id."""
        session = _make_session(bankroll=1000.0)
        opp = _make_opp(ask_cents=40)
        db = MagicMock()
        fetcher = MagicMock()
        fetcher.place_order.return_value = {
            "order_id": "ORD-123",
            "status": "executed",
            "fill_count": 1,
        }
        log_file = StringIO()

        _enter_position(opp, session, fetcher, True, db, log_file)

        fetcher.place_order.assert_called_once_with(
            ticker="WEATHER-TX-HIGH-92",
            side="yes",
            price_cents=40,
            count=1,
        )
        pos = db.add.call_args[0][0]
        assert pos.order_ids == json.dumps(["ORD-123"])
        assert pos.status == "open"
        assert "[LIVE] filled" in log_file.getvalue()

    def test_live_order_no_fill_reverts(self) -> None:
        """Live order with no fill reverts bankroll and voids position."""
        session = _make_session(bankroll=1000.0)
        opp = _make_opp(ask_cents=40)
        db = MagicMock()
        fetcher = MagicMock()
        fetcher.place_order.return_value = {
            "order_id": "ORD-456",
            "status": "cancelled",
            "fill_count": 0,
        }
        log_file = StringIO()

        _enter_position(opp, session, fetcher, True, db, log_file)

        pos = db.add.call_args[0][0]
        assert pos.status == "voided"
        assert session.current_bankroll_cents == 1000.0
        assert session.total_trades == 0
        assert session.voided == 1

    def test_live_no_fill_does_not_log_enter(self) -> None:
        """KPE-48: voided live position must NOT produce an ENTER log."""
        session = _make_session(bankroll=1000.0)
        opp = _make_opp(ask_cents=40)
        db = MagicMock()
        fetcher = MagicMock()
        fetcher.place_order.return_value = {
            "order_id": "ORD-789",
            "status": "cancelled",
            "fill_count": 0,
        }
        log_file = StringIO()

        _enter_position(opp, session, fetcher, True, db, log_file)

        log_output = log_file.getvalue()
        assert "no fill" in log_output
        assert "reverting" in log_output
        assert "ENTER" not in log_output

    def test_live_exception_does_not_log_enter(self) -> None:
        """KPE-48: failed live order must NOT produce an ENTER log."""
        session = _make_session(bankroll=1000.0)
        opp = _make_opp(ask_cents=40)
        db = MagicMock()
        fetcher = MagicMock()
        fetcher.place_order.side_effect = Exception("timeout")
        log_file = StringIO()

        _enter_position(opp, session, fetcher, True, db, log_file)

        log_output = log_file.getvalue()
        assert "order FAILED" in log_output
        assert "ENTER" not in log_output

    def test_live_order_exception_reverts(self) -> None:
        """Live order exception reverts bankroll and voids position."""
        session = _make_session(bankroll=1000.0)
        opp = _make_opp(ask_cents=40)
        db = MagicMock()
        fetcher = MagicMock()
        fetcher.place_order.side_effect = Exception("network error")
        log_file = StringIO()

        _enter_position(opp, session, fetcher, True, db, log_file)

        pos = db.add.call_args[0][0]
        assert pos.status == "voided"
        assert session.current_bankroll_cents == 1000.0
        assert session.total_trades == 0
        assert session.voided == 1
        assert "order FAILED" in log_file.getvalue()


# ---------------------------------------------------------------------------
# run_weather_strategy tests
# ---------------------------------------------------------------------------

class TestRunWeatherStrategy:
    @patch("src.weather.strategy.KalshiFetcher")
    @patch("src.weather.strategy.get_session")
    @patch("src.weather.strategy.scan_weather_markets")
    @patch("src.weather.strategy.time")
    @patch("builtins.open", create=True)
    @patch("src.weather.strategy.os.makedirs")
    def test_creates_session_and_runs_one_cycle(
        self,
        mock_makedirs: MagicMock,
        mock_open_builtin: MagicMock,
        mock_time: MagicMock,
        mock_scan: MagicMock,
        mock_get_session: MagicMock,
        mock_fetcher_cls: MagicMock,
    ) -> None:
        """Strategy creates a session, scans once, then stops on interrupt."""
        from src.weather.strategy import run_weather_strategy

        # Setup mocks
        db = MagicMock()
        mock_get_session.return_value = db
        mock_fetcher_cls.return_value = MagicMock()
        mock_scan.return_value = []

        log_buf = StringIO()
        mock_open_builtin.return_value = log_buf

        # Make time.time() return 0 first (triggers scan),
        # then raise KeyboardInterrupt on sleep.
        mock_time.time.return_value = 0.0
        mock_time.sleep.side_effect = KeyboardInterrupt

        # Mock db.add to set id on session
        def add_side_effect(obj: object) -> None:
            if isinstance(obj, SimSession):
                obj.id = 42

        db.add.side_effect = add_side_effect
        db.query.return_value.filter.return_value.all.return_value = []

        run_weather_strategy(
            live=False,
            bankroll_cents=500,
            interval_seconds=300,
            min_edge=0.05,
        )

        # Session was created and committed
        db.add.assert_called()
        added = db.add.call_args_list[0][0][0]
        assert isinstance(added, SimSession)
        assert added.initial_bankroll_cents == 500
        assert added.status == "stopped"

    @patch("src.weather.strategy.KalshiFetcher")
    @patch("src.weather.strategy.get_session")
    @patch("src.weather.strategy.scan_weather_markets")
    @patch("src.weather.strategy.time")
    @patch("builtins.open", create=True)
    def test_resume_logs_session_bankroll_not_cli_arg(
        self,
        mock_open_builtin: MagicMock,
        mock_time: MagicMock,
        mock_scan: MagicMock,
        mock_get_session: MagicMock,
        mock_fetcher_cls: MagicMock,
    ) -> None:
        """KPE-49: resumed session startup log uses session balance, not CLI arg."""
        from src.weather.strategy import run_weather_strategy

        db = MagicMock()
        mock_get_session.return_value = db
        mock_fetcher_cls.return_value = MagicMock()
        mock_scan.return_value = []

        log_buf = StringIO()
        mock_open_builtin.return_value = log_buf

        mock_time.time.return_value = 0.0
        mock_time.sleep.side_effect = KeyboardInterrupt

        # Resumed session has 750c, but CLI argument is 500c.
        resumed = _make_session(session_id=10, bankroll=750.0)
        resumed.log_path = "logs/test_resume.log"
        db.get.return_value = resumed
        db.query.return_value.filter.return_value.all.return_value = []

        # Prevent the finally block from closing our StringIO.
        log_buf.close = MagicMock()

        run_weather_strategy(
            live=False,
            bankroll_cents=500,
            interval_seconds=300,
            min_edge=0.05,
            session_id=10,
        )

        log_output = log_buf.getvalue()
        # Should show $7.50 (session balance), NOT $5.00 (CLI arg).
        assert "$7.50" in log_output
        assert "$5.00" not in log_output

    @patch("src.weather.strategy.KalshiFetcher")
    @patch("src.weather.strategy.get_session")
    def test_resume_invalid_session_raises(
        self,
        mock_get_session: MagicMock,
        mock_fetcher_cls: MagicMock,
    ) -> None:
        """Resuming a non-running session raises ValueError."""
        from src.weather.strategy import run_weather_strategy

        db = MagicMock()
        mock_get_session.return_value = db
        mock_fetcher_cls.return_value = MagicMock()

        stopped_session = _make_session()
        stopped_session.status = "stopped"
        db.get.return_value = stopped_session

        with pytest.raises(ValueError, match="expected 'running'"):
            run_weather_strategy(
                live=False,
                bankroll_cents=500,
                session_id=1,
            )

    @patch("src.weather.strategy.KalshiFetcher")
    @patch("src.weather.strategy.get_session")
    def test_resume_missing_session_raises(
        self,
        mock_get_session: MagicMock,
        mock_fetcher_cls: MagicMock,
    ) -> None:
        """Resuming a nonexistent session raises ValueError."""
        from src.weather.strategy import run_weather_strategy

        db = MagicMock()
        mock_get_session.return_value = db
        mock_fetcher_cls.return_value = MagicMock()
        db.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            run_weather_strategy(
                live=False,
                bankroll_cents=500,
                session_id=999,
            )
