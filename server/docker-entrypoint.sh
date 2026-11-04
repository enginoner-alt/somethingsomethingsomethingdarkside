#!/bin/sh
set -e

case "$1" in
  api)
    # The database is a separate stack (it may start later or live on another
    # machine), so wait for it instead of relying on compose ordering.
    python - <<'PY'
import os, sys, time
import sqlalchemy as sa
url = os.environ.get("DATABASE_URL", "")
engine = sa.create_engine(url)
for attempt in range(60):
    try:
        with engine.connect():
            sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"waiting for database ({exc.__class__.__name__})", flush=True)
        time.sleep(2)
print("database not reachable after 120 s", file=sys.stderr)
sys.exit(1)
PY
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
