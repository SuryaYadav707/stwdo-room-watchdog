"""Deterministic match engine.

Pure functions only — no network, no database, no clock. Everything here is
unit-testable offline, which matters because this code decides whether to spend
the single application you are allowed to make.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .config import RulesConfig
from .models import MatchResult, Offer, RoomType, ScoreBreakdown


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _linear_descending(value: float, best: float, worst: float) -> float:
    """100 at or below `best`, 0 at or above `worst`, linear between.

    Used for "smaller is better" axes (rent, walking minutes).
    """
    if worst <= best:
        return 100.0 if value <= best else 0.0
    if value <= best:
        return 100.0
    if value >= worst:
        return 0.0
    return _clamp(100.0 * (worst - value) / (worst - best))


def _linear_ascending(value: float, worst: float, best: float) -> float:
    """0 at or below `worst`, 100 at or above `best`, linear between.

    Used for "bigger is better" axes (size, available count).
    """
    if best <= worst:
        return 100.0 if value >= best else 0.0
    if value >= best:
        return 100.0
    if value <= worst:
        return 0.0
    return _clamp(100.0 * (value - worst) / (best - worst))


# --------------------------------------------------------------------------- #
# hard filters
# --------------------------------------------------------------------------- #


def hard_filter(offer: Offer, rules: RulesConfig) -> tuple[str, ...]:
    """Return the reasons this offer is disqualified. Empty tuple = passes.

    Missing data is treated as disqualifying rather than as a pass. An offer we
    cannot read is an offer we must not spend the one application on.
    """
    reasons: list[str] = []

    allowed_cities = {c.strip().casefold() for c in rules.cities}
    if allowed_cities and offer.city.strip().casefold() not in allowed_cities:
        reasons.append(f"city {offer.city!r} not in {sorted(rules.cities)}")

    if offer.room_type == RoomType.UNKNOWN:
        reasons.append("room type could not be determined")
    elif offer.room_type.value not in rules.allowed_types:
        reasons.append(f"room type {offer.room_type.value!r} not allowed")

    # Buckets carry a price range. Compare the cheapest unit against the budget:
    # a bucket is viable if any room in it is affordable.
    if offer.price_min is None:
        reasons.append("no price could be parsed")
    elif offer.price_min > rules.max_rent:
        reasons.append(f"cheapest room {offer.price_min:.2f} EUR over max_rent {rules.max_rent:.2f}")

    # Same logic mirrored for size: the largest room decides viability.
    if offer.size_max is None:
        reasons.append("no size could be parsed")
    elif offer.size_max < rules.min_size:
        reasons.append(f"largest room {offer.size_max:.1f} m2 under min_size {rules.min_size:.1f}")

    if offer.available_count is not None and offer.available_count <= 0:
        reasons.append("no units available")

    return tuple(reasons)


# --------------------------------------------------------------------------- #
# component scores
# --------------------------------------------------------------------------- #


def score_rent(offer: Offer, rules: RulesConfig) -> float:
    if offer.price_min is None:
        return 0.0
    return _linear_descending(
        offer.price_min,
        best=rules.rent_scoring.floor_rent,
        worst=rules.max_rent,
    )


def score_size(offer: Offer, rules: RulesConfig) -> float:
    if offer.size_max is None:
        return 0.0
    return _linear_ascending(
        offer.size_max,
        worst=rules.min_size,
        best=rules.size_scoring.ideal_size,
    )


def score_room_type(offer: Offer, rules: RulesConfig) -> float:
    """Rank position in `type_preference` mapped onto 0-100, best first."""
    preference = rules.type_preference
    if not preference or offer.room_type.value not in preference:
        return 0.0
    if len(preference) == 1:
        return 100.0
    index = preference.index(offer.room_type.value)
    return _clamp(100.0 * (len(preference) - 1 - index) / (len(preference) - 1))


def score_availability(offer: Offer, rules: RulesConfig) -> float:
    """More free units means better odds of actually landing one."""
    if offer.available_count is None:
        return 0.0
    return _linear_ascending(
        float(offer.available_count),
        worst=0.0,
        best=float(max(1, rules.availability_scoring.saturation_count)),
    )


def lookup_walking_minutes(address: str, rules: RulesConfig, city: str = "") -> float:
    """Travel time for an offer, most specific source first.

    1. `walking_minutes`, matched per address
    2. `city_minutes`, so an unlisted building in a preferred city still scores
       better than one in a distant city
    3. `default_minutes`

    The site writes addresses inconsistently ("Im Alten Holz, 133" vs
    "Im Alten Holz 133"), so address matching is a casefolded substring test in
    both directions rather than equality.
    """
    scoring = rules.location_scoring
    needle = address.strip().casefold()
    table = scoring.walking_minutes

    if table and needle:
        best_key: Optional[str] = None
        for key in table:
            candidate = key.strip().casefold()
            if not candidate:
                continue
            if candidate in needle or needle in candidate:
                if best_key is None or len(candidate) > len(best_key.strip().casefold()):
                    best_key = key
        if best_key is not None:
            return float(table[best_key])

    city_key = city.strip().casefold()
    if city_key:
        for key, minutes in scoring.city_minutes.items():
            if key.strip().casefold() == city_key:
                return float(minutes)

    return scoring.default_minutes


def score_location(offer: Offer, rules: RulesConfig) -> float:
    minutes = lookup_walking_minutes(offer.address, rules, offer.city)
    return _linear_descending(
        minutes,
        best=rules.location_scoring.good_minutes,
        worst=rules.location_scoring.bad_minutes,
    )


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #


def evaluate(offer: Offer, rules: RulesConfig) -> MatchResult:
    """Score one offer. Rejected offers still get a breakdown, for debugging."""
    reasons = hard_filter(offer, rules)

    breakdown = ScoreBreakdown(
        rent=score_rent(offer, rules),
        room_type=score_room_type(offer, rules),
        size=score_size(offer, rules),
        location=score_location(offer, rules),
        availability=score_availability(offer, rules),
    )

    weights = rules.weights
    score = (
        breakdown.rent * weights.rent
        + breakdown.room_type * weights.room_type
        + breakdown.size * weights.size
        + breakdown.location * weights.location
        + breakdown.availability * weights.availability
    )

    # A rejected offer scores zero so it can never win a ranking by accident.
    if reasons:
        score = 0.0

    return MatchResult(
        offer=offer,
        passed_filters=not reasons,
        score=round(score, 2),
        breakdown=breakdown,
        rejections=reasons,
    )


def rank(offers: Iterable[Offer], rules: RulesConfig) -> list[MatchResult]:
    """Evaluate and sort. Best first, deterministic on ties."""
    results = [evaluate(offer, rules) for offer in offers]
    results.sort(key=lambda result: result.sort_key())
    return results


def best_match(offers: Iterable[Offer], rules: RulesConfig) -> Optional[MatchResult]:
    """Highest-scoring offer that passes filters AND clears the auto-apply bar."""
    for result in rank(offers, rules):
        if result.passed_filters and result.score >= rules.auto_apply_min_score:
            return result
    return None
