# Stockroom

A warehouse operations API and console: stock moves in and out under a lock that
cannot go negative, purchases carry a price-change trail, reimbursements run
through an approval chain, and every state change lands in an audit log.

![Dashboard](docs/screenshots/dashboard.png)

<details>
<summary>More screenshots — login, issuing stock, procurement, audit log</summary>

| | |
|---|---|
| ![Login](docs/screenshots/login.png) | ![Issue stock](docs/screenshots/issue-stock.png) |
| ![Procurement](docs/screenshots/procurement.png) | ![Audit log](docs/screenshots/audit-log.png) |

</details>

```mermaid
flowchart LR
    B["Next.js console<br/>3 roles"] -->|"Bearer JWT"| A["FastAPI<br/>19 endpoints"]
    A -->|"verify RS256 via JWKS"| C["AWS Cognito"]
    A -->|"SELECT … FOR UPDATE"| D[("PostgreSQL<br/>8 tables")]
    A -->|"read-through cache"| R[("Redis")]
    A -->|"presigned PUT/GET"| S["S3 receipts"]
    D -.->|"writes invalidate"| R
```

| Layer | Built with |
|---|---|
| API | Python, FastAPI, SQLAlchemy, Alembic |
| Data | PostgreSQL (8 tables), Redis cache |
| Auth | AWS Cognito JWT — RS256 via JWKS, groups mapped to 3 roles |
| Console | Next.js, TypeScript, React |
| Delivery | Docker Compose, Kubernetes, Terraform, GitHub Actions |

## The parts worth reading

**Stock cannot go negative under concurrency.** Issuing and receiving both take a
row-level `SELECT … FOR UPDATE` on the item inside a transaction, so two
operations touching the same item serialise instead of racing on a read-modify-write.
[`services.py`](backend/app/services.py)

**Replenishment is derived, not guessed.** Daily usage comes from outbound
movement history over a 90-day window, days of cover from stock divided by that
usage, and the suggested order quantity is sized against the slowest supplier's
lead time. Suppliers are ranked by price, lead time and rating; paused suppliers
drop out. Every number the endpoint returns can be traced back to a row.

**The cache degrades instead of failing.** The replenishment endpoint scans every
item, supplier, movement and purchase, so its result is cached and invalidated on
any write that moves stock or changes suppliers. If Redis is unreachable the
service falls back to an in-process cache rather than returning an error —
`/health` reports which backend is live and the current hit rate.
[`cache.py`](backend/app/cache.py)

**Receipts never pass through the API.** The client asks for a presigned S3 URL
and uploads directly; the key is namespaced per user and ownership is checked
server-side before it can be attached to anything.

## Tests

159 tests, 94% statement coverage, with an 80% floor enforced in CI.

```
Name              Stmts   Miss  Cover
-------------------------------------
app/services.py     162      0   100%
app/models.py        99      0   100%
app/schemas.py      140      0   100%
app/config.py        21      0   100%
app/cache.py        141      7    95%
app/main.py         121     20    83%
app/auth.py          75     16    79%
app/db.py            13      4    69%
-------------------------------------
TOTAL               772     47    94%
```

`conftest.py` pins the suite to SQLite so it stays fast and isolated, which
means the `SELECT … FOR UPDATE` lock is never really contended there — SQLite
has no row-level locking to contend. `tests/test_concurrency.py` covers that
gap against a real PostgreSQL and is skipped unless you point it at one:

```bash
POSTGRES_TEST_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_test \
    pytest tests/test_concurrency.py --no-cov
```

CI runs it on every push, along with a second pass of the cache tests against
a live Redis rather than the in-process fallback.

Correctness and capacity are different questions — [`backend/loadtest/`](backend/loadtest)
answers the second one by driving the real app with concurrent virtual users
over real HTTP. 50 users, 30 seconds, one unscaled `uvicorn` process:
**163.5 req/s, 0 errors, p95 272.7 ms**, stock and per-user scoping still
correct under the load.

![Load test results](docs/screenshots/loadtest-results.png)

## Run it

```bash
docker compose up -d          # PostgreSQL + Redis + API on :8000
npm install && npm run dev    # console on :3000
```

Backend tests:

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Kubernetes manifests and their notes are in [`infra/k8s/`](infra/k8s); AWS
resources (RDS, Cognito, the receipt bucket, ECR) are described in
[`infra/terraform/`](infra/terraform).

## More detail

The full design write-up — business context, workflows, the data model, the API
reference, AWS deployment and troubleshooting — is in
[`docs/design-notes.md`](docs/design-notes.md).

## Scope

A portfolio project, not a deployed product: there are no real users behind it,
and the AWS pieces are described in Terraform rather than left running. The
engineering above is real and the test suite backs it.
