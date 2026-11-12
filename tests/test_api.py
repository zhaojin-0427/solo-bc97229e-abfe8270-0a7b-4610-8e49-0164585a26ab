"""End-to-end tests for the scan-time normalization API."""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.main import DEMO_DST_FALLBACK, app

client = TestClient(app)

# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_dst_fallback_demo_determined_and_needs_review():
    resp = client.post("/normalize", json=DEMO_DST_FALLBACK)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["summary"] == {"total": 5, "determined": 4, "needs_review": 1}

    by_id = {r["id"]: r for r in body["records"]}

    # 01:30 New York on 2024-11-03 is ambiguous (05:30Z/06:30Z); the 1-2h
    # transfer window to DEP_JFK (07:45Z) eliminates 05:30Z.
    arr = by_id["ARR_JFK"]
    assert arr["status"] == "determined"
    assert arr["utc"] == "2024-11-03T06:30:00Z"
    assert arr["candidates"] == ["2024-11-03T06:30:00Z"]

    assert by_id["DEP_JFK"]["utc"] == "2024-11-03T07:45:00Z"
    assert by_id["ARR_ORD"]["utc"] == "2024-11-03T10:10:00Z"
    assert by_id["DEP_LAX"]["utc"] == "2024-11-03T11:20:00Z"

    # 01:30 Chicago stays ambiguous: both readings satisfy every link.
    xfr = by_id["XFR_ORD"]
    assert xfr["status"] == "needs_review"
    assert xfr["utc"] is None  # never picked arbitrarily
    assert xfr["candidates"] == ["2024-11-03T06:30:00Z", "2024-11-03T07:30:00Z"]


def test_chain_propagation_through_shared_records():
    payload = {
        "records": [
            {"id": "A", "local_time": "2024-11-03T01:00:00", "timezone": "America/New_York"},
            {"id": "B", "local_time": "2024-11-03T03:30:00", "timezone": "America/New_York"},
            {"id": "C", "local_time": "2024-11-03T01:15:00", "timezone": "America/Chicago"},
        ],
        "links": [
            # B fixed at 08:30Z; window forces A = 06:00Z (not 05:00Z).
            {"from_id": "A", "to_id": "B", "min_seconds": 5400, "max_seconds": 9000},
            # A -> C window then forces C = 06:15Z (not 07:15Z).
            {"from_id": "A", "to_id": "C", "min_seconds": 0, "max_seconds": 1800},
        ],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 200, resp.text
    by_id = {r["id"]: r for r in resp.json()["records"]}
    assert by_id["A"]["utc"] == "2024-11-03T06:00:00Z"
    assert by_id["B"]["utc"] == "2024-11-03T08:30:00Z"
    assert by_id["C"]["utc"] == "2024-11-03T06:15:00Z"


def test_ambiguity_preserved_when_constraints_allow_both():
    payload = {
        "records": [
            {"id": "X", "local_time": "2024-11-03T01:30:00", "timezone": "America/New_York"},
            {"id": "Y", "local_time": "2024-11-03T09:00:00", "timezone": "UTC"},
        ],
        "links": [{"from_id": "X", "to_id": "Y", "min_seconds": 0, "max_seconds": 20000}],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 200, resp.text
    by_id = {r["id"]: r for r in resp.json()["records"]}
    assert by_id["X"]["status"] == "needs_review"
    assert by_id["X"]["candidates"] == ["2024-11-03T05:30:00Z", "2024-11-03T06:30:00Z"]
    assert by_id["Y"]["status"] == "determined"


def test_no_links_and_exact_duration_link():
    payload = {
        "records": [
            {"id": "A", "local_time": "2024-06-01T10:00:00", "timezone": "UTC"},
            {"id": "B", "local_time": "2024-06-01T11:00:00", "timezone": "UTC"},
        ],
        "links": [{"from_id": "A", "to_id": "B", "min_seconds": 3600, "max_seconds": 3600}],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"] == {"total": 2, "determined": 2, "needs_review": 0}


def test_self_link_is_supported():
    payload = {
        "records": [{"id": "A", "local_time": "2024-06-01T10:00:00", "timezone": "UTC"}],
        "links": [{"from_id": "A", "to_id": "A", "min_seconds": 0, "max_seconds": 0}],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 200, resp.text


def test_non_dst_timezone_single_candidate():
    payload = {
        "records": [{"id": "PEK", "local_time": "2024-11-03T01:30:00", "timezone": "Asia/Shanghai"}],
        "links": [],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 200, resp.text
    rec = resp.json()["records"][0]
    assert rec["status"] == "determined"
    assert rec["utc"] == "2024-11-02T17:30:00Z"


# ---------------------------------------------------------------------------
# domain errors -> 409, never a partial conversion
# ---------------------------------------------------------------------------


def test_imaginary_local_time_rejected_with_reason():
    payload = {
        "records": [
            {"id": "OK", "local_time": "2024-03-10T01:59:59", "timezone": "America/New_York"},
            {"id": "GAP", "local_time": "2024-03-10T02:30:00", "timezone": "America/New_York"},
        ],
        "links": [],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "no_legal_candidate"
    assert [r["id"] for r in body["invalid_records"]] == ["GAP"]
    assert "records" not in body  # no partial conversion


def test_conflicting_links_report_irreducible_core():
    payload = {
        "records": [
            {"id": "A", "local_time": "2024-06-01T10:00:00", "timezone": "UTC"},
            {"id": "B", "local_time": "2024-06-01T12:00:00", "timezone": "UTC"},
            {"id": "C", "local_time": "2024-06-01T13:00:00", "timezone": "UTC"},
        ],
        "links": [
            {"from_id": "A", "to_id": "B", "min_seconds": 0, "max_seconds": 3600},   # impossible
            {"from_id": "B", "to_id": "C", "min_seconds": 0, "max_seconds": 7200},   # fine
        ],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "no_global_solution"
    assert [l["index"] for l in body["conflicting_links"]] == [0]
    assert body["involved_records"] == ["A", "B"]
    assert "records" not in body


def test_conflict_only_through_ambiguity():
    # A ambiguous 05:00Z/06:00Z; B fixed 05:30Z; C fixed 07:00Z.
    # A->B needs <= 1800s (forces A=05:00Z... 05:00->05:30 ok, 06:00->05:30 negative ok too)
    # B->C fine; A->C needs <= 3600s: A=05:00Z gives 7200 -> fail; A=06:00Z gives 3600 ok.
    # A->B: A=06:00Z -> delta = -1800 < 0 -> fail. Jointly unsatisfiable.
    payload = {
        "records": [
            {"id": "A", "local_time": "2024-11-03T01:00:00", "timezone": "America/New_York"},
            {"id": "B", "local_time": "2024-11-03T05:30:00", "timezone": "UTC"},
            {"id": "C", "local_time": "2024-11-03T07:00:00", "timezone": "UTC"},
        ],
        "links": [
            {"from_id": "A", "to_id": "B", "min_seconds": 0, "max_seconds": 1800},
            {"from_id": "A", "to_id": "C", "min_seconds": 0, "max_seconds": 3600},
        ],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "no_global_solution"
    assert sorted(l["index"] for l in body["conflicting_links"]) == [0, 1]
    assert body["involved_records"] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# validation -> 422
# ---------------------------------------------------------------------------


def _base_payload():
    return copy.deepcopy(DEMO_DST_FALLBACK)


def test_unknown_timezone_422():
    payload = _base_payload()
    payload["records"][0]["timezone"] = "Mars/Olympus"
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_aware_local_time_422():
    payload = _base_payload()
    payload["records"][0]["local_time"] = "2024-11-03T01:30:00-04:00"
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_subsecond_precision_422():
    payload = _base_payload()
    payload["records"][0]["local_time"] = "2024-11-03T01:30:00.500"
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_duplicate_record_id_422():
    payload = _base_payload()
    payload["records"].append(payload["records"][0])
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_link_unknown_record_422():
    payload = _base_payload()
    payload["links"].append({"from_id": "ARR_JFK", "to_id": "GHOST", "min_seconds": 0, "max_seconds": 1})
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_inverted_interval_422():
    payload = _base_payload()
    payload["links"][0] = {"from_id": "ARR_JFK", "to_id": "DEP_JFK", "min_seconds": 7200, "max_seconds": 3600}
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_negative_seconds_422():
    payload = _base_payload()
    payload["links"][0]["min_seconds"] = -1
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_too_many_records_422():
    payload = {
        "records": [
            {"id": f"R{i}", "local_time": "2024-06-01T10:00:00", "timezone": "UTC"}
            for i in range(201)
        ],
        "links": [],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_too_many_links_422():
    payload = {
        "records": [
            {"id": "A", "local_time": "2024-06-01T10:00:00", "timezone": "UTC"},
            {"id": "B", "local_time": "2024-06-01T11:00:00", "timezone": "UTC"},
        ],
        "links": [
            {"from_id": "A", "to_id": "B", "min_seconds": 0, "max_seconds": 7200}
            for _ in range(401)
        ],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 422


def test_limits_accept_200_records_and_400_links():
    payload = {
        "records": [
            {"id": f"R{i}", "local_time": "2024-06-01T10:00:00", "timezone": "UTC"}
            for i in range(200)
        ],
        "links": [
            {"from_id": f"R{i}", "to_id": f"R{i + 1}", "min_seconds": 0, "max_seconds": 0}
            for i in range(199)
        ]
        + [
            {"from_id": "R0", "to_id": "R199", "min_seconds": 0, "max_seconds": 0}
            for _ in range(201)
        ],
    }
    resp = client.post("/normalize", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["total"] == 200
