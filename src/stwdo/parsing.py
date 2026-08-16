"""Text parsing for STWDO listing cards.

Pure string -> value functions, kept out of `scraper.py` so they can be tested
without any HTML. The site mixes German and English number formats on the same
page ("359,00-407,00€" next to "€347.00 - €461.00"), so every parser here has to
cope with both.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import RoomType

# Matches a number in either convention, with optional thousands separators.
_NUMBER = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?"

_PRICE_RANGE = re.compile(
    rf"(?P<low>{_NUMBER})\s*(?:€|EUR)?\s*(?:-|–|—|bis|to)\s*(?:€|EUR)?\s*(?P<high>{_NUMBER})",
    re.IGNORECASE,
)
_PRICE_SINGLE = re.compile(rf"(?:€|EUR)\s*(?P<value>{_NUMBER})|(?P<value2>{_NUMBER})\s*(?:€|EUR)", re.IGNORECASE)

_SIZE_RANGE = re.compile(
    rf"(?P<low>{_NUMBER})\s*(?:m²|m2|qm)?\s*(?:-|–|—|bis|to)\s*(?P<high>{_NUMBER})\s*(?:m²|m2|qm)",
    re.IGNORECASE,
)
_SIZE_SINGLE = re.compile(rf"(?P<value>{_NUMBER})\s*(?:m²|m2|qm)", re.IGNORECASE)

_COUNT = re.compile(r"(?P<count>\d+)\s*(?:available|verf[üu]gbare?|freie?|frei)", re.IGNORECASE)
_COUNT_LEADING = re.compile(r"^\s*(?P<count>\d+)\b")

_OFFER_ID = re.compile(r"/freie-zimmer/(?P<id>\d+)")


def parse_number(text: str) -> Optional[float]:
    """Parse one number written in either German or English convention.

    Disambiguation rules, in order:
      * both separators present -> the LAST one is the decimal separator
      * only a comma -> decimal separator (German)
      * only a dot -> decimal if it has 1-2 trailing digits, else thousands
    """
    if text is None:
        return None
    cleaned = text.strip().replace("\xa0", " ").replace(" ", "")
    if not cleaned:
        return None

    has_comma = "," in cleaned
    has_dot = "." in cleaned

    if has_comma and has_dot:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        cleaned = cleaned.replace(",", ".")
    elif has_dot:
        # "1.234" is a thousands separator; "347.00" and "12.5" are decimals.
        decimals = len(cleaned.rsplit(".", 1)[1])
        if decimals == 3:
            cleaned = cleaned.replace(".", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_price_range(text: str) -> tuple[Optional[float], Optional[float]]:
    """Extract (min, max) EUR from a price badge. A single price yields (p, p)."""
    if not text:
        return None, None

    match = _PRICE_RANGE.search(text)
    if match:
        low = parse_number(match.group("low"))
        high = parse_number(match.group("high"))
        if low is not None and high is not None and low > high:
            low, high = high, low
        return low, high

    match = _PRICE_SINGLE.search(text)
    if match:
        value = parse_number(match.group("value") or match.group("value2") or "")
        return value, value

    return None, None


def parse_size_range(text: str) -> tuple[Optional[float], Optional[float]]:
    """Extract (min, max) m² from a size badge. A single size yields (s, s)."""
    if not text:
        return None, None

    match = _SIZE_RANGE.search(text)
    if match:
        low = parse_number(match.group("low"))
        high = parse_number(match.group("high"))
        if low is not None and high is not None and low > high:
            low, high = high, low
        return low, high

    match = _SIZE_SINGLE.search(text)
    if match:
        value = parse_number(match.group("value"))
        return value, value

    return None, None


def parse_available_count(text: str) -> Optional[int]:
    """Number of free units, from titles like "9 available 3-person shared flats"
    or the German "9 freie Zimmer in 3er-Wohngemeinschaften"."""
    if not text:
        return None

    match = _COUNT.search(text)
    if match:
        try:
            return int(match.group("count"))
        except ValueError:
            return None

    # Fall back to a leading integer, which is where the count always sits.
    match = _COUNT_LEADING.search(text)
    if match:
        try:
            return int(match.group("count"))
        except ValueError:
            return None

    return None


def parse_room_type(text: str) -> RoomType:
    """Classify a listing title into a room category.

    Deliberately conservative: anything ambiguous returns UNKNOWN, which the
    hard filters reject. Guessing wrong here would spend the one application.
    """
    if not text:
        return RoomType.UNKNOWN

    lowered = text.casefold()

    # Shared flats: "3-person shared flat", "3er-WG", "3er Wohngemeinschaft".
    shared = re.search(r"(\d+)\s*(?:-|\s)?\s*(?:person|er[-\s]?w|personen)", lowered)
    if shared:
        try:
            size = int(shared.group(1))
        except ValueError:
            size = 0
        mapping = {
            2: RoomType.SHARED_2,
            3: RoomType.SHARED_3,
            4: RoomType.SHARED_4,
        }
        if size in mapping:
            return mapping[size]
        return RoomType.UNKNOWN

    if "wohngemeinschaft" in lowered or "shared flat" in lowered or re.search(r"\bwg\b", lowered):
        return RoomType.UNKNOWN  # shared, but the size is unreadable

    if "apartment" in lowered or "appartement" in lowered or "apartement" in lowered:
        return RoomType.SINGLE_APARTMENT

    return RoomType.UNKNOWN


def parse_location(text: str) -> tuple[str, str]:
    """Split the card's location line into (city, address).

    The site writes it as "City, Street, Number" (e.g. "Hagen, Im Alten Holz, 133"
    or "Dortmund, Dortmund, 1").
    """
    if not text:
        return "", ""

    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""

    city = parts[0]
    address = " ".join(parts[1:])
    return city, address


def parse_offer_id(href: str) -> Optional[str]:
    """Pull the numeric offer id out of a /freie-zimmer/<id> link."""
    if not href:
        return None
    match = _OFFER_ID.search(href)
    return match.group("id") if match else None
