"""In-process pub/sub feeding the SSE stream (/api/events). Notifications only; SQLite is the truth."""

import asyncio
import json
import logging
from collections.abc import Callable

_subscribers: set[asyncio.Queue] = set()
_listeners: dict[object, tuple[object, Callable[[str, dict], None]]] = {}
_logger = logging.getLogger(__name__)


def add_listener(callback: Callable[[str, dict], None], *, source: object) -> Callable[[], None]:
    """Observe one application instance's publications, matched by source identity."""
    if source is None:
        raise ValueError("Event listeners require an explicit non-None source")
    key = object()
    _listeners[key] = (source, callback)

    def unsubscribe() -> None:
        _listeners.pop(key, None)

    return unsubscribe


def publish(event: str, data: dict, *, source: object | None = None) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    for q in list(_subscribers):
        q.put_nowait(msg)
    for listener_source, callback in list(_listeners.values()):
        if listener_source is source:
            try:
                callback(event, data)
            except Exception:
                # Payloads and exception messages can contain private operational data.
                _logger.warning("Operational event listener failed; publication continued.")


async def subscribe():
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.add(q)
    try:
        yield "event: hello\ndata: {}\n\n"
        while True:
            try:
                yield await asyncio.wait_for(q.get(), timeout=15)
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(q)
