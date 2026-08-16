"""Command line interface.

    stwdo probe                  # can we get past the mosparo gate?
    stwdo scan                   # what is currently listed?
    stwdo match                  # how do the listings score?
    stwdo recon --offer 6583     # dump the real application form
    stwdo apply --offer 6583     # dry run by default
    stwdo watch                  # the actual watchdog
    stwdo status                 # lock state and recent runs
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import (
    AppConfig,
    ConfigError,
    load_config,
    load_profile,
    load_secrets,
    load_selectors,
)
from .fetcher import FetchError, Fetcher
from .models import LockState
from .notify import Notifier
from .rules import rank
from .scraper import ScrapeError, parse_offers
from .store import Store
from .watchdog import Watchdog

app = typer.Typer(add_completion=False, help="Watchdog and auto-apply agent for STWDO rooms.")
console = Console()


def setup_logging(config: AppConfig, verbose: bool = False) -> None:
    """Console plus rotating-free file log.

    The file handler forces utf-8: on Windows the default is cp1252, which throws
    UnicodeEncodeError the first time a German address (ü, ß, m²) is logged.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        log_path = config.path(config.storage.log_path)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:
        console.print(f"[yellow]Could not open log file: {exc}[/yellow]")

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _load(verbose: bool = False) -> AppConfig:
    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    setup_logging(config, verbose)
    return config


def _store(config: AppConfig) -> Store:
    return Store(config.path(config.storage.database_path))


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


@app.command()
def probe(
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check whether the mosparo gate can be passed from this machine."""
    config = _load(verbose)
    if headed:
        config.browser.headless = False

    from .browser import BrowserError, browser_page
    from .gate import GateError, unlock

    try:
        with browser_page(config) as page:
            elapsed = unlock(page, config)
            html = page.content()
    except (BrowserError, GateError) as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]PASS[/green] gate opened in {elapsed:.1f}s")
    try:
        offers = parse_offers(html, config.site.base_url)
        console.print(f"Parsed [bold]{len(offers)}[/bold] offers from the unlocked page.")
    except ScrapeError as exc:
        console.print(f"[yellow]Gate opened but parsing failed:[/yellow] {exc}")


# --------------------------------------------------------------------------- #
# scan / match
# --------------------------------------------------------------------------- #


def _fetch_offers(config: AppConfig):
    fetcher = Fetcher(config)
    try:
        fetched = fetcher.fetch_offers()
        return parse_offers(fetched.html, config.site.base_url), fetched
    except (FetchError, ScrapeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        fetcher.close()


@app.command()
def scan(
    save: bool = typer.Option(True, "--save/--no-save", help="Record results in the database."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch and print the current listings."""
    config = _load(verbose)
    offers, fetched = _fetch_offers(config)

    table = Table(title=f"STWDO listings ({fetched.transport}, {fetched.duration_ms} ms)")
    for column in ("ID", "City", "Address", "Type", "Free", "Rent", "Size"):
        table.add_column(column)
    for offer in offers:
        table.add_row(
            offer.offer_id,
            offer.city,
            offer.address,
            offer.room_type.value,
            str(offer.available_count or "?"),
            f"{offer.price_min or 0:.0f}-{offer.price_max or 0:.0f}€",
            f"{offer.size_min or 0:.0f}-{offer.size_max or 0:.0f}m²",
        )
    console.print(table)

    if save:
        with _store(config) as store:
            new, changed = store.upsert_offers(offers)
        console.print(f"{len(new)} new, {len(changed)} changed since the last scan.")


@app.command()
def match(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Score the current listings against the rules and show the breakdown."""
    config = _load(verbose)
    offers, _ = _fetch_offers(config)
    results = rank(offers, config.rules)
    threshold = config.rules.auto_apply_min_score

    table = Table(title=f"Ranked matches (auto-apply at >= {threshold:.0f})")
    for column in ("Score", "ID", "Type", "Rent", "Size", "City", "Rent·Type·Size·Loc·Avail", "Verdict"):
        table.add_column(column)

    for result in results:
        offer = result.offer
        breakdown = "·".join(f"{v:.0f}" for v in result.breakdown.as_dict().values())
        if result.rejected:
            verdict = f"[dim]rejected: {result.rejections[0]}[/dim]"
        elif result.score >= threshold:
            verdict = "[bold green]WOULD APPLY[/bold green]"
        else:
            verdict = "[yellow]below threshold[/yellow]"
        table.add_row(
            f"{result.score:.1f}",
            offer.offer_id,
            offer.room_type.value,
            f"{offer.price_min or 0:.0f}€",
            f"{offer.size_max or 0:.0f}m²",
            offer.city,
            breakdown,
            verdict,
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# recon
# --------------------------------------------------------------------------- #


@app.command()
def recon(
    offer: str = typer.Option(..., "--offer", help="Offer id, e.g. 6583."),
    headed: bool = typer.Option(True, "--headed/--headless", help="Watch it happen."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Dump the unlocked application form so selectors.yaml can be filled in.

    The form is invisible to anyone outside the gate, so this has to run once
    before `apply` will do anything.
    """
    config = _load(verbose)
    config.browser.headless = not headed

    from .applicant import ApplicationError, Applicant

    try:
        profile = load_profile(config.root)
    except ConfigError:
        from .config import Profile

        profile = Profile()  # recon does not need real data

    with _store(config) as store:
        applicant = Applicant(config, profile, load_selectors(config.root), store)
        output_dir = config.root / "tests" / "fixtures"
        try:
            inventory = applicant.recon(offer, output_dir)
        except ApplicationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    report = config.path("data/recon_inventory.json")
    report.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")

    table = Table(title=f"Application form controls (offer {offer})")
    for column in ("tag", "type", "id", "required", "label / text"):
        table.add_column(column)
    for control in inventory.get("controls", []):
        table.add_row(
            str(control.get("tag", "")),
            str(control.get("type", "")),
            str(control.get("id", ""))[:44],
            "yes" if control.get("required") else "",
            (str(control.get("label", "")) or str(control.get("text", "")))[:50],
        )
    console.print(table)

    selects = inventory.get("selects", [])
    if selects:
        console.print("Dropdowns (Angular Material): " + ", ".join(s.get("id", "") for s in selects))
    console.print(f"Form iframe: [dim]{inventory.get('frame_url', '')}[/dim]")
    console.print(f"\nFull inventory: [bold]{report}[/bold]")
    console.print(f"HTML + screenshot: [bold]{output_dir}[/bold] and data/evidence/")
    console.print(
        "\nNext: check these against "
        "[bold]selectors.yaml -> application_form.fields[/bold]. Angular renumbers "
        "mat-input ids on every load, so prefer [italic]label:[/italic] selectors "
        "or id substrings."
    )


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


@app.command()
def apply(
    offer: str = typer.Option(..., "--offer", help="Offer id to apply to."),
    live: bool = typer.Option(False, "--live", help="Actually submit. Requires config live_enabled."),
    headed: bool = typer.Option(False, "--headed"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fill the application form. Dry run unless --live is given."""
    config = _load(verbose)
    config.browser.headless = not headed

    from .applicant import ApplicationError, Applicant
    from .models import MatchResult, ScoreBreakdown

    try:
        profile = load_profile(config.root)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    offers, _ = _fetch_offers(config)
    target = next((o for o in offers if o.offer_id == offer), None)
    if target is None:
        console.print(f"[red]Offer {offer} is not in the current listing.[/red]")
        raise typer.Exit(code=1)

    if live:
        console.print(
            "\n[bold red]LIVE APPLICATION[/bold red]\n"
            "STWDO counts only ONE application per person — submitting more than one "
            "gets them all deleted.\n"
            f"About to apply to: [bold]{target.title}[/bold] ({target.url})\n"
        )
        if not typer.confirm("Submit this application now?"):
            console.print("Aborted.")
            raise typer.Exit(code=0)

    result = MatchResult(offer=target, passed_filters=True, score=0.0, breakdown=ScoreBreakdown())

    with _store(config) as store:
        applicant = Applicant(config, profile, load_selectors(config.root), store)
        try:
            outcome = applicant.apply(result, live=live)
        except ApplicationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    console.print(f"\n{outcome.message}")
    console.print(f"Filled: {', '.join(outcome.filled_fields) or 'nothing'}")
    if outcome.missing_fields:
        console.print(f"[yellow]Unfilled: {', '.join(outcome.missing_fields)}[/yellow]")
    if outcome.evidence_path:
        console.print(f"Screenshot: [bold]{outcome.evidence_path}[/bold]")


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


@app.command()
def watch(
    live: bool = typer.Option(False, "--live", help="Allow the watchdog to submit an application."),
    max_polls: Optional[int] = typer.Option(None, "--max-polls", help="Stop after N polls."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the polling loop. Dry run unless --live is given."""
    config = _load(verbose)
    secrets = load_secrets(config.root)
    notifier = Notifier(config, secrets)

    profile = None
    if live:
        try:
            profile = load_profile(config.root)
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2)
        if not config.application.live_enabled:
            console.print(
                "[red]--live requires application.live_enabled: true in config.yaml.[/red]"
            )
            raise typer.Exit(code=2)

    if not notifier.enabled:
        console.print("[yellow]Telegram is not configured — alerts will only go to the log.[/yellow]")

    with _store(config) as store:
        dog = Watchdog(config, store, notifier, profile=profile, live=live)
        stats = dog.run(max_polls=max_polls)

    console.print(
        f"\nStopped after {stats.polls} polls. "
        f"Applied: {stats.applied}. Last error: {stats.last_error or 'none'}"
    )


# --------------------------------------------------------------------------- #
# status / lock management
# --------------------------------------------------------------------------- #


@app.command()
def status(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Show the application lock and the last few polls."""
    config = _load(verbose)
    with _store(config) as store:
        lock = store.lock_status()
        state = store.lock_state()
        colour = {"none": "green", "in_flight": "red", "submitted": "yellow"}.get(state.value, "white")
        console.print(f"Application lock: [{colour}]{state.value}[/{colour}]")
        if lock["offer_id"]:
            console.print(f"  offer {lock['offer_id']} at {lock['ts']}")
        if lock["note"]:
            console.print(f"  note: {lock['note']}")
        if lock["evidence_path"]:
            console.print(f"  evidence: {lock['evidence_path']}")

        allowed, reason = store.can_apply()
        console.print(f"Can apply: {'yes' if allowed else 'no'}")
        if reason:
            console.print(f"  [yellow]{reason}[/yellow]")

        table = Table(title="Recent polls")
        for column in ("when", "ok", "offers", "ms", "via", "error"):
            table.add_column(column)
        for row in store.recent_runs(10):
            table.add_row(
                row["ts"],
                "ok" if row["ok"] else "FAIL",
                str(row["offers_found"]),
                str(row["duration_ms"]),
                row["transport"],
                (row["error"] or "")[:60],
            )
        console.print(table)


@app.command("unlock-application")
def unlock_application(
    force: bool = typer.Option(False, "--force", help="Required."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Reset the application lock. Only after confirming no application exists."""
    config = _load(verbose)
    if not force:
        console.print("[red]Refusing without --force.[/red]")
        raise typer.Exit(code=2)

    with _store(config) as store:
        state = store.lock_state()
        if state == LockState.NONE:
            console.print("Lock is already free.")
            raise typer.Exit(code=0)

        console.print(
            f"\n[bold red]The lock is {state.value}.[/bold red]\n"
            "Resetting it lets the watchdog submit ANOTHER application. If STWDO already "
            "has one from you, a second submission deletes them all.\n"
            "Check your email and the STWDO confirmation before continuing.\n"
        )
        if not typer.confirm("Have you confirmed that NO application is currently registered?"):
            console.print("Aborted — lock left in place.")
            raise typer.Exit(code=0)

        store.release_lock(note="manually reset via CLI")
        console.print("[green]Lock reset.[/green]")


@app.command("check-config")
def check_config(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Validate config.yaml, selectors.yaml, profile.yaml, and .env."""
    config = _load(verbose)
    console.print(f"[green]ok[/green] config.yaml (root: {config.root})")

    selectors = load_selectors(config.root)
    fields = ((selectors.get("application_form") or {}).get("fields")) or {}
    if fields:
        console.print(f"[green]ok[/green] selectors.yaml ({len(fields)} form fields mapped)")
    else:
        console.print("[yellow]todo[/yellow] selectors.yaml has no form fields — run `stwdo recon`")

    try:
        profile = load_profile(config.root)
        values = profile.flat()

        # Contact pairs override the flat email/mobile fields, so resolve the
        # pair this run would actually use before reporting anything as missing.
        with _store(config) as store:
            contact = profile.contact_for_attempt(store.attempt_count())
            attempts = store.attempt_count()
        if contact is not None:
            values["email"] = contact.email or values.get("email", "")
            values["mobile"] = contact.mobile or values.get("mobile", "")

        # `phone` is the optional landline field; blank is a valid choice.
        empty = [key for key, value in values.items() if not value and key not in ("phone", "notes")]
        console.print(f"[green]ok[/green] profile.yaml ({len(empty)} required fields empty)")
        if empty:
            console.print(f"  [yellow]empty: {', '.join(empty)}[/yellow]")
        if contact is not None:
            console.print(
                f"  contact pair {attempts + 1} of {len(profile.contacts)}: "
                f"{contact.email} / {contact.mobile}"
            )
    except ConfigError as exc:
        console.print(f"[yellow]todo[/yellow] {exc}")

    secrets = load_secrets(config.root)
    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        console.print("[green]ok[/green] Telegram credentials present")
    else:
        console.print("[yellow]todo[/yellow] Telegram not configured in .env")

    console.print(
        f"\nlive_enabled: {'[red]TRUE[/red]' if config.application.live_enabled else 'false'}"
    )


@app.command("test-telegram")
def test_telegram(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Send a test message to confirm the bot works."""
    config = _load(verbose)
    notifier = Notifier(config, load_secrets(config.root))
    if not notifier.enabled:
        console.print("[red]Telegram is not configured. Fill in .env first.[/red]")
        raise typer.Exit(code=2)
    if notifier.send("<b>STWDO watchdog</b>\nTelegram is wired up correctly."):
        console.print("[green]Sent.[/green]")
    else:
        console.print("[red]Failed — see the log.[/red]")
        raise typer.Exit(code=1)


def main() -> None:  # pragma: no cover - console entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
