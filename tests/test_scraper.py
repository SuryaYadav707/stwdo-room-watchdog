"""Scraper tests.

`gate_page.html` is a real capture of the live mosparo gate, so the gate
detection here is tested against the actual markup rather than a guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stwdo.gate import is_gated
from stwdo.models import RoomType
from stwdo.scraper import GatedPageError, ScrapeError, is_listing_page, parse_offers

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def offers():
    html = (FIXTURES / "offer_list_sample.html").read_text(encoding="utf-8")
    return {offer.offer_id: offer for offer in parse_offers(html)}


def test_all_cards_are_found(offers):
    assert set(offers) == {"6583", "6584", "6590", "6601", "6612"}


def test_german_price_format(offers):
    offer = offers["6583"]
    assert (offer.price_min, offer.price_max) == (359.0, 407.0)
    assert (offer.size_min, offer.size_max) == (20.0, 37.0)


def test_english_price_format(offers):
    offer = offers["6601"]
    assert (offer.price_min, offer.price_max) == (347.0, 461.0)
    assert (offer.size_min, offer.size_max) == (16.0, 36.0)


def test_single_price_becomes_a_degenerate_range(offers):
    offer = offers["6590"]
    assert (offer.price_min, offer.price_max) == (426.74, 426.74)


def test_room_types(offers):
    assert offers["6583"].room_type == RoomType.SHARED_3
    assert offers["6584"].room_type == RoomType.SHARED_4
    assert offers["6601"].room_type == RoomType.SHARED_2
    assert offers["6590"].room_type == RoomType.SINGLE_APARTMENT
    assert offers["6612"].room_type == RoomType.SINGLE_APARTMENT


def test_location_split(offers):
    assert offers["6590"].city == "Hagen"
    assert offers["6590"].address == "Im Alten Holz 133"
    assert offers["6612"].city == "Iserlohn"


def test_available_counts(offers):
    assert offers["6583"].available_count == 9
    assert offers["6601"].available_count == 49


def test_urls_are_absolute(offers):
    assert offers["6583"].url == "https://www.stwdo.de/freie-zimmer/6583"


def test_duplicate_links_in_one_card_yield_one_offer(offers):
    """Each card links twice (image + button) — that must not double-count."""
    assert len(offers) == 5


def test_fingerprint_tracks_count_and_price(offers):
    offer = offers["6583"]
    assert offer.fingerprint() == "6583|9|359.0|407.0"


# --------------------------------------------------------------------------- #
# failure modes
# --------------------------------------------------------------------------- #


def test_real_gate_page_is_detected():
    html = (FIXTURES / "gate_page.html").read_text(encoding="utf-8")
    assert is_gated(html)


def test_gate_page_raises_rather_than_returning_nothing():
    """Silently returning zero offers would look like "no rooms available"."""
    html = (FIXTURES / "gate_page.html").read_text(encoding="utf-8")
    with pytest.raises(GatedPageError):
        parse_offers(html)


def test_empty_document_raises():
    with pytest.raises(ScrapeError):
        parse_offers("")


def test_unrecognised_markup_raises():
    with pytest.raises(ScrapeError):
        parse_offers("<html><body><p>Wartungsarbeiten</p></body></html>")


# --------------------------------------------------------------------------- #
# empty listing — the normal state between publication windows
# --------------------------------------------------------------------------- #


EMPTY_LISTING = """
<html><body>
  <nav><meta itemprop="url" content="/wohnen/aktuelle-wohnangebote/"></nav>
  <main>
    <h1>Freie Zimmer in unseren Wohnanlagen</h1>
    <p>Sollten hier gerade keine Angebote vorliegen, schau bitte zu einem
       spaeteren Zeitpunkt wieder vorbei.</p>
  </main>
</body></html>
"""


def test_empty_listing_page_returns_no_offers_instead_of_raising():
    """Offers publish Mon 10:00-Tue 12:00 and Wed 10:00-Thu 12:00. Most of the
    week the page is legitimately empty; treating that as a failure would fire
    false alarms and back the poller off for no reason."""
    assert parse_offers(EMPTY_LISTING) == []


def test_empty_page_is_recognised_as_the_listing_page():
    assert is_listing_page(EMPTY_LISTING)


def test_error_page_is_not_recognised_as_the_listing_page():
    assert not is_listing_page("<html><body><h1>502 Bad Gateway</h1></body></html>")


def test_error_page_still_raises():
    """An unrecognisable page must NOT be silently reported as "no rooms"."""
    with pytest.raises(ScrapeError):
        parse_offers("<html><body><h1>502 Bad Gateway</h1></body></html>")
