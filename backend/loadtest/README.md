# Load testing

Measures real throughput and latency under concurrent load by running the
actual application (`app.main:app`, unmodified) against a real, disposable
PostgreSQL + Redis, with only Cognito verification swapped for a test header
(see `server.py`). Every route, query, cache call, and business rule is the
real one -- this is not a mock.

Not run in CI: it needs a human to size the result against the target
deployment, and a sustained 30-second run isn't something to run on every
push. Run it by hand when capacity is in question (e.g. before claiming a
concurrent-user number).

## Run it

```bash
# 1. A disposable Postgres + Redis (never point this at a real database)
createdb -U stockroom stockroom_loadtest   # or an equivalent docker/local instance

# 2. Migrate and seed
cd backend
DATABASE_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_loadtest alembic upgrade head
DATABASE_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_loadtest python loadtest/seed.py --users 50

# 3. Boot the real app against the seeded database (separate terminal)
DATABASE_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_loadtest \
REDIS_URL=redis://localhost:6379/0 \
    uvicorn loadtest.server:app --port 8000

# 4. Drive it
python loadtest/client.py --duration 30
```

## Results (measured 2026-09-09)

50 concurrent virtual users, 30-second run, ~85% reads / ~15% writes
(browse inventory/activity/expenses; occasionally issue stock or submit an
expense), single `uvicorn` process, no `--workers` flag, local PostgreSQL +
Redis of a size comparable to the `db.t4g.micro` RDS instance this repo's
Terraform provisions:

| Metric | Value |
| --- | --- |
| Total requests | 4,906 |
| Throughput | 163.5 req/s |
| Errors | 0 |
| Latency p50 | 170.5 ms |
| Latency p95 | 272.7 ms |
| Latency p99 | 344.0 ms |
| Latency max | 510.6 ms |

Data integrity held under load: both seeded items decremented by exactly
the issued quantity with no negative balance, and each employee's
`/api/movements` and `/api/expenses` responses contained only their own
records -- the row lock and the role-scoping held under concurrency, not
just correctness in isolation.

These numbers are for this workload, on this hardware, on this date --
re-run the script rather than treating them as a permanent SLA. The
takeaway that should generalize: a single, unscaled process comfortably
clears 50 concurrent users of this workload with no errors and sub-500ms
worst-case latency, well before anything here needs the Kubernetes HPA
or App Runner's own scaling to kick in.
