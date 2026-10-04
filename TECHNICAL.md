# Stockroom: technical overview

[← Back to the project overview](README.md) ·
[Open the live demo](https://stockroom-warehouse-system.vercel.app)

A warehouse operations API and console: stock moves in and out under a lock that
cannot go negative, purchases carry a price-change trail, reimbursements run
through an approval chain, and every state change lands in an audit log.

![Dashboard](docs/screenshots/dashboard.png)
![Login](docs/screenshots/login.png)
![Issue stock](docs/screenshots/issue-stock.png)
![Procurement](docs/screenshots/procurement.png)
![Audit log](docs/screenshots/audit-log.png)
![Dark mode](docs/screenshots/dark-mode.png)

```mermaid
flowchart LR
    B["Next.js console<br/>3 roles"] -->|"Bearer JWT"| A["FastAPI<br/>22 endpoints"]
    A -->|"verify RS256 via JWKS"| C["AWS Cognito"]
    A -->|"SELECT … FOR UPDATE"| D[("PostgreSQL<br/>9 tables")]
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
| Data | PostgreSQL (9 tables), MongoDB audit events, Redis cache |
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

Passing once is not permanent, though, and that is the harder half. The
presigned PUT stays usable for its whole window, so the bytes behind an
already-attached key can be replaced afterwards — and the dangerous
replacement is not one that fails validation but one that *passes*, because a
different, equally valid PDF gets tagged `passed` too and is then served in
place of the document somebody approved. Re-reading the verdict on download
cannot catch that, since there is nothing wrong with the new object. So a
receipt is attached by version rather than by key: the bucket is versioned,
the validator judges and tags the exact version its event names instead of
whatever is current, the version that passed is pinned on the row at attach
time, and downloads are presigned for that version. A replacement becomes a
new version the row does not point at.
[`handler.py`](backend/lambdas/receipt_validator/handler.py)

**The procurement agent can propose, and only propose.** Ask it what to
reorder and it works through a fixed set of tools — what is short, what an item
has cost, who supplies it — then drafts specific purchases with a reason for
each. It cannot place one. Every tool is a read except `propose_purchase`,
which checks a proposal against the engine's own current findings and hands it
back; approving it is an ordinary authenticated call made by a person.
Existence checks are not enough there: a real SKU that is not short, a real
supplier that is paused or has never supplied it, and a quantity with an extra
six zeros are all things a model will produce, so the engine's result is the
authority on all three, and a proposal that departs from its suggested quantity
says so in the response. The loop caps iterations, refuses tools outside the
allowlist, turns a bad argument into a tool error the model can correct rather
than an exception that ends the run, and writes the run — each call with its
arguments, and the proposals — to the audit trail as one event.

The console's Procurement page has a panel for it: ask a question, watch the
tool-call trace, and "Review & approve" opens the ordinary purchase form
pre-filled from the proposal rather than submitting anything on its own.

![Procurement agent proposing purchases](docs/screenshots/agent-proposals.png)
![The purchase form a proposal opens, with the engine's guidance for the chosen item](docs/screenshots/agent-review-approve.png)

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
last cost — only when the currencies match, since subtracting 10 USD from
13.5 CAD produces a 35% "rise" that is an artefact of the exchange rate, not
a real one — and raises a price alert when the rise clears the threshold an
administrator configured. A submission is recorded as consumed in the same
transaction as the alert, so a supplier's retry, a delete that fails after the
commit, or two ingest calls racing the same object cannot apply the same quote
twice. An administrator drains the inbox from the console's Procurement page —
a "Check now" button, not a customer-facing feature — and sees each submission's
outcome as it lands.
[`handler.py`](backend/lambdas/supplier_price_webhook/handler.py)

**Audit is stored twice, on purpose.** The relational `audit_logs` row is
written inside the same transaction as the change, so it is the system of
record and cannot go missing. But every action flattens into one `detail`
sentence, which makes "every purchase that rose more than 20%" unanswerable.
The same event is therefore also published as a document whose payload keeps
the fields that action actually has — a recipient and a quantity for a stock
issue, a unit cost and a price delta for a purchase. The Audit log page has a
search panel above the relational table for exactly this — filter by action,
minimum quantity, or minimum price change, and the matching payload fields
show up as chips instead of a parsed sentence.

![Structured audit search over MongoDB event payloads](docs/screenshots/audit-search.png)

Events are buffered on the session and flushed only when the **outermost**
transaction commits. That distinction is the whole trick: `after_commit` also
fires when a SAVEPOINT is released, and every write here happens inside
`db.begin_nested()`, so publishing on the first signal sends the event while
the outer transaction is still open — and a later failure leaves an event
describing a change that never landed. A MongoDB that is unreachable at
startup *or* fails later degrades to an in-process buffer rather than failing
the write or dropping the event. [`audit_events.py`](backend/app/audit_events.py)

## Tests

334 backend tests at 95% statement coverage, with an 80% floor enforced in
CI, plus 33 browser tests driving the real console in Chromium and one more
against a production build of it.

```
Name                                         Stmts   Miss  Cover
-----------------------------------------------------------------
app/agent/__init__.py                            3      0   100%
app/agent/loop.py                               55      0   100%
app/models.py                                  109      0   100%
app/schemas.py                                 183      0   100%
app/config.py                                   27      0   100%
app/services.py                                243      1    99%
lambdas/receipt_validator/handler.py            59      1    98%
app/agent/llm.py                                97      5    95%
app/cache.py                                   141      7    95%
lambdas/supplier_price_webhook/handler.py       80      4    95%
app/main.py                                    240     15    94%
app/agent/tools.py                              72      5    93%
app/audit_events.py                            117     13    89%
app/auth.py                                     75     16    79%
app/db.py                                       13      4    69%
-----------------------------------------------------------------
TOTAL                                         1514     71    95%
```

Includes a regression suite ([`tests/test_review_regressions.py`](backend/tests/test_review_regressions.py))
for defects an external code review found in the four features above — the
kind that pass because a test checks that a design works *as imagined* rather
than what the mechanism actually does. Kept as its own file because that
pattern is the useful part, not any single fix.

The console has its own suite ([`e2e/`](e2e)), run in Chromium by Playwright
against the dev server, because `lint` and `build` prove it compiles and say
nothing about what it renders. They cover what only a browser can answer: the
storage codes it spells out, the currency it prints, the dark theme's computed
colours, and that a long table renders a page rather than the warehouse.
Playwright starts the dev server itself, since demo mode comes free under
`next dev`.

One test is run against a production build instead
([`playwright.demo.config.ts`](playwright.demo.config.ts)), because the suite
above cannot see the thing a host would actually serve: `NEXT_PUBLIC_*` values
are inlined by `next build`, so whether a build reaches demo mode is decided at
build time. Demo mode was gated on `NODE_ENV` alone until this, which meant
every artifact a host could serve stopped at a login nothing could complete —
the console was only ever runnable from a developer's own machine. It now also
answers to an explicit `NEXT_PUBLIC_DEMO=true`, and the gate keeps its second
half: however the flag is set, demo mode is off the moment a Cognito pool is
configured, so a real deployment cannot be talked into it.

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

Three things CI checks that no test touches. It brings the stack up with
`docker compose up` and waits for `/health`, because the suite installs the
dependencies itself and never runs the container's own entrypoint — the gap
that let a missing `prepend_sys_path` crash-loop the API on a fresh start while
every other job stayed green. It runs the Kubernetes manifests through
kubeconform. And it formats and validates both Terraform stacks, which nothing
else reads: without that a stack could stop parsing and the whole run would
still report success. That last one is deliberately static — `-backend=false`
and `validate` need no credentials, reach no AWS account and create nothing, so
it catches an undefined reference or a mistyped argument, not a bad plan.

Correctness and capacity are different questions — [`backend/loadtest/`](backend/loadtest)
answers the second one by driving the real app with concurrent virtual users
over real HTTP. 50 users, 30 seconds, one unscaled `uvicorn` process:
**163.5 req/s, 0 errors, p95 272.7 ms**, stock and per-user scoping still
correct under the load.

![Load test results](docs/screenshots/loadtest-results.png)

## Behaviour at 10,000 items

The load test answers how many people can use the system at once. How large
the warehouse itself can be is a different question, and the answer was that
three things scaled with the wrong number. Seeded with 10,000 items, 20
suppliers, 50,000 movements and 30,000 purchases:

| | before | after |
|---|---|---|
| Replenishment guidance | 3.3 s, 161 MB | 0.97 s, 29 MB |
| Inventory table | 7406 ms, 130,287 DOM nodes | 151 ms, 840 nodes |
| Typing in the inventory search | 371 ms | 50 ms |
| Opening the item dialog | 2516 ms | 102 ms |
| Picking an item in it | 1101 ms | 80 ms |

**The replenishment engine was linear in history, not in the catalogue.** It
loaded every outbound movement and every purchase ever recorded as ORM
entities and grouped them in Python, so the cost of answering "what should we
reorder" grew with how long the warehouse had been running. A profile put four
fifths of the time inside SQLAlchemy's row hydration — 280,000 UUID
conversions to produce two numbers per item — and almost none of it in the
arithmetic. Demand is now one grouped row per item (`SUM` of what left, `MIN`
of when the first of it did, the 90-day cut applied in SQL) and price history
one row per item/supplier pair, picked by `ROW_NUMBER` over that pair.
Nothing is selected as an entity. Both tables are indexed for the reads that
remain, because fewer rows returned is not fewer rows scanned.

**The inventory table rendered the whole warehouse.** 10,000 rows is 130,287
nodes the browser lays out before the first one is readable, for a screen that
shows a few dozen. Long tables now render a 50-row window with a pager, which
clamps rather than resets when a filter shortens the list.

**The item dialog was a `<select>` with one `<option>` per SKU.** 2.5 seconds
to open, another 1.1 to register a choice — and scrolling 10,000 names for the
one on the shelf is worse than either number. It is a search box over the
catalogue now, showing the eight closest matches and matching the aisle code
too, so a picker can find a row by what is printed on the rack.

### A row names what it is about

A movement, purchase and reimbursement each stored only the item's id, so the
console put names on them by holding the whole catalogue — 2.55 MB of it at
10,000 SKUs — and looking each row up. That is also wrong in the small: a row
about an item outside whatever page the client holds has no name to show. All
three now carry `item_name` and `item_unit`, joined in the same query that
reads the rows, the way they already carried `actor_name`. The demo store runs
the same join over its seed data so one set of components reads both.

Driving the real API over that simulated warehouse, every row comes back named
and each endpoint still costs one query — resolving a name per row would have
traded a catalogue-sized response for a row-sized pile of queries, which is
why there is a test that counts them:

| endpoint | rows | body | queries | named |
|---|---|---|---|---|
| `/api/movements` | 50,000 | 16.00 MB | 1 | 50,000 / 50,000 |
| `/api/purchases` | 30,000 | 12.16 MB | 1 | 30,000 / 30,000 |
| `/api/expenses` | 2,000 | 0.95 MB | 1 | 1,800 / 2,000 |
| `/api/items` | 10,000 | 2.55 MB | 2 | — |
| `/api/items?limit=50` | 50 | 13 KB | 2 | — |

The 1,800 of 2,000 is the point of the nullable column: a reimbursement need
not be for a catalogued item, so that one alone is an outer join.

### History is read a page at a time

The table above made the next step obvious: the 2.55 MB catalogue the console
no longer needs was the smallest number on it. `/api/movements` and
`/api/purchases` both take `limit`/`offset` now, newest first, with the length
of the whole history in `X-Total-Count` — and the rows being self-describing is
what made that possible, since a page of movements can no longer be matched to
a catalogue the console does not hold.

Both order by `created_at DESC, id`. The timestamp alone is a partial order,
and that is not a theoretical problem: a batch of issues recorded in one shift
shares a timestamp to the second, and two pages cut from a partial order can
repeat one row and never show another. The test writes 25 movements with an
identical `created_at` and walks them in pages of ten, which fails without the
tiebreak.

| endpoint | rows | body | queries |
|---|---|---|---|
| `/api/movements` unpaged | 50,000 | 16.00 MB | 2 |
| `/api/movements?limit=50` | 50 | 0.02 MB | 2 |
| `/api/purchases` unpaged | 30,000 | 12.19 MB | 2 |
| `/api/purchases?limit=50` | 50 | 0.02 MB | 2 |
| `/api/price-alerts?limit=50` | 50 | 0.03 MB | 4 |

Signing in went from **31.69 MB to 3.56 MB** against the same simulated
warehouse.

**Paging the purchase list took an answer away from the console**, which is why
`/api/price-alerts` exists. The alerts, and the two alternative prices quoted
beside each one, were worked out by filtering every purchase the console had
been sent. In this dataset that is 10,817 alerts across the database against 19
visible in the fifty most recent purchases — so deriving them from a page would
have quietly turned "price alerts" into "price alerts among the last fifty
purchases", on a number the dashboard prints on a card. The endpoint answers
with the breaching purchases newest first, each carrying the latest price from
every *other* supplier of that item, picked by one `ROW_NUMBER` query for the
whole page rather than one per alert. Its count is taken before the page is
cut, so the card is right whatever the page holds.

### Nothing answers with a whole table

Two things were still reducing over the entire catalogue in the browser, and
both are now the server's job.

**The dashboard's three figures** — SKUs, units on hand, what is below its
minimum — came from looping over every item. `/api/items/summary` answers them
as three aggregates and a short list of the most depleted, ordered by the
shortfall against the minimum rather than by what is left: 2 of a minimum of 50
is more urgent than 2 of 2. `low_stock` is the handful the attention queue
shows; `low_stock_count` is what the card prints, so the two cannot disagree.
The card's own link into the inventory became `/api/items?low_only=true`, since
a filter the console applies to a page it holds is a filter over the wrong set.

**The item picker** filtered an in-memory array. It takes a search *function*
now — `/api/items?search=` against a real API, a memory filter in demo mode —
debounced, with a ticket that discards an answer arriving after a later one so
a slow response to "la" cannot overwrite the results for "label". The chosen
item is kept by the picker and handed back whole, because nothing upstream has
a catalogue to look an id up in; the barcode scanner resolves its SKU the same
way, and so does approving an agent proposal.

With those two done, `/api/expenses`, `/api/audit-logs` and the ranked purchase
plan took the same `limit`/`offset` treatment, and every long table in the
console has a pager. The plan is the one that is cached whole and windowed
afterwards, because it is a ranking and a page of it only means anything cut
from the same ordering. It also needed a second way in: the purchase dialog
shows one item's guidance, and cannot find that line in a page it was not sent,
so `?item_id=` returns the one row. Measuring found that comparison needs to be
made on the text of the id — the cached plan round-trips through JSON and comes
back with string ids while the engine itself yields `uuid.UUID`.

Signing in against the same simulated warehouse:

| | before | after |
|---|---|---|
| `/api/items` | 10,000 rows, 2.55 MB | 50 rows, 0.01 MB + a 0.00 MB summary |
| `/api/movements` | 50,000 rows, 16.00 MB | 50 rows, 0.02 MB |
| `/api/purchases` | 30,000 rows, 12.19 MB | 50 rows, 0.02 MB, + 0.03 MB of alerts |
| `/api/replenishment-recommendations` | 1,741 rows, 1.45 MB | 50 rows, 0.04 MB |
| `/api/expenses` | 2,000 rows, 0.95 MB | 50 rows, 0.02 MB |
| **total** | **33.13 MB** | **151 KB** |

`limit` stays optional on every one of them rather than defaulting to a page.
An unpaged read is still the honest answer for a small warehouse and for
anything scripted against this API; what changed is that nothing in the console
asks for one.

One list is deliberately left whole: `/api/suppliers`. The number of suppliers
a warehouse buys from is bounded by the business rather than by how long the
system has been running, and the Procurement page and the purchase form both
want all of them at once. It is the one place a `<select>` is still the right
control.

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
[`infra/terraform/`](infra/terraform), alongside a
[free-tier stack](infra/terraform/freetier) that stands up only the pieces the
application actually talks to — Cognito, the bucket and the two Lambdas — so
the AWS integration can be proven against AWS and torn down the same
afternoon. Both stacks are format-checked and validated in CI.

## More detail

The full design write-up — business context, workflows, the data model, the API
reference, AWS deployment and troubleshooting — is in
[`docs/design-notes.md`](docs/design-notes.md).

## Scope

A portfolio project, not a deployed product: there are no real users behind it,
and the AWS pieces are described in Terraform rather than left running. The
engineering above is real and the test suite backs it.
