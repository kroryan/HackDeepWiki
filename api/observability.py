"""Small dependency-free runtime metrics for the single-process application."""

from __future__ import annotations

import threading
from collections import Counter
from time import monotonic
from typing import Any

_LOCK = threading.Lock()
_STARTED = monotonic()
_REQUESTS: Counter[tuple[str, str, int]] = Counter()
_REQUEST_SECONDS: Counter[tuple[str, str]] = Counter()


def record_http_request(
    method: str,
    route: str,
    status: int,
    duration_seconds: float,
) -> None:
    key = (method.upper(), route, int(status))
    duration_key = (method.upper(), route)
    with _LOCK:
        _REQUESTS[key] += 1
        _REQUEST_SECONDS[duration_key] += max(0.0, duration_seconds)


def metrics_snapshot() -> dict[str, Any]:
    with _LOCK:
        requests = [
            {
                "method": method,
                "route": route,
                "status": status,
                "count": count,
            }
            for (method, route, status), count in sorted(_REQUESTS.items())
        ]
        durations = [
            {
                "method": method,
                "route": route,
                "seconds": round(seconds, 6),
            }
            for (method, route), seconds in sorted(_REQUEST_SECONDS.items())
        ]
    return {
        "uptime_seconds": round(monotonic() - _STARTED, 3),
        "http_requests": requests,
        "http_request_seconds": durations,
    }
