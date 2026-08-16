# STWDO Room Watchdog

Watches [stwdo.de](https://www.stwdo.de/wohnen/aktuelle-wohnangebote) for free student rooms in Dortmund, scores every listing against your constraints with a deterministic rules engine, and submits the application form for the best match — before a human could finish typing.

The room goes to *"die erste Person mit vollständigen Unterlagen"*. Speed is the entire game.

> **The one rule that shapes everything:** STWDO accepts **one application per person**. Submitting more than one gets **all of them deleted**. This tool therefore applies exactly once, latches a lock, and then refuses to apply again until you explicitly reset it.

---

## How the site actually works

Established by probing the live site, not by guesswork:

| | |
|---|---|
| **Publication windows** | Listings are visible Mon 10:00 → Tue 12:00 and Wed 10:00 → Thu 12:00 (Berlin). Individual rooms can be added at any time, so the watchdog polls around the clock. |
| **Site gate** | Every `/wohnen` and `/freie-zimmer` URL sits behind [mosparo](https://mosparo.io) (self-hosted at `mosparo.stwdo.de`). It renders **inline, no iframe**, as a consent checkbox. Ticking it validates via `check-form-data`, then the page submits itself and sets a short-lived cookie. |
| **The gotcha** | mosparo fetches a submit token asynchronously. Clicking before it lands fails with *"No submit token available"*. Waiting for that token is the difference between a working unlock and a permanent timeout. |
| **Listings** | Category buckets, not single rooms — "9 verfügbare 3er-WGs", with a price range, a size range and a count. An existing bucket can gain units, so change detection watches counts and prices, not just new ids. |
| **Applying** | Three stages: site gate → a *second* mosparo box plus a Wohnungshelden data-processing consent → a **Wohnungshelden form in an iframe** (`app.wohnungshelden.de`), built with Angular Material. |
| **Documents** | The Immatrikulationsbescheinigung is requested **after** selection. This tool never uploads anything — it alerts you to do it, and STWDO emails you directly. |

---

## Setup (Windows)

```powershell
git clone <this repo>
cd stwdo-room-watchdog

py -m venv .venv
.venv\Scripts\activate
pip install -e .
playwright install chromium

copy profile.example.yaml profile.yaml   # then fill it in
copy .env.example .env                   # only needed if you enable Telegram

stwdo check-config
```

`profile.yaml`, `.env`, `data/` and `docs/` are gitignored — none of your personal data is ever committed.

On Linux/macOS the only difference is `python3 -m venv .venv && source .venv/bin/activate`.

### Alerts

Telegram is **off by default** (`notifications.telegram.enabled: false`). STWDO emails you about the application itself — the confirmation and the document request go to the address in the form.

Nothing emails you about the *watchdog*, though. With Telegram off, every alert — applied, gate broken, page unparseable, repeated failures — is written to **`data/alerts.log`** and to `data/watchdog.log`, and nothing is silently dropped. Check in with `stwdo status`.

To turn Telegram on:

1. Message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token into `.env`.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy `chat.id` into `.env`.
4. Set `enabled: true` in `config.yaml`, then `stwdo test-telegram`.

---

## Usage

| Command | What it does |
|---|---|
| `stwdo check-config` | Validates config, selectors, profile, credentials. |
| `stwdo probe` | Tests the mosparo gate. Prints PASS and the time it took. |
| `stwdo scan` | Fetches and prints the current listings. |
| `stwdo match` | Scores every listing and shows which one would be applied to. |
| `stwdo recon --offer 6583` | Dumps the real application form and its controls. |
| `stwdo apply --offer 6583` | **Dry run**: fills the form, screenshots it, submits nothing. |
| `stwdo watch` | The watchdog. Dry run unless `--live`. |
| `stwdo status` | Lock state and recent polls. |
| `stwdo unlock-application --force` | Resets the lock after you have verified no application exists. |

Add `--verbose` for debug logging, `--headed` to `probe`/`apply` to watch the browser work.

### Going live

Three independent gates must all be open before anything is submitted:

1. `application.live_enabled: true` in `config.yaml`
2. `--live` on the command line
3. the application lock in the database is free

Do this first, in order:

```powershell
stwdo probe                       # gate works from this machine
stwdo match                       # the scores look right to you
stwdo apply --offer <id>          # dry run — then OPEN THE SCREENSHOT and read every field
stwdo watch --max-polls 5         # loop works, alerts land in data/alerts.log
```

Then run one **full dry-run window** across a real Mon or Wed 10:00 and confirm it detects the drop and reports what it *would* have applied to. Only then:

```powershell
# after setting application.live_enabled: true in config.yaml
stwdo watch --live
```

Run it permanently with `powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1` (add `-Live` once you trust it), which registers a Windows scheduled task that starts at logon and restarts on failure.

---

## Contact rotation

The form has one email field and one required mobile field. `profile.yaml -> contacts` holds them as pairs:

```yaml
contacts:
  - email: "first@example.com"
    mobile: "+91 1111111111"
  - email: "second@example.com"
    mobile: "+91 2222222222"
```

Attempt 1 uses the first pair, attempt 2 the second, then it wraps. `stwdo check-config` prints which pair is active.

An "attempt" advances only when a submission is actually *started* — polling, dry runs and near-misses never burn a pair. Since the lock permits exactly one submission until you reset it by hand, rotation moves to the next pair only after you have confirmed the previous application is dead.

**It is not a way to hold two applications at once.** STWDO counts one application per *person*, not per email address; a second live one gets both deleted. The lock enforces that regardless of which address is in the field.

If the form rejects the active email while filling — before anything is sent — the other pair's address is tried once within the same attempt.

## Tuning the match

Everything lives in `config.yaml` under `rules`:

```yaml
cities: ["Dortmund", "Hagen", "Iserlohn", "Soest"]
max_rent: 400.0            # compared against the bucket's CHEAPEST room
min_size: 15.0             # compared against the bucket's LARGEST room
auto_apply_min_score: 35.0 # a backstop, not the gate — see below
weights:                   # must sum to 1.0
  room_type: 0.45          # a self-contained apartment beats a room in a WG
  rent: 0.35
  size: 0.15
  availability: 0.05
  location: 0.0            # no city preference
type_preference: [single_apartment, shared_2, shared_3, shared_4]
```

Every listing is a single **room**. `single_apartment` is self-contained (own kitchen and bath); `shared_3` means your own private room in a three-person flat. Both are acceptable, so the hard filters above are the real gate and the threshold is only a guard against junk data — preference is expressed by ranking. Whenever an apartment and a WG room are both listed, the apartment scores higher and wins. Raise the threshold to 70 to make it apartment-only.

`stwdo match` prints the per-component breakdown for every listing, so tune against real numbers rather than guessing.

**Location currently carries weight 0.0** — all four cities count equally. To favour one campus, give the weight a non-zero share and fill in either table:

```yaml
walking_minutes:        # per address, most specific
  "Dortmund 1": 8
city_minutes:           # fallback for addresses not listed above
  Dortmund: 15
  Iserlohn: 50
```

Anything unparseable — a price the regex cannot read, an unrecognisable room type — is **rejected**, never optimistically accepted. An offer we cannot read is an offer worth not spending the one application on.

---

## Safety design

- **Dry run is the default** everywhere. Submission needs two config gates plus a free lock.
- **The lock is latched before the submit click.** A crash mid-submit leaves it `in_flight`, which blocks all further attempts and tells you to check your email. It never auto-recovers — auto-recovery is exactly how you would end up with two applications.
- **Incomplete applications are refused.** If any required field could not be filled, the run aborts rather than sending a half-form.
- **Every submission is evidenced**: full-page screenshot plus the response HTML in `data/evidence/`.
- **The watchdog survives its own bugs.** A failing poll is logged, backed off, and alerted after three in a row; it never kills the loop.
- **Polling is polite**: one HTTP GET per poll (not a browser launch), jittered intervals, browser only for unlocking and applying.

---

## Architecture

```
fetcher.py    hybrid transport — httpx with the mosparo cookie, browser only when the gate demands it
gate.py       mosparo unlock (token wait -> consent click -> page self-submits)
scraper.py    listing HTML -> Offer objects
parsing.py    German and English number formats, room types, locations
rules.py      hard filters + weighted score. Pure functions, no I/O, fully tested
store.py      SQLite: offers, snapshots, run log, application lock
applicant.py  the three-stage apply flow, including the Angular Material form
notify.py     Telegram
watchdog.py   the polling loop and its interval policy
```

The rules engine is deliberately **not** an LLM: the decision is cheap to express as arithmetic, must be reproducible, and is spending something you only get once. `config.yaml -> llm` is reserved for advisory use (proposing new selectors when the site changes) and is off by default.

### When the site changes

Every site-coupled selector lives in `selectors.yaml`. If a scan suddenly parses zero offers, the scraper raises rather than reporting "no rooms" — run `stwdo recon --offer <id>`, compare against the dumped HTML in `tests/fixtures/`, and edit `selectors.yaml`. No Python changes needed for the common cases.

Note that Angular renumbers `mat-input-N` ids on every load, so form selectors use `label:Vorname` or id substrings (`input[id*='date_of_birth']`) rather than exact ids.

---

## Tests

```powershell
pytest -q
```

151 tests, no network required. `tests/fixtures/` holds real captures: the mosparo gate page, an unlocked listing page, and the application form.

---

## GitHub Actions

`.github/workflows/watch.yml` is shipped but **unverified** — see the caveats in its header, chiefly that GitHub's shared Azure egress IPs may trip mosparo's per-IP lockout, and that the application lock has to be committed to a `state` branch to survive between stateless runs. Use a private repository: your profile data goes into repository secrets. The primary deployment is your own machine.
