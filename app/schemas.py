"""Request/response models for the scan-time normalization API."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

UTC = timezone.utc


class ScanRecord(BaseModel):
    """One carrier scan event: unique id + naive local time + IANA timezone."""

    id: str = Field(min_length=1, max_length=64, examples=["ARR_JFK"])
    local_time: datetime = Field(
        examples=["2024-11-03T01:30:00"],
        description="Naive local date-time, whole-second precision, no UTC offset.",
    )
    timezone: str = Field(
        min_length=1, max_length=64,
        examples=["America/New_York"],
        description="IANA timezone name, e.g. America/New_York.",
    )

    @field_validator("local_time")
    @classmethod
    def _naive_whole_second(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            raise ValueError("local_time must be naive local time without a UTC offset")
        if v.microsecond != 0:
            raise ValueError("local_time must have whole-second precision")
        return v

    @field_validator("timezone")
    @classmethod
    def _known_iana_name(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v


class TransferLink(BaseModel):
    """A leg/transfer connection: UTC duration from_id -> to_id in [min, max] seconds."""

    from_id: str = Field(min_length=1, max_length=64, examples=["ARR_JFK"])
    to_id: str = Field(min_length=1, max_length=64, examples=["DEP_JFK"])
    min_seconds: int = Field(ge=0, examples=[3600])
    max_seconds: int = Field(ge=0, examples=[7200])

    @model_validator(mode="after")
    def _ordered_interval(self) -> "TransferLink":
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must be <= max_seconds")
        return self


class NormalizeRequest(BaseModel):
    records: list[ScanRecord] = Field(min_length=1, max_length=200)
    links: list[TransferLink] = Field(default_factory=list, max_length=400)

    @model_validator(mode="after")
    def _consistent_references(self) -> "NormalizeRequest":
        ids = [r.id for r in self.records]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate record id(s): {', '.join(dupes)}")
        known = set(ids)
        unknown = sorted(({l.from_id for l in self.links} | {l.to_id for l in self.links}) - known)
        if unknown:
            raise ValueError(f"link(s) reference unknown record id(s): {', '.join(unknown)}")
        return self


class RecordOut(BaseModel):
    id: str
    timezone: str
    local_time: datetime
    status: Literal["determined", "needs_review"] = Field(
        description="determined = exactly one feasible UTC instant (确定); "
                    "needs_review = several feasible instants, none picked (待核对)."
    )
    utc: datetime | None = Field(
        default=None, description="The single feasible UTC instant; null unless determined."
    )
    candidates: list[datetime] = Field(
        description="All UTC instants that appear in at least one globally feasible "
                    "interpretation, sorted ascending."
    )


class Summary(BaseModel):
    total: int
    determined: int
    needs_review: int


class NormalizeResponse(BaseModel):
    status: Literal["ok"] = "ok"
    summary: Summary
    records: list[RecordOut]


class InvalidRecordInfo(BaseModel):
    id: str
    local_time: datetime
    timezone: str
    reason: str


class LinkInfo(BaseModel):
    index: int
    from_id: str
    to_id: str
    min_seconds: int
    max_seconds: int


class ErrorResponse(BaseModel):
    error: Literal["no_legal_candidate", "no_global_solution"]
    message: str
    invalid_records: list[InvalidRecordInfo] = Field(default_factory=list)
    conflicting_links: list[LinkInfo] = Field(default_factory=list)
    involved_records: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Transfer ingestion (late reports, corrections and retractions)
# ---------------------------------------------------------------------------

# Accepted spellings make the API pleasant to integrate with existing carrier
# exports.  The canonical names (id/revision/airport/arrival_utc/departure_utc)
# are the ones shown in the interactive documentation.
_ID_ALIASES = (
    "id", "transfer_id", "transferId", "record_id", "transit_id", "transfer_no",
)
_REVISION_ALIASES = (
    "revision", "rev", "version", "revision_no", "rev_no", "revision_number",
    "revision_id", "rev_id",
)
_AIRPORT_ALIASES = (
    "airport", "airport_code", "airportCode", "airport_name", "iata", "station",
    "station_code",
)
_ARRIVAL_ALIASES = (
    "arrival_utc", "arrived_at", "arrival", "arrival_time", "arrival_time_utc",
    "arrivalUtc", "arrived_utc", "arrive_at", "arr_time", "arr_time_utc", "ata",
)
_DEPARTURE_ALIASES = (
    "departure_utc", "departed_at", "departure", "departure_time", "departure_time_utc",
    "departureUtc", "departed_utc", "depart_at", "dep_time", "dep_time_utc", "atd",
)
_RETRACTED_ALIASES = (
    "retracted", "retract", "is_retracted", "is_retraction", "is_retract",
    "retracted_flag", "withdrawn", "canceled", "cancelled",
)
_ACTION_ALIASES = (
    "action", "kind", "operation", "op", "mode", "type", "event",
    "op_type", "operation_type",
)

_REPORT_ACTIONS = {
    "report", "reported", "fact", "upsert", "correct", "correction", "update",
    "补报", "更正",
}
_RETRACT_ACTIONS = {
    "retract", "retraction", "retracted", "withdraw", "withdrawal", "delete",
    "cancel", "canceled", "cancelled", "撤回",
}


def _validate_utc_second(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("must be a timezone-aware UTC timestamp (e.g. 2024-11-03T06:30:00Z)")
    if v.utcoffset() != timedelta(0):
        raise ValueError("must be a UTC timestamp (offset must be +00:00)")
    if v.microsecond != 0:
        raise ValueError("must have whole-second precision")
    return v.astimezone(UTC)


class IngestEntry(BaseModel):
    """One late-reported transfer fact or a retraction.

    A fact carries ``id``, a positive ``revision``, ``airport``, ``arrival_utc``
    and ``departure_utc``.  A retraction carries only ``id`` and ``revision``
    (the optional ``retracted``/``action`` flags are metadata, not fact data).
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(
        min_length=1, max_length=128,
        validation_alias=AliasChoices(*_ID_ALIASES),
        examples=["XFR_1001"],
    )
    revision: int = Field(
        ge=1, le=9_223_372_036_854_775_807,
        validation_alias=AliasChoices(*_REVISION_ALIASES),
        description="Positive integer revision. Higher revisions replace lower ones.",
        examples=[1],
    )
    airport: str | None = Field(
        default=None, min_length=1, max_length=128,
        validation_alias=AliasChoices(*_AIRPORT_ALIASES),
        examples=["JFK"],
    )
    arrival_utc: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices(*_ARRIVAL_ALIASES),
        description="Arrival instant, UTC, whole-second precision.",
        examples=["2024-11-03T06:30:00Z"],
    )
    departure_utc: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices(*_DEPARTURE_ALIASES),
        description="Departure instant, UTC, whole-second precision; must not be earlier than arrival.",
        examples=["2024-11-03T08:00:00Z"],
    )
    retracted: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(*_RETRACTED_ALIASES),
        description="Set true for a retraction; may be omitted because a retraction "
                    "is also recognised by the absence of fact fields.",
    )
    action: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*_ACTION_ALIASES),
        description="Optional explicit action: 'report' (or 'correct') / 'retract'.",
    )

    _is_retraction: bool = PrivateAttr(default=False)

    @field_validator("id")
    @classmethod
    def _non_blank_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("id must not be blank")
        return v

    @field_validator("revision", mode="before")
    @classmethod
    def _reject_boolean_revision(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("revision must be a positive integer")
        return v

    @field_validator("airport")
    @classmethod
    def _non_blank_airport(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("airport must not be blank")
        return v

    @field_validator("arrival_utc", "departure_utc")
    @classmethod
    def _utc_whole_second(cls, v: datetime | None) -> datetime | None:
        return _validate_utc_second(v)

    @model_validator(mode="after")
    def _classify_and_validate(self) -> "IngestEntry":
        action: str | None = None
        if self.action is not None:
            candidate = self.action.strip().lower()
            if candidate in _RETRACT_ACTIONS:
                action = "retract"
            elif candidate in _REPORT_ACTIONS:
                action = "report"
            # Unknown action-like metadata (e.g. type="transfer") is ignored;
            # the presence of fact fields then decides the entry kind.

        if action is not None and self.retracted is not None:
            implied = "retract" if self.retracted else "report"
            if implied != action:
                raise ValueError("action and retracted flag disagree")

        if action is not None:
            kind = action
        elif self.retracted is not None:
            kind = "retract" if self.retracted else "report"
        else:
            has_fact_fields = any(
                value is not None
                for value in (self.airport, self.arrival_utc, self.departure_utc)
            )
            kind = "report" if has_fact_fields else "retract"

        if kind == "retract":
            if any(value is not None for value in (self.airport, self.arrival_utc, self.departure_utc)):
                raise ValueError("a retraction may only contain id and revision")
            self._is_retraction = True
            return self

        missing = [
            name for name, value in (
                ("airport", self.airport),
                ("arrival_utc", self.arrival_utc),
                ("departure_utc", self.departure_utc),
            )
            if value is None
        ]
        if missing:
            raise ValueError("a reported transfer requires " + ", ".join(missing))
        assert self.arrival_utc is not None and self.departure_utc is not None
        if self.departure_utc < self.arrival_utc:
            raise ValueError("departure_utc must not be earlier than arrival_utc")
        self._is_retraction = False
        return self

    @property
    def is_retraction(self) -> bool:
        return self._is_retraction


_BATCH_LIST_KEYS = (
    "records", "items", "entries", "transfers", "facts", "reports",
    "late_reports", "corrections", "retractions", "data", "batch",
)


class IngestBatch(BaseModel):
    """A batch of at most 100 transfer facts/retractions with unique ids."""

    records: list[IngestEntry] = Field(min_length=1, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def _accept_common_envelopes(cls, data: Any) -> Any:
        # Also accept a bare JSON array, a single entry object, or an envelope
        # using {items|entries|transfers|facts|reports|retractions|data}.
        if isinstance(data, list):
            return {"records": data}
        if isinstance(data, dict):
            if not any(key in data for key in _BATCH_LIST_KEYS):
                if "id" in data or "transfer_id" in data:
                    return {"records": [data]}
                return data
            collected: list[Any] = []
            for key in _BATCH_LIST_KEYS:
                if key not in data:
                    continue
                value = data[key]
                if value is None:
                    continue
                if not isinstance(value, list):
                    raise ValueError(f"{key} must be a list of entries")
                collected.extend(value)
            return {"records": collected}
        return data

    @model_validator(mode="after")
    def _unique_ids(self) -> "IngestBatch":
        ids = [record.id for record in self.records]
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            raise ValueError("duplicate transfer id(s) in batch: " + ", ".join(duplicates))
        return self


class IngestSummary(BaseModel):
    received: int
    applied: int = Field(description="Entries that changed stored state.")
    deduplicated: int = Field(description="Same id+revision+content replays; no double counting.")
    duplicates: int = Field(description="Alias of deduplicated for convenience.")
    ignored: int = Field(description="Lower revisions superseded by stored state.")
    retracted: int = Field(description="Retraction entries that changed stored state.")


class IngestEntryResult(BaseModel):
    id: str
    revision: int
    action: Literal["report", "retract"]
    outcome: Literal["applied", "deduplicated", "ignored"]


class IngestResponse(BaseModel):
    status: Literal["ok"] = "ok"
    summary: IngestSummary
    results: list[IngestEntryResult]


class RevisionConflictDetail(BaseModel):
    id: str
    revision: int
    stored_revision: int
    stored_status: Literal["active", "retracted"]
    reason: str
    incoming: dict[str, Any]
    stored: dict[str, Any]


class IngestConflictResponse(BaseModel):
    status: Literal["rejected"] = "rejected"
    error: Literal["revision_conflict"] = "revision_conflict"
    message: str
    conflicts: list[RevisionConflictDetail]


# ---------------------------------------------------------------------------
# Daily airport-stop report
# ---------------------------------------------------------------------------


class DailyReportRow(BaseModel):
    airport: str
    departure_date: date = Field(description="UTC date of departure.")
    transfer_count: int = Field(description="Number of active transfers in this group.")
    dwell_seconds: int = Field(description="Sum of departure-arrival seconds in this group.")


class DailyReportTotals(BaseModel):
    transfer_count: int
    dwell_seconds: int


class DailyReportResponse(BaseModel):
    status: Literal["ok"] = "ok"
    rows: list[DailyReportRow]
    totals: DailyReportTotals
