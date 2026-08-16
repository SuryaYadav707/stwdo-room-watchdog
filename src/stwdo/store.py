"""SQLite persistence: seen offers, change snapshots, run log, application lock.

The application lock is the safety-critical part of this module. STWDO deletes
*all* of a person's applications if more than one is submitted, so the lock is
treated as a one-way latch: it is set before submitting, and it is never cleared
automatically — only by an explicit, confirmed human action.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .models import LockState, Offer, RoomType

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    offer_id        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    city            TEXT NOT NULL,
    address         TEXT NOT NULL,
    room_type       TEXT NOT NULL,
    price_min       REAL,
    price_max       REAL,
    size_min        REAL,
    size_max        REAL,
    available_count INTEGER,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id        TEXT NOT NULL,
    ts              TEXT NOT NULL,
    available_count INTEGER,
    price_min       REAL,
    price_max       REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_offer ON offer_snapshots(offer_id, ts);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    offers_found INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    transport    TEXT NOT NULL DEFAULT 'http',
    error        TEXT
);

-- One row per submission attempt ever started. Drives contact-pair rotation and
-- is never deleted, so resetting the lock does not reuse a spent contact pair.
CREATE TABLE IF NOT EXISTS application_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    contact  TEXT
);

CREATE TABLE IF NOT EXISTS application_lock (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    state         TEXT NOT NULL,
    offer_id      TEXT,
    ts            TEXT,
    evidence_path TEXT,
    note          TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thin SQLite wrapper. One connection, created lazily, closed explicitly."""

    def __init__(self, database_path: Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(str(self.path), timeout=10)
            except sqlite3.Error as exc:
                raise RuntimeError(f"Cannot open database {self.path}: {exc}") from exc
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO application_lock (id, state) VALUES (1, ?)",
                (LockState.NONE.value,),
            )
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        try:
            with conn:
                yield conn
        except sqlite3.Error as exc:
            raise RuntimeError(f"Database transaction failed: {exc}") from exc

    # -- offers ------------------------------------------------------------- #

    def upsert_offers(self, offers: list[Offer]) -> tuple[list[Offer], list[Offer]]:
        """Record the current listing state.

        Returns (new_offers, changed_offers). "Changed" means the fingerprint
        moved — an existing bucket gained units or repriced, which is just as
        actionable as a brand new bucket.
        """
        new: list[Offer] = []
        changed: list[Offer] = []
        now = _now()

        with self._transaction() as conn:
            for offer in offers:
                row = conn.execute(
                    "SELECT fingerprint FROM offers WHERE offer_id = ?", (offer.offer_id,)
                ).fetchone()
                fingerprint = offer.fingerprint()

                if row is None:
                    new.append(offer)
                    conn.execute(
                        """
                        INSERT INTO offers (
                            offer_id, url, title, city, address, room_type,
                            price_min, price_max, size_min, size_max,
                            available_count, first_seen, last_seen, fingerprint
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            offer.offer_id, offer.url, offer.title, offer.city, offer.address,
                            offer.room_type.value, offer.price_min, offer.price_max,
                            offer.size_min, offer.size_max, offer.available_count,
                            now, now, fingerprint,
                        ),
                    )
                else:
                    if row["fingerprint"] != fingerprint:
                        changed.append(offer)
                    conn.execute(
                        """
                        UPDATE offers SET
                            url=?, title=?, city=?, address=?, room_type=?,
                            price_min=?, price_max=?, size_min=?, size_max=?,
                            available_count=?, last_seen=?, fingerprint=?
                        WHERE offer_id=?
                        """,
                        (
                            offer.url, offer.title, offer.city, offer.address,
                            offer.room_type.value, offer.price_min, offer.price_max,
                            offer.size_min, offer.size_max, offer.available_count,
                            now, fingerprint, offer.offer_id,
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO offer_snapshots (offer_id, ts, available_count, price_min, price_max)
                    VALUES (?,?,?,?,?)
                    """,
                    (offer.offer_id, now, offer.available_count, offer.price_min, offer.price_max),
                )

        return new, changed

    def all_offers(self) -> list[Offer]:
        rows = self.conn.execute("SELECT * FROM offers ORDER BY offer_id").fetchall()
        return [self._row_to_offer(row) for row in rows]

    @staticmethod
    def _row_to_offer(row: sqlite3.Row) -> Offer:
        try:
            room_type = RoomType(row["room_type"])
        except ValueError:
            room_type = RoomType.UNKNOWN
        return Offer(
            offer_id=row["offer_id"],
            url=row["url"],
            title=row["title"],
            city=row["city"],
            address=row["address"],
            room_type=room_type,
            price_min=row["price_min"],
            price_max=row["price_max"],
            size_min=row["size_min"],
            size_max=row["size_max"],
            available_count=row["available_count"],
        )

    # -- run log ------------------------------------------------------------ #

    def record_run(
        self,
        ok: bool,
        offers_found: int = 0,
        duration_ms: int = 0,
        transport: str = "http",
        error: Optional[str] = None,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO runs (ts, ok, offers_found, duration_ms, transport, error) "
                "VALUES (?,?,?,?,?,?)",
                (_now(), 1 if ok else 0, offers_found, duration_ms, transport, error),
            )

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- application lock --------------------------------------------------- #

    def lock_status(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM application_lock WHERE id = 1").fetchone()
        if row is None:  # pragma: no cover - the row is seeded on connect
            raise RuntimeError("Application lock row is missing; database is corrupt.")
        return row

    def lock_state(self) -> LockState:
        try:
            return LockState(self.lock_status()["state"])
        except ValueError:
            # An unreadable state must fail closed, not open.
            return LockState.IN_FLIGHT

    def can_apply(self) -> tuple[bool, str]:
        """Whether an application may be submitted right now, and why not."""
        state = self.lock_state()
        if state == LockState.NONE:
            return True, ""
        if state == LockState.SUBMITTED:
            row = self.lock_status()
            return False, (
                f"An application was already submitted for offer {row['offer_id']} "
                f"at {row['ts']}. STWDO counts only one application per person."
            )
        row = self.lock_status()
        return False, (
            f"A previous application attempt for offer {row['offer_id']} started at "
            f"{row['ts']} and never confirmed. Check your email/inbox to see whether it "
            "went through, then run `stwdo unlock-application --force` if it did not."
        )

    def attempt_count(self) -> int:
        """How many submission attempts have ever been started.

        Used as the rotation index for contact pairs. Counts attempts, not
        successes, so a failed attempt still retires its contact pair.
        """
        row = self.conn.execute("SELECT COUNT(*) AS n FROM application_attempts").fetchone()
        return int(row["n"]) if row else 0

    def acquire_lock(self, offer_id: str, contact: str = "") -> None:
        """Latch the lock to IN_FLIGHT before submitting.

        Written first and committed immediately, so a crash mid-submit leaves an
        unresolved lock that blocks further attempts rather than a clean slate
        that would allow a second application.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE application_lock SET state=?, offer_id=?, ts=?, note=NULL "
                "WHERE id=1 AND state=?",
                (LockState.IN_FLIGHT.value, offer_id, _now(), LockState.NONE.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Refusing to apply: the application lock is not free. "
                    "Run `stwdo status` to see its state."
                )
            conn.execute(
                "INSERT INTO application_attempts (ts, offer_id, contact) VALUES (?,?,?)",
                (_now(), offer_id, contact),
            )

    def confirm_submission(self, offer_id: str, evidence_path: str, note: str = "") -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE application_lock SET state=?, offer_id=?, ts=?, evidence_path=?, note=? "
                "WHERE id=1",
                (LockState.SUBMITTED.value, offer_id, _now(), evidence_path, note),
            )

    def release_lock(self, note: str = "") -> None:
        """Reset to NONE. Only ever called from an explicitly confirmed CLI path."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE application_lock SET state=?, offer_id=NULL, ts=?, "
                "evidence_path=NULL, note=? WHERE id=1",
                (LockState.NONE.value, _now(), note),
            )
