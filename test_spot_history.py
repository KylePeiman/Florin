"""
Regression tests for the streaming spot-price history that feeds the
last-second stability check.

Bug: PriceCache kept only the latest spot price, and the PriceTracker that
powers is_stable() was sampled once per main-loop iteration (~every 20s). Over
a 15s stability window that yielded <2 observations, so is_stable() returned
False forever and no trades fired even when the price was calm. The Kraken WS
delivers many trades/sec; that data must be retained, not collapsed.
"""
import time

from src.streaming.price_cache import PriceCache
from src.engine.last_second import PriceTracker


def test_cache_retains_spot_history():
    cache = PriceCache()
    for px in (100.0, 100.1, 100.2, 100.15):
        cache.set_spot("XBTUSD", px)
    series = cache.spot_series("XBTUSD")
    assert [p for _, p in series] == [100.0, 100.1, 100.2, 100.15]
    # Latest still works as before.
    assert cache.get_spot("XBTUSD") == 100.15
    # Unknown pair → empty series, not an error.
    assert cache.spot_series("NOPE") == []


def test_cache_prunes_old_history():
    cache = PriceCache()
    now = time.time()
    # Inject an ancient point directly, then add a fresh one via set_spot.
    cache._spot_history["ETHUSD"] = [(now - 10_000, 1.0)]
    cache.set_spot("ETHUSD", 2.0)
    series = cache.spot_series("ETHUSD")
    assert [p for _, p in series] == [2.0]  # stale point pruned


def test_tracker_replace_history_enables_stability():
    """A dense WS history makes is_stable() evaluable, where one-sample-per-tick
    sampling could not reach the 2-observation minimum."""
    tracker = PriceTracker()
    now = time.time()
    # 20 points over the last ~10s, price drifting only 0.02% — clearly stable.
    pts = [(now - 10 + i * 0.5, 66500.0 + (i % 2) * 5) for i in range(20)]
    tracker.replace_history(pts)
    assert tracker.observation_count() == 20
    assert tracker.is_stable(window_seconds=15, max_move_pct=0.003) is True


def test_tracker_replace_history_detects_real_move():
    tracker = PriceTracker()
    now = time.time()
    # Same density but a 1% drift across the window → unstable.
    pts = [(now - 10 + i * 0.5, 66500.0 + i * 35) for i in range(20)]
    tracker.replace_history(pts)
    assert tracker.is_stable(window_seconds=15, max_move_pct=0.003) is False


def test_tracker_replace_history_prunes_stale_points():
    tracker = PriceTracker(max_age_seconds=120)
    now = time.time()
    pts = [(now - 1000, 1.0), (now - 5, 2.0), (now - 1, 2.0)]
    tracker.replace_history(pts)
    assert tracker.observation_count() == 2  # the 1000s-old point is dropped


def test_tracker_observations_in_window():
    """observations_in_window counts only points inside the window, while
    observation_count() returns the full retained history."""
    tracker = PriceTracker(max_age_seconds=120)
    now = time.time()
    # 3 points within 15s, 2 points older than 15s but within max_age.
    pts = [(now - 40, 1.0), (now - 20, 1.0),
           (now - 10, 1.0), (now - 5, 1.0), (now - 1, 1.0)]
    tracker.replace_history(pts)
    assert tracker.observation_count() == 5
    assert tracker.observations_in_window(15) == 3
    assert tracker.observations_in_window(60) == 5
