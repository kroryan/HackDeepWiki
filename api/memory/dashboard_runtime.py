"""Lifecycle adapter for the loopback-only Engraphis dashboard."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class DashboardRuntime:
    server: Any
    thread: threading.Thread
    port: int


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])


def start_dashboard(application: Any) -> DashboardRuntime:
    import uvicorn

    port = _free_loopback_port()
    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        name="engraphis-dashboard",
        daemon=True,
    )
    thread.start()
    return DashboardRuntime(server=server, thread=thread, port=port)


def stop_dashboard(runtime: DashboardRuntime | None) -> None:
    if runtime is None:
        return
    runtime.server.should_exit = True
    if runtime.thread.is_alive():
        runtime.thread.join(timeout=5)
