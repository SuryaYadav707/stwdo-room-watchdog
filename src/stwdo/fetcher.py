"""Hybrid transport: cheap HTTP polling, browser only when the gate demands it.

Rooms are uploaded at any time on any day, so polling runs every couple of
minutes around the clock. Launching Chromium that often would be both wasteful
and a loud fingerprint. Instead the mosparo cookie obtained once via Playwright
is replayed on a plain `httpx` client, and the browser is woken only when the
cookie has expired.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .browser import BrowserError, browser_page, load_cookies
from .config import AppConfig
from .gate import GateError, is_gated, unlock

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """The offer page could not be retrieved."""


@dataclass
class FetchResult:
    html: str
    transport: str  # "http" or "browser"
    duration_ms: int
    refreshed_gate: bool = False


class Fetcher:
    """Fetches the offer list, refreshing the mosparo session when needed."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._client: Optional[httpx.Client] = None

    # -- http fast path ----------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        # Mirrors what the Playwright context sends, so the session looks like
        # one consistent client rather than two different ones.
        language = "de-DE,de;q=0.9,en;q=0.8"
        if self.config.site.language.lower().startswith("en"):
            language = "en-GB,en;q=0.9,de;q=0.8"
        return {
            "User-Agent": self.config.browser.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": language,
            "Cache-Control": "no-cache",
        }

    def _build_client(self) -> httpx.Client:
        cookies = load_cookies(self.config.path(self.config.storage.storage_state_path))
        return httpx.Client(
            headers=self._headers(),
            cookies=cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
        )

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _reset_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._client = None

    def close(self) -> None:
        self._reset_client()

    # -- gate refresh ------------------------------------------------------- #

    def refresh_session(self) -> float:
        """Run the browser once to pass the gate and persist a fresh cookie."""
        logger.info("Refreshing mosparo session via browser")
        with browser_page(self.config) as page:
            elapsed = unlock(page, self.config)
        self._reset_client()  # pick up the new cookie
        return elapsed

    # -- public API --------------------------------------------------------- #

    def fetch_offers(self, allow_gate_refresh: bool = True) -> FetchResult:
        """Fetch the offer list HTML, unlocking the gate at most once."""
        started = time.monotonic()

        try:
            response = self.client.get(self.config.site.offers_url())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP request to the offer list failed: {exc}") from exc

        html = response.text
        if not is_gated(html):
            return FetchResult(
                html=html,
                transport="http",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if not allow_gate_refresh:
            raise FetchError("Session expired and a gate refresh was not permitted.")

        logger.info("Cookie expired, gate detected on the HTTP path")
        try:
            self.refresh_session()
        except (GateError, BrowserError) as exc:
            # Surfaced as FetchError so the caller has a single failure type to
            # handle whether the problem was the network, the gate, or Chromium.
            raise FetchError(f"Gate refresh failed: {exc}") from exc

        try:
            response = self.client.get(self.config.site.offers_url())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP retry after gate refresh failed: {exc}") from exc

        html = response.text
        if is_gated(html):
            raise FetchError(
                "Still gated after a successful unlock — the cookie is not being "
                "carried over. Check data/storage_state.json."
            )

        return FetchResult(
            html=html,
            transport="browser",
            duration_ms=int((time.monotonic() - started) * 1000),
            refreshed_gate=True,
        )

    def fetch_detail(self, offer_id: str) -> str:
        """Fetch a single offer detail page over the HTTP path."""
        url = self.config.site.detail_url(offer_id)
        try:
            response = self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"Could not fetch {url}: {exc}") from exc
        return response.text
