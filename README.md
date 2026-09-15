# Stockroom

A warehouse operations API and console: stock moves in and out under a lock that
cannot go negative, purchases carry a price-change trail, reimbursements run
through an approval chain, and every state change lands in an audit log.

![Dashboard](docs/screenshots/dashboard.png)
![Login](docs/screenshots/login.png)
![Issue stock](docs/screenshots/issue-stock.png)
![Procurement](docs/screenshots/procurement.png)
![Audit log](docs/screenshots/audit-log.png)

```mermaid
flowchart LR
    B["Next.js console<br/>3 roles"] -->|"Bearer JWT"| A["FastAPI<br/>19 endpoints"]
    A -->|"verify RS256 via JWKS"| C["AWS Cognito"]
    A -->|"SELECT … FOR UPDATE"| D[("PostgreSQL<br/>8 tables")]
    A -->|"read-through cache"| R[("Redis")]
    A -->|"presigned PUT/GET"| S["S3 receipts"]
    A -->|"publish on commit"| M[("MongoDB<br/>audit events")]
    D -.->|"writes invalidate"| R
    A -->|"read-only tools"| AG["Procurement agent<br/>proposes only"]
    AG -.->|"proposals for approval"| B
    SUP["Supplier systems"] -->|"HMAC-signed POST"| G["API Gateway"]
    G --> L2["Lambda<br/>price webhook"]
    L2 -->|"park submission"| S
    S -->|"ObjectCreated"| L1["Lambda<br/>receipt validator"]
    L1 -.->|"tags verdict"| S
```

| Layer | Built with |
|---|---|
| API | Python, FastAPI, SQLAlchemy, Alembic |
| Data | PostgreSQL (8 tables), MongoDB audit events, Redis cache |
| Auth | AWS Cognito JWT — RS256 via JWKS, groups mapped to 3 roles |
| Console | Next.js, TypeScript, React |
| Serverless | AWS Lambda (receipt validation, price webhook), API Gateway |
| Agent | Tool-using procurement agent, provider-agnostic (Gemini free tier or local Ollama) |
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

**Receipts never pass through the API — so something else has to check them.**
The client asks for a presigned S3 URL and uploads directly; the key is
namespaced per user and ownership is checked server-side before it can be
attached to anything. But presign can only validate what the client *declares*,
and the bytes go straight from the browser to S3, so nothing on the server ever
sees them. A Lambda closes that gap: `ObjectCreated` triggers it, it reads the
first bytes, compares the real file signature against the declared content
type, and tags the verdict on the object. The API then refuses to attach a
receipt that failed — and answers 409 rather than rejecting one that has not
been scanned yet, because validation is asynchronous.
[`handler.py`](backend/lambdas/receipt_validator/handler.py)

**The procurement agent can propose, and only propose.** Ask it what to
reorder and it works through a fixed set of tools — what is short, what an item
has cost, who supplies it — then drafts specific purchases with a reason for
each. It cannot place one. Every tool is a read except `propose_purchase`,
which validates a proposal against real SKUs and suppliers and hands it back;
approving it is an ordinary authenticated call made by a person. The
quantities and supplier rankings are not the model's either: the tools call the
same replenishment engine the console uses, so the arithmetic stays in code
where it is tested and the model decides only what to look at and how to
explain it. The loop caps iterations, refuses tools outside the allowlist, and
writes the whole run — every call, its arguments, the proposals — to the audit
trail as one event.

The provider sits behind a one-method interface, so the test suite drives the
real loop and real tools with a scripted model: no API key, no network, no
cost, and the same result every run. A free Gemini tier or a local Ollama slots
in behind the same interface.
[`agent/`](backend/app/agent)

**Suppliers can push prices without an account, and without a route inward.**
Price rises are only noticed today when someone records a purchase. A supplier
can now POST a quote to an API Gateway endpoint, authenticated the way webhooks
normally are — HMAC-SHA256 over the raw body with a shared secret, and the
timestamp inside the signed material so a captured request cannot be replayed
with a fresh header. The function behind it does not touch the database: it
verifies, validates, and parks the submission in S3. A webhook has to answer
inside the caller's timeout and must not lose a submission because our backend
is down, and a route reachable from the whole internet is the last thing that
should also hold a path to private data. `POST /api/supplier-prices/ingest`
then drains that inbox, compares each quote against what the item actually
last cost, and raises a price alert when the rise clears the threshold an
administrator configured.
[`handler.py`](backend/lambdas/supplier_price_webhook/handler.py)

**Audit is stored twice, on purpose.** The relational `audit_logs` row is
written inside the same transaction as the change, so it is the system of
record and cannot go missing. But every action flattens into one `detail`
sentence, which makes "every purchase that rose more than 20%" unanswerable.
The same event is therefore also published as a document whose payload keeps
the fields that action actually has — a recipient and a quantity for a stock
issue, a unit cost and a price delta for a purchase. Events are buffered and
flushed on `after_commit`, so a rolled-back write publishes nothing, and an
unreachable MongoDB degrades to an in-process buffer instead of failing the
write. [`audit_events.py`](backend/app/audit_events.py)

## Tests

264 tests, 95% statement coverage, with an 80% floor enforced in CI.

```
Name                                         Stmts   Miss  Cover
-----------------------------------------------------------------
app/agent/llm.py                                85      0   100%
app/agent/loop.py                               53      0   100%
app/agent/tools.py                              50      0   100%
app/services.py                                233      0   100%
app/schemas.py                                 162      0   100%
app/models.py                                    99      0   100%
app/config.py                                    27      0   100%
lambdas/receipt_validator/handler.py            51      1    98%
lambdas/supplier_price_webhook/handler.py       67      2    97%
app/cache.py                                   141      7    95%
app/audit_events.py                             96      8    92%
app/main.py                                    172     23    87%
app/auth.py                                      75     16    79%
app/db.py                                        13      4    69%
-----------------------------------------------------------------
TOTAL                                         1327     61    95%
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
a live Redis, and a pass of the audit-event tests against a live MongoDB —
the in-memory stand-ins only approximate those query engines, so the real ones
are exercised rather than assumed.

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

The agent is off unless a provider is configured — `/api/agent/replenishment`
answers 503 rather than failing at request time. To switch it on, add one of
these to `backend/.env`:

```bash
AGENT_PROVIDER=gemini          # free tier, needs a key but no card
GEMINI_API_KEY=...
AGENT_MODEL=gemini-2.0-flash

AGENT_PROVIDER=ollama          # local, no account at all
AGENT_MODEL=llama3.1
```

The test suite needs neither: it runs the agent against a scripted model.

`backend/.env` is optional for the local demo. Copy
[`backend/.env.example`](backend/.env.example) to `backend/.env` only when
testing a Cognito user pool, S3 receipts, or other local AWS overrides.

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
