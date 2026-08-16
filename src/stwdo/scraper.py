"""Turn the unlocked offer-list HTML into `Offer` objects.

Anchored on `a[href*='/freie-zimmer/']` rather than on styling classes: the href
is what the site is actually *about*, so it survives redesigns that rename
Tailwind classes. From each anchor we climb to the nearest ancestor that holds
the whole card (heading + price + size badges) and read the text out of it.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from .gate import is_gated
from .models import Offer, RoomType
from .parsing import (
    parse_available_count,
    parse_location,
    parse_offer_id,
    parse_price_range,
    parse_room_type,
    parse_size_range,
)

logger = logging.getLogger(__name__)

MAX_ANCESTOR_CLIMB = 8

# Proof that we are looking at the real listing page. Offers are only published
# Mon 10:00-Tue 12:00 and Wed 10:00-Thu 12:00, so "zero offers" is a normal
# state for most of the week — it must not be mistaken for a parse failure.
PAGE_MARKERS = (
    "/wohnen/aktuelle-wohnangebote",   # breadcrumb metadata, language-independent
    "Freie Zimmer",
    "Aktuelle Wohnangebote",
    "Available rooms",
)


class ScrapeError(RuntimeError):
    """The page could not be parsed into offers."""


class GatedPageError(ScrapeError):
    """The page handed to the parser is the mosparo gate, not the listing."""


def parse_offers(html: str, base_url: str = "https://www.stwdo.de") -> list[Offer]:
    """Parse every listing bucket on the page. Order follows the document."""
    if not html or not html.strip():
        raise ScrapeError("Empty document.")
    if is_gated(html):
        raise GatedPageError("This is the mosparo gate page, not the offer list.")

    tree = HTMLParser(html)
    offers: dict[str, Offer] = {}

    for anchor in tree.css("a[href*='/freie-zimmer/']"):
        href = anchor.attributes.get("href") or ""
        offer_id = parse_offer_id(href)
        if offer_id is None or offer_id in offers:
            continue  # cards link twice (image + button); first one wins

        card = _find_card_root(anchor)
        if card is None:
            logger.debug("No card container found for offer %s", offer_id)
            continue

        offer = _build_offer(offer_id, urljoin(base_url, href), card)
        if offer is not None:
            offers[offer_id] = offer

    if not offers:
        if is_listing_page(html):
            # Genuinely nothing on offer right now — the normal state between
            # publication windows. Not an error.
            logger.info("Listing page is up but currently empty.")
            return []
        raise ScrapeError(
            "No offers parsed and the page does not look like the listing page. "
            "The markup has most likely changed — re-run `stwdo recon` and "
            "update selectors.yaml."
        )

    return list(offers.values())


def is_listing_page(html: str) -> bool:
    """Whether this document is recognisably the offers page, empty or not."""
    return any(marker in html for marker in PAGE_MARKERS)


def _find_card_root(anchor: Node) -> Optional[Node]:
    """Find the node that contains the whole card.

    On the live site the anchor *is* the card — it wraps image, heading and
    badges — so the anchor itself is checked first. Other layouts wrap the link
    in an <article>, so we then climb, stopping as soon as a node would swallow a
    neighbouring offer link.

    A card is recognised by containing both a price and a size marker.
    """
    node: Optional[Node] = anchor
    best: Optional[Node] = None

    anchor_text = anchor.text(separator=" ", strip=True) or ""
    if _looks_like_card(anchor_text):
        best = anchor

    for _ in range(MAX_ANCESTOR_CLIMB):
        if node is None:
            break
        parent = node.parent
        if parent is None:
            break

        links = parent.css("a[href*='/freie-zimmer/']")
        ids = {parse_offer_id(link.attributes.get("href") or "") for link in links}
        ids.discard(None)
        if len(ids) > 1:
            break  # this ancestor already covers a neighbouring card

        text = parent.text(separator=" ", strip=True) or ""
        if _looks_like_card(text):
            best = parent

        node = parent

    return best


def _looks_like_card(text: str) -> bool:
    has_price = "€" in text or "EUR" in text.upper()
    has_size = "m²" in text or "m2" in text.lower() or "qm" in text.lower()
    return has_price and has_size


def _build_offer(offer_id: str, url: str, card: Node) -> Optional[Offer]:
    text = card.text(separator="\n", strip=True) or ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    title = _extract_title(card, lines)
    location_line = _extract_location_line(lines, title)
    city, address = parse_location(location_line)

    price_min, price_max = _first_match(lines, parse_price_range, marker=("€", "EUR"))
    size_min, size_max = _first_match(lines, parse_size_range, marker=("m²", "m2", "qm"))

    room_type = parse_room_type(title)
    if room_type == RoomType.UNKNOWN:
        # Some layouts put the category in a subtitle rather than the heading.
        for line in lines:
            candidate = parse_room_type(line)
            if candidate != RoomType.UNKNOWN:
                room_type = candidate
                break

    return Offer(
        offer_id=offer_id,
        url=url,
        title=title,
        city=city,
        address=address,
        room_type=room_type,
        price_min=price_min,
        price_max=price_max,
        size_min=size_min,
        size_max=size_max,
        available_count=parse_available_count(title),
    )


def _extract_title(card: Node, lines: list[str]) -> str:
    for selector in ("h1", "h2", "h3", "h4"):
        heading = card.css_first(selector)
        if heading is not None:
            heading_text = heading.text(separator=" ", strip=True)
            if heading_text:
                return " ".join(heading_text.split())

    # No heading: the longest line is reliably the title on these cards.
    return max(lines, key=len) if lines else ""


def _extract_location_line(lines: list[str], title: str) -> str:
    """The location sits above the heading as "City, Street, Number"."""
    for line in lines:
        if line == title:
            break
        if "," in line and "€" not in line and "m²" not in line:
            return line

    for line in lines:
        if "," in line and "€" not in line and "m²" not in line and line != title:
            return line
    return ""


def _first_match(lines: list[str], parser, marker: tuple[str, ...]):
    """Run `parser` over the first line carrying one of `marker`."""
    for line in lines:
        lowered = line.lower()
        if any(token.lower() in lowered for token in marker):
            low, high = parser(line)
            if low is not None or high is not None:
                return low, high
    return None, None
