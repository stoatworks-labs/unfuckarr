"""Live application state and the event bus the web UI subscribes to.

The scan and transcode workers are threads; the web layer is async. Events
cross that boundary through ``asyncio.run_coroutine_threadsafe`` onto the loop
captured at startup, so a worker thread can publish without owning a loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CurrentTask:
    """What the app is doing right now — the headline on the dashboard."""

    kind: str = "idle"          # idle | scanning | probing | transcoding | ...
    path: str = ""
    title: str = ""
    detail: str = ""
    progress: float = 0.0       # 0..1, -1 when indeterminate
    started: float = 0.0
    eta: float | None = None


@dataclass
class ScanProgress:
    running: bool = False
    scan_id: int | None = None
    trigger: str = ""
    total: int = 0
    checked: int = 0
    ok: int = 0
    failed: int = 0
    actions: int = 0
    started: float = 0.0
    current: str = ""
    aborted: str | None = None


@dataclass
class AppState:
    started: float = field(default_factory=time.time)
    scan: ScanProgress = field(default_factory=ScanProgress)
    tasks: dict[str, CurrentTask] = field(default_factory=dict)
    watchers: list[str] = field(default_factory=list)
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_scan_finished: float | None = None
    next_scan_at: float | None = None
    paused: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "uptime": time.time() - self.started,
            "scan": asdict(self.scan),
            "tasks": {k: asdict(v) for k, v in self.tasks.items()},
            "watchers": list(self.watchers),
            "services": dict(self.services),
            "last_scan_finished": self.last_scan_finished,
            "next_scan_at": self.next_scan_at,
            "paused": self.paused,
        }


class EventBus:
    """Fan-out to SSE subscribers. Slow clients are dropped, not waited on."""

    def __init__(self, queue_size: int = 100):
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._queue_size = queue_size

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, data: Any = None) -> None:
        """Safe to call from any thread."""
        payload = {"event": event, "data": data, "ts": time.time()}
        with self._lock:
            subs = list(self._subs)
        if not subs:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if threading.current_thread() is threading.main_thread():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._deliver(subs, payload)
                return
        loop.call_soon_threadsafe(self._deliver, subs, payload)

    @staticmethod
    def _deliver(subs: list[asyncio.Queue], payload: dict[str, Any]) -> None:
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A browser tab that stopped reading must not stall a scan.
                pass


state = AppState()
bus = EventBus()


def set_task(key: str, **kw: Any) -> None:
    task = state.tasks.get(key) or CurrentTask(started=time.time())
    for k, v in kw.items():
        setattr(task, k, v)
    state.tasks[key] = task
    bus.publish("task", {"key": key, "task": asdict(task)})


def clear_task(key: str) -> None:
    state.tasks.pop(key, None)
    bus.publish("task", {"key": key, "task": None})


def publish_scan() -> None:
    bus.publish("scan", asdict(state.scan))
