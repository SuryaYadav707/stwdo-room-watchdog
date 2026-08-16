"""Fills and (optionally) submits the STWDO application.

Applying is a three-stage flow, discovered by `stwdo recon` against the live
site:

  1. the site-wide mosparo gate on /freie-zimmer/<id>
  2. an offer-level gate: a Wohnungshelden data-processing consent plus a SECOND
     mosparo box, which together reveal a "Bewerbungsformular laden" button
  3. the real form — a Wohnungshelden widget (app.wohnungshelden.de) inside an
     iframe, built with Angular Material + formly

Two independent safety gates guard the submit click:

  1. `application.live_enabled: true` in config.yaml
  2. `--live` on the command line

Both must be open. Anything else is a dry run: the form is filled and
screenshotted but never submitted. Above both sits the application lock in
`store.py`, which makes a second submission impossible even when both gates are
open — STWDO deletes every application from a person who submits more than once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig, Profile
from .gate import GateError, unlock, unlock_mosparo_box
from .models import MatchResult
from .store import Store

logger = logging.getLogger(__name__)

LABEL_PREFIX = "label:"


class ApplicationError(RuntimeError):
    """The application could not be filled or submitted."""


@dataclass
class ApplyOutcome:
    offer_id: str
    submitted: bool
    dry_run: bool
    evidence_path: Optional[Path]
    filled_fields: dict[str, str]
    missing_fields: list[str]
    message: str


def format_date_for_site(iso_date: str, language: str = "de") -> str:
    """ISO date -> the format the form expects (DD.MM.YYYY on the German form)."""
    if not iso_date:
        return ""
    try:
        parsed = datetime.strptime(iso_date.strip(), "%Y-%m-%d")
    except ValueError:
        return iso_date  # already in another format; pass through untouched
    return parsed.strftime("%d.%m.%Y" if language.lower().startswith("de") else "%Y-%m-%d")


class Applicant:
    def __init__(
        self,
        config: AppConfig,
        profile: Profile,
        selectors: dict[str, Any],
        store: Store,
    ) -> None:
        self.config = config
        self.profile = profile
        self.selectors = selectors or {}
        self.store = store

    # -- config helpers ----------------------------------------------------- #

    @property
    def form_config(self) -> dict[str, Any]:
        return self.selectors.get("application_form") or {}

    @property
    def detail_config(self) -> dict[str, Any]:
        return self.selectors.get("offer_detail") or {}

    def _field_specs(self) -> dict[str, dict]:
        specs = self.form_config.get("fields") or {}
        return {str(key): dict(value) for key, value in specs.items() if isinstance(value, dict)}

    def active_contact(self):
        """The email/mobile pair for this attempt, or None if none configured."""
        return self.profile.contact_for_attempt(self.store.attempt_count())

    def _profile_values(self) -> dict[str, str]:
        values = self.profile.flat()

        # Contact pairs rotate per attempt, so they override the flat fields.
        contact = self.active_contact()
        if contact is not None:
            if contact.email:
                values["email"] = contact.email
            if contact.mobile:
                values["mobile"] = contact.mobile

        if values.get("date_of_birth"):
            values["date_of_birth"] = format_date_for_site(
                values["date_of_birth"], self.config.site.language
            )
        return values

    def _evidence_path(self, offer_id: str, tag: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = self.config.path(self.config.storage.evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{stamp}-offer{offer_id}-{tag}.png"

    def _screenshot(self, page: Any, offer_id: str, tag: str) -> Optional[Path]:
        path = self._evidence_path(offer_id, tag)
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as exc:  # evidence is best-effort, never fatal
            logger.warning("Could not capture screenshot %s: %s", path, exc)
            return None

    # -- stage 2: reveal the form ------------------------------------------- #

    def open_application_form(self, page: Any, offer_id: str):
        """Walk stages 1-3 and return a FrameLocator for the Wohnungshelden form."""
        url = self.config.site.detail_url(offer_id)
        try:
            unlock(page, self.config, target_url=url)
        except GateError as exc:
            raise ApplicationError(f"Could not pass the site gate: {exc}") from exc

        consent_selector = self.detail_config.get("consent_checkbox") or "#application-consent-cb"
        try:
            page.wait_for_selector(consent_selector, timeout=20000)
            # The input is styled/hidden, so force the state change directly.
            page.check(consent_selector, force=True)
        except Exception as exc:
            raise ApplicationError(
                f"Could not accept the Wohnungshelden data-processing consent: {exc}"
            ) from exc

        box_selector = self.detail_config.get("mosparo_box") or "[id^='room-application-mosparo-box']"
        try:
            unlock_mosparo_box(page, box_selector, self.config.browser.gate_timeout_seconds * 1000)
        except GateError as exc:
            raise ApplicationError(f"Could not pass the offer-level spam check: {exc}") from exc

        load_selector = self.detail_config.get("load_form_button") or "#application-load-btn"
        try:
            page.wait_for_selector(load_selector, state="visible", timeout=30000)
            page.click(load_selector)
        except Exception as exc:
            raise ApplicationError(
                f"The 'Bewerbungsformular laden' button never became clickable: {exc}"
            ) from exc

        frame_selector = self.form_config.get("frame_selector") or "iframe[src*='wohnungshelden']"
        try:
            page.wait_for_selector(frame_selector, timeout=45000)
        except Exception as exc:
            raise ApplicationError(f"The Wohnungshelden form iframe never loaded: {exc}") from exc

        # A Frame (not a FrameLocator) is what we want: the Material controls need
        # to be driven through the page's own event handlers, which means running
        # JavaScript inside the frame.
        frame = None
        deadline = 45
        for _ in range(deadline):
            frame = next((f for f in page.frames if "wohnungshelden" in f.url), None)
            if frame is not None:
                break
            page.wait_for_timeout(1000)
        if frame is None:
            raise ApplicationError("The Wohnungshelden frame never attached.")

        try:
            frame.wait_for_selector("input, mat-select", timeout=45000)
        except Exception as exc:
            raise ApplicationError(f"The application form never rendered: {exc}") from exc
        return frame

    # -- recon -------------------------------------------------------------- #

    def recon(self, offer_id: str, output_dir: Path) -> dict[str, Any]:
        """Dump the real application form so selectors.yaml can be verified.

        The form sits behind two gates and inside a third-party iframe, so this
        is the only way to see it without applying.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        inventory: dict[str, Any] = {
            "url": self.config.site.detail_url(offer_id),
            "controls": [],
            "selects": [],
            "frame_url": "",
        }

        with browser_page_for(self.config) as page:
            frame = self.open_application_form(page, offer_id)
            page.wait_for_timeout(2000)

            target = next((f for f in page.frames if "wohnungshelden" in f.url), None)
            if target is None:
                raise ApplicationError("The Wohnungshelden frame disappeared during recon.")
            inventory["frame_url"] = target.url

            try:
                (output_dir / f"application_form_{offer_id}.html").write_text(
                    target.content(), encoding="utf-8"
                )
                inventory["controls"] = target.eval_on_selector_all(
                    "input, textarea, button",
                    """nodes => nodes.map(n => ({
                        tag: n.tagName.toLowerCase(),
                        type: n.type || '',
                        id: n.id || '',
                        required: !!n.required,
                        text: (n.innerText || '').trim().slice(0, 60),
                        label: (n.labels && n.labels[0] ? n.labels[0].innerText : '').trim().slice(0, 90)
                    }))""",
                )
                inventory["selects"] = target.eval_on_selector_all(
                    "mat-select", "nodes => nodes.map(n => ({id: n.id}))"
                )
            except Exception as exc:
                raise ApplicationError(f"Could not inventory the form: {exc}") from exc

            self._screenshot(page, offer_id, "recon")
            _ = frame  # the FrameLocator keeps the iframe alive for the screenshot

        return inventory

    # -- apply -------------------------------------------------------------- #

    def apply(self, result: MatchResult, live: bool = False) -> ApplyOutcome:
        offer_id = result.offer.offer_id
        specs = self._field_specs()

        if not specs:
            raise ApplicationError(
                "selectors.yaml -> application_form.fields is empty. "
                f"Run `stwdo recon --offer {offer_id}` first."
            )

        really_live = bool(live and self.config.application.live_enabled)
        if live and not really_live:
            raise ApplicationError(
                "--live was passed but config.yaml has application.live_enabled: false. "
                "Both gates must be open before anything is submitted."
            )

        if really_live:
            allowed, reason = self.store.can_apply()
            if not allowed:
                raise ApplicationError(reason)

        values = self._profile_values()
        filled: dict[str, str] = {}
        missing: list[str] = []
        evidence: Optional[Path] = None

        with browser_page_for(self.config) as page:
            frame = self.open_application_form(page, offer_id)

            for key, spec in specs.items():
                value = values.get(key, "")
                optional = bool(spec.get("optional"))
                kind = str(spec.get("kind", "text"))

                if kind in ("checkbox", "radio"):
                    ok = self._set_boolean(frame, spec, value, kind)
                elif not value:
                    ok = False
                else:
                    ok = self._set_value(frame, spec, value, kind)

                if ok:
                    filled[key] = value
                elif not optional:
                    missing.append(key)

            swapped = self._retry_email_if_rejected(frame, specs, values, filled)
            if swapped:
                logger.warning("Primary email rejected by the form; used the alternate.")

            evidence = self._screenshot(page, offer_id, "filled")

            if not really_live:
                return ApplyOutcome(
                    offer_id=offer_id,
                    submitted=False,
                    dry_run=True,
                    evidence_path=evidence,
                    filled_fields=filled,
                    missing_fields=missing,
                    message=(
                        "DRY RUN — form filled but not submitted. Review "
                        f"{evidence if evidence else 'the screenshot'} before going live."
                    ),
                )

            if missing:
                raise ApplicationError(
                    "Refusing to submit an incomplete application. Unfilled required fields: "
                    f"{', '.join(missing)}. STWDO gives the room to the first COMPLETE "
                    "application, and you only get one."
                )

            # Latch the lock BEFORE clicking. If the process dies mid-submit the
            # lock stays IN_FLIGHT and blocks a second attempt, which is the safe
            # direction to fail in.
            self.store.acquire_lock(offer_id, contact=filled.get("email", ""))

            submit_selector = self.form_config.get("submit_button") or "button[type='submit']"
            try:
                frame.locator(submit_selector).first.click(timeout=20000)
            except Exception as exc:
                raise ApplicationError(
                    f"Submit click failed: {exc}. The lock is now IN_FLIGHT — check by hand "
                    "whether the application went through before doing anything else."
                ) from exc

            page.wait_for_timeout(5000)
            evidence = self._screenshot(page, offer_id, "submitted") or evidence

            body_text = ""
            target = next((f for f in page.frames if "wohnungshelden" in f.url), None)
            if target is not None:
                try:
                    body_text = target.evaluate("() => document.body.innerText") or ""
                    (self.config.path(self.config.storage.evidence_dir) /
                     f"offer{offer_id}-response.html").write_text(target.content(), encoding="utf-8")
                except Exception as exc:
                    logger.warning("Could not save the response: %s", exc)

            success_text = self.form_config.get("success_text") or ""
            confirmed = bool(success_text) and success_text.lower() in body_text.lower()

            self.store.confirm_submission(
                offer_id,
                str(evidence) if evidence else "",
                note="confirmed" if confirmed else "submitted, confirmation text not verified",
            )

            message = "Application submitted."
            if not confirmed:
                message += (
                    " Confirmation text was not verified — check the evidence screenshot and "
                    "your email. Once you know the wording, put it in "
                    "selectors.yaml -> application_form.success_text."
                )

            return ApplyOutcome(
                offer_id=offer_id,
                submitted=True,
                dry_run=False,
                evidence_path=evidence,
                filled_fields=filled,
                missing_fields=missing,
                message=message,
            )

    # -- control drivers ---------------------------------------------------- #

    def _retry_email_if_rejected(
        self,
        frame: Any,
        specs: dict[str, dict],
        values: dict[str, str],
        filled: dict[str, str],
    ) -> bool:
        """Swap in the alternate email if the form rejected the primary one.

        This runs BEFORE anything is submitted, so it is a retry of a failed
        *fill*, not a second application. It can never produce two applications:
        the store lock still permits exactly one submission in total, whichever
        address ends up in the field.
        """
        alternate = self._next_contact_email(values.get("email", ""))
        spec = specs.get("email")
        if not alternate or not spec:
            return False

        selector = str(spec.get("selector") or "")
        if not selector or not self._field_has_error(frame, selector):
            return False

        if self._set_value(frame, spec, alternate, str(spec.get("kind", "text"))):
            filled["email"] = alternate
            return True
        return False

    def _next_contact_email(self, current: str) -> str:
        """The other pair's email, for a same-attempt retry of a rejected fill."""
        for pair in self.profile.contacts:
            if pair.email and pair.email.strip() != current.strip():
                return pair.email.strip()
        return ""

    def _field_has_error(self, frame: Any, selector: str) -> bool:
        """Whether Angular Material is showing a validation error for a control."""
        try:
            if selector.startswith(LABEL_PREFIX):
                label = selector[len(LABEL_PREFIX):]
                handle = frame.get_by_label(label, exact=True).first.element_handle(timeout=5000)
                if handle is None:
                    return False
                return bool(
                    handle.evaluate(
                        """node => {
                            const field = node.closest('mat-form-field');
                            const hasError = !!(field && field.querySelector('mat-error'));
                            return hasError || node.getAttribute('aria-invalid') === 'true';
                        }"""
                    )
                )
            return bool(
                frame.eval_on_selector(
                    selector,
                    """node => {
                        const field = node.closest('mat-form-field');
                        const hasError = !!(field && field.querySelector('mat-error'));
                        return hasError || node.getAttribute('aria-invalid') === 'true';
                    }""",
                )
            )
        except Exception as exc:
            logger.debug("Could not read validation state for %s: %s", selector, exc)
            return False

    def _locate(self, frame: Any, selector: str):
        """Resolve a selector string: "label:Vorname" or plain CSS."""
        if selector.startswith(LABEL_PREFIX):
            return frame.get_by_label(selector[len(LABEL_PREFIX):], exact=True).first
        return frame.locator(selector).first

    def _set_value(self, frame: Any, spec: dict, value: str, kind: str) -> bool:
        selector = str(spec.get("selector") or "")
        if not selector:
            return False
        try:
            if kind == "mat_select":
                return self._select_material_option(frame, selector, value)
            self._locate(frame, selector).fill(value, timeout=15000)
            return True
        except Exception as exc:
            logger.warning("Could not fill %s (%s): %s", selector, kind, exc)
            return False

    def _select_material_option(self, frame: Any, selector: str, value: str) -> bool:
        """Drive an Angular Material dropdown.

        These are not <select> elements: the trigger opens a CDK overlay of
        <mat-option> nodes. Playwright's actionability checks time out against
        that overlay, so both the trigger and the option are clicked through the
        page's own DOM handlers instead. Match is exact first, then prefix, so
        "China" cannot accidentally select "Chinese Taipei".
        """
        try:
            opened = frame.eval_on_selector(
                selector, "node => { node.click(); return true; }"
            )
            if not opened:
                return False
            frame.wait_for_selector("mat-option", timeout=15000)
            picked = frame.eval_on_selector_all(
                "mat-option",
                """(nodes, wanted) => {
                    const text = n => (n.innerText || '').trim();
                    const hit = nodes.find(n => text(n) === wanted)
                             || nodes.find(n => text(n).startsWith(wanted));
                    if (!hit) return false;
                    hit.click();
                    return true;
                }""",
                arg=value,
            )
            if not picked:
                logger.warning("Dropdown %s has no option matching %r", selector, value)
                # Close the overlay so it cannot swallow the next click.
                frame.evaluate("() => document.querySelector('.cdk-overlay-backdrop')?.click()")
                return False
            frame.wait_for_timeout(300)
            return True
        except Exception as exc:
            logger.warning("Could not pick option %r: %s", value, exc)
            return False

    def _set_boolean(self, frame: Any, spec: dict, value: str, kind: str) -> bool:
        """Tick a Material checkbox or radio.

        The real <input> is visually hidden and Playwright's check() reports
        "Clicking the checkbox did not change its state", so the click goes
        through the DOM handler and the resulting state is verified afterwards.
        """
        wants_true = str(value).strip().lower() in ("1", "true", "yes", "ja", "on")

        if kind == "radio":
            key = "selector_true" if wants_true else "selector_false"
            selector = str(spec.get(key) or "")
        else:
            selector = str(spec.get("selector") or "")
            if not wants_true:
                # An unticked consent is a deliberate "no", not a failed fill.
                return True

        if not selector:
            return False

        try:
            return bool(
                frame.eval_on_selector(
                    selector,
                    "node => { if (!node.checked) { node.click(); } return !!node.checked; }",
                )
            )
        except Exception as exc:
            logger.warning("Could not set %s control %s: %s", kind, selector, exc)
            return False


def browser_page_for(config: AppConfig):
    """Indirection so tests can patch the browser out."""
    from .browser import browser_page

    return browser_page(config)
