#!/bin/sh
set -e

case "$1" in
  api)
    alembic upgrade head
    python -m app.bootstrap
    exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
      --proxy-headers --forwarded-allow-ips='*'
    ;;
  worker)
    shift
    exec python -m app.worker "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
