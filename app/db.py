"""SQLite persistence for transfer facts, retraction tombstones and the daily report.

Invariants
----------
* A transfer row stores the *latest* revision only.  ``status`` is either
  ``active`` (a reported fact) or ``retracted`` (a tombstone that still carries
  the retraction revision so late, older revisions cannot resurrect the record).
* ``content_hash`` makes same-id/same-revision replays idempotent.  A same
  revision with different content is a batch-level conflict.
* Every ingest batch runs inside one ``BEGIN IMMEDIATE`` transaction.  Fact
  changes and the corresponding daily-report deltas therefore commit (or roll
  back) together, survive process interruption, and are safe to replay.
* The daily report only stores affected ``(airport, departure UTC date)``
  groups; a correction deducts the old group contribution and adds the new one
  rather than recomputing the whole table.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import IngestEntry

_DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent / "data" / "aircargo.db"
)

_initialized_paths: set[str] = set()
_init_lock = threading.Lock()
# SQLite itself also serialises writers across processes; this lock only avoids
# needless busy-waiting between request threads in one process.
_write_lock = threading.Lock()
# A shared-cache in-memory database disappears when its last connection closes,
# so keep one connection alive when tests select DATABASE_PATH=:memory:.
_memory_keepalive: dict[str, sqlite3.Connection] = {}


class BatchConflict(Exception):
    """A same-revision/different-content conflict rejects the whole batch."""

    def __init__(self, conflicts: list[dict[str, Any]]):
        super().__init__("same revision with different content")
        self.conflicts = conflicts


def db_path() -> str:
    """Resolve the SQLite path at call time so tests can isolate via env."""
    return os.environ.get("DATABASE_PATH") or os.environ.get("DB_PATH") or _DEFAULT_DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    id              TEXT PRIMARY KEY,
    revision        INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active', 'retracted')),
    airport         TEXT,
    arrival_epoch   INTEGER,
    departure_epoch INTEGER,
    content_hash    TEXT NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_reports (
    airport          TEXT NOT NULL,
    departure_date   TEXT NOT NULL,          -- YYYY-MM-DD in UTC
    transfer_count   INTEGER NOT NULL,
    dwell_seconds    INTEGER NOT NULL,
    PRIMARY KEY (airport, departure_date)
);
"""


def _sqlite_target(path: str) -> tuple[str, bool]:
    """Map a logical path to a sqlite3 target and whether it is a URI."""
    if path == ":memory:":
        # Every connection to a plain :memory: database gets its own store.  A
        # shared-cache URI plus a keepalive connection makes all request
        # connections see the same in-memory database.
        return "file:aircargo_inmemory?mode=memory&cache=shared", True
    return path, path.startswith("file:")


def _ensure_schema_for_path(path: str) -> None:
    if path != ":memory:" and not path.startswith("file:"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    target, is_uri = _sqlite_target(path)
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(30):
        conn = sqlite3.connect(target, timeout=30, isolation_level=None, uri=is_uri)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            # WAL keeps readers on a stable snapshot while a writer is committing;
            # FULL synchronous makes a committed batch durable across power loss.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA user_version=1")
        except sqlite3.OperationalError as exc:
            # Several processes may initialise the same fresh database at once.
            last_error = exc
            conn.close()
            message = str(exc).lower()
            if ("locked" in message or "busy" in message) and attempt < 29:
                time.sleep(min(0.05 * (attempt + 1), 0.5))
                continue
            raise
        else:
            if path == ":memory:":
                _memory_keepalive.setdefault(path, conn)
            else:
                conn.close()
            return
    assert last_error is not None
    raise last_error


def ensure_schema() -> str:
    path = db_path()
    if path in _initialized_paths:
        if path == ":memory:" or path.startswith("file:") or os.path.exists(path):
            return path
        # The database file was removed/recreated (common in test isolation);
        # forget the cached initialisation and lay the schema down again.
        _initialized_paths.discard(path)
    with _init_lock:
        if path not in _initialized_paths:
            _ensure_schema_for_path(path)
            _initialized_paths.add(path)
    return path


def _connect() -> sqlite3.Connection:
    target, is_uri = _sqlite_target(db_path())
    conn = sqlite3.connect(target, timeout=30, isolation_level=None, uri=is_uri)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """Start a write transaction, retrying briefly if another writer holds it."""
    for attempt in range(10):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if ("locked" not in message and "busy" not in message) or attempt == 9:
                raise
            time.sleep(0.02 * (attempt + 1))


# ---------------------------------------------------------------------------
# Canonical representations
# ---------------------------------------------------------------------------


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


def _iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _entry_hash(entry: IngestEntry) -> str:
    if entry.is_retraction:
        payload: list[Any] = ["retract"]
    else:
        assert entry.airport is not None
        assert entry.arrival_utc is not None
        assert entry.departure_utc is not None
        payload = [
            "report",
            entry.airport,
            _epoch(entry.arrival_utc),
            _epoch(entry.departure_utc),
        ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _incoming_payload(entry: IngestEntry) -> dict[str, Any]:
    if entry.is_retraction:
        return {"kind": "retract", "revision": entry.revision}
    assert entry.airport is not None
    assert entry.arrival_utc is not None
    assert entry.departure_utc is not None
    return {
        "kind": "report",
        "revision": entry.revision,
        "airport": entry.airport,
        "arrival_utc": _iso_utc(_epoch(entry.arrival_utc)),
        "departure_utc": _iso_utc(_epoch(entry.departure_utc)),
    }


def _stored_payload(row: sqlite3.Row) -> dict[str, Any]:
    if row["status"] == "retracted":
        return {"kind": "retract", "revision": int(row["revision"])}
    return {
        "kind": "report",
        "revision": int(row["revision"]),
        "airport": row["airport"],
        "arrival_utc": _iso_utc(int(row["arrival_epoch"])),
        "departure_utc": _iso_utc(int(row["departure_epoch"])),
    }


# ---------------------------------------------------------------------------
# Daily-report deltas
# ---------------------------------------------------------------------------


def _adjust_group(
    conn: sqlite3.Connection,
    airport: str,
    departure_date: str,
    count_delta: int,
    seconds_delta: int,
) -> None:
    """Apply a delta to exactly one (airport, UTC date) group.

    Removing the last transfer deletes the group, so a report never exposes a
    zero-count row left behind by retractions.
    """
    row = conn.execute(
        "SELECT transfer_count, dwell_seconds FROM daily_reports "
        "WHERE airport = ? AND departure_date = ?",
        (airport, departure_date),
    ).fetchone()

    if row is None:
        if count_delta < 0 or seconds_delta < 0:
            raise RuntimeError("daily report underflow: negative delta on missing group")
        if count_delta == 0 and seconds_delta == 0:
            return
        conn.execute(
            "INSERT INTO daily_reports (airport, departure_date, transfer_count, dwell_seconds) "
            "VALUES (?, ?, ?, ?)",
            (airport, departure_date, count_delta, seconds_delta),
        )
        return

    new_count = int(row["transfer_count"]) + count_delta
    new_seconds = int(row["dwell_seconds"]) + seconds_delta
    if new_count < 0 or new_seconds < 0:
        raise RuntimeError(
            f"daily report underflow for {airport}/{departure_date}: "
            f"count={new_count}, seconds={new_seconds}"
        )
    if new_count == 0:
        if new_seconds != 0:
            raise RuntimeError(
                f"daily report inconsistency for {airport}/{departure_date}: "
                f"count=0 but seconds={new_seconds}"
            )
        conn.execute(
            "DELETE FROM daily_reports WHERE airport = ? AND departure_date = ?",
            (airport, departure_date),
        )
        return

    conn.execute(
        "UPDATE daily_reports SET transfer_count = ?, dwell_seconds = ? "
        "WHERE airport = ? AND departure_date = ?",
        (new_count, new_seconds, airport, departure_date),
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _load(conn: sqlite3.Connection, transfer_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, revision, status, airport, arrival_epoch, departure_epoch, content_hash "
        "FROM transfers WHERE id = ?",
        (transfer_id,),
    ).fetchone()


def _upsert_active(conn: sqlite3.Connection, entry: IngestEntry, content_hash: str) -> None:
    assert entry.airport is not None
    assert entry.arrival_utc is not None
    assert entry.departure_utc is not None
    conn.execute(
        """
        INSERT INTO transfers
            (id, revision, status, airport, arrival_epoch, departure_epoch, content_hash, updated_at_ms)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            revision = excluded.revision,
            status = 'active',
            airport = excluded.airport,
            arrival_epoch = excluded.arrival_epoch,
            departure_epoch = excluded.departure_epoch,
            content_hash = excluded.content_hash,
            updated_at_ms = excluded.updated_at_ms
        """,
        (
            entry.id,
            entry.revision,
            entry.airport,
            _epoch(entry.arrival_utc),
            _epoch(entry.departure_utc),
            content_hash,
            _now_ms(),
        ),
    )


def _upsert_retracted(conn: sqlite3.Connection, entry: IngestEntry, content_hash: str) -> None:
    conn.execute(
        """
        INSERT INTO transfers
            (id, revision, status, airport, arrival_epoch, departure_epoch, content_hash, updated_at_ms)
        VALUES (?, ?, 'retracted', NULL, NULL, NULL, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            revision = excluded.revision,
            status = 'retracted',
            airport = NULL,
            arrival_epoch = NULL,
            departure_epoch = NULL,
            content_hash = excluded.content_hash,
            updated_at_ms = excluded.updated_at_ms
        """,
        (entry.id, entry.revision, content_hash, _now_ms()),
    )


def _apply_entry(conn: sqlite3.Connection, entry: IngestEntry) -> str:
    """Apply one already-preflighted entry; return applied/deduplicated/ignored."""
    content_hash = _entry_hash(entry)
    row = _load(conn, entry.id)

    if entry.is_retraction:
        if row is None:
            _upsert_retracted(conn, entry, content_hash)
            return "applied"

        stored_revision = int(row["revision"])
        if entry.revision < stored_revision:
            return "ignored"
        if entry.revision == stored_revision:
            if row["status"] == "retracted" and row["content_hash"] == content_hash:
                return "deduplicated"
            # Preflight should have rejected this; keep the invariant explicit.
            raise RuntimeError("unpreflighted revision conflict for " + entry.id)

        # Higher revision: remove the active fact's contribution, keep a tombstone.
        if row["status"] == "active":
            assert row["airport"] is not None
            old_departure = int(row["departure_epoch"])
            _adjust_group(
                conn,
                str(row["airport"]),
                _utc_date(old_departure),
                -1,
                -(old_departure - int(row["arrival_epoch"])),
            )
        _upsert_retracted(conn, entry, content_hash)
        return "applied"

    # Reported fact / correction.
    assert entry.airport is not None
    assert entry.arrival_utc is not None
    assert entry.departure_utc is not None
    new_arrival = _epoch(entry.arrival_utc)
    new_departure = _epoch(entry.departure_utc)
    new_seconds = new_departure - new_arrival

    if row is None:
        _upsert_active(conn, entry, content_hash)
        _adjust_group(conn, entry.airport, _utc_date(new_departure), +1, new_seconds)
        return "applied"

    stored_revision = int(row["revision"])
    if entry.revision < stored_revision:
        return "ignored"
    if entry.revision == stored_revision:
        if row["status"] == "active" and row["content_hash"] == content_hash:
            return "deduplicated"
        raise RuntimeError("unpreflighted revision conflict for " + entry.id)

    # Higher revision: deduct the old airport/day contribution, then add the new
    # one.  If only the dwell or the airport/day changed, only those groups move.
    if row["status"] == "active":
        assert row["airport"] is not None
        old_departure = int(row["departure_epoch"])
        _adjust_group(
            conn,
            str(row["airport"]),
            _utc_date(old_departure),
            -1,
            -(old_departure - int(row["arrival_epoch"])),
        )
    _upsert_active(conn, entry, content_hash)
    _adjust_group(conn, entry.airport, _utc_date(new_departure), +1, new_seconds)
    return "applied"


def ingest(entries: list[IngestEntry]) -> dict[str, Any]:
    """Atomically apply a batch and return per-entry outcomes plus counters."""
    ensure_schema()
    with _write_lock:
        conn = _connect()
        in_transaction = False
        try:
            _begin_immediate(conn)
            in_transaction = True

            # Preflight every same-revision conflict before mutating anything:
            # "different content at the same revision rejects the whole batch".
            conflicts: list[dict[str, Any]] = []
            for entry in entries:
                row = _load(conn, entry.id)
                if (
                    row is not None
                    and int(row["revision"]) == entry.revision
                    and row["content_hash"] != _entry_hash(entry)
                ):
                    conflicts.append(
                        {
                            "id": entry.id,
                            "revision": entry.revision,
                            "stored_revision": int(row["revision"]),
                            "stored_status": row["status"],
                            "reason": "same id and revision but different content",
                            "incoming": _incoming_payload(entry),
                            "stored": _stored_payload(row),
                        }
                    )
            if conflicts:
                conn.execute("ROLLBACK")
                in_transaction = False
                raise BatchConflict(conflicts)

            results: list[dict[str, Any]] = []
            applied = deduplicated = ignored = retracted = 0
            for entry in entries:
                outcome = _apply_entry(conn, entry)
                if outcome == "applied":
                    applied += 1
                    if entry.is_retraction:
                        retracted += 1
                elif outcome == "deduplicated":
                    deduplicated += 1
                else:
                    ignored += 1
                results.append(
                    {
                        "id": entry.id,
                        "revision": entry.revision,
                        "action": "retract" if entry.is_retraction else "report",
                        "outcome": outcome,
                    }
                )

            conn.execute("COMMIT")
            in_transaction = False
        except Exception:
            if in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    return {
        "summary": {
            "received": len(entries),
            "applied": applied,
            "deduplicated": deduplicated,
            "duplicates": deduplicated,
            "ignored": ignored,
            "retracted": retracted,
        },
        "results": results,
    }


# ---------------------------------------------------------------------------
# Report reads
# ---------------------------------------------------------------------------


def daily_report(
    airport: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    """Read all matching groups from one consistent snapshot."""
    ensure_schema()
    conn = _connect()
    in_transaction = False
    try:
        # A deferred read transaction pins one WAL snapshot: the returned rows
        # (and totals derived from them) can never mix pre- and post-batch state.
        conn.execute("BEGIN")
        in_transaction = True

        sql = (
            "SELECT airport, departure_date, transfer_count, dwell_seconds "
            "FROM daily_reports WHERE 1 = 1"
        )
        params: list[Any] = []
        if airport is not None:
            sql += " AND airport = ?"
            params.append(airport)
        if date_from is not None:
            sql += " AND departure_date >= ?"
            params.append(date_from.isoformat())
        if date_to is not None:
            sql += " AND departure_date <= ?"
            params.append(date_to.isoformat())
        sql += " ORDER BY airport, departure_date"

        rows = conn.execute(sql, params).fetchall()
        conn.execute("COMMIT")
        in_transaction = False
    except Exception:
        if in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()

    output_rows = [
        {
            "airport": row["airport"],
            "departure_date": row["departure_date"],
            "transfer_count": int(row["transfer_count"]),
            "dwell_seconds": int(row["dwell_seconds"]),
        }
        for row in rows
    ]
    return {
        "rows": output_rows,
        "totals": {
            "transfer_count": sum(row["transfer_count"] for row in output_rows),
            "dwell_seconds": sum(row["dwell_seconds"] for row in output_rows),
        },
    }
