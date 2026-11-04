"""Job handler registry, in its own module so it exists exactly once.

(If it lived in app/worker.py, `python -m app.worker` would load that file
twice, as __main__ and as app.worker, and handlers registered by
app.pipeline would land in the copy the running loop doesn't read.)
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.models import Job

Handler = Callable[[Session, Job], dict[str, Any] | None]
HANDLERS: dict[str, Handler] = {}


def handler(type_: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[type_] = fn
        return fn

    return register
