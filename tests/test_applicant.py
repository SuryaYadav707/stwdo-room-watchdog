"""Applicant tests that need no browser.

The browser-driven parts are exercised by `stwdo apply --dry-run` against the
live site; what is pinned here is the logic that decides *what* gets typed and
*whether* a submission is allowed at all.
"""

from __future__ import annotations

import pytest

from stwdo.applicant import ApplicationError, Applicant, format_date_for_site
from stwdo.config import AppConfig, ContactPair, Profile, load_selectors
from stwdo.models import MatchResult, Offer, RoomType, ScoreBreakdown
from stwdo.store import Store


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2001-04-17", "17.04.2001"),   # the German form wants DD.MM.YYYY
        ("2026-10-01", "01.10.2026"),
        ("17.04.2001", "17.04.2001"),   # already converted; left alone
        ("", ""),
        ("not a date", "not a date"),
    ],
)
def test_format_date_for_site(iso, expected):
    assert format_date_for_site(iso) == expected


def test_format_date_for_site_english():
    assert format_date_for_site("2001-04-17", language="en") == "2001-04-17"


# --------------------------------------------------------------------------- #
# profile flattening
# --------------------------------------------------------------------------- #


def test_profile_flattens_booleans_to_strings():
    profile = Profile()
    profile.consents.confirm_privacy_policy = True
    flat = profile.flat()
    assert flat["confirm_privacy_policy"] == "true"
    assert flat["confirm_enrollment_certificate"] == "false"


def test_profile_exposes_every_form_field():
    """Each selector in selectors.yaml must have a matching profile key."""
    selectors = load_selectors()
    fields = (selectors.get("application_form") or {}).get("fields") or {}
    flat = Profile().flat()
    missing = [key for key in fields if key not in flat]
    assert missing == [], f"selectors.yaml references unknown profile keys: {missing}"


def test_date_is_converted_before_filling(tmp_path):
    config = AppConfig()
    config.root = tmp_path
    profile = Profile()
    profile.personal.date_of_birth = "2001-04-17"
    with Store(tmp_path / "a.sqlite3") as store:
        applicant = Applicant(config, profile, load_selectors(), store)
        assert applicant._profile_values()["date_of_birth"] == "17.04.2001"


# --------------------------------------------------------------------------- #
# submission guards
# --------------------------------------------------------------------------- #


def _match() -> MatchResult:
    offer = Offer(
        offer_id="6583",
        url="https://www.stwdo.de/freie-zimmer/6583",
        title="9 verfügbare 3er-WGs",
        city="Dortmund",
        address="Dortmund 1",
        room_type=RoomType.SHARED_3,
        price_min=359.0,
        price_max=407.0,
        size_min=20.0,
        size_max=37.0,
        available_count=9,
    )
    return MatchResult(offer=offer, passed_filters=True, score=90.0, breakdown=ScoreBreakdown())


def test_live_requires_config_gate_as_well_as_flag(tmp_path):
    """--live alone must not be enough to submit."""
    config = AppConfig()
    config.root = tmp_path
    config.application.live_enabled = False
    with Store(tmp_path / "b.sqlite3") as store:
        applicant = Applicant(config, Profile(), load_selectors(), store)
        with pytest.raises(ApplicationError, match="live_enabled"):
            applicant.apply(_match(), live=True)


def test_live_refused_when_lock_is_already_spent(tmp_path):
    config = AppConfig()
    config.root = tmp_path
    config.application.live_enabled = True
    with Store(tmp_path / "c.sqlite3") as store:
        store.acquire_lock("1111")
        store.confirm_submission("1111", "")
        applicant = Applicant(config, Profile(), load_selectors(), store)
        with pytest.raises(ApplicationError, match="already submitted"):
            applicant.apply(_match(), live=True)


def test_empty_selector_map_is_refused(tmp_path):
    config = AppConfig()
    config.root = tmp_path
    with Store(tmp_path / "d.sqlite3") as store:
        applicant = Applicant(config, Profile(), {"application_form": {"fields": {}}}, store)
        with pytest.raises(ApplicationError, match="recon"):
            applicant.apply(_match(), live=False)


# --------------------------------------------------------------------------- #
# contact-pair rotation
# --------------------------------------------------------------------------- #


def _profile_with_pairs() -> Profile:
    profile = Profile()
    profile.contacts = [
        ContactPair(email="one@example.com", mobile="+91 1111111111"),
        ContactPair(email="two@example.com", mobile="+91 2222222222"),
    ]
    return profile


def test_first_attempt_uses_the_first_pair():
    profile = _profile_with_pairs()
    pair = profile.contact_for_attempt(0)
    assert (pair.email, pair.mobile) == ("one@example.com", "+91 1111111111")


def test_second_attempt_uses_the_second_pair():
    assert _profile_with_pairs().contact_for_attempt(1).email == "two@example.com"


def test_rotation_wraps_around():
    assert _profile_with_pairs().contact_for_attempt(2).email == "one@example.com"


def test_no_contacts_configured_returns_none():
    assert Profile().contact_for_attempt(0) is None


def test_blank_pairs_are_skipped():
    profile = Profile()
    profile.contacts = [ContactPair(), ContactPair(email="real@example.com")]
    assert profile.contact_for_attempt(0).email == "real@example.com"


def test_email_and_mobile_come_from_the_active_pair(tmp_path):
    config = AppConfig()
    config.root = tmp_path
    with Store(tmp_path / "e.sqlite3") as store:
        applicant = Applicant(config, _profile_with_pairs(), load_selectors(), store)
        values = applicant._profile_values()
        assert values["email"] == "one@example.com"
        assert values["mobile"] == "+91 1111111111"


def test_rotation_advances_only_after_an_attempt_is_started(tmp_path):
    """Polling, dry runs and failed matches must not burn a contact pair."""
    config = AppConfig()
    config.root = tmp_path
    with Store(tmp_path / "f.sqlite3") as store:
        applicant = Applicant(config, _profile_with_pairs(), load_selectors(), store)
        assert applicant.active_contact().email == "one@example.com"

        store.acquire_lock("6583", contact="one@example.com")
        store.confirm_submission("6583", "")
        # Still pair one until a human authorises another attempt.
        assert store.attempt_count() == 1
        store.release_lock(note="previous application confirmed dead")
        assert applicant.active_contact().email == "two@example.com"


def test_a_spent_pair_is_not_reused_after_a_lock_reset(tmp_path):
    config = AppConfig()
    config.root = tmp_path
    with Store(tmp_path / "g.sqlite3") as store:
        store.acquire_lock("1", contact="one@example.com")
        store.release_lock()
        applicant = Applicant(config, _profile_with_pairs(), load_selectors(), store)
        assert applicant.active_contact().email == "two@example.com"


def test_rotation_cannot_create_a_second_live_application(tmp_path):
    """Different email, same person — the lock is what counts."""
    config = AppConfig()
    config.root = tmp_path
    config.application.live_enabled = True
    with Store(tmp_path / "h.sqlite3") as store:
        store.acquire_lock("6583", contact="one@example.com")
        store.confirm_submission("6583", "")
        applicant = Applicant(config, _profile_with_pairs(), load_selectors(), store)
        with pytest.raises(ApplicationError, match="already submitted"):
            applicant.apply(_match(), live=True)


def test_rejected_email_falls_back_to_the_other_pair(tmp_path):
    """A retry of a failed FILL — nothing has been submitted at this point."""
    config = AppConfig()
    config.root = tmp_path

    class _FakeFrame:
        def __init__(self):
            self.filled: dict[str, str] = {}

        def eval_on_selector(self, selector, script, arg=None):
            return selector == "#email"   # the form is showing an error

        def locator(self, selector):
            frame = self

            class _Loc:
                first = None

                def fill(self, value, timeout=None):
                    frame.filled[selector] = value

            loc = _Loc()
            loc.first = loc
            return loc

    with Store(tmp_path / "i.sqlite3") as store:
        applicant = Applicant(config, _profile_with_pairs(), load_selectors(), store)
        specs = {"email": {"selector": "#email", "kind": "text"}}
        values = applicant._profile_values()
        filled = {"email": values["email"]}
        frame = _FakeFrame()

        assert applicant._retry_email_if_rejected(frame, specs, values, filled) is True
        assert filled["email"] == "two@example.com"
        assert frame.filled["#email"] == "two@example.com"
