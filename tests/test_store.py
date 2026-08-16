"""Store tests, focused on the application lock.

The lock is what stands between the watchdog and submitting a second
application, which STWDO punishes by deleting every application from that
person. It has to fail closed in every direction.
"""

from __future__ import annotations

import pytest

from stwdo.models import LockState, Offer, RoomType
from stwdo.store import Store


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "test.sqlite3") as instance:
        yield instance


def make_offer(offer_id="6583", count=9, price_min=359.0) -> Offer:
    return Offer(
        offer_id=offer_id,
        url=f"https://www.stwdo.de/freie-zimmer/{offer_id}",
        title="9 available 3-person shared flats",
        city="Dortmund",
        address="Dortmund 1",
        room_type=RoomType.SHARED_3,
        price_min=price_min,
        price_max=407.0,
        size_min=20.0,
        size_max=37.0,
        available_count=count,
    )


# --------------------------------------------------------------------------- #
# offer tracking
# --------------------------------------------------------------------------- #


def test_first_sighting_is_new(store):
    new, changed = store.upsert_offers([make_offer()])
    assert [offer.offer_id for offer in new] == ["6583"]
    assert changed == []


def test_unchanged_offer_is_neither_new_nor_changed(store):
    store.upsert_offers([make_offer()])
    new, changed = store.upsert_offers([make_offer()])
    assert new == [] and changed == []


def test_extra_units_on_a_known_offer_count_as_changed(store):
    """A standing bucket gaining rooms is as actionable as a brand new one."""
    store.upsert_offers([make_offer(count=9)])
    _, changed = store.upsert_offers([make_offer(count=14)])
    assert [offer.offer_id for offer in changed] == ["6583"]


def test_price_change_counts_as_changed(store):
    store.upsert_offers([make_offer(price_min=359.0)])
    _, changed = store.upsert_offers([make_offer(price_min=340.0)])
    assert len(changed) == 1


def test_offers_round_trip(store):
    store.upsert_offers([make_offer()])
    loaded = store.all_offers()
    assert loaded[0].room_type == RoomType.SHARED_3
    assert loaded[0].price_min == 359.0


# --------------------------------------------------------------------------- #
# application lock
# --------------------------------------------------------------------------- #


def test_lock_starts_free(store):
    assert store.lock_state() == LockState.NONE
    allowed, _ = store.can_apply()
    assert allowed


def test_acquire_then_confirm(store):
    store.acquire_lock("6583")
    assert store.lock_state() == LockState.IN_FLIGHT
    store.confirm_submission("6583", "data/evidence/x.png")
    assert store.lock_state() == LockState.SUBMITTED


def test_second_acquire_is_refused(store):
    store.acquire_lock("6583")
    with pytest.raises(RuntimeError):
        store.acquire_lock("6584")


def test_cannot_apply_after_submission(store):
    store.acquire_lock("6583")
    store.confirm_submission("6583", "")
    allowed, reason = store.can_apply()
    assert not allowed
    assert "already submitted" in reason


def test_crash_mid_submit_blocks_further_attempts(store):
    """IN_FLIGHT means the outcome is unknown, so the safe move is to stop."""
    store.acquire_lock("6583")
    allowed, reason = store.can_apply()
    assert not allowed
    assert "never confirmed" in reason


def test_lock_survives_reopening_the_database(tmp_path):
    path = tmp_path / "lock.sqlite3"
    with Store(path) as first:
        first.acquire_lock("6583")
        first.confirm_submission("6583", "")
    with Store(path) as second:
        assert second.lock_state() == LockState.SUBMITTED


def test_unreadable_lock_state_fails_closed(store):
    store.conn.execute("UPDATE application_lock SET state='garbage' WHERE id=1")
    store.conn.commit()
    assert store.lock_state() == LockState.IN_FLIGHT
    allowed, _ = store.can_apply()
    assert not allowed


def test_release_frees_the_lock(store):
    store.acquire_lock("6583")
    store.release_lock(note="verified by hand")
    assert store.lock_state() == LockState.NONE
    assert store.can_apply()[0]


# --------------------------------------------------------------------------- #
# run log
# --------------------------------------------------------------------------- #


def test_runs_are_logged_newest_first(store):
    store.record_run(ok=True, offers_found=5, duration_ms=120)
    store.record_run(ok=False, error="boom")
    rows = store.recent_runs(5)
    assert rows[0]["error"] == "boom"
    assert rows[1]["offers_found"] == 5
