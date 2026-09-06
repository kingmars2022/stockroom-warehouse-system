# Stockroom API

FastAPI service backing the Stockroom warehouse operations system.

## Requirements

**Python 3.10 or newer.** The ORM models use PEP 604 unions (`Mapped[str | None]`)
in annotations that SQLAlchemy evaluates at runtime, so 3.9 and older fail at
import. 3.12 is what CI runs.

## Local setup

```bash
python3.12 -m venv .venv          # or any python >= 3.10
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

## Tests

```bash
pytest                            # coverage gate is set in pytest.ini
```

The suite runs entirely against an in-memory SQLite database and the in-process
cache fallback, so neither PostgreSQL nor Redis is required.

To exercise the Redis code path against a real server:

```bash
docker compose up -d redis        # from the repository root
REDIS_URL=redis://localhost:6379/0 pytest tests/test_cache.py --no-cov -q
```

## Pinning

`requirements.txt` carries ranges; `requirements.lock.txt` carries the exact
resolved versions from a known-good install. Regenerate the lock after changing
a dependency:

```bash
pip freeze --exclude-editable > requirements.lock.txt
```
