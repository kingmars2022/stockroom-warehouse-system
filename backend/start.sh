#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  alembic upgrade head
fi

# A host that assigns the port passes it as $PORT (Render, Fly, Cloud Run).
# Nothing sets it in compose, so it falls back to the port the Dockerfile
# exposes and the compose mapping publishes. Worth knowing: compose also reads
# backend/.env, so a PORT left in there moves the listener out from under the
# 8000:8000 mapping and the API looks dead from the host.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
