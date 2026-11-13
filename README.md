# rpcms-compliance

RPCMS partner compliance and reporting adapter: verifies a partner against **URSB**, **NGO Bureau** and its **AMP**
funding record, and sends quarterly expenditure to **AMP** and indicators to **NIMES**.
Contract: [`openapi/rpcms-compliance.yaml`](openapi/rpcms-compliance.yaml) (drafted from the RPCMS demo screens only).

**All four upstream systems are behind pluggable clients and only development mocks exist.** Their interfaces,
access terms and data formats are unconfirmed, so nothing here talks to URSB, the NGO Bureau, AMP or NIMES.

## API (officer JWT; `Idempotency-Key` required on POSTs)
| Endpoint | Purpose |
|---|---|
| `POST /api/v1/partners/{partnerId}/compliance-checks` | Check URSB, NGO Bureau and (optionally) an AMP record |
| `GET /api/v1/amp/records/{ampRecordId}` | AMP funding record (404 not found, 502 AMP unavailable) |
| `POST /api/v1/amp/expenditure-reports` | Queue a quarterly expenditure report (202 new, 200 identical resend, 409 same `reportId` with different content) |
| `POST /api/v1/nimes/indicator-submissions` | Queue quarterly indicators (same rules, keyed on `submissionRef`) |
| `GET /api/v1/submissions/{id}` | Status: `QUEUED`, `ACCEPTED`, `REJECTED`, `FAILED` |
| `POST /api/v1/submissions/{id}/requeue` | `FAILED` -> `QUEUED` with a fresh retry budget. Needs the role in `RPCMS_COMPLIANCE_ADMIN_ROLE` (default `compliance-admin`) in the JWT `roles` claim (403 otherwise); `REJECTED` and other states return 409 |

Rules worth knowing:
- `overall` is `COMPLIANT` only if every requested source is `VERIFIED`. A definite failure (`NOT_FOUND`, `EXPIRED`, `MISMATCH`) gives `NON_COMPLIANT` even if another source is down; an outage alone gives `INCOMPLETE`. An unreachable source is never treated as verified.
- A `VERIFIED` answer whose validity date has passed is downgraded to `EXPIRED`. A non-`ACTIVE` AMP record is reported as `EXPIRED`.
- Check idempotency replays definite results, but **re-runs an `INCOMPLETE` result** on the same key, so an outage is never pinned as the answer.
- Submissions go through a database outbox; the worker retries unavailability with backoff (60 s doubling, capped at 1 h, 8 attempts, then `FAILED`). A rejection by the upstream is final.

## Run
```
pip install -e ".[test]"
export RPCMS_COMPLIANCE_JWT_KEY=...     # >=16 chars (32+ for HS256)
# optional: RPCMS_COMPLIANCE_DATABASE_URL, _URSB_CLIENT_IMPL, _NGO_CLIENT_IMPL, _AMP_CLIENT_IMPL, _NIMES_CLIENT_IMPL
uvicorn --factory rpcms_compliance.api:create_app
rpcms-compliance-worker
pytest
```
Mock rules (see `upstream.py`): number/ID suffix `-DOWN` unavailable, `-NOTFOUND`, `-EXPIRED`, `-MISMATCH`; AMP `-CLOSED`; `-REJECT` makes a submission rejected.

## Database migrations
Schema changes are managed with Alembic (`migrations/`, initial revision `0001`). For anything beyond local development:
```
pip install -e ".[postgres]"
export RPCMS_COMPLIANCE_DATABASE_URL=postgresql+psycopg://user:pass@host/db  RPCMS_COMPLIANCE_CREATE_TABLES=false
alembic upgrade head
```
`RPCMS_COMPLIANCE_CREATE_TABLES` defaults to `true` (tables auto-created at startup, for development and tests); set it to `false` where Alembic owns the schema. A test checks that the migrations produce exactly the model schema. To run the suite against a migrated Postgres: `RPCMS_TEST_DATABASE_URL=... pytest` (it truncates the tables between tests, so use a throwaway database).

## Known limits (read before relying on this)
- Mock-only (above). The `MISMATCH` status has no defined meaning until a real registry interface says what is compared.
- Expenditure lines are **not** reconciled against AMP funding records before submission; "matched to AMP records" in the demo is not implemented.
- Period is validated only as `YYYY-Qn`; NIMES indicator codes and units are not validated against any list.
- **Access:** a token acts for the partner in its `partner_id` claim. Compliance checks need the path `{partnerId}` to equal it (403 otherwise); expenditure lines may only name that partner; a submission is readable only by the partner that filed it (anyone else gets 404, as for a missing id) and a submission id already used by another partner returns 409. The role in `RPCMS_COMPLIANCE_ADMIN_ROLE` (default `compliance-admin`) may act for any partner and requeue. Submissions created before ownership existed have no owner and are admin-only. `GET /api/v1/amp/records/{id}` is still open to any valid officer. The `partner_id` and `roles` claim names are assumptions about the token issuer.
- Compliance results are stored as returned, with no freshness policy (how recent a check must be to count).
- Migrations exist for the current schema only (two revisions), verified on PostgreSQL 16 locally (upgrade, downgrade, full test suite) but not on other versions. Not load-tested; requeue is per submission only, and the `partner_id`/`roles` claims are an assumption about the token issuer.
- The 200 response for an identical resend and the 400 for a missing `Idempotency-Key` are additions to the drafted contract.
