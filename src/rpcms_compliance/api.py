from __future__ import annotations

import importlib

import jwt
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from . import service
from .config import Settings
from .models import Base, Submission
from .schemas import ComplianceCheckRequest, ExpenditureReport, IndicatorSubmission
from .service import ApiError, Clients
from .upstream import AmpClient, NimesClient, RegistryClient, UpstreamUnavailable

PROBLEM = "application/problem+json"


def _problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"type": "about:blank", "title": title, "status": status, "detail": detail},
                        status_code=status, media_type=PROBLEM)


def _load(dotted: str, proto):
    module, _, name = dotted.rpartition(".")
    if not module:
        raise ValueError(f"client impl must be a dotted path, got {dotted!r}")
    obj = getattr(importlib.import_module(module), name)()
    if not isinstance(obj, proto):
        raise TypeError(f"{dotted} does not implement {proto.__name__}")
    return obj


def load_clients(s: Settings) -> Clients:
    return Clients(_load(s.ursb_client_impl, RegistryClient), _load(s.ngo_client_impl, RegistryClient),
                   _load(s.amp_client_impl, AmpClient), _load(s.nimes_client_impl, NimesClient))


def make_session_factory(settings: Settings):
    kwargs = {}
    if settings.database_url.startswith("sqlite") and ":memory:" in settings.database_url:
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(settings.database_url, **kwargs)
    Base.metadata.create_all(engine)  # no migrations yet: see README
    return sessionmaker(engine, expire_on_commit=False)


def create_app(settings: Settings | None = None, clients: Clients | None = None, session_factory=None) -> FastAPI:
    settings = settings or Settings()
    clients = clients or load_clients(settings)
    Session_ = session_factory or make_session_factory(settings)
    app = FastAPI(title="RPCMS compliance adapter", version="0.1.0")

    def db():
        with Session_() as s:
            yield s

    def officer(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise ApiError(401, "Missing bearer token")
        try:
            c = jwt.decode(authorization[7:].strip(), settings.jwt_key, algorithms=[settings.jwt_algorithm],
                           audience=settings.jwt_audience,
                           options={"require": ["sub", "exp"], "verify_aud": settings.jwt_audience is not None})
        except jwt.PyJWTError:
            raise ApiError(401, "Invalid token")
        return c["sub"]

    def idem(idempotency_key: str | None = Header(default=None)) -> str:
        if not idempotency_key or not (8 <= len(idempotency_key) <= 128):
            raise ApiError(400, "Invalid request", "Idempotency-Key header (8-128 chars) is required")
        return idempotency_key

    @app.exception_handler(ApiError)
    async def _e(_: Request, e: ApiError):
        return _problem(e.status, e.title, e.detail)

    @app.exception_handler(RequestValidationError)
    async def _v(_: Request, e: RequestValidationError):
        return _problem(400, "Invalid request", "; ".join(f"{'.'.join(map(str, x['loc'][1:]))}: {x['msg']}" for x in e.errors()))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/api/v1/partners/{partner_id}/compliance-checks")
    def check(partner_id: str, body: ComplianceCheckRequest, _: str = Depends(officer), key: str = Depends(idem),
              s: Session = Depends(db)):
        return service.run_check(s, clients, partner_id, key, body)

    @app.get("/api/v1/amp/records/{amp_record_id}")
    def amp_record(amp_record_id: str, _: str = Depends(officer)):
        try:
            rec = clients.amp.get_record(amp_record_id)
        except UpstreamUnavailable:
            raise ApiError(502, "AMP unavailable")
        if rec is None:
            raise ApiError(404, "AMP record not found")
        return {"ampRecordId": rec.amp_record_id, "funder": rec.funder, "committedAmount": rec.committed_amount,
                "currency": rec.currency, "status": rec.status}

    def _submit(target, ref, body, response, key, s):
        row, created = service.enqueue(s, target, ref, key, body)
        response.status_code = 202 if created else 200
        return service.submission_view(row)

    @app.post("/api/v1/amp/expenditure-reports", status_code=202)
    def expenditure(body: ExpenditureReport, response: Response, _: str = Depends(officer), key: str = Depends(idem),
                    s: Session = Depends(db)):
        return _submit("AMP", body.reportId, body, response, key, s)

    @app.post("/api/v1/nimes/indicator-submissions", status_code=202)
    def indicators(body: IndicatorSubmission, response: Response, _: str = Depends(officer), key: str = Depends(idem),
                   s: Session = Depends(db)):
        return _submit("NIMES", body.submissionRef, body, response, key, s)

    @app.get("/api/v1/submissions/{submission_id}")
    def get_submission(submission_id: str, _: str = Depends(officer), s: Session = Depends(db)):
        row = s.get(Submission, int(submission_id)) if submission_id.isdigit() else None
        if row is None:
            raise ApiError(404, "Submission not found")
        return service.submission_view(row)

    return app
