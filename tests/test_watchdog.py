"""Scheduling and loop-safety tests. No network, no browser."""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from stwdo.config import AppConfig, Secrets
from stwdo.models import LockState, Offer, RoomType
from stwdo.notify import Notifier
from stwdo.store import Store
from stwdo.watchdog import (
    PollStats,
    Watchdog,
    WatchdogError,
    apply_jitter,
    current_interval,
    load_timezone,
)

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture()
def config() -> AppConfig:
    return AppConfig()


def at(year=2026, month=8, day=17, hour=10, minute=0) -> datetime:
    """2026-08-17 is a Monday."""
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN)


# --------------------------------------------------------------------------- #
# interval selection
# --------------------------------------------------------------------------- #


def test_monday_ten_am_is_burst(config):
    interval, mode = current_interval(config, at(hour=10, minute=0))
    assert mode == "burst"
    assert interval == 15


def test_wednesday_ten_am_is_burst(config):
    interval, mode = current_interval(config, at(day=19, hour=10, minute=10))
    assert mode == "burst"


def test_tuesday_ten_am_is_baseline(config):
    """Rooms appear on any day, so non-drop days still poll every 2 minutes."""
    interval, mode = current_interval(config, at(day=18, hour=10))
    assert mode == "baseline"
    assert interval == 120


def test_monday_afternoon_is_baseline(config):
    _, mode = current_interval(config, at(hour=15))
    assert mode == "baseline"


def test_sunday_night_still_polls(config):
    interval, mode = current_interval(config, at(day=16, hour=23, minute=30))
    assert mode == "baseline"
    assert interval == 120


def test_quiet_hours_apply_only_when_enabled(config):
    assert current_interval(config, at(hour=3))[1] == "baseline"
    config.polling.quiet_hours.enabled = True
    interval, mode = current_interval(config, at(hour=3))
    assert mode == "quiet"
    assert interval == 600


def test_burst_beats_quiet_hours(config):
    config.polling.quiet_hours.enabled = True
    config.polling.quiet_hours.start = "00:00"
    config.polling.quiet_hours.end = "23:59"
    assert current_interval(config, at(hour=10))[1] == "burst"


def test_intervals_have_a_floor(config):
    config.polling.baseline_interval_seconds = 1
    interval, _ = current_interval(config, at(hour=15))
    assert interval >= 15


def test_window_wrapping_past_midnight(config):
    config.polling.quiet_hours.enabled = True
    config.polling.quiet_hours.start = "23:00"
    config.polling.quiet_hours.end = "05:00"
    assert current_interval(config, at(hour=23, minute=30))[1] == "quiet"
    assert current_interval(config, at(hour=4))[1] == "quiet"
    assert current_interval(config, at(hour=12))[1] == "baseline"


def test_malformed_time_falls_back_instead_of_crashing(config):
    config.polling.burst.start = "not a time"
    _, mode = current_interval(config, at(hour=10, minute=0))
    assert mode in ("burst", "baseline")  # must not raise


# --------------------------------------------------------------------------- #
# jitter
# --------------------------------------------------------------------------- #


def test_jitter_stays_within_bounds():
    rng = random.Random(1)
    for _ in range(200):
        value = apply_jitter(120, 0.25, rng)
        assert 90 <= value <= 150


def test_zero_jitter_is_exact():
    assert apply_jitter(120, 0.0) == 120.0


def test_jitter_never_goes_below_five_seconds():
    assert apply_jitter(6, 0.9, random.Random(2)) >= 5.0


# --------------------------------------------------------------------------- #
# loop safety
# --------------------------------------------------------------------------- #


class _StubNotifier(Notifier):
    """Real Notifier with the network calls swapped out for a list."""

    def __init__(self, config: AppConfig):
        super().__init__(config, Secrets())
        self.messages: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True

    def send_photo(self, image_path, caption: str = "") -> bool:
        self.messages.append(caption)
        return True


def _offer(offer_id="6583", price=300.0) -> Offer:
    return Offer(
        offer_id=offer_id,
        url=f"https://www.stwdo.de/freie-zimmer/{offer_id}",
        title="9 available single apartments",
        city="Dortmund",
        address="Dortmund 1",
        room_type=RoomType.SINGLE_APARTMENT,
        price_min=price,
        price_max=price + 20,
        size_min=25.0,
        size_max=30.0,
        available_count=9,
    )


@pytest.fixture()
def dog(tmp_path, config):
    config.root = tmp_path
    store = Store(tmp_path / "w.sqlite3")
    watchdog = Watchdog(config, store, _StubNotifier(config), profile=None, live=False)
    yield watchdog
    store.close()


def test_poll_failure_does_not_stop_the_loop(dog, monkeypatch):
    from stwdo.fetcher import FetchError

    def boom(*_args, **_kwargs):
        raise FetchError("network down")

    monkeypatch.setattr(dog.fetcher, "fetch_offers", boom)
    stats = dog.run(max_polls=3, sleeper=lambda _s: None)
    assert stats.polls == 3
    assert stats.consecutive_failures == 3


def test_failure_alert_fires_once_not_every_poll(dog, monkeypatch):
    from stwdo.fetcher import FetchError

    monkeypatch.setattr(
        dog.fetcher, "fetch_offers", lambda *a, **k: (_ for _ in ()).throw(FetchError("down"))
    )
    dog.run(max_polls=6, sleeper=lambda _s: None)
    alerts = [m for m in dog.notifier.messages if "consecutive poll failures" in m]
    assert len(alerts) == 1


def test_backoff_grows_after_failures(dog):
    dog.stats = PollStats(consecutive_failures=3)
    delay, mode = dog._sleep_seconds()
    assert "backoff" in mode
    assert delay > dog.config.polling.baseline_interval_seconds


def test_backoff_is_capped(dog):
    dog.stats = PollStats(consecutive_failures=40)
    delay, _ = dog._sleep_seconds()
    assert delay <= dog.config.polling.backoff_max_seconds


def test_dry_run_never_submits(dog, monkeypatch):
    """The whole point of the default mode: notify, never spend the application."""
    monkeypatch.setattr("stwdo.watchdog.parse_offers", lambda *a, **k: [_offer()])
    monkeypatch.setattr(
        dog.fetcher,
        "fetch_offers",
        lambda *a, **k: type("R", (), {"html": "<html></html>", "transport": "http", "duration_ms": 5})(),
    )

    def fail(*_a, **_k):
        raise AssertionError("dry run must not submit")

    monkeypatch.setattr(dog, "_submit", fail)
    dog.run(max_polls=1, sleeper=lambda _s: None)
    assert dog.store.lock_state() == LockState.NONE


def test_a_standing_match_is_not_re_triggered(dog, monkeypatch):
    monkeypatch.setattr("stwdo.watchdog.parse_offers", lambda *a, **k: [_offer()])
    monkeypatch.setattr(
        dog.fetcher,
        "fetch_offers",
        lambda *a, **k: type("R", (), {"html": "<html></html>", "transport": "http", "duration_ms": 5})(),
    )
    dog.run(max_polls=4, sleeper=lambda _s: None)
    decisions = [m for m in dog.notifier.messages if "Best match" in m]
    assert len(decisions) == 1


def test_locked_watchdog_keeps_watching_but_does_not_apply(dog, monkeypatch):
    dog.live = True
    dog.store.acquire_lock("9999")
    dog.store.confirm_submission("9999", "")

    monkeypatch.setattr("stwdo.watchdog.parse_offers", lambda *a, **k: [_offer()])
    monkeypatch.setattr(
        dog.fetcher,
        "fetch_offers",
        lambda *a, **k: type("R", (), {"html": "<html></html>", "transport": "http", "duration_ms": 5})(),
    )
    monkeypatch.setattr(
        dog, "_submit", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not apply"))
    )
    dog.run(max_polls=2, sleeper=lambda _s: None)
    assert dog.store.lock_state() == LockState.SUBMITTED


def test_empty_listing_is_a_successful_poll_not_a_failure(dog, monkeypatch):
    """Between publication windows the site is legitimately empty."""
    monkeypatch.setattr("stwdo.watchdog.parse_offers", lambda *a, **k: [])
    monkeypatch.setattr(
        dog.fetcher,
        "fetch_offers",
        lambda *a, **k: type("R", (), {"html": "<html></html>", "transport": "http", "duration_ms": 5})(),
    )
    stats = dog.run(max_polls=2, sleeper=lambda _s: None)
    assert stats.consecutive_failures == 0
    assert stats.last_error == ""
    assert dog.notifier.messages == []


# --------------------------------------------------------------------------- #
# timezone loading
# --------------------------------------------------------------------------- #


def test_berlin_timezone_loads():
    """The whole schedule hangs off Berlin time, not the machine's local time."""
    tz = load_timezone("Europe/Berlin")
    assert datetime(2026, 8, 17, 10, 0, tzinfo=tz).utcoffset().total_seconds() in (3600, 7200)


def test_missing_timezone_database_fails_loudly(monkeypatch):
    """On Windows without tzdata this must say what to install, not guess."""
    def boom(_name):
        raise ZoneInfoNotFoundError("No time zone found with key Europe/Berlin")

    monkeypatch.setattr("stwdo.watchdog.ZoneInfo", boom)
    with pytest.raises(WatchdogError, match="pip install tzdata"):
        load_timezone("Europe/Berlin")


def test_burst_uses_berlin_not_local_time(config):
    """A machine in IST must still burst at 10:00 Berlin, not 10:00 local."""
    berlin_ten = datetime(2026, 8, 17, 10, 0, tzinfo=BERLIN)
    assert current_interval(config, berlin_ten)[1] == "burst"
    # The same instant is 13:30 in IST — which must NOT be treated as a burst.
    ist_thirteen_thirty = datetime(2026, 8, 17, 13, 30, tzinfo=BERLIN)
    assert current_interval(config, ist_thirteen_thirty)[1] == "baseline"
