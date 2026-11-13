import time

import jwt
import pytest
from fastapi.testclient import TestClient

from rpcms_compliance.api import create_app, load_clients, make_session_factory
from rpcms_compliance.config import Settings

KEY = "test-signing-key-0123456789-abcdef"


@pytest.fixture
def settings():
    return Settings(jwt_key=KEY, database_url="sqlite:///:memory:")


@pytest.fixture
def sf(settings):
    return make_session_factory(settings)


@pytest.fixture
def clients(settings):
    return load_clients(settings)


@pytest.fixture
def client(settings, sf, clients):
    return TestClient(create_app(settings, clients, sf))


def auth(roles=None):
    claims = {"sub": "off-1", "exp": int(time.time()) + 300}
    if roles:
        claims["roles"] = roles
    return {"Authorization": "Bearer " + jwt.encode(claims, KEY, algorithm="HS256")}


def idem(n=1):
    return {**auth(), "Idempotency-Key": f"idem-key-{n:04d}"}


def check_body(**kw):
    return {"ursbRegistrationNo": "URSB-80020001", "ngoBureauPermitNo": "NGO-2026-0042",
            "ampRecordId": "AMP-UG-2026-00731", **kw}


def report(rid="EXP-2026-Q3", ref="AMP-UG-2026-00731"):
    return {"reportId": rid, "period": "2026-Q3", "lines": [
        {"ampRecordId": ref, "partnerId": "PRT-2026-0187", "amount": 1000000, "currency": "UGX"}]}


def indicators(ref="NIM-2026-Q3", code="IND-001"):
    return {"submissionRef": ref, "period": "2026-Q3", "indicators": [{"code": code, "value": 214, "unit": "people"}]}
