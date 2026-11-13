from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import KEY, auth, check_body, idem, indicators, report
from rpcms_compliance.api import create_app, load_clients, make_session_factory
from rpcms_compliance.config import Settings
from rpcms_compliance.service import process_due
from rpcms_compliance.upstream import (MockAmpClient, MockNimesClient, MockRegistryClient, RegistryClient,
                                       SourceVerdict, UpstreamUnavailable)


def run(client, n=1, partner="PRT-2026-0187", **kw):
    return client.post(f"/api/v1/partners/{partner}/compliance-checks", json=check_body(**kw), headers=idem(n))


def by_source(r):
    return {x["source"]: x["status"] for x in r.json()["results"]}


def test_all_sources_verified_is_compliant(client):
    r = run(client)
    assert r.status_code == 200 and r.json()["overall"] == "COMPLIANT"
    assert by_source(r) == {"URSB": "VERIFIED", "NGO_BUREAU": "VERIFIED", "AMP": "VERIFIED"}


def test_amp_is_optional(client):
    body = check_body()
    del body["ampRecordId"]
    r = client.post("/api/v1/partners/P1/compliance-checks", json=body, headers=idem())
    assert set(by_source(r)) == {"URSB", "NGO_BUREAU"} and r.json()["overall"] == "COMPLIANT"


@pytest.mark.parametrize("field,value,source,status", [
    ("ursbRegistrationNo", "URSB-1-NOTFOUND", "URSB", "NOT_FOUND"),
    ("ngoBureauPermitNo", "NGO-1-EXPIRED", "NGO_BUREAU", "EXPIRED"),
    ("ngoBureauPermitNo", "NGO-1-MISMATCH", "NGO_BUREAU", "MISMATCH"),
    ("ampRecordId", "AMP-1-NOTFOUND", "AMP", "NOT_FOUND"),
    ("ampRecordId", "AMP-1-CLOSED", "AMP", "EXPIRED"),
])
def test_definite_failures_are_non_compliant(client, field, value, source, status):
    r = run(client, **{field: value})
    assert r.json()["overall"] == "NON_COMPLIANT" and by_source(r)[source] == status


def test_outage_is_incomplete_never_compliant_and_failure_beats_outage(client):
    r = run(client, ursbRegistrationNo="URSB-1-DOWN")
    assert r.json()["overall"] == "INCOMPLETE" and by_source(r)["URSB"] == "UNAVAILABLE"
    both = run(client, n=2, ursbRegistrationNo="URSB-1-DOWN", ngoBureauPermitNo="NGO-1-NOTFOUND")
    assert both.json()["overall"] == "NON_COMPLIANT"


def test_verified_with_past_validity_is_downgraded_to_expired(settings, sf):
    class Stale:
        def verify(self, number):
            return SourceVerdict("VERIFIED", (datetime.now(timezone.utc) - timedelta(days=2)).date(), "R1")

    clients = load_clients(settings)
    clients.ursb = Stale()
    assert isinstance(clients.ursb, RegistryClient)
    c = TestClient(create_app(settings, clients, sf))
    r = run(c)
    assert by_source(r)["URSB"] == "EXPIRED" and r.json()["overall"] == "NON_COMPLIANT"


def test_check_idempotency_replays_definite_but_reruns_incomplete(settings, sf):
    calls = {"n": 0, "down": True}

    class Flaky:
        def verify(self, number):
            calls["n"] += 1
            if calls["down"]:
                raise UpstreamUnavailable("down")
            return SourceVerdict("VERIFIED", None, "R")

    clients = load_clients(settings)
    clients.ursb = Flaky()
    c = TestClient(create_app(settings, clients, sf))
    assert run(c).json()["overall"] == "INCOMPLETE"
    calls["down"] = False
    assert run(c).json()["overall"] == "COMPLIANT"  # same key re-checked after the outage
    n = calls["n"]
    assert run(c).json()["overall"] == "COMPLIANT" and calls["n"] == n  # definite result replayed
    assert run(c, ngoBureauPermitNo="NGO-OTHER").status_code == 409  # same key, different request


def test_auth_and_validation(client):
    assert client.post("/api/v1/partners/P/compliance-checks", json=check_body()).status_code == 401
    assert client.post("/api/v1/partners/P/compliance-checks", json=check_body(), headers=auth()).status_code == 400
    assert client.post("/api/v1/partners/P/compliance-checks", json={"ursbRegistrationNo": "x"}, headers=idem()).status_code == 400
    assert client.post("/api/v1/partners/P/compliance-checks", json=check_body(extra=1), headers=idem()).status_code == 400


def test_amp_record_lookup(client):
    r = client.get("/api/v1/amp/records/AMP-UG-2026-00731", headers=auth())
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE" and r.json()["currency"] == "UGX"
    assert client.get("/api/v1/amp/records/AMP-1-NOTFOUND", headers=auth()).status_code == 404
    assert client.get("/api/v1/amp/records/AMP-1-DOWN", headers=auth()).status_code == 502


def test_submissions_queue_dedupe_and_process(client, settings, sf, clients):
    r = client.post("/api/v1/amp/expenditure-reports", json=report(), headers=idem(1))
    assert r.status_code == 202 and r.json()["status"] == "QUEUED"
    again = client.post("/api/v1/amp/expenditure-reports", json=report(), headers=idem(2))
    assert again.status_code == 200 and again.json()["submissionId"] == r.json()["submissionId"]
    changed = report(); changed["lines"][0]["amount"] = 1
    assert client.post("/api/v1/amp/expenditure-reports", json=changed, headers=idem(3)).status_code == 409
    n = client.post("/api/v1/nimes/indicator-submissions", json=indicators(), headers=idem(4))
    assert n.status_code == 202 and n.json()["target"] == "NIMES"
    with sf() as s:
        assert process_due(s, settings, clients, datetime.now(timezone.utc) + timedelta(seconds=1)) == {"ACCEPTED": 2}
    done = client.get(f"/api/v1/submissions/{r.json()['submissionId']}", headers=auth()).json()
    assert done["status"] == "ACCEPTED" and done["remoteReference"].startswith("AMP-SUB-")
    assert client.get("/api/v1/submissions/999", headers=auth()).status_code == 404
    assert client.get("/api/v1/submissions/abc", headers=auth()).status_code == 404


def test_submission_rejected_and_retry_then_failed(client, settings, sf, clients):
    client.post("/api/v1/amp/expenditure-reports", json=report("R-REJ", "AMP-1-REJECT"), headers=idem(1))
    client.post("/api/v1/nimes/indicator-submissions", json=indicators("N-DOWN", "IND-1-DOWN"), headers=idem(2))
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    with sf() as s:
        assert process_due(s, settings, clients, now) == {"REJECTED": 1, "retry": 1}
        assert process_due(s, settings, clients, now) == {}  # backing off
    for _ in range(settings.max_attempts - 1):
        now += timedelta(hours=2)
        with sf() as s:
            counts = process_due(s, settings, clients, now)
    assert counts == {"FAILED": 1}
    assert client.get("/api/v1/submissions/2", headers=auth()).json()["status"] == "FAILED"
    assert client.get("/api/v1/submissions/1", headers=auth()).json()["status"] == "REJECTED"


def test_submission_validation(client):
    bad = report(); bad["period"] = "2026-Q5"
    assert client.post("/api/v1/amp/expenditure-reports", json=bad, headers=idem()).status_code == 400
    neg = report(); neg["lines"][0]["amount"] = -1
    assert client.post("/api/v1/amp/expenditure-reports", json=neg, headers=idem()).status_code == 400
    assert client.post("/api/v1/nimes/indicator-submissions", json={**indicators(), "indicators": []}, headers=idem()).status_code == 400


def test_load_clients_rejects_bad_impl():
    with pytest.raises(ValueError):
        load_clients(Settings(jwt_key=KEY, ursb_client_impl="nodots"))
    with pytest.raises(TypeError):
        load_clients(Settings(jwt_key=KEY, amp_client_impl="collections.OrderedDict"))
    assert isinstance(MockAmpClient(), type(load_clients(Settings(jwt_key=KEY)).amp))
    assert isinstance(MockNimesClient(), type(load_clients(Settings(jwt_key=KEY)).nimes))
    assert isinstance(MockRegistryClient(), RegistryClient)
