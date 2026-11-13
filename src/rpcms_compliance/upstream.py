"""Boundaries to URSB, NGO Bureau, AMP (MoFPED) and NIMES.

The interfaces, access terms and data formats of ALL four are UNCONFIRMED. Implement these protocols
against the confirmed interfaces and point the RPCMS_COMPLIANCE_*_CLIENT_IMPL settings at them.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, runtime_checkable


class UpstreamUnavailable(Exception):
    """The system could not be reached or answered unusably. Never to be read as 'verified'."""


@dataclass(frozen=True)
class SourceVerdict:
    status: str  # VERIFIED | NOT_FOUND | EXPIRED | MISMATCH
    valid_until: date | None = None
    source_reference: str | None = None


@dataclass(frozen=True)
class AmpRecordData:
    amp_record_id: str
    funder: str
    committed_amount: float
    currency: str
    status: str  # e.g. ACTIVE


@dataclass(frozen=True)
class SubmitResult:
    status: str  # ACCEPTED | REJECTED
    remote_reference: str | None = None
    detail: str | None = None


@runtime_checkable
class RegistryClient(Protocol):
    def verify(self, number: str) -> SourceVerdict: ...


@runtime_checkable
class AmpClient(Protocol):
    def get_record(self, amp_record_id: str) -> AmpRecordData | None: ...
    def submit_expenditure(self, report: dict) -> SubmitResult: ...


@runtime_checkable
class NimesClient(Protocol):
    def submit_indicators(self, submission: dict) -> SubmitResult: ...


def _suffix_rules(value: str) -> None:
    if value.upper().endswith("-DOWN"):
        raise UpstreamUnavailable("mock upstream down")


class MockRegistryClient:
    """Rules: suffix -DOWN unavailable, -NOTFOUND not found, -EXPIRED expired (valid_until in the past),
    -MISMATCH mismatch; anything else verified for one more year."""

    def verify(self, number: str) -> SourceVerdict:
        _suffix_rules(number)
        n = number.upper()
        if n.endswith("-NOTFOUND"):
            return SourceVerdict("NOT_FOUND")
        if n.endswith("-EXPIRED"):
            return SourceVerdict("EXPIRED", date.today() - timedelta(days=30), "MOCK-" + n)
        if n.endswith("-MISMATCH"):
            return SourceVerdict("MISMATCH", None, "MOCK-" + n)
        return SourceVerdict("VERIFIED", date.today() + timedelta(days=365), "MOCK-" + n)


class MockAmpClient:
    """-NOTFOUND no record, -DOWN unavailable, -CLOSED closed record; submissions rejected when any line
    has ampRecordId ending -REJECT, unavailable when ending -DOWN."""

    def get_record(self, amp_record_id: str) -> AmpRecordData | None:
        _suffix_rules(amp_record_id)
        n = amp_record_id.upper()
        if n.endswith("-NOTFOUND"):
            return None
        return AmpRecordData(amp_record_id, "MOCK FUNDER", 1_200_000_000.0, "UGX",
                             "CLOSED" if n.endswith("-CLOSED") else "ACTIVE")

    def submit_expenditure(self, report: dict) -> SubmitResult:
        for line in report["lines"]:
            _suffix_rules(line["ampRecordId"])
            if line["ampRecordId"].upper().endswith("-REJECT"):
                return SubmitResult("REJECTED", None, "mock: record rejected")
        return SubmitResult("ACCEPTED", "AMP-SUB-" + secrets.token_hex(4).upper())


class MockNimesClient:
    """Indicator code ending -DOWN unavailable, -REJECT rejected."""

    def submit_indicators(self, submission: dict) -> SubmitResult:
        for i in submission["indicators"]:
            _suffix_rules(i["code"])
            if i["code"].upper().endswith("-REJECT"):
                return SubmitResult("REJECTED", None, "mock: indicator rejected")
        return SubmitResult("ACCEPTED", "NIMES-SUB-" + secrets.token_hex(4).upper())
