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
    r = client.post("/api/v1/partners/PRT-2026-0187/compliance-checks", json=body, headers=idem())
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


def test_requeue_needs_admin_role_and_only_works_on_failed(client, settings, sf, clients):
    client.post("/api/v1/amp/expenditure-reports", json=report("R-REJ", "AMP-1-REJECT"), headers=idem(1))
    client.post("/api/v1/nimes/indicator-submissions", json=indicators("N-DOWN", "IND-1-DOWN"), headers=idem(2))
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    for _ in range(settings.max_attempts):
        with sf() as s:
            process_due(s, settings, clients, now)
        now += timedelta(hours=2)
    assert client.get("/api/v1/submissions/2", headers=auth()).json()["status"] == "FAILED"
    admin = {"roles": ["compliance-admin"]}
    url = "/api/v1/submissions/2/requeue"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=auth()).status_code == 403
    assert client.post(url, headers=auth(["other"])).status_code == 403
    assert client.post("/api/v1/submissions/1/requeue", headers=auth(admin["roles"])).status_code == 409  # REJECTED is final
    assert client.post("/api/v1/submissions/99/requeue", headers=auth(admin["roles"])).status_code == 404
    r = client.post(url, headers=auth(admin["roles"]))
    assert r.status_code == 200 and r.json()["status"] == "QUEUED"
    assert client.post(url, headers=auth(admin["roles"])).status_code == 409  # no longer FAILED
    with sf() as s:  # still down upstream: it retries with a fresh budget rather than failing at once
        assert process_due(s, settings, clients, now + timedelta(hours=5)) == {"retry": 1}


ADMIN = ["compliance-admin"]


def idem_for(n, **kw):
    return {**auth(**kw), "Idempotency-Key": f"idem-key-{n:04d}"}


def test_partner_can_only_check_itself_but_admin_can_check_any(client):
    other = idem_for(1, partner="PRT-OTHER")
    assert client.post("/api/v1/partners/PRT-2026-0187/compliance-checks", json=check_body(), headers=other).status_code == 403
    assert run(client, 2).status_code == 200  # its own partner id
    no_partner = idem_for(3, partner=None)
    assert client.post("/api/v1/partners/PRT-2026-0187/compliance-checks", json=check_body(), headers=no_partner).status_code == 403
    admin = idem_for(4, roles=ADMIN, partner=None)
    assert client.post("/api/v1/partners/PRT-ANY/compliance-checks", json=check_body(), headers=admin).status_code == 200


def test_submissions_belong_to_the_submitting_partner(client):
    mine, other = idem_for(1), idem_for(2, partner="PRT-OTHER")
    # a partner cannot file report lines for someone else
    foreign = report(); foreign["lines"][0]["partnerId"] = "PRT-OTHER"
    assert client.post("/api/v1/amp/expenditure-reports", json=foreign, headers=mine).status_code == 403
    r = client.post("/api/v1/amp/expenditure-reports", json=report(), headers=mine)
    n = client.post("/api/v1/nimes/indicator-submissions", json=indicators(), headers=idem_for(5))
    assert r.status_code == n.status_code == 202
    sid, nid = r.json()["submissionId"], n.json()["submissionId"]
    # tokens without partner_id cannot submit; admins can
    assert client.post("/api/v1/nimes/indicator-submissions", json=indicators("NIM-X"), headers=idem_for(6, partner=None)).status_code == 403
    assert client.post("/api/v1/nimes/indicator-submissions", json=indicators("NIM-ADM"), headers=idem_for(7, roles=ADMIN, partner=None)).status_code == 202
    # another partner sees them as missing, and cannot take over the same id (no leak of whose it is)
    for i in (sid, nid):
        assert client.get(f"/api/v1/submissions/{i}", headers=auth(partner="PRT-OTHER")).status_code == 404
        assert client.get(f"/api/v1/submissions/{i}", headers=auth(partner=None)).status_code == 404
        assert client.get(f"/api/v1/submissions/{i}", headers=auth()).status_code == 200
        assert client.get(f"/api/v1/submissions/{i}", headers=auth(ADMIN, partner=None)).status_code == 200
    shared = report("EXP-SHARED"); shared["lines"][0]["partnerId"] = None  # identical content, no line owner
    assert client.post("/api/v1/amp/expenditure-reports", json=shared, headers=mine).status_code == 202
    assert client.post("/api/v1/amp/expenditure-reports", json=shared, headers=other).status_code == 409  # same body, other partner
    assert client.post("/api/v1/nimes/indicator-submissions", json=indicators(), headers=idem_for(8, partner="PRT-OTHER")).status_code == 409
    assert client.post("/api/v1/amp/expenditure-reports", json=report(), headers=idem_for(9)).status_code == 200  # own replay still fine


def test_legacy_submission_without_owner_is_admin_only(client, sf):
    from rpcms_compliance.models import Submission
    with sf() as s:
        s.add(Submission(target="NIMES", client_ref="OLD", idempotency_key="idem-key-old1", fingerprint="x", payload={}, partner_id=None))
        s.commit()
    assert client.get("/api/v1/submissions/1", headers=auth()).status_code == 404
    assert client.get("/api/v1/submissions/1", headers=auth(ADMIN, partner=None)).status_code == 200
