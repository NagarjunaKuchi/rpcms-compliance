from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .models import ComplianceCheck, Submission
from .schemas import ComplianceCheckRequest
from .upstream import AmpClient, NimesClient, RegistryClient, UpstreamUnavailable


class ApiError(Exception):
    def __init__(self, status: int, title: str, detail: str = ""):
        super().__init__(title)
        self.status, self.title, self.detail = status, title, detail


@dataclass
class Clients:
    ursb: RegistryClient
    ngo: RegistryClient
    amp: AmpClient
    nimes: NimesClient


def fingerprint(model) -> str:
    return hashlib.sha256(json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _result(source: str, status: str, checked_at: datetime, valid_until: date | None = None, ref: str | None = None) -> dict:
    return {"source": source, "status": status, "validUntil": valid_until.isoformat() if valid_until else None,
            "sourceReference": ref, "checkedAt": checked_at.isoformat()}


def _registry(source: str, client: RegistryClient, number: str, now: datetime) -> dict:
    try:
        v = client.verify(number)
    except UpstreamUnavailable:
        return _result(source, "UNAVAILABLE", now)
    status = v.status
    # Do not trust a VERIFIED answer whose validity has already ended.
    if status == "VERIFIED" and v.valid_until is not None and v.valid_until < now.date():
        status = "EXPIRED"
    return _result(source, status, now, v.valid_until, v.source_reference)


def _amp(client: AmpClient, amp_id: str, now: datetime) -> dict:
    try:
        rec = client.get_record(amp_id)
    except UpstreamUnavailable:
        return _result("AMP", "UNAVAILABLE", now)
    if rec is None:
        return _result("AMP", "NOT_FOUND", now)
    return _result("AMP", "VERIFIED" if rec.status.upper() == "ACTIVE" else "EXPIRED", now, None, rec.amp_record_id)


def overall(results: list[dict]) -> str:
    """A definite failure beats an outage; an outage never counts as compliant."""
    statuses = [r["status"] for r in results]
    if any(s in ("NOT_FOUND", "EXPIRED", "MISMATCH") for s in statuses):
        return "NON_COMPLIANT"
    if any(s == "UNAVAILABLE" for s in statuses):
        return "INCOMPLETE"
    return "COMPLIANT"


def run_check(session: Session, clients: Clients, partner_id: str, key: str, req: ComplianceCheckRequest,
              now: datetime | None = None) -> dict:
    """Idempotent per (partner, key). A stored INCOMPLETE result is re-run on retry, so an outage is
    never pinned as the answer; definite results are replayed unchanged."""
    now = now or datetime.now(timezone.utc)
    fp = fingerprint(req)
    row = session.scalar(select(ComplianceCheck).where(ComplianceCheck.partner_id == partner_id,
                                                       ComplianceCheck.idempotency_key == key))
    if row is not None:
        if row.fingerprint != fp:
            raise ApiError(409, "Idempotency-Key reused with a different request")
        if row.overall != "INCOMPLETE":
            return view(row)
    results = [_registry("URSB", clients.ursb, req.ursbRegistrationNo, now),
               _registry("NGO_BUREAU", clients.ngo, req.ngoBureauPermitNo, now)]
    if req.ampRecordId:
        results.append(_amp(clients.amp, req.ampRecordId, now))
    if row is None:
        row = ComplianceCheck(partner_id=partner_id, idempotency_key=key, fingerprint=fp, overall="", results=[])
        session.add(row)
    row.overall, row.results = overall(results), results
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError(409, "Concurrent request with the same Idempotency-Key; retry")
    return view(row)


def view(row: ComplianceCheck) -> dict:
    return {"partnerId": row.partner_id, "overall": row.overall, "results": row.results}


def enqueue(session: Session, target: str, client_ref: str, key: str, model, now: datetime | None = None) -> tuple[Submission, bool]:
    fp = fingerprint(model)
    row = session.scalar(select(Submission).where(Submission.target == target, Submission.client_ref == client_ref))
    if row is not None:
        if row.fingerprint != fp:
            raise ApiError(409, "Submission id reused with different content")
        return row, False
    row = Submission(target=target, client_ref=client_ref, idempotency_key=key, fingerprint=fp,
                     payload=model.model_dump(mode="json"), next_attempt_at=now or datetime.now(timezone.utc))
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        row = session.scalar(select(Submission).where(Submission.target == target, Submission.client_ref == client_ref))
        if row is None or row.fingerprint != fp:
            raise ApiError(409, "Submission id reused with different content")
        return row, False
    return row, True


def submission_view(s: Submission) -> dict:
    return {"submissionId": str(s.id), "target": s.target, "status": s.status,
            "remoteReference": s.remote_reference, "detail": s.detail}


def process_due(session: Session, settings: Settings, clients: Clients, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    due = session.scalars(select(Submission).where(Submission.status == "QUEUED", Submission.next_attempt_at <= now)
                          .order_by(Submission.id).limit(settings.worker_batch_size)).all()
    counts: dict[str, int] = {}
    for sub in due:
        try:
            res = (clients.amp.submit_expenditure(sub.payload) if sub.target == "AMP"
                   else clients.nimes.submit_indicators(sub.payload))
        except UpstreamUnavailable:
            sub.attempts += 1
            sub.detail = "upstream unavailable"
            if sub.attempts >= settings.max_attempts:
                sub.status = "FAILED"
            else:
                sub.next_attempt_at = now + timedelta(seconds=min(settings.backoff_base_seconds * 2 ** (sub.attempts - 1),
                                                                  settings.backoff_max_seconds))
            key = "FAILED" if sub.status == "FAILED" else "retry"
        else:
            sub.attempts += 1
            sub.status, sub.remote_reference, sub.detail = res.status, res.remote_reference, res.detail
            key = res.status
        counts[key] = counts.get(key, 0) + 1
        session.commit()
    return counts
