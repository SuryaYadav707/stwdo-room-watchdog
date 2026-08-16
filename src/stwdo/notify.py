"""Telegram notifications.

Plain HTTP against the Bot API — no async framework, because the watchdog is a
synchronous loop and a whole bot runtime would be dead weight.

Notification failures are logged and swallowed: losing an alert is bad, but
crashing the watchdog because Telegram is down is worse.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from .config import AppConfig, Secrets
from .models import MatchResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class Notifier:
    def __init__(self, config: AppConfig, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.notifications.telegram.enabled
            and self.secrets.telegram_bot_token
            and self.secrets.telegram_chat_id
        )

    def _fallback(self, text: str) -> bool:
        """Record an alert locally when Telegram is off.

        STWDO emails you about the application itself, but nothing emails you
        about the watchdog — so an undelivered alert must never vanish. Every
        message lands in the log and in data/alerts.log regardless.
        """
        plain = _strip_tags(text)
        logger.warning("ALERT (no Telegram): %s", plain.replace("\n", " | "))
        try:
            path = self.config.path("data/alerts.log")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"\n[{stamp}]\n{plain}\n")
        except OSError as exc:
            logger.error("Could not write the local alert log: %s", exc)
        return False

    def _post(self, method: str, data: dict, files: Optional[dict] = None) -> bool:
        if not self.enabled:
            return self._fallback(str(data.get("text") or data.get("caption") or method))
        url = f"{API_BASE}/bot{self.secrets.telegram_bot_token}/{method}"
        try:
            response = httpx.post(url, data=data, files=files, timeout=20.0)
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            # Never let a notification failure take down the watcher.
            logger.error("Telegram %s failed: %s", method, exc)
            return False

    def send(self, text: str) -> bool:
        return self._post(
            "sendMessage",
            {
                "chat_id": self.secrets.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def send_photo(self, image_path: Path, caption: str = "") -> bool:
        if not self.config.notifications.telegram.send_screenshots:
            return self.send(caption) if caption else False
        try:
            with open(image_path, "rb") as handle:
                return self._post(
                    "sendPhoto",
                    {
                        "chat_id": self.secrets.telegram_chat_id,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                    },
                    files={"photo": (image_path.name, handle, "image/png")},
                )
        except OSError as exc:
            logger.error("Cannot read screenshot %s: %s", image_path, exc)
            return self.send(caption) if caption else False

    # -- message templates -------------------------------------------------- #

    def offers_changed(self, new: list, changed: list) -> bool:
        if not new and not changed:
            return False
        lines = ["<b>STWDO listing update</b>"]
        if new:
            lines.append(f"\n<b>New ({len(new)}):</b>")
            lines.extend(_format_offer(offer) for offer in new)
        if changed:
            lines.append(f"\n<b>Changed ({len(changed)}):</b>")
            lines.extend(_format_offer(offer) for offer in changed)
        return self.send("\n".join(lines))

    def match_decision(self, result: MatchResult, will_apply: bool, reason: str = "") -> bool:
        """Report the top match. `reason` says why it is NOT being applied to."""
        offer = result.offer
        breakdown = " | ".join(
            f"{name} {value:.0f}" for name, value in result.breakdown.as_dict().items()
        )
        if will_apply:
            headline = "APPLYING NOW"
        else:
            headline = f"Best match — not applying ({reason})" if reason else "Best match"
        return self.send(
            f"<b>{headline}</b>\n"
            f"{_format_offer(offer)}\n"
            f"Score: <b>{result.score:.1f}</b> / {self.config.rules.auto_apply_min_score:.0f}\n"
            f"<code>{html.escape(breakdown)}</code>"
        )

    def application_submitted(self, result: MatchResult, evidence: Optional[Path]) -> bool:
        text = (
            "<b>APPLICATION SUBMITTED</b>\n"
            f"{_format_offer(result.offer)}\n"
            f"Score: {result.score:.1f}\n\n"
            f"{html.escape(self.config.application.document_reminder.strip())}\n\n"
            "<i>The watchdog is now locked and will not apply again "
            "(STWDO counts only one application per person).</i>"
        )
        if evidence is not None and evidence.is_file():
            return self.send_photo(evidence, text)
        return self.send(text)

    def error(self, title: str, detail: str) -> bool:
        return self.send(
            f"<b>⚠ {html.escape(title)}</b>\n<code>{html.escape(detail[:1500])}</code>"
        )


def _strip_tags(text: str) -> str:
    """Telegram HTML -> plain text, for the log and the local alert file."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _format_offer(offer) -> str:
    price = "?"
    if offer.price_min is not None:
        price = (
            f"{offer.price_min:.0f}€"
            if offer.price_max in (None, offer.price_min)
            else f"{offer.price_min:.0f}-{offer.price_max:.0f}€"
        )
    size = "?"
    if offer.size_min is not None:
        size = (
            f"{offer.size_min:.0f}m²"
            if offer.size_max in (None, offer.size_min)
            else f"{offer.size_min:.0f}-{offer.size_max:.0f}m²"
        )
    count = f"{offer.available_count}x " if offer.available_count is not None else ""
    return (
        f"• {count}{html.escape(offer.room_type.value)} — {price}, {size} — "
        f"{html.escape(offer.city)} {html.escape(offer.address)}\n"
        f"  <a href=\"{html.escape(offer.url)}\">{html.escape(offer.url)}</a>"
    )
