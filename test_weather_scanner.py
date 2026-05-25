"""Tests for src.weather.scanner — weather edge scanner."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.weather.scanner import (
    _threshold_probability,
    get_nws_probability,
    scan_weather_markets,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight stand-ins for Market / Selection
# ---------------------------------------------------------------------------


@dataclass
class FakeSelection:
    name: str = "Yes"
    odds: float = 2.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeMarket:
    id: str = "WEATHER-TEST"
    event_name: str = "Will the high temperature in Chicago exceed 75°F on March 22?"
    name: str = "Will the high temperature in Chicago exceed 75°F on March 22?"
    starts_at: datetime.datetime | None = None
    selections: list[FakeSelection] = field(default_factory=list)
    source: str = "kalshi"
    category: str = "weather"
    metadata: dict[str, Any] = field(default_factory=dict)


def _make_forecast_periods(
    target_date: datetime.date,
    temps: list[float] | None = None,
    precips: list[int] | None = None,
    winds: list[float] | None = None,
    hours: int = 24,
) -> list[dict[str, Any]]:
    """Build a list of fake hourly forecast periods for a given date."""
    periods = []
    for h in range(hours):
        periods.append(
            {
                "time": datetime.datetime(
                    target_date.year,
                    target_date.month,
                    target_date.day,
                    h,
                    tzinfo=datetime.timezone.utc,
                ),
                "temp_f": (temps[h] if temps else 70.0),
                "precip_pct": (precips[h] if precips else 0),
                "wind_mph": (winds[h] if winds else 5.0),
                "short_forecast": "Clear",
            }
        )
    return periods


# ---------------------------------------------------------------------------
# get_nws_probability
# ---------------------------------------------------------------------------


class TestGetNwsProbability:
    """Tests for get_nws_probability."""

    def test_returns_none_for_empty_forecast(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        assert get_nws_probability(parsed, []) is None

    def test_returns_none_when_no_periods_match_date(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        wrong_day = _make_forecast_periods(datetime.date(2026, 3, 23))
        assert get_nws_probability(parsed, wrong_day) is None

    def test_precip_average(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "precip",
            "threshold": 0.5,
            "direction": "above",
        }
        # 4 hours, precip_pct = [20, 40, 60, 80] → avg = 50 → 0.50
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            precips=[20, 40, 60, 80],
            hours=4,
        )
        result = get_nws_probability(parsed, forecast)
        assert result == pytest.approx(0.50)

    def test_precip_below_direction(self) -> None:
        """KPE-47: direction='below' should invert the probability."""
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "precip",
            "threshold": 0.5,
            "direction": "below",
        }
        # 4 hours, precip_pct = [60, 70, 80, 70] → avg = 70 → 0.70
        # direction="below" → 1.0 - 0.70 = 0.30
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            precips=[60, 70, 80, 70],
            hours=4,
        )
        result = get_nws_probability(parsed, forecast)
        assert result == pytest.approx(0.30)

    def test_precip_zero(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "precip",
            "threshold": 0.1,
            "direction": "above",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            precips=[0, 0, 0],
            hours=3,
        )
        result = get_nws_probability(parsed, forecast)
        assert result == pytest.approx(0.0)

    def test_high_temp_well_above(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "high_temp",
            "threshold": 70.0,
            "direction": "above",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            temps=[65.0, 68.0, 80.0],
            hours=3,
        )
        # max = 80, diff = 10 > 3 → 0.85
        assert get_nws_probability(parsed, forecast) == 0.85

    def test_high_temp_well_below(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "high_temp",
            "threshold": 90.0,
            "direction": "above",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            temps=[60.0, 65.0, 70.0],
            hours=3,
        )
        # max = 70, diff = -20 < -3 → 0.15
        assert get_nws_probability(parsed, forecast) == 0.15

    def test_high_temp_near_threshold(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "high_temp",
            "threshold": 72.0,
            "direction": "above",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            temps=[68.0, 70.0, 73.0],
            hours=3,
        )
        # max = 73, diff = 1 → abs(1) <= 3 → 0.50
        assert get_nws_probability(parsed, forecast) == 0.50

    def test_low_temp_below_direction(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "low_temp",
            "threshold": 40.0,
            "direction": "below",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            temps=[30.0, 35.0, 45.0],
            hours=3,
        )
        # min = 30, diff = 30 - 40 = -10 < -3, direction=below → 0.85
        assert get_nws_probability(parsed, forecast) == 0.85

    def test_low_temp_above_threshold_below_direction(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "low_temp",
            "threshold": 30.0,
            "direction": "below",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            temps=[40.0, 45.0, 50.0],
            hours=3,
        )
        # min = 40, diff = 40 - 30 = 10 > 3, direction=below → 0.15
        assert get_nws_probability(parsed, forecast) == 0.15

    def test_wind_above(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "wind",
            "threshold": 20.0,
            "direction": "above",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22),
            winds=[10.0, 15.0, 30.0],
            hours=3,
        )
        # max = 30, diff = 10 > 3 → 0.85
        assert get_nws_probability(parsed, forecast) == 0.85

    def test_unknown_metric_returns_none(self) -> None:
        parsed = {
            "date": datetime.date(2026, 3, 22),
            "metric": "humidity",
            "threshold": 50.0,
            "direction": "above",
        }
        forecast = _make_forecast_periods(
            datetime.date(2026, 3, 22), hours=3
        )
        assert get_nws_probability(parsed, forecast) is None


# ---------------------------------------------------------------------------
# _threshold_probability
# ---------------------------------------------------------------------------


class TestThresholdProbability:
    """Tests for the threshold heuristic helper."""

    def test_above_well_over(self) -> None:
        assert _threshold_probability(80.0, 70.0, "above") == 0.85

    def test_above_near(self) -> None:
        assert _threshold_probability(71.0, 70.0, "above") == 0.50

    def test_above_well_under(self) -> None:
        assert _threshold_probability(60.0, 70.0, "above") == 0.15

    def test_below_well_under(self) -> None:
        assert _threshold_probability(60.0, 70.0, "below") == 0.85

    def test_below_near(self) -> None:
        assert _threshold_probability(69.0, 70.0, "below") == 0.50

    def test_below_well_over(self) -> None:
        assert _threshold_probability(80.0, 70.0, "below") == 0.15

    def test_exact_boundary_above(self) -> None:
        # diff = 3 exactly → abs(3) <= 3 → 0.50
        assert _threshold_probability(73.0, 70.0, "above") == 0.50

    def test_exact_boundary_below(self) -> None:
        # diff = -3 exactly → abs(-3) <= 3 → 0.50
        assert _threshold_probability(67.0, 70.0, "below") == 0.50


# ---------------------------------------------------------------------------
# scan_weather_markets
# ---------------------------------------------------------------------------


class TestScanWeatherMarkets:
    """Tests for scan_weather_markets end-to-end."""

    def _make_today_market(
        self,
        market_id: str = "WEATHER-CHI-HIGH",
        title: str = (
            "Will the high temperature in Chicago "
            "exceed 75°F on March 22?"
        ),
        yes_ask: int = 50,
        no_ask: int = 52,
    ) -> FakeMarket:
        today = datetime.date.today()
        return FakeMarket(
            id=market_id,
            event_name=title,
            name=title,
            starts_at=datetime.datetime(
                today.year,
                today.month,
                today.day,
                18,
                0,
                tzinfo=datetime.timezone.utc,
            ),
            selections=[
                FakeSelection(
                    name="Yes",
                    odds=2.0,
                    metadata={"yes_ask": yes_ask, "no_ask": no_ask},
                ),
                FakeSelection(
                    name="No",
                    odds=2.0,
                    metadata={"yes_ask": yes_ask, "no_ask": no_ask},
                ),
            ],
        )

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_finds_edge_opportunity(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        today = datetime.date.today()
        market = self._make_today_market(yes_ask=50, no_ask=52)

        mock_parse.return_value = {
            "city": "Chicago",
            "lat": 41.8781,
            "lon": -87.6298,
            "date": today,
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        # Forecast: max temp = 85 → prob 0.85, kalshi_prob = 0.50
        mock_forecast.return_value = _make_forecast_periods(
            today, temps=[60.0, 70.0, 85.0], hours=3
        )

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 1
        assert results[0]["side"] == "yes"
        assert results[0]["ask_cents"] == 50
        assert results[0]["nws_prob"] == 0.85
        assert results[0]["kalshi_prob"] == 0.50
        assert results[0]["edge"] == pytest.approx(0.35)

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_no_side_picks_no_when_nws_lower(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        today = datetime.date.today()
        market = self._make_today_market(yes_ask=80, no_ask=22)

        mock_parse.return_value = {
            "city": "Chicago",
            "lat": 41.8781,
            "lon": -87.6298,
            "date": today,
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        # Forecast: max temp = 60 → prob 0.15, kalshi_prob = 0.80
        mock_forecast.return_value = _make_forecast_periods(
            today, temps=[50.0, 55.0, 60.0], hours=3
        )

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 1
        assert results[0]["side"] == "no"
        assert results[0]["ask_cents"] == 22

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_skips_below_min_edge(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        today = datetime.date.today()
        market = self._make_today_market(yes_ask=50, no_ask=52)

        mock_parse.return_value = {
            "city": "Chicago",
            "lat": 41.8781,
            "lon": -87.6298,
            "date": today,
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        # Forecast: max temp = 76 → prob 0.50, kalshi_prob = 0.50
        # edge = 0.0 < min_edge
        mock_forecast.return_value = _make_forecast_periods(
            today, temps=[70.0, 74.0, 76.0], hours=3
        )

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_skips_market_closing_tomorrow(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        market = FakeMarket(
            id="WEATHER-TOMORROW",
            event_name="Will Chicago high exceed 80°F tomorrow?",
            name="Will Chicago high exceed 80°F tomorrow?",
            starts_at=datetime.datetime(
                tomorrow.year,
                tomorrow.month,
                tomorrow.day,
                18,
                0,
                tzinfo=datetime.timezone.utc,
            ),
            selections=[
                FakeSelection(
                    metadata={"yes_ask": 50, "no_ask": 52}
                ),
            ],
        )

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0
        mock_parse.assert_not_called()

    def test_skips_market_with_no_selections(self) -> None:
        market = FakeMarket(
            id="WEATHER-EMPTY",
            starts_at=datetime.datetime.now(datetime.timezone.utc),
            selections=[],
        )
        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0

    def test_skips_market_with_zero_yes_ask(self) -> None:
        market = FakeMarket(
            id="WEATHER-ZERO",
            starts_at=datetime.datetime.now(datetime.timezone.utc),
            selections=[
                FakeSelection(metadata={"yes_ask": 0, "no_ask": 50}),
            ],
        )
        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0

    @patch("src.weather.scanner.parse_weather_market")
    def test_skips_unparseable_market(
        self,
        mock_parse: MagicMock,
    ) -> None:
        market = self._make_today_market()
        mock_parse.return_value = None

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_skips_when_nws_returns_none(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        today = datetime.date.today()
        market = self._make_today_market()

        mock_parse.return_value = {
            "city": "Chicago",
            "lat": 41.8781,
            "lon": -87.6298,
            "date": today,
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        mock_forecast.return_value = []

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_handles_exception_per_market(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        """One market raising should not crash the whole scan."""
        today = datetime.date.today()
        bad_market = self._make_today_market(market_id="BAD")
        good_market = self._make_today_market(
            market_id="GOOD", yes_ask=50, no_ask=52
        )

        call_count = 0

        def parse_side_effect(m: Any) -> dict | None:
            nonlocal call_count
            call_count += 1
            if m.id == "BAD":
                raise RuntimeError("boom")
            return {
                "city": "Chicago",
                "lat": 41.8781,
                "lon": -87.6298,
                "date": today,
                "metric": "high_temp",
                "threshold": 75.0,
                "direction": "above",
            }

        mock_parse.side_effect = parse_side_effect
        mock_forecast.return_value = _make_forecast_periods(
            today, temps=[60.0, 70.0, 85.0], hours=3
        )

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [bad_market, good_market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        # The good market should still be returned.
        assert len(results) == 1
        assert results[0]["market"].id == "GOOD"

    def test_returns_empty_when_fetcher_raises(self) -> None:
        fetcher = MagicMock()
        fetcher.get_markets.side_effect = RuntimeError("network down")

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert results == []

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_skips_market_with_no_close_time(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        market = FakeMarket(
            id="WEATHER-NOTIME",
            starts_at=None,
            selections=[
                FakeSelection(metadata={"yes_ask": 50, "no_ask": 52}),
            ],
        )
        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_equal_prob_skipped(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        today = datetime.date.today()
        # yes_ask = 50 → kalshi_prob = 0.50
        market = self._make_today_market(yes_ask=50, no_ask=52)

        mock_parse.return_value = {
            "city": "Chicago",
            "lat": 41.8781,
            "lon": -87.6298,
            "date": today,
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        # nws_prob = 0.50 == kalshi_prob → skip
        mock_forecast.return_value = _make_forecast_periods(
            today, temps=[70.0, 74.0, 76.0], hours=3
        )

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.0)

        assert len(results) == 0

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    def test_skips_no_side_with_zero_no_ask(
        self,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        """KPE-46: no_ask=0 should not produce a valid opportunity."""
        today = datetime.date.today()
        # nws_prob will be < kalshi_prob → side="no", but no_ask=0
        market = self._make_today_market(yes_ask=80, no_ask=0)

        mock_parse.return_value = {
            "city": "Chicago",
            "lat": 41.8781,
            "lon": -87.6298,
            "date": today,
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        # max temp = 60 → prob 0.15, kalshi_prob = 0.80 → side="no"
        mock_forecast.return_value = _make_forecast_periods(
            today, temps=[50.0, 55.0, 60.0], hours=3
        )

        fetcher = MagicMock()
        fetcher.get_markets.return_value = [market]

        results = scan_weather_markets(fetcher, min_edge=0.05)

        assert len(results) == 0
