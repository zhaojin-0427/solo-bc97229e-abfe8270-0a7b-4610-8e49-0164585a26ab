"""Request/response models for the scan-time normalization API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


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
