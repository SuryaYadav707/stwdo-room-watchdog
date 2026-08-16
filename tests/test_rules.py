"""Rules engine tests.

This engine decides where the single permitted application goes, so the failure
modes worth pinning down are the conservative ones: unparseable data must be
rejected, never optimistically accepted.
"""

from __future__ import annotations

import pytest

from stwdo.config import RulesConfig
from stwdo.models import Offer, RoomType
from stwdo.rules import best_match, evaluate, hard_filter, lookup_walking_minutes, rank


def make_offer(**overrides) -> Offer:
    base = dict(
        offer_id="6583",
        url="https://www.stwdo.de/freie-zimmer/6583",
        title="9 available 3-person shared flats",
        city="Dortmund",
        address="Dortmund 1",
        room_type=RoomType.SHARED_3,
        price_min=359.0,
        price_max=407.0,
        size_min=20.0,
        size_max=37.0,
        available_count=9,
    )
    base.update(overrides)
    return Offer(**base)


@pytest.fixture()
def rules() -> RulesConfig:
    return RulesConfig()


# --------------------------------------------------------------------------- #
# hard filters
# --------------------------------------------------------------------------- #


def test_matching_offer_passes(rules):
    assert hard_filter(make_offer(), rules) == ()


def test_wrong_city_rejected(rules):
    reasons = hard_filter(make_offer(city="Hagen"), rules)
    assert any("city" in reason for reason in reasons)


def test_city_match_is_case_insensitive(rules):
    assert hard_filter(make_offer(city="dortmund"), rules) == ()


def test_over_budget_rejected(rules):
    reasons = hard_filter(make_offer(price_min=600.0, price_max=700.0), rules)
    assert any("max_rent" in reason for reason in reasons)


def test_budget_uses_cheapest_room_in_bucket(rules):
    """A bucket spanning the budget line is viable — some rooms are affordable."""
    assert hard_filter(make_offer(price_min=400.0, price_max=900.0), rules) == ()


def test_too_small_rejected(rules):
    reasons = hard_filter(make_offer(size_min=8.0, size_max=10.0), rules)
    assert any("min_size" in reason for reason in reasons)


def test_size_uses_largest_room_in_bucket(rules):
    assert hard_filter(make_offer(size_min=8.0, size_max=30.0), rules) == ()


def test_unknown_room_type_rejected(rules):
    reasons = hard_filter(make_offer(room_type=RoomType.UNKNOWN), rules)
    assert any("room type" in reason for reason in reasons)


def test_disallowed_room_type_rejected(rules):
    rules.allowed_types = ["single_apartment"]
    reasons = hard_filter(make_offer(room_type=RoomType.SHARED_3), rules)
    assert any("not allowed" in reason for reason in reasons)


def test_missing_price_rejected(rules):
    """Unparseable data must never be treated as acceptable."""
    reasons = hard_filter(make_offer(price_min=None), rules)
    assert any("price" in reason for reason in reasons)


def test_missing_size_rejected(rules):
    reasons = hard_filter(make_offer(size_max=None), rules)
    assert any("size" in reason for reason in reasons)


def test_zero_availability_rejected(rules):
    reasons = hard_filter(make_offer(available_count=0), rules)
    assert any("available" in reason for reason in reasons)


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def test_cheap_large_single_apartment_scores_high(rules):
    result = evaluate(
        make_offer(
            room_type=RoomType.SINGLE_APARTMENT,
            price_min=280.0,
            price_max=300.0,
            size_min=30.0,
            size_max=35.0,
            available_count=12,
        ),
        rules,
    )
    assert result.passed_filters
    assert result.score >= 80.0


def test_expensive_cramped_four_share_scores_low(rules):
    result = evaluate(
        make_offer(
            room_type=RoomType.SHARED_4,
            price_min=445.0,
            price_max=450.0,
            size_min=15.0,
            size_max=16.0,
            available_count=1,
        ),
        rules,
    )
    assert result.passed_filters
    assert result.score < 30.0


def test_rejected_offer_scores_zero(rules):
    """Otherwise a rejected offer could out-rank a valid one."""
    result = evaluate(make_offer(city="Soest", price_min=100.0), rules)
    assert result.rejected
    assert result.score == 0.0


def test_score_never_exceeds_bounds(rules):
    result = evaluate(make_offer(price_min=1.0, size_max=999.0, available_count=999), rules)
    assert 0.0 <= result.score <= 100.0


def test_cheaper_offer_outranks_dearer_one_all_else_equal(rules):
    cheap = make_offer(offer_id="1", price_min=300.0)
    dear = make_offer(offer_id="2", price_min=440.0)
    ranked = rank([dear, cheap], rules)
    assert ranked[0].offer.offer_id == "1"


def test_ranking_is_deterministic_on_ties(rules):
    a = make_offer(offer_id="100")
    b = make_offer(offer_id="200")
    assert [r.offer.offer_id for r in rank([a, b], rules)] == ["100", "200"]
    assert [r.offer.offer_id for r in rank([b, a], rules)] == ["100", "200"]


# --------------------------------------------------------------------------- #
# location table
# --------------------------------------------------------------------------- #


def test_walking_minutes_matches_despite_comma_differences(rules):
    rules.location_scoring.walking_minutes = {"Im Alten Holz 133": 12.0}
    assert lookup_walking_minutes("Im Alten Holz 133", rules) == 12.0


def test_walking_minutes_falls_back_when_address_unknown(rules):
    rules.location_scoring.walking_minutes = {"Im Alten Holz 133": 12.0}
    rules.location_scoring.default_minutes = 30.0
    assert lookup_walking_minutes("Somewhere Else 9", rules) == 30.0


# --------------------------------------------------------------------------- #
# auto-apply gate
# --------------------------------------------------------------------------- #


def test_best_match_respects_threshold(rules):
    rules.auto_apply_min_score = 95.0
    assert best_match([make_offer()], rules) is None


def test_best_match_returns_top_qualifying_offer(rules):
    rules.auto_apply_min_score = 10.0
    good = make_offer(offer_id="1", room_type=RoomType.SINGLE_APARTMENT, price_min=290.0)
    meh = make_offer(offer_id="2", room_type=RoomType.SHARED_4, price_min=440.0)
    match = best_match([meh, good], rules)
    assert match is not None
    assert match.offer.offer_id == "1"


def test_best_match_ignores_rejected_offers(rules):
    rules.auto_apply_min_score = 0.0
    assert best_match([make_offer(city="Hagen")], rules) is None


# --------------------------------------------------------------------------- #
# city preference
# --------------------------------------------------------------------------- #


def test_city_minutes_used_when_address_is_unknown(rules):
    """A brand new Dortmund building must still outrank a known distant one."""
    rules.location_scoring.city_minutes = {"Dortmund": 15.0, "Iserlohn": 50.0}
    assert lookup_walking_minutes("Neue Straße 9", rules, city="Dortmund") == 15.0
    assert lookup_walking_minutes("Neue Straße 9", rules, city="Iserlohn") == 50.0


def test_address_table_beats_city_fallback(rules):
    rules.location_scoring.walking_minutes = {"Dortmund 1": 10.0}
    rules.location_scoring.city_minutes = {"Dortmund": 15.0}
    assert lookup_walking_minutes("Dortmund 1", rules, city="Dortmund") == 10.0


def test_city_match_ignores_case(rules):
    rules.location_scoring.city_minutes = {"Dortmund": 15.0}
    assert lookup_walking_minutes("", rules, city="dortmund") == 15.0


def test_unknown_city_falls_back_to_default(rules):
    rules.location_scoring.city_minutes = {"Dortmund": 15.0}
    rules.location_scoring.default_minutes = 30.0
    assert lookup_walking_minutes("", rules, city="Bochum") == 30.0


def test_dortmund_outranks_a_cheaper_distant_offer(rules):
    """The point of the city table: location must be able to beat price."""
    rules.location_scoring.city_minutes = {"Dortmund": 15.0, "Iserlohn": 50.0}
    dortmund = make_offer(offer_id="1", city="Dortmund", address="Dortmund 1",
                          room_type=RoomType.SINGLE_APARTMENT, price_min=380.0)
    iserlohn = make_offer(offer_id="2", city="Iserlohn", address="Steubenstraße 14-18",
                          room_type=RoomType.SINGLE_APARTMENT, price_min=326.0)
    rules.cities = ["Dortmund", "Iserlohn"]
    ranked = rank([iserlohn, dortmund], rules)
    assert ranked[0].offer.city == "Dortmund"
