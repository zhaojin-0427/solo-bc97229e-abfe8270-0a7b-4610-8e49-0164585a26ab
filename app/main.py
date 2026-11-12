"""FastAPI entrypoint for the scan-time normalization service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from . import solver
from .schemas import (
    ErrorResponse,
    InvalidRecordInfo,
    LinkInfo,
    NormalizeRequest,
    NormalizeResponse,
    RecordOut,
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

app = FastAPI(
    title="Scan Time Normalizer",
    description=DESCRIPTION,
    version="1.0.0",
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "scan-time-normalizer",
        "docs": "/docs",
        "normalize": "POST /normalize",
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
