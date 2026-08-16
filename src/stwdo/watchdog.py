"""The polling loop.

Rooms are uploaded at any time on any day — Mon/Wed 10:00 is only the bulk drop
— so the baseline interval is tight (2 min by default) and runs around the clock.
That is affordable because a baseline poll is one HTTP GET, not a browser launch.

The loop is a plain `while` with a computed sleep rather than APScheduler jobs:
the interval changes with the clock (burst / quiet / baseline), and a single
sleep that recomputes each pass is easier to reason about than three overlapping
schedules.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .applicant import ApplicationError, Applicant
from .config import AppConfig, Profile, Secrets, load_selectors
from .fetcher import FetchError, Fetcher
from .models import LockState, MatchResult
from .notify import Notifier
from .rules import best_match, rank
from .scraper import ScrapeError, parse_offers
from .store import Store

logger = logging.getLogger(__name__)

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class WatchdogError(RuntimeError):
    """Unrecoverable watchdog configuration problem."""


@dataclass
class PollStats:
    polls: int = 0
    consecutive_failures: int = 0
    alerted_failure: bool = False
    applied: bool = False
    last_error: str = ""
    seen_fingerprints: set = field(default_factory=set)


def load_timezone(name: str) -> ZoneInfo:
    """Load a named timezone, failing with an actionable message.

    Windows ships no timezone database, so `zoneinfo` needs the `tzdata` package.
    Silently falling back to local time would be worse than failing: the whole
    schedule hangs off 10:00 *Berlin*, and a machine in another timezone would
    poll at the wrong hour without ever saying so.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise WatchdogError(
            f"Timezone {name!r} is unavailable on this system: {exc}\n"
            "On Windows this means the tzdata package is missing. Fix with:\n"
            "  pip install tzdata\n"
            "(or reinstall the project: pip install -e .)"
        ) from exc


def _parse_hhmm(value: str, fallback: dtime) -> dtime:
    try:
        hours, minutes = value.strip().split(":")
        return dtime(int(hours), int(minutes))
    except (ValueError, AttributeError):
        logger.warning("Invalid time %r in config, using %s", value, fallback)
        return fallback


def _within_window(now: datetime, start: dtime, end: dtime) -> bool:
    """Inclusive window; handles windows that wrap past midnight."""
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def current_interval(config: AppConfig, now: datetime) -> tuple[int, str]:
    """Pick the poll interval for this moment. Returns (seconds, mode)."""
    polling = config.polling

    burst = polling.burst
    if burst.enabled:
        weekdays = {_WEEKDAYS.get(day.strip().lower(), -1) for day in burst.weekdays}
        if now.weekday() in weekdays:
            start = _parse_hhmm(burst.start, dtime(9, 55))
            end = _parse_hhmm(burst.end, dtime(10, 45))
            if _within_window(now, start, end):
                return max(5, burst.interval_seconds), "burst"

    quiet = polling.quiet_hours
    if quiet.enabled:
        start = _parse_hhmm(quiet.start, dtime(2, 0))
        end = _parse_hhmm(quiet.end, dtime(6, 0))
        if _within_window(now, start, end):
            return max(30, quiet.interval_seconds), "quiet"

    return max(15, polling.baseline_interval_seconds), "baseline"


def apply_jitter(seconds: int, ratio: float, rng: Optional[random.Random] = None) -> float:
    """Spread requests out so the polling pattern is not a metronome."""
    if ratio <= 0:
        return float(seconds)
    generator = rng or random
    delta = seconds * min(ratio, 0.9)
    return max(5.0, seconds + generator.uniform(-delta, delta))


class Watchdog:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        notifier: Notifier,
        profile: Optional[Profile] = None,
        live: bool = False,
    ) -> None:
        self.config = config
        self.store = store
        self.notifier = notifier
        self.profile = profile
        self.live = live
        self.fetcher = Fetcher(config)
        self.stats = PollStats()
        self.tz = load_timezone(config.site.timezone)

    # -- one pass ----------------------------------------------------------- #

    def poll_once(self) -> list[MatchResult]:
        """Fetch, parse, persist, notify, and apply if a match clears the bar."""
        started = time.monotonic()
        fetched = None
        try:
            fetched = self.fetcher.fetch_offers()
            offers = parse_offers(fetched.html, self.config.site.base_url)
        except (FetchError, ScrapeError) as exc:
            if isinstance(exc, ScrapeError) and fetched is not None:
                # Keep the page that broke us; without it the failure is
                # impossible to diagnose after the fact.
                self._dump_failed_page(fetched.html)
            self._record_failure(str(exc))
            self.store.record_run(ok=False, duration_ms=int((time.monotonic() - started) * 1000),
                                  error=str(exc))
            return []

        self._record_success()
        duration_ms = int((time.monotonic() - started) * 1000)
        self.store.record_run(
            ok=True,
            offers_found=len(offers),
            duration_ms=duration_ms,
            transport=fetched.transport,
        )

        new, changed = self.store.upsert_offers(offers)
        if new or changed:
            logger.info("Listing changed: %d new, %d updated", len(new), len(changed))
            self.notifier.offers_changed(new, changed)

        results = rank(offers, self.config.rules)
        self._maybe_apply(offers, results)
        return results

    def _maybe_apply(self, offers: list, results: list[MatchResult]) -> None:
        match = best_match(offers, self.config.rules)
        if match is None:
            return

        # Only act on a given offer state once, so a standing good-enough offer
        # does not re-trigger every two minutes.
        fingerprint = match.offer.fingerprint()
        if fingerprint in self.stats.seen_fingerprints:
            return
        self.stats.seen_fingerprints.add(fingerprint)

        allowed, reason = self.store.can_apply()
        if not allowed:
            logger.info("Match found but the application lock is closed: %s", reason)
            self.notifier.match_decision(match, will_apply=False, reason="application lock closed")
            return

        if not self.live:
            logger.info("Match %s scored %.1f (dry run, not applying)",
                        match.offer.offer_id, match.score)
            self.notifier.match_decision(match, will_apply=False, reason="dry run")
            return

        self.notifier.match_decision(match, will_apply=True)
        self._submit(match)

    def _submit(self, match: MatchResult) -> None:
        if self.profile is None:
            self.notifier.error("Cannot apply", "profile.yaml is not loaded.")
            return
        try:
            applicant = Applicant(
                self.config, self.profile, load_selectors(self.config.root), self.store
            )
            outcome = applicant.apply(match, live=True)
        except ApplicationError as exc:
            logger.error("Application failed: %s", exc)
            self.notifier.error("Application failed", str(exc))
            return

        self.stats.applied = outcome.submitted
        if outcome.submitted:
            self.notifier.application_submitted(match, outcome.evidence_path)
            logger.info("Applied to offer %s — watchdog is now locked", outcome.offer_id)

    # -- failure accounting ------------------------------------------------- #

    def _dump_failed_page(self, html: str) -> None:
        try:
            directory = self.config.path(self.config.storage.evidence_dir)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(self.tz).strftime("%Y%m%d-%H%M%S")
            path = directory / f"{stamp}-unparseable.html"
            path.write_text(html, encoding="utf-8")
            logger.info("Saved the unparseable page to %s", path)
        except OSError as exc:
            logger.warning("Could not save the unparseable page: %s", exc)

    def _record_failure(self, message: str) -> None:
        self.stats.consecutive_failures += 1
        self.stats.last_error = message
        logger.error("Poll failed (%d in a row): %s", self.stats.consecutive_failures, message)
        threshold = self.config.polling.failure_alert_threshold
        if self.stats.consecutive_failures >= threshold and not self.stats.alerted_failure:
            self.notifier.error(
                f"{self.stats.consecutive_failures} consecutive poll failures", message
            )
            self.stats.alerted_failure = True

    def _record_success(self) -> None:
        if self.stats.consecutive_failures:
            logger.info("Recovered after %d failures", self.stats.consecutive_failures)
        self.stats.consecutive_failures = 0
        self.stats.alerted_failure = False

    def _sleep_seconds(self) -> tuple[float, str]:
        now = datetime.now(self.tz)
        interval, mode = current_interval(self.config, now)

        if self.stats.consecutive_failures:
            # Exponential backoff, capped, so a site outage does not turn into a
            # request flood.
            backoff = min(
                interval * (2 ** self.stats.consecutive_failures),
                self.config.polling.backoff_max_seconds,
            )
            return float(backoff), f"{mode}+backoff"

        return apply_jitter(interval, self.config.polling.jitter_ratio), mode

    # -- loop --------------------------------------------------------------- #

    def run(self, max_polls: Optional[int] = None, sleeper: Callable[[float], None] = time.sleep) -> PollStats:
        mode_note = "LIVE — will submit an application" if self.live else "dry run — will not submit"
        logger.info("Watchdog starting (%s)", mode_note)

        if self.live and self.store.lock_state() != LockState.NONE:
            allowed, reason = self.store.can_apply()
            if not allowed:
                logger.warning("Running in monitor-only mode: %s", reason)

        try:
            while max_polls is None or self.stats.polls < max_polls:
                self.stats.polls += 1
                try:
                    self.poll_once()
                except Exception as exc:  # a bug must not kill an overnight run
                    logger.exception("Unexpected error during poll")
                    self._record_failure(f"{type(exc).__name__}: {exc}")

                if self.stats.applied:
                    logger.info("Application submitted — the watchdog has nothing left to do.")
                    break

                if max_polls is not None and self.stats.polls >= max_polls:
                    break

                delay, mode = self._sleep_seconds()
                logger.debug("Sleeping %.0fs (%s)", delay, mode)
                sleeper(delay)
        except KeyboardInterrupt:
            logger.info("Interrupted — shutting down cleanly.")
        finally:
            self.fetcher.close()

        return self.stats
