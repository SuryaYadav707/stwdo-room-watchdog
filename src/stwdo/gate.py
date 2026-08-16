"""mosparo gate handling.

Every housing URL on stwdo.de redirects to a gate page carrying a mosparo widget
served from `https://mosparo.stwdo.de`. Observed behaviour on the live site:

  * mosparo renders INLINE in the page DOM — there is no iframe.
  * It is a consent checkbox ("Ich akzeptiere, dass die Formulareingaben auf Spam
    überprüft ... werden"), not an invisible auto-check. Nothing happens until
    the box is ticked.
  * Ticking it posts to `/api/v1/frontend/check-form-data`; on `valid: true` the
    page's own JavaScript calls `form.requestSubmit()` and the gate opens.
  * The widget first has to fetch a submit token asynchronously. Clicking before
    that token lands fails with "No submit token available. Validation of this
    form is not possible." — so we wait for the token to appear in the DOM
    before clicking. This was the single reason unlocking used to time out.

The cookie the gate sets is explicitly short-lived ("Der Zugriff bleibt danach
auf diesem Gerät für kurze Zeit aktiv"), so callers must be ready to re-unlock at
any time.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .config import AppConfig

logger = logging.getLogger(__name__)

GATE_MARKER = "housing-offers-access-form"


class GateError(RuntimeError):
    """The mosparo gate could not be passed."""


def is_gated(html: str) -> bool:
    """Whether a fetched document is the gate page rather than real content."""
    return GATE_MARKER in (html or "")


def unlock(page: Any, config: AppConfig, target_url: Optional[str] = None) -> float:
    """Navigate to `target_url` and pass the gate. Returns elapsed seconds.

    Raises GateError on timeout so callers can alert rather than silently parse
    an empty page.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    url = target_url or config.site.offers_url()
    selectors = _gate_selectors(config)
    timeout_ms = config.browser.gate_timeout_seconds * 1000
    started = time.monotonic()

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except (PlaywrightTimeout, PlaywrightError) as exc:
        raise GateError(f"Could not load {url}: {exc}") from exc

    if not _gate_present(page, selectors["form"]):
        logger.debug("No gate encountered at %s (cookie still valid)", url)
        return time.monotonic() - started

    logger.info("mosparo gate hit, accepting the spam-check consent")

    try:
        page.wait_for_selector(selectors["mosparo_box"], timeout=timeout_ms)
    except (PlaywrightTimeout, PlaywrightError) as exc:
        raise GateError(f"mosparo widget never rendered: {exc}") from exc

    unlock_mosparo_box(page, selectors["mosparo_box"], timeout_ms, selectors)

    # On valid data the page calls form.requestSubmit() itself. Wait for the gate
    # form to disappear rather than for a navigation event, so a client-side
    # rerender counts too.
    try:
        page.wait_for_selector(selectors["form"], state="detached", timeout=timeout_ms)
    except (PlaywrightTimeout, PlaywrightError) as exc:
        raise GateError(
            f"mosparo gate did not open within {config.browser.gate_timeout_seconds}s. "
            f"Widget error: {_widget_error(page) or 'none reported'}. "
            "Run `stwdo probe --headed` to watch it."
        ) from exc

    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except (PlaywrightTimeout, PlaywrightError):
        pass  # content is already present; the load state is a nicety

    elapsed = time.monotonic() - started
    logger.info("Gate passed in %.1fs", elapsed)
    return elapsed


def _gate_selectors(config: AppConfig) -> dict[str, str]:
    from .config import load_selectors

    defaults = {
        "form": "#housing-offers-access-form",
        "mosparo_box": "#housing-offers-mosparo-box",
        # The styled div, not the hidden <input> — see _tick_consent.
        "mosparo_checkbox": ".mosparo__checkbox",
        "mosparo_submit_token": "input.mosparo__submit-token",
    }
    try:
        configured = load_selectors(config.root).get("gate") or {}
    except Exception as exc:  # a broken selectors file must not break the gate
        logger.warning("Falling back to built-in gate selectors: %s", exc)
        configured = {}
    defaults.update({k: v for k, v in configured.items() if v})
    return defaults


def _gate_present(page: Any, form_selector: str) -> bool:
    try:
        return page.query_selector(form_selector) is not None
    except Exception:  # pragma: no cover - defensive
        return False


def unlock_mosparo_box(
    page: Any,
    box_selector: str,
    timeout_ms: int,
    selectors: Optional[dict[str, str]] = None,
) -> None:
    """Validate one mosparo widget: wait for its token, then tick its consent.

    Used for both mosparo instances in the flow — the site-wide gate and the
    per-offer application gate — since they behave identically.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    token_selector = (selectors or {}).get("mosparo_submit_token", "input.mosparo__submit-token")
    checkbox_selector = (selectors or {}).get("mosparo_checkbox", ".mosparo__checkbox")

    # Clicking before the token lands fails with "No submit token available.
    # Validation of this form is not possible." — this wait is the whole fix.
    try:
        page.wait_for_function(
            "selector => { const el = document.querySelector(selector); "
            "return !!(el && el.value && el.value.length > 10); }",
            arg=f"{box_selector} {token_selector}",
            timeout=timeout_ms,
        )
    except (PlaywrightTimeout, PlaywrightError) as exc:
        raise GateError(
            "mosparo never issued a submit token — its API may be unreachable "
            f"or rate-limiting this IP: {exc}"
        ) from exc

    # The real <input> is visually hidden and ignores synthetic clicks; the
    # sibling styled div carries the handler.
    target = f"{box_selector} {checkbox_selector}"
    try:
        page.click(target, timeout=min(timeout_ms, 15000))
        logger.debug("mosparo consent clicked: %s", target)
    except (PlaywrightTimeout, PlaywrightError) as exc:
        raise GateError(f"Could not click the mosparo consent control {target!r}: {exc}") from exc


def _widget_error(page: Any) -> str:
    """Read mosparo's own error text, which explains most failures precisely."""
    try:
        messages = page.eval_on_selector_all(
            ".mosparo__error-message", "nodes => nodes.map(n => n.innerText).filter(Boolean)"
        )
        return "; ".join(messages)[:300]
    except Exception:  # pragma: no cover - diagnostics only
        return ""
