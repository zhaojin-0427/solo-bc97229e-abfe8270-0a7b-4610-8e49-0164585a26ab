"""End-to-end tests for transfer ingestion, retractions and the daily report."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.schemas import IngestEntry

BASE_TIME = "2024-11-03T06:30:00Z"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "transfers.db"))
    with TestClient(app) as test_client:
        yield test_client


def fact(transfer_id: str, revision: int, airport: str, arrival: str, departure: str) -> dict:
    return {
        "id": transfer_id,
        "revision": revision,
        "airport": airport,
        "arrival_utc": arrival,
        "departure_utc": departure,
    }


def report_rows(client: TestClient, **params) -> list[dict]:
    response = client.get("/reports/daily", params=params)
    assert response.status_code == 200, response.text
    return response.json()["rows"]


def test_late_reports_are_aggregated_by_airport_and_utc_departure_date(client):
    payload = {
        "records": [
            fact("T1", 1, "JFK", "2024-11-03T06:30:00Z", "2024-11-03T08:00:00Z"),
            fact("T2", 1, "JFK", "2024-11-03T09:00:00Z", "2024-11-03T09:45:00Z"),
            fact("T3", 1, "ORD", "2024-11-03T23:00:00Z", "2024-11-04T01:00:00Z"),
        ]
    }
    response = client.post("/transfers", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["summary"] == {
        "received": 3,
        "applied": 3,
        "deduplicated": 0,
        "duplicates": 0,
        "ignored": 0,
        "retracted": 0,
    }

    rows = report_rows(client)
    assert rows == [
        {"airport": "JFK", "departure_date": "2024-11-03", "transfer_count": 2, "dwell_seconds": 8100},
        {"airport": "ORD", "departure_date": "2024-11-04", "transfer_count": 1, "dwell_seconds": 7200},
    ]
    totals = client.get("/reports/daily").json()["totals"]
    assert totals == {"transfer_count": 3, "dwell_seconds": 15300}


def test_higher_revision_replaces_lower_and_lower_is_ignored(client):
    first = client.post(
        "/transfers",
        json={"records": [fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z")]},
    )
    assert first.status_code == 200

    correction = client.post(
        "/transfers",
        json={"records": [fact("T1", 2, "JFK", BASE_TIME, "2024-11-03T08:30:00Z")]},
    )
    assert correction.status_code == 200, correction.text
    assert correction.json()["summary"]["applied"] == 1

    stale = client.post(
        "/transfers",
        json={"records": [fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z")]},
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["results"][0]["outcome"] == "ignored"

    rows = report_rows(client)
    assert rows == [
        {"airport": "JFK", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 7200}
    ]


def test_same_revision_same_content_is_idempotent(client):
    payload = {
        "records": [
            fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z"),
            fact("T2", 1, "LHR", "2024-11-03T09:00:00Z", "2024-11-03T10:00:00Z"),
        ]
    }
    first = client.post("/transfers", json=payload)
    assert first.status_code == 200, first.text
    replay = client.post("/transfers", json=payload)
    assert replay.status_code == 200, replay.text
    summary = replay.json()["summary"]
    assert summary["applied"] == 0
    assert summary["deduplicated"] == 2
    assert all(item["outcome"] == "deduplicated" for item in replay.json()["results"])

    rows = report_rows(client)
    assert [row["transfer_count"] for row in rows] == [1, 1]
    assert sum(row["dwell_seconds"] for row in rows) == 5400 + 3600


def test_same_revision_different_content_rejects_whole_batch(client):
    initial = client.post(
        "/transfers",
        json={
            "records": [
                fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z"),
                fact("T2", 1, "ORD", "2024-11-03T10:00:00Z", "2024-11-03T11:00:00Z"),
            ]
        },
    )
    assert initial.status_code == 200, initial.text

    conflicting = client.post(
        "/transfers",
        json={
            "records": [
                # This valid correction must not be applied either: the batch is atomic.
                fact("T2", 2, "ORD", "2024-11-03T10:00:00Z", "2024-11-03T12:00:00Z"),
                fact("T1", 1, "LAX", BASE_TIME, "2024-11-03T08:00:00Z"),
            ]
        },
    )
    assert conflicting.status_code == 409, conflicting.text
    body = conflicting.json()
    assert body["error"] == "revision_conflict"
    assert body["status"] == "rejected"
    assert [item["id"] for item in body["conflicts"]] == ["T1"]

    # Nothing changed: T2's revision-2 correction was rolled back with the batch.
    rows = report_rows(client)
    assert rows == [
        {"airport": "JFK", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 5400},
        {"airport": "ORD", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 3600},
    ]


def test_correction_deducts_old_airport_day_and_adds_new_group(client):
    payload = {
        "records": [
            fact("T1", 1, "JFK", "2024-11-03T06:30:00Z", "2024-11-03T08:00:00Z"),
            fact("T2", 1, "ORD", "2024-11-03T10:00:00Z", "2024-11-03T11:00:00Z"),
        ]
    }
    assert client.post("/transfers", json=payload).status_code == 200

    # Rev 2 moves T1 to LHR and across the UTC date boundary (330 minutes).
    correction = client.post(
        "/transfers",
        json={
            "records": [
                fact("T1", 2, "LHR", "2024-11-03T06:30:00Z", "2024-11-03T12:00:00Z"),
            ]
        },
    )
    assert correction.status_code == 200, correction.text

    rows = report_rows(client)
    assert rows == [
        {"airport": "LHR", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 19800},
        {"airport": "ORD", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 3600},
    ]

    # Same airport, different UTC day: deduct yesterday and add today.
    moved_day = client.post(
        "/transfers",
        json={
            "records": [
                fact("T1", 3, "LHR", "2024-11-03T06:30:00Z", "2024-11-04T01:00:00Z"),
            ]
        },
    )
    assert moved_day.status_code == 200, moved_day.text
    rows = report_rows(client)
    assert rows == [
        {"airport": "LHR", "departure_date": "2024-11-04", "transfer_count": 1, "dwell_seconds": 66600},
        {"airport": "ORD", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 3600},
    ]


def test_retraction_removes_contribution_and_old_revision_cannot_resurrect(client):
    assert client.post(
        "/transfers",
        json={"records": [fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z")]},
    ).status_code == 200

    retraction = client.post(
        "/transfers",
        json={"records": [{"id": "T1", "revision": 2}]},
    )
    assert retraction.status_code == 200, retraction.text
    assert retraction.json()["results"][0] == {
        "id": "T1",
        "revision": 2,
        "action": "retract",
        "outcome": "applied",
    }
    assert report_rows(client) == []

    # A late delivery of the older fact revision must be ignored, not revived.
    late = client.post(
        "/transfers",
        json={"records": [fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z")]},
    )
    assert late.status_code == 200, late.text
    assert late.json()["results"][0]["outcome"] == "ignored"
    assert report_rows(client) == []

    # A genuinely newer fact revision is allowed to restore the transfer.
    revived = client.post(
        "/transfers",
        json={"records": [fact("T1", 3, "JFK", BASE_TIME, "2024-11-03T09:00:00Z")]},
    )
    assert revived.status_code == 200, revived.text
    assert revived.json()["results"][0]["outcome"] == "applied"
    assert report_rows(client) == [
        {"airport": "JFK", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 9000}
    ]


def test_retraction_replay_is_idempotent_and_same_revision_conflict_rejected(client):
    assert client.post(
        "/transfers",
        json={"records": [fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z")]},
    ).status_code == 200

    retract = client.post("/transfers", json={"records": [{"id": "T1", "revision": 2, "retracted": True}]})
    assert retract.status_code == 200
    replay = client.post("/transfers", json={"records": [{"id": "T1", "revision": 2, "retracted": True}]})
    assert replay.status_code == 200
    assert replay.json()["results"][0]["outcome"] == "deduplicated"

    # Same revision, but now a fact instead of a retraction: different content.
    conflict = client.post(
        "/transfers",
        json={"records": [fact("T1", 2, "JFK", BASE_TIME, "2024-11-03T08:00:00Z")]},
    )
    assert conflict.status_code == 409, conflict.text


@pytest.mark.parametrize(
    "entry, expected_snippet",
    [
        ({"id": "T1", "revision": 0, "airport": "JFK", "arrival_utc": BASE_TIME,
          "departure_utc": "2024-11-03T08:00:00Z"}, "revision"),
        ({"id": "T1", "revision": 1, "airport": "JFK", "arrival_utc": "2024-11-03T08:00:00Z",
          "departure_utc": BASE_TIME}, "departure"),
        ({"id": "T1", "revision": 1, "airport": "JFK", "arrival_utc": "2024-11-03T06:30:00",
          "departure_utc": "2024-11-03T08:00:00Z"}, "UTC"),
        ({"id": "T1", "revision": 1, "airport": "JFK", "arrival_utc": "2024-11-03T06:30:00+08:00",
          "departure_utc": "2024-11-03T08:00:00Z"}, "UTC"),
        ({"id": "T1", "revision": 1, "airport": "JFK", "arrival_utc": "2024-11-03T06:30:00.500Z",
          "departure_utc": "2024-11-03T08:00:00Z"}, "whole-second"),
        ({"id": "T1", "revision": 1, "action": "report"}, "requires"),
        ({"id": "T1", "revision": 1, "retracted": True, "airport": "JFK",
          "arrival_utc": BASE_TIME, "departure_utc": "2024-11-03T08:00:00Z"}, "only contain"),
    ],
)
def test_entry_validation_is_422(client, entry, expected_snippet):
    response = client.post("/transfers", json={"records": [entry]})
    assert response.status_code == 422, response.text
    assert expected_snippet.lower() in response.text.lower()


def test_batch_limits_and_unique_ids(client):
    too_many = [
        fact(f"T{i}", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z") for i in range(101)
    ]
    assert client.post("/transfers", json={"records": too_many}).status_code == 422

    duplicate = [
        fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z"),
        fact("T1", 2, "LHR", BASE_TIME, "2024-11-03T09:00:00Z"),
    ]
    assert client.post("/transfers", json={"records": duplicate}).status_code == 422

    exactly_100 = [
        fact(f"T{i}", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z") for i in range(100)
    ]
    response = client.post("/transfers", json={"records": exactly_100})
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["applied"] == 100


def test_accepts_bare_array_and_envelope_aliases(client):
    entries = [
        {"transfer_id": "T1", "rev": 1, "airport_code": "JFK",
         "arrived_at": BASE_TIME, "departed_at": "2024-11-03T08:00:00Z"},
    ]
    assert client.post("/transfers", json=entries).status_code == 200
    rows = report_rows(client)
    assert rows[0]["airport"] == "JFK"

    envelope = {
        "reports": [fact("T2", 1, "ORD", "2024-11-03T10:00:00Z", "2024-11-03T11:00:00Z")],
        "retractions": [{"id": "T1", "revision": 2}],
    }
    response = client.post("/transfers", json=envelope)
    assert response.status_code == 200, response.text
    assert report_rows(client) == [
        {"airport": "ORD", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 3600}
    ]


def test_report_filters(client):
    assert client.post(
        "/transfers",
        json={
            "records": [
                fact("T1", 1, "JFK", BASE_TIME, "2024-11-03T08:00:00Z"),
                fact("T2", 1, "LHR", BASE_TIME, "2024-11-04T08:00:00Z"),
                fact("T3", 1, "ORD", BASE_TIME, "2024-11-05T08:00:00Z"),
            ]
        },
    ).status_code == 200

    assert [row["airport"] for row in report_rows(client, airport="LHR")] == ["LHR"]
    assert [row["departure_date"] for row in report_rows(client, date_from="2024-11-04")] == [
        "2024-11-04",
        "2024-11-05",
    ]
    assert [row["departure_date"] for row in report_rows(client, date="2024-11-04")] == [
        "2024-11-04"
    ]
    bad = client.get("/reports/daily", params={"date_from": "2024-11-05", "date_to": "2024-11-04"})
    assert bad.status_code == 422


def test_concurrent_ingests_do_not_lose_or_double_count():
    # Exercise the SQLite write path directly with independent entries.  The
    # global transaction lock plus BEGIN IMMEDIATE serialises writers; each
    # committed batch must show up exactly once.
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    previous = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = path
    try:
        def submit(index: int) -> str:
            entry = IngestEntry(
                id=f"C{index}",
                revision=1,
                airport="JFK" if index % 2 == 0 else "LHR",
                arrival_utc=BASE_TIME,
                departure_utc="2024-11-03T08:00:00Z",
            )
            db.ingest([entry])
            return entry.id

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(submit, range(40)))

        rows = db.daily_report()["rows"]
        by_airport = {row["airport"]: row for row in rows}
        assert by_airport["JFK"]["transfer_count"] == 20
        assert by_airport["JFK"]["dwell_seconds"] == 20 * 5400
        assert by_airport["LHR"]["transfer_count"] == 20
        assert by_airport["LHR"]["dwell_seconds"] == 20 * 5400
        assert len(set(ids)) == 40
    finally:
        if previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass
