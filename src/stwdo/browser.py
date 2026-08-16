"""Playwright context factory.

The browser is used for exactly two things — passing the mosparo gate and
submitting the application. Routine polling goes through `fetcher.py` over plain
HTTP, so this stays cold most of the time.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import AppConfig

logger = logging.getLogger(__name__)


class BrowserError(RuntimeError):
    """Playwright is unavailable or the browser could not be started."""


@contextmanager
def browser_page(config: AppConfig, headless: Optional[bool] = None) -> Iterator["object"]:
    """Yield a Playwright `Page` with any saved session state preloaded.

    Session state is written back on clean exit so the mosparo cookie survives
    between runs.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment problem
        raise BrowserError(
            "Playwright is not installed. Run: pip install -e . && playwright install chromium"
        ) from exc

    state_path = config.path(config.storage.storage_state_path)
    use_headless = config.browser.headless if headless is None else headless

    storage_state = None
    if state_path.is_file():
        try:
            storage_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable session state %s: %s", state_path, exc)

    playwright = None
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=use_headless)
        except Exception as exc:  # playwright raises its own error types
            raise BrowserError(
                f"Could not launch Chromium: {exc}\n"
                "If this is a fresh install, run: playwright install chromium"
            ) from exc

        context = browser.new_context(
            user_agent=config.browser.user_agent,
            locale=config.browser.locale,
            timezone_id=config.site.timezone,
            storage_state=storage_state,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        yield page

        try:
            context.storage_state(path=str(state_path))
        except Exception as exc:  # non-fatal: we just lose the cookie cache
            logger.warning("Could not persist session state: %s", exc)
    finally:
        for closer in (context, browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:  # pragma: no cover - best effort teardown
                    pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:  # pragma: no cover
                pass


def load_cookies(state_path: Path) -> dict[str, str]:
    """Read cookies out of a saved storage state, for the httpx fast path."""
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read cookies from %s: %s", state_path, exc)
        return {}

    cookies: dict[str, str] = {}
    for cookie in data.get("cookies", []):
        name = cookie.get("name")
        value = cookie.get("value")
        domain = str(cookie.get("domain", ""))
        if name and value is not None and "stwdo.de" in domain:
            cookies[name] = value
    return cookies
