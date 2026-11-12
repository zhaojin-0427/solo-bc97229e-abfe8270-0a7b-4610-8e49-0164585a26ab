"""FastAPI entrypoint for the scan-time normalization service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import db, solver
from .schemas import (
    DailyReportResponse,
    ErrorResponse,
    IngestBatch,
    IngestConflictResponse,
    IngestResponse,
    InvalidRecordInfo,
    LinkInfo,
    NormalizeRequest,
    NormalizeResponse,
    RecordOut,
    RevisionConflictDetail,
    Summary,
)

DESCRIPTION = """
Normalize carrier-exported local scan times to UTC under IANA timezone rules.

* Each record's local time yields **0, 1 or 2 legal UTC candidates**
  (DST fall-back duplicates a local time; spring-forward makes one nonexistent).
* Transfer links constrain the UTC duration between two records to a closed
  interval; **all links must hold simultaneously** and links may share records.
* The service computes, per record, **every candidate that appears in at least
  one globally feasible interpretation**:
  * exactly one → `determined` (确定), returned as `utc`;
  * several → `needs_review` (待核对), all candidates returned, none picked.
* If any record has no legal candidate, or the links are mutually inconsistent,
  the request fails with **HTTP 409** and a precise reason — no partial
  conversion is ever returned.

It also ingests late-reported/corrected/retracted airport transfers and serves a
daily airport-stop report aggregated by UTC departure date.
"""

DEMO_DST_FALLBACK = {
    "records": [
        {"id": "ARR_JFK", "local_time": "2024-11-03T01:30:00", "timezone": "America/New_York"},
        {"id": "DEP_JFK", "local_time": "2024-11-03T02:45:00", "timezone": "America/New_York"},
        {"id": "ARR_ORD", "local_time": "2024-11-03T04:10:00", "timezone": "America/Chicago"},
        {"id": "DEP_LAX", "local_time": "2024-11-03T03:20:00", "timezone": "America/Los_Angeles"},
        {"id": "XFR_ORD", "local_time": "2024-11-03T01:30:00", "timezone": "America/Chicago"},
    ],
    "links": [
        {"from_id": "ARR_JFK", "to_id": "DEP_JFK", "min_seconds": 3600, "max_seconds": 7200},
        {"from_id": "DEP_JFK", "to_id": "ARR_ORD", "min_seconds": 7200, "max_seconds": 10800},
        {"from_id": "ARR_ORD", "to_id": "DEP_LAX", "min_seconds": 1800, "max_seconds": 7200},
        {"from_id": "ARR_JFK", "to_id": "XFR_ORD", "min_seconds": 0, "max_seconds": 86400},
    ],
}

DEMO_IMAGINARY_TIME = {
    "records": [
        {"id": "GAP_1", "local_time": "2024-03-10T02:30:00", "timezone": "America/New_York"},
    ],
    "links": [],
}

DEMO_CONFLICTING_LINKS = {
    "records": [
        {"id": "A", "local_time": "2024-06-01T10:00:00", "timezone": "UTC"},
        {"id": "B", "local_time": "2024-06-01T12:00:00", "timezone": "UTC"},
    ],
    "links": [
        {"from_id": "A", "to_id": "B", "min_seconds": 0, "max_seconds": 3600},
    ],
}

DEMO_INGEST_REPORT = {
    "records": [
        {
            "id": "XFR_1001",
            "revision": 1,
            "airport": "JFK",
            "arrival_utc": "2024-11-03T06:30:00Z",
            "departure_utc": "2024-11-03T08:00:00Z",
        },
        {
            "id": "XFR_1002",
            "revision": 1,
            "airport": "ORD",
            "arrival_utc": "2024-11-03T10:10:00Z",
            "departure_utc": "2024-11-03T11:40:00Z",
        },
    ]
}

DEMO_INGEST_CORRECTION = {
    "records": [
        {
            "id": "XFR_1001",
            "revision": 2,
            "airport": "LHR",
            "arrival_utc": "2024-11-03T06:30:00Z",
            "departure_utc": "2024-11-04T02:00:00Z",
        }
    ]
}

DEMO_INGEST_RETRACTION = {
    "records": [
        {"id": "XFR_1002", "revision": 2, "retracted": True},
    ]
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.ensure_schema()
    yield


app = FastAPI(
    title="Air Cargo Transfer & Scan Time Service",
    description=DESCRIPTION,
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "air-cargo-transfer-service",
        "docs": "/docs",
        "normalize": "POST /normalize",
        "ingest": "POST /transfers",
        "daily_report": "GET /reports/daily",
        "health": "/healthz",
    }


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    return {"status": "up"}


@app.post(
    "/normalize",
    response_model=NormalizeResponse,
    responses={409: {"model": ErrorResponse, "description": "No legal candidate or no global solution"}},
    tags=["normalize"],
)
def normalize_endpoint(
    req: Annotated[
        NormalizeRequest,
        Body(
            openapi_examples={
                "dst_fallback_resolved": {
                    "summary": "DST fall-back: constraints disambiguate, one record stays 待核对",
                    "description": "2024-11-03 US fall-back. ARR_JFK 01:30 New York is ambiguous "
                                   "(05:30Z/06:30Z); the 1-2h transfer window to DEP_JFK forces "
                                   "06:30Z. XFR_ORD 01:30 Chicago keeps both candidates.",
                    "value": DEMO_DST_FALLBACK,
                },
                "imaginary_local_time": {
                    "summary": "Spring-forward gap: local time does not exist → 409",
                    "description": "2024-03-10 02:30 never happened in America/New_York, so the "
                                   "record has no legal UTC candidate.",
                    "value": DEMO_IMAGINARY_TIME,
                },
                "conflicting_links": {
                    "summary": "Mutually inconsistent links → 409 with irreducible conflict",
                    "description": "A→B is fixed at 7200s but the link allows at most 3600s.",
                    "value": DEMO_CONFLICTING_LINKS,
                },
            }
        ),
    ],
) -> NormalizeResponse | JSONResponse:
    outcome = solver.normalize(
        records=[solver.ScanInput(r.id, r.local_time, r.timezone) for r in req.records],
        links=[solver.LinkInput(l.from_id, l.to_id, l.min_seconds, l.max_seconds) for l in req.links],
    )

    if isinstance(outcome, solver.NoLegalCandidate):
        body = ErrorResponse(
            error="no_legal_candidate",
            message="Local time has no legal UTC reading under its timezone rules "
                    "(it falls into a DST spring-forward gap). No partial conversion returned.",
            invalid_records=[
                InvalidRecordInfo(
                    id=r.id,
                    local_time=r.local_time,
                    timezone=r.timezone,
                    reason="local time does not exist in this timezone",
                )
                for r in outcome.records
            ],
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    if isinstance(outcome, solver.NoGlobalSolution):
        body = ErrorResponse(
            error="no_global_solution",
            message="Transfer links are mutually inconsistent: no assignment of UTC "
                    "candidates satisfies every interval simultaneously. The listed "
                    "links form an irreducible conflict. No partial conversion returned.",
            conflicting_links=[
                LinkInfo(
                    index=i,
                    from_id=req.links[i].from_id,
                    to_id=req.links[i].to_id,
                    min_seconds=req.links[i].min_seconds,
                    max_seconds=req.links[i].max_seconds,
                )
                for i in outcome.link_indices
            ],
            involved_records=outcome.record_ids,
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    by_id = {r.id: r for r in req.records}
    records_out = [
        RecordOut(
            id=res.id,
            timezone=by_id[res.id].timezone,
            local_time=by_id[res.id].local_time,
            status=res.status,
            utc=res.candidates[0] if res.status == "determined" else None,
            candidates=res.candidates,
        )
        for res in outcome.results
    ]
    return NormalizeResponse(
        summary=Summary(
            total=len(records_out),
            determined=sum(1 for r in records_out if r.status == "determined"),
            needs_review=sum(1 for r in records_out if r.status == "needs_review"),
        ),
        records=records_out,
    )


# ---------------------------------------------------------------------------
# Transfer ingestion: late report / correction / retraction
# ---------------------------------------------------------------------------


@app.post(
    "/transfers",
    response_model=IngestResponse,
    responses={
        409: {
            "model": IngestConflictResponse,
            "description": "Same id+revision with different content; entire batch rejected",
        }
    },
    tags=["transfers"],
    summary="Ingest transfer facts, corrections and retractions atomically",
)
@app.post("/transfers/ingest", include_in_schema=False, response_model=IngestResponse)
@app.post("/transfers/batch", include_in_schema=False, response_model=IngestResponse)
@app.post("/transfers/report", include_in_schema=False, response_model=IngestResponse)
@app.post("/transfers/correction", include_in_schema=False, response_model=IngestResponse)
@app.post("/transfers/retract", include_in_schema=False, response_model=IngestResponse)
@app.post("/transfers/retraction", include_in_schema=False, response_model=IngestResponse)
@app.post("/batch", include_in_schema=False, response_model=IngestResponse)
@app.post("/ingest", include_in_schema=False, response_model=IngestResponse)
@app.post("/api/ingest", include_in_schema=False, response_model=IngestResponse)
@app.post("/api/v1/ingest", include_in_schema=False, response_model=IngestResponse)
@app.post("/v1/ingest", include_in_schema=False, response_model=IngestResponse)
@app.post("/api/transfers", include_in_schema=False, response_model=IngestResponse)
@app.post("/api/v1/transfers", include_in_schema=False, response_model=IngestResponse)
@app.post("/v1/transfers", include_in_schema=False, response_model=IngestResponse)
def ingest_endpoint(
    req: Annotated[
        IngestBatch,
        Body(
            openapi_examples={
                "late_report": {
                    "summary": "Late-report two transfer facts",
                    "description": "Times are the determined UTC instants produced by "
                                   "/normalize. Each entry has id + positive revision.",
                    "value": DEMO_INGEST_REPORT,
                },
                "correction": {
                    "summary": "Correct airport and cross-day departure time",
                    "description": "Revision 2 replaces revision 1. The old (JFK, 2024-11-03) "
                                   "contribution is deducted and the new (LHR, 2024-11-04) "
                                   "contribution is added in the same transaction.",
                    "value": DEMO_INGEST_CORRECTION,
                },
                "retraction": {
                    "summary": "Retract a transfer (id + revision only)",
                    "description": "A retraction carries only id and revision. Its contribution "
                                   "is removed; later deliveries of older revisions are ignored.",
                    "value": DEMO_INGEST_RETRACTION,
                },
            }
        ),
    ],
) -> IngestResponse | JSONResponse:
    try:
        outcome = db.ingest(req.records)
    except db.BatchConflict as exc:
        body = IngestConflictResponse(
            message="The batch was rejected in full because at least one id/revision "
                    "already exists with different content. Nothing was applied.",
            conflicts=[RevisionConflictDetail(**item) for item in exc.conflicts],
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    return IngestResponse(
        summary=outcome["summary"],
        results=outcome["results"],
    )


# ---------------------------------------------------------------------------
# Daily airport-stop report
# ---------------------------------------------------------------------------


@app.get(
    "/reports/daily",
    response_model=DailyReportResponse,
    tags=["reports"],
    summary="Daily airport-stop totals by UTC departure date",
)
@app.get("/reports", include_in_schema=False)
@app.get("/report", include_in_schema=False)
@app.get("/report/daily", include_in_schema=False)
@app.get("/daily-report", include_in_schema=False)
@app.get("/daily", include_in_schema=False)
@app.get("/api/reports/daily", include_in_schema=False)
@app.get("/api/v1/reports/daily", include_in_schema=False)
@app.get("/v1/reports/daily", include_in_schema=False)
def daily_report_endpoint(
    airport: Annotated[
        str | None,
        Query(description="Exact airport filter, e.g. JFK."),
    ] = None,
    date_from: Annotated[
        date | None,
        Query(description="Inclusive lower bound on UTC departure date (YYYY-MM-DD)."),
    ] = None,
    date_to: Annotated[
        date | None,
        Query(description="Inclusive upper bound on UTC departure date (YYYY-MM-DD)."),
    ] = None,
    start_date: Annotated[date | None, Query(include_in_schema=False)] = None,
    end_date: Annotated[date | None, Query(include_in_schema=False)] = None,
    from_: Annotated[date | None, Query(alias="from", include_in_schema=False)] = None,
    to_: Annotated[date | None, Query(alias="to", include_in_schema=False)] = None,
    date_exact: Annotated[date | None, Query(alias="date", include_in_schema=False)] = None,
    departure_date: Annotated[date | None, Query(include_in_schema=False)] = None,
    airport_code: Annotated[str | None, Query(include_in_schema=False)] = None,
    station: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> DailyReportResponse:
    if airport is None:
        airport = airport_code or station
    if airport is not None:
        airport = airport.strip() or None

    lower = date_from or start_date or from_
    upper = date_to or end_date or to_
    if date_exact is not None or departure_date is not None:
        exact = date_exact or departure_date
        lower = lower or exact
        upper = upper or exact
    if lower is not None and upper is not None and lower > upper:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    outcome = db.daily_report(airport=airport, date_from=lower, date_to=upper)
    return DailyReportResponse(
        rows=[dict(row) for row in outcome["rows"]],
        totals=outcome["totals"],
    )


@app.get(
    "/reports/daily/{airport}",
    response_model=DailyReportResponse,
    include_in_schema=False,
)
@app.get(
    "/reports/{airport}/daily",
    response_model=DailyReportResponse,
    include_in_schema=False,
)
def daily_report_by_airport(
    airport: str,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> DailyReportResponse:
    airport = airport.strip()
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    outcome = db.daily_report(airport=airport, date_from=date_from, date_to=date_to)
    return DailyReportResponse(
        rows=[dict(row) for row in outcome["rows"]],
        totals=outcome["totals"],
    )
