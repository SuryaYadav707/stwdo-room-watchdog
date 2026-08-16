"""Core domain types. Pure data, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RoomType(str, Enum):
    """Room categories STWDO publishes. `unknown` never passes the hard filters."""

    SINGLE_APARTMENT = "single_apartment"
    SHARED_2 = "shared_2"
    SHARED_3 = "shared_3"
    SHARED_4 = "shared_4"
    UNKNOWN = "unknown"


class LockState(str, Enum):
    """State of the one-application-per-person lock."""

    NONE = "none"
    IN_FLIGHT = "in_flight"
    SUBMITTED = "submitted"


@dataclass(frozen=True)
class Offer:
    """One listing bucket, e.g. "9 available 3-person shared flats".

    STWDO lists categories, not individual rooms, so prices and sizes are ranges
    and `available_count` can change without the offer id changing.
    """

    offer_id: str
    url: str
    title: str
    city: str
    address: str
    room_type: RoomType
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    size_min: Optional[float] = None
    size_max: Optional[float] = None
    available_count: Optional[int] = None

    def fingerprint(self) -> str:
        """Identity for change detection: id plus the fields worth re-alerting on."""
        return f"{self.offer_id}|{self.available_count}|{self.price_min}|{self.price_max}"


@dataclass(frozen=True)
class ScoreBreakdown:
    """Per-component contributions, so a decision can always be explained."""

    rent: float = 0.0
    room_type: float = 0.0
    size: float = 0.0
    location: float = 0.0
    availability: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "rent": self.rent,
            "room_type": self.room_type,
            "size": self.size,
            "location": self.location,
            "availability": self.availability,
        }


@dataclass(frozen=True)
class MatchResult:
    """Outcome of scoring one offer against the rules."""

    offer: Offer
    passed_filters: bool
    score: float
    breakdown: ScoreBreakdown
    rejections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rejected(self) -> bool:
        return not self.passed_filters

    def sort_key(self) -> tuple[float, float, str]:
        """Deterministic ranking: score desc, then cheaper, then stable by id.

        Negated so a plain ascending sort puts the best match first, and so two
        identical runs always pick the same offer.
        """
        price = self.offer.price_min if self.offer.price_min is not None else float("inf")
        return (-self.score, price, self.offer.offer_id)
