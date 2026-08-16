"""Parser tests. Every literal here was taken from the live STWDO listing page."""

from __future__ import annotations

import pytest

from stwdo.models import RoomType
from stwdo.parsing import (
    parse_available_count,
    parse_location,
    parse_number,
    parse_offer_id,
    parse_price_range,
    parse_room_type,
    parse_size_range,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("359,00", 359.0),
        ("347.00", 347.0),
        ("426,74", 426.74),
        ("1.234", 1234.0),  # German thousands separator
        ("1,234.56", 1234.56),  # English
        ("1.234,56", 1234.56),  # German
        ("12,5", 12.5),
        ("40", 40.0),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("359,00-407,00€", (359.0, 407.0)),
        ("333,00-379,00 €", (333.0, 379.0)),
        ("€347.00 - €461.00", (347.0, 461.0)),
        ("€326.00 - €594.00", (326.0, 594.0)),
        ("426,74€", (426.74, 426.74)),
        ("", (None, None)),
    ],
)
def test_parse_price_range(text, expected):
    assert parse_price_range(text) == expected


def test_parse_price_range_normalises_inverted_bounds():
    assert parse_price_range("407,00-359,00€") == (359.0, 407.0)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("20m² - 37m²", (20.0, 37.0)),
        ("21 - 27 m²", (21.0, 27.0)),
        ("25m² - 26m²", (25.0, 26.0)),
        ("16m² - 36m²", (16.0, 36.0)),
        ("11-40 m²", (11.0, 40.0)),
        ("18 qm", (18.0, 18.0)),
        ("", (None, None)),
    ],
)
def test_parse_size_range(text, expected):
    assert parse_size_range(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("9 available 3-person shared flats at the Dortmund location", 9),
        ("23 available 4-person shared flats", 23),
        ("18 available single apartments at HAGEN FH Südwestfalen", 18),
        ("9 freie Zimmer in 3er-Wohngemeinschaften", 9),
        ("keine Angabe", None),
    ],
)
def test_parse_available_count(text, expected):
    assert parse_available_count(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("9 available 3-person shared flats", RoomType.SHARED_3),
        ("23 available 4-person shared flats", RoomType.SHARED_4),
        ("49 available 2-person shared flats", RoomType.SHARED_2),
        ("18 available single apartments at HAGEN", RoomType.SINGLE_APARTMENT),
        ("25 available single apartments at Iserlohn", RoomType.SINGLE_APARTMENT),
        ("9 freie Zimmer in 3er-Wohngemeinschaften", RoomType.SHARED_3),
        ("18 freie Einzelapartments", RoomType.SINGLE_APARTMENT),
        # Shared but unreadable size must NOT be guessed.
        ("freie Zimmer in Wohngemeinschaften", RoomType.UNKNOWN),
        ("", RoomType.UNKNOWN),
    ],
)
def test_parse_room_type(text, expected):
    assert parse_room_type(text) == expected


def test_parse_room_type_rejects_unsupported_share_size():
    assert parse_room_type("7-person shared flat") == RoomType.UNKNOWN


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Dortmund, Dortmund, 1", ("Dortmund", "Dortmund 1")),
        ("Hagen, Im Alten Holz, 133", ("Hagen", "Im Alten Holz 133")),
        ("Iserlohn, Steubenstraße, 14-18", ("Iserlohn", "Steubenstraße 14-18")),
        ("Dortmund", ("Dortmund", "")),
        ("", ("", "")),
    ],
)
def test_parse_location(text, expected):
    assert parse_location(text) == expected


@pytest.mark.parametrize(
    "href,expected",
    [
        ("/freie-zimmer/6583", "6583"),
        ("https://www.stwdo.de/freie-zimmer/6583#bewerbung", "6583"),
        ("/wohnen/aktuelle-wohnangebote", None),
        ("", None),
    ],
)
def test_parse_offer_id(href, expected):
    assert parse_offer_id(href) == expected
