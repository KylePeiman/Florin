"""Unit tests for the weather strategy modules.

Tests cover:
- src/weather/market_parser.py -- parse_weather_market()
- src/weather/noaa.py -- get_forecast() with mocked HTTP
- src/weather/scanner.py -- get_nws_probability() and scan_weather_markets()
  (skipped if scanner module is not yet implemented)

Linear task: KPE-43
"""

import datetime
import unittest
from unittest.mock import MagicMock, patch

from src.fetchers.base import Market, Selection
from src.weather.market_parser import CITY_COORDS, parse_weather_market
from src.weather import noaa
from src.weather.noaa import get_forecast

# ---------------------------------------------------------------------------
# Attempt to import scanner (may not exist yet)
# ---------------------------------------------------------------------------

_scanner_available = True
try:
    from src.weather.scanner import get_nws_probability, scan_weather_markets
except ImportError:
    _scanner_available = False
    get_nws_probability = None  # type: ignore[assignment]
    scan_weather_markets = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockMarket:
    """Lightweight stand-in for a Kalshi market object.

    Used by parse_weather_market tests which only need ``.name`` and
    ``.market_id`` attributes.

    Attributes:
        name: Human-readable market title.
        market_id: Unique market identifier.
    """

    def __init__(self, name: str, market_id: str = "MOCK-001") -> None:
        self.name = name
        self.market_id = market_id


def _make_scanner_market(
    market_id: str = "WEATHER-001",
    yes_ask: int = 60,
    no_ask: int = 40,
    starts_at: datetime.datetime | None = None,
    name: str = "Weather market",
) -> MagicMock:
    """Build a MagicMock that mimics the Market dataclass as used by
    scanner.scan_weather_markets.

    The scanner accesses: market.id, market.selections,
    market.selections[0].metadata, market.starts_at, and
    market.name (via parse_weather_market).
    """
    selection = MagicMock()
    selection.metadata = {"yes_ask": yes_ask, "no_ask": no_ask}

    market = MagicMock()
    market.id = market_id
    market.name = name
    market.category = "weather"
    market.selections = [selection]
    market.starts_at = starts_at
    return market


# ---------------------------------------------------------------------------
# Tests for parse_weather_market()
# ---------------------------------------------------------------------------


class TestParseWeatherMarket(unittest.TestCase):
    """Tests for market_parser.parse_weather_market()."""

    def test_high_temp_new_york(self) -> None:
        """Standard high-temp market with 'New York City' alias."""
        market = MockMarket(
            "Will the high temperature in New York City "
            "exceed 75\u00b0F on March 22?"
        )
        result = parse_weather_market(market)

        self.assertIsNotNone(result)
        assert result is not None  # for type narrowing
        self.assertEqual(result["city"], "New York")
        self.assertEqual(result["metric"], "high_temp")
        self.assertEqual(result["threshold"], 75.0)
        self.assertEqual(result["direction"], "above")
        self.assertEqual(result["date"], datetime.date(2026, 3, 22))
        self.assertAlmostEqual(result["lat"], CITY_COORDS["New York"][0])
        self.assertAlmostEqual(result["lon"], CITY_COORDS["New York"][1])

    def test_low_temp_chicago(self) -> None:
        """Low-temp / below market for Chicago."""
        market = MockMarket(
            "Will the low temperature in Chicago fall below "
            "32\u00b0F on March 25?"
        )
        result = parse_weather_market(market)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["city"], "Chicago")
        self.assertEqual(result["metric"], "low_temp")
        self.assertEqual(result["direction"], "below")
        self.assertEqual(result["threshold"], 32.0)
        self.assertEqual(result["date"], datetime.date(2026, 3, 25))

    def test_precipitation_los_angeles(self) -> None:
        """Precipitation market with a threshold value."""
        market = MockMarket(
            "Will there be precipitation above 0.5 inches "
            "in Los Angeles on March 22?"
        )
        result = parse_weather_market(market)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["city"], "Los Angeles")
        self.assertEqual(result["metric"], "precip")
        self.assertEqual(result["threshold"], 0.5)

    def test_wind_miami(self) -> None:
        """Wind-speed market for Miami."""
        market = MockMarket(
            "Will Miami wind speeds exceed 25 mph on March 22?"
        )
        result = parse_weather_market(market)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["city"], "Miami")
        self.assertEqual(result["metric"], "wind")
        self.assertEqual(result["threshold"], 25.0)
        self.assertEqual(result["direction"], "above")

    def test_no_recognizable_city_returns_none(self) -> None:
        """Title mentioning a city not in CITY_COORDS returns None."""
        market = MockMarket(
            "Will the high temperature in Reykjavik exceed "
            "10\u00b0F on March 22?"
        )
        result = parse_weather_market(market)
        self.assertIsNone(result)

    def test_no_threshold_returns_none(self) -> None:
        """Title with a recognized city but no parsable threshold."""
        market = MockMarket(
            "Will the high temperature in Chicago exceed "
            "expectations on March 22?"
        )
        result = parse_weather_market(market)
        self.assertIsNone(result)

    def test_empty_name_returns_none(self) -> None:
        """Market with an empty name returns None."""
        market = MockMarket("")
        result = parse_weather_market(market)
        self.assertIsNone(result)

    def test_no_date_returns_none(self) -> None:
        """Title with no date at all returns None."""
        market = MockMarket(
            "Will the high temperature in Denver exceed 90\u00b0F?"
        )
        result = parse_weather_market(market)
        self.assertIsNone(result)

    def test_real_market_with_event_name(self) -> None:
        """A real Market dataclass (event_name, no .name) is parsed."""
        market = Market(
            id="WEATHER-NYC-HIGHTEMP-2026-03-22",
            category="weather",
            event_name=(
                "Will the high temperature in New York City "
                "exceed 75\u00b0F on March 22?"
            ),
            starts_at=None,
            selections=[
                Selection(name="Yes", odds=2.0),
                Selection(name="No", odds=2.0),
            ],
            source="kalshi",
        )
        result = parse_weather_market(market)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["city"], "New York")
        self.assertEqual(result["metric"], "high_temp")
        self.assertEqual(result["threshold"], 75.0)
        self.assertEqual(result["direction"], "above")
        self.assertEqual(result["date"], datetime.date(2026, 3, 22))


# ---------------------------------------------------------------------------
# Tests for noaa.get_forecast() -- mocked HTTP
# ---------------------------------------------------------------------------


def _make_nws_period(
    start_iso: str,
    temp: int,
    precip: int = 0,
    wind: str = "5 mph",
    short: str = "Clear",
) -> dict:
    """Build a single NWS-style period dict for mocking."""
    return {
        "startTime": start_iso,
        "temperature": temp,
        "probabilityOfPrecipitation": {"value": precip},
        "windSpeed": wind,
        "shortForecast": short,
    }


class TestGetForecast(unittest.TestCase):
    """Tests for noaa.get_forecast() with mocked HTTP."""

    def setUp(self) -> None:
        """Clear the NOAA cache before each test."""
        noaa._cache.clear()

    def _build_mock_client(
        self, periods: list[dict]
    ) -> MagicMock:
        """Return a mock httpx.Client whose .get() returns the right
        responses for the two-step NWS flow (points -> forecast).
        """
        points_response = MagicMock()
        points_response.json.return_value = {
            "properties": {
                "forecastHourly": (
                    "https://api.weather.gov/gridpoints/OKX/33,37"
                    "/forecast/hourly"
                )
            }
        }
        points_response.raise_for_status = MagicMock()

        forecast_response = MagicMock()
        forecast_response.json.return_value = {
            "properties": {"periods": periods}
        }
        forecast_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get = MagicMock(
            side_effect=[points_response, forecast_response]
        )
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        return mock_client

    @patch("src.weather.noaa.httpx.Client")
    def test_successful_fetch(self, mock_client_cls: MagicMock) -> None:
        """Happy-path: two HTTP calls, parsed periods returned."""
        periods = [
            _make_nws_period(
                "2026-03-22T12:00:00-04:00", 72, 10, "8 mph"
            ),
            _make_nws_period(
                "2026-03-22T13:00:00-04:00", 75, 20, "10 mph"
            ),
        ]
        mock_client_cls.return_value = self._build_mock_client(periods)

        result = get_forecast(40.71, -74.01)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["temp_f"], 72.0)
        self.assertEqual(result[1]["temp_f"], 75.0)
        self.assertEqual(result[0]["precip_pct"], 10)
        self.assertEqual(result[1]["wind_mph"], 10.0)

    @patch("src.weather.noaa.httpx.Client")
    def test_cache_hit(self, mock_client_cls: MagicMock) -> None:
        """Second call with same coords uses cache -- only one HTTP
        request batch is made.
        """
        periods = [
            _make_nws_period("2026-03-22T12:00:00-04:00", 70),
        ]
        mock_client_cls.return_value = self._build_mock_client(periods)

        first = get_forecast(40.71, -74.01)
        self.assertEqual(len(first), 1)

        # Second call should hit cache -- no new Client constructed.
        mock_client_cls.reset_mock()
        second = get_forecast(40.71, -74.01)
        self.assertEqual(len(second), 1)
        mock_client_cls.assert_not_called()

    @patch("src.weather.noaa.httpx.Client")
    def test_http_error_returns_empty(
        self, mock_client_cls: MagicMock
    ) -> None:
        """Network or HTTP error returns an empty list."""
        mock_client = MagicMock()
        mock_client.get = MagicMock(
            side_effect=Exception("Connection refused")
        )
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = get_forecast(40.71, -74.01)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Tests for scanner.get_nws_probability()
# ---------------------------------------------------------------------------


def _make_forecast_hour(
    hour: int,
    temp: float = 70.0,
    precip: int = 0,
    wind: float = 5.0,
) -> dict:
    """Build a forecast-period dict for scanner tests."""
    return {
        "time": datetime.datetime(
            2026, 3, 22, hour, 0, tzinfo=datetime.timezone.utc
        ),
        "temp_f": temp,
        "precip_pct": precip,
        "wind_mph": wind,
        "short_forecast": "Clear",
    }


@unittest.skipIf(
    not _scanner_available,
    "src.weather.scanner not yet implemented",
)
class TestGetNwsProbability(unittest.TestCase):
    """Tests for scanner.get_nws_probability()."""

    def _parsed(
        self,
        metric: str = "high_temp",
        threshold: float = 75.0,
        direction: str = "above",
    ) -> dict:
        """Build a minimal parsed-market dict."""
        return {
            "city": "New York",
            "lat": 40.71,
            "lon": -74.01,
            "date": datetime.date(2026, 3, 22),
            "metric": metric,
            "threshold": threshold,
            "direction": direction,
        }

    def test_high_temp_clearly_above(self) -> None:
        """Max forecast temp (80) well above threshold (75): sigmoid gives ~0.97."""
        forecast = [
            _make_forecast_hour(10, temp=72.0),
            _make_forecast_hour(12, temp=78.0),
            _make_forecast_hour(14, temp=80.0),
            _make_forecast_hour(16, temp=76.0),
        ]
        prob = get_nws_probability(self._parsed(), forecast)
        self.assertIsNotNone(prob)
        # daily range=8, sigma=1.5, diff=+5 -> sigmoid ~0.966
        self.assertGreater(prob, 0.90)

    def test_high_temp_borderline(self) -> None:
        """Max forecast temp (74) just below threshold (75): sigmoid gives ~0.34."""
        forecast = [
            _make_forecast_hour(10, temp=68.0),
            _make_forecast_hour(14, temp=74.0),
        ]
        prob = get_nws_probability(self._parsed(), forecast)
        self.assertIsNotNone(prob)
        # daily range=6, sigma=1.5, diff=-1 -> sigmoid ~0.34
        self.assertAlmostEqual(prob, 0.34, delta=0.02)

    def test_high_temp_clearly_below(self) -> None:
        """Max forecast temp (68) well below threshold (75): sigmoid gives ~0.01."""
        forecast = [
            _make_forecast_hour(10, temp=62.0),
            _make_forecast_hour(14, temp=68.0),
        ]
        prob = get_nws_probability(self._parsed(), forecast)
        self.assertIsNotNone(prob)
        # daily range=6, sigma=1.5, diff=-7 -> sigmoid ~0.009
        self.assertLess(prob, 0.05)

    def test_precip_blend(self) -> None:
        """Precip with [60, 70, 80] pct values: blended 60/40 max/avg -> ~0.76."""
        forecast = [
            _make_forecast_hour(10, precip=60),
            _make_forecast_hour(12, precip=70),
            _make_forecast_hour(14, precip=80),
        ]
        parsed = self._parsed(
            metric="precip", threshold=0.0, direction="above"
        )
        prob = get_nws_probability(parsed, forecast)
        self.assertIsNotNone(prob)
        # (0.6*80 + 0.4*70) / 100 = 0.76
        self.assertAlmostEqual(prob, 0.76, delta=0.01)

    def test_empty_forecast_returns_none(self) -> None:
        """Empty forecast list -> None."""
        prob = get_nws_probability(self._parsed(), [])
        self.assertIsNone(prob)


# ---------------------------------------------------------------------------
# Tests for scanner.scan_weather_markets() -- edge/side logic
# ---------------------------------------------------------------------------


@unittest.skipIf(
    not _scanner_available,
    "src.weather.scanner not yet implemented",
)
class TestScanWeatherMarkets(unittest.TestCase):
    """Tests for scanner.scan_weather_markets() with mocked deps."""

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    @patch("src.weather.scanner.get_nws_probability", return_value=0.80)
    def test_yes_side_when_nws_prob_high(
        self,
        mock_nws_prob: MagicMock,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        """nws_prob=0.80 and kalshi yes_ask=60c -> side='yes',
        edge=0.20.
        """
        today = datetime.date.today()
        # starts_at must be in the future for the scanner's time-window filter.
        closes_at = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(hours=6)

        mock_market = _make_scanner_market(
            market_id="WEATHER-NYC-TEMP",
            yes_ask=60,
            no_ask=40,
            starts_at=closes_at,
        )

        mock_fetcher = MagicMock()
        mock_fetcher.get_markets_by_series.return_value = [mock_market]

        mock_parse.return_value = {
            "city": "New York",
            "lat": 40.71,
            "lon": -74.01,
            "date": today,
            "metric": "high_temp",
            "threshold": 75.0,
            "direction": "above",
        }
        mock_forecast.return_value = [
            _make_forecast_hour(14, temp=80.0)
        ]

        results = scan_weather_markets(mock_fetcher, min_edge=0.05)

        yes_results = [r for r in results if r.get("side") == "yes"]
        self.assertEqual(len(yes_results), 1)
        # kalshi_prob = 60/100 = 0.60; edge = 0.80 - 0.60 = 0.20
        self.assertAlmostEqual(yes_results[0]["edge"], 0.20, delta=0.01)
        self.assertEqual(yes_results[0]["ask_cents"], 60)

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    @patch("src.weather.scanner.get_nws_probability", return_value=0.30)
    def test_no_side_when_nws_prob_low(
        self,
        mock_nws_prob: MagicMock,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        """nws_prob=0.30 and kalshi yes_ask=60c -> side='no'."""
        closes_at = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(hours=6)

        mock_market = _make_scanner_market(
            market_id="WEATHER-DEN-TEMP",
            yes_ask=60,
            no_ask=40,
            starts_at=closes_at,
        )

        mock_fetcher = MagicMock()
        mock_fetcher.get_markets_by_series.return_value = [mock_market]

        mock_parse.return_value = {
            "city": "Denver",
            "lat": 39.74,
            "lon": -104.99,
            "date": datetime.date.today(),
            "metric": "high_temp",
            "threshold": 90.0,
            "direction": "above",
        }
        mock_forecast.return_value = [
            _make_forecast_hour(14, temp=68.0)
        ]

        results = scan_weather_markets(mock_fetcher, min_edge=0.05)

        no_results = [r for r in results if r.get("side") == "no"]
        self.assertEqual(len(no_results), 1)

    @patch("src.weather.scanner.get_forecast")
    @patch("src.weather.scanner.parse_weather_market")
    @patch("src.weather.scanner.get_nws_probability", return_value=0.90)
    def test_filters_out_tomorrow(
        self,
        mock_nws_prob: MagicMock,
        mock_parse: MagicMock,
        mock_forecast: MagicMock,
    ) -> None:
        """Markets closing tomorrow (starts_at.date() != today) are
        filtered out.
        """
        tomorrow_dt = datetime.datetime.now(
            tz=datetime.timezone.utc
        ) + datetime.timedelta(days=1)

        mock_market = _make_scanner_market(
            market_id="WEATHER-MIA-TEMP",
            yes_ask=60,
            no_ask=40,
            starts_at=tomorrow_dt,
        )

        mock_fetcher = MagicMock()
        mock_fetcher.get_markets.return_value = [mock_market]

        mock_parse.return_value = {
            "city": "Miami",
            "lat": 25.76,
            "lon": -80.19,
            "date": datetime.date.today() + datetime.timedelta(days=1),
            "metric": "high_temp",
            "threshold": 85.0,
            "direction": "above",
        }
        mock_forecast.return_value = [
            _make_forecast_hour(14, temp=90.0)
        ]

        results = scan_weather_markets(mock_fetcher, min_edge=0.05)

        self.assertEqual(len(results), 0)


if __name__ == "__main__":
    unittest.main()
