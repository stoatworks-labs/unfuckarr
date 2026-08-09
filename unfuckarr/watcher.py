"""Watch folders — the live import gate.

A file that has just landed is the cheapest one to reject: nothing has imported
it yet, and the *arr will simply grab another release. The whole difficulty is
knowing when it has *finished* landing. A file still being copied, or an
unpacking rar, is indistinguishable from a truncated download if you probe it
too early — so every event starts a settle timer that is reset by further
writes, and the probe only runs once the size has held steady.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from . import db
from .config import VIDEO_EXTENSIONS, Settings, WatchFolder
from .state import bus, state
from .transcode import is_temp_output

log = logging.getLogger(__name__)

# Downloaders write to these while a transfer is in flight.
PARTIAL_SUFFIXES = {".part", ".!qb", ".tmp", ".partial", ".crdownload", ".rar", ".r00"}


class _Handler(FileSystemEventHandler):
    def __init__(self, folder: WatchFolder, touch: Callable[[str], None]):
        self.folder = folder
        self.touch = touch

    def _consider(self, path: str) -> None:
        p = Path(path)
        if p.suffix.lower() in PARTIAL_SUFFIXES:
            return
        if p.suffix.lower() not in VIDEO_EXTENSIONS:
            return
        if p.name.startswith("."):
            return
        # A watch folder over the library sees our own transcode output being
        # written; settling on it would start a second job racing the first.
        if is_temp_output(p.name):
            return
        self.touch(str(p))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # The rename off a .part suffix is the interesting event, and it is the
        # destination that matters.
        dest = getattr(event, "dest_path", None)
        if dest and not event.is_directory:
            self._consider(str(dest))


class WatchManager:
    """Owns the observers and the settle timer.

    ``on_ready`` is called with a path once it has stopped changing. It runs on
    the settle thread, so it should hand off rather than block for hours.
    """

    def __init__(self, settings_getter: Callable[[], Settings],
                 on_ready: Callable[[str, WatchFolder], None]):
        self._settings = settings_getter
        self._on_ready = on_ready
        self._observer: Any = None
        self._pending: dict[str, tuple[float, int, WatchFolder]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.stop()
        self._stop.clear()
        folders = [f for f in self._settings().watch_folders if f.enabled]
        active: list[str] = []
        if folders:
            # A network share (SMB/NFS) delivers no inotify events, and an
            # Unraid user share is exactly that. Poll unless every folder is
            # demonstrably local.
            self._observer = PollingObserver(timeout=5) if self._needs_polling(folders) \
                else Observer()
            for f in folders:
                if not os.path.isdir(f.path):
                    db.log("watch_folder_missing", "warn", f.path)
                    continue
                self._observer.schedule(
                    _Handler(f, lambda p, fol=f: self._touch(p, fol)),
                    f.path, recursive=f.recursive,
                )
                active.append(f.path)
            if active:
                self._observer.start()
        state.watchers = active
        bus.publish("watchers", active)
        if active:
            db.log("watch_started", "info", detail={"folders": active})

        self._thread = threading.Thread(target=self._settle_loop, daemon=True,
                                        name="unfuckarr-settle")
        self._thread.start()

    @staticmethod
    def _needs_polling(folders: list[WatchFolder]) -> bool:
        """Detect a network mount by asking what filesystem the path is on.

        Cheap and best-effort — if we cannot tell, poll, because a missed
        event is a silent failure and polling merely costs a stat.
        """
        override = os.environ.get("UNFUCKARR_WATCH_POLL")
        if override is not None:
            return override.lower() not in ("0", "false", "no")
        try:
            with open("/proc/mounts") as fh:
                mounts = [line.split()[1:3] for line in fh if len(line.split()) > 2]
        except OSError:
            return True
        network = {"nfs", "nfs4", "cifs", "smbfs", "fuse.sshfs", "9p", "fuse.rclone"}
        for f in folders:
            best = ""
            fstype = ""
            for point, typ in mounts:
                if f.path.startswith(point) and len(point) > len(best):
                    best, fstype = point, typ
            if fstype in network or not best:
                return True
        return False

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except RuntimeError:
                pass
            self._observer = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=6)
        self._thread = None
        with self._lock:
            self._pending.clear()
        state.watchers = []

    # -- settle -----------------------------------------------------------

    def _touch(self, path: str, folder: WatchFolder) -> None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        with self._lock:
            self._pending[path] = (time.time(), size, folder)

    def _settle_loop(self) -> None:
        while not self._stop.wait(2.0):
            now = time.time()
            ready: list[tuple[str, WatchFolder]] = []
            with self._lock:
                for path, (last, size, folder) in list(self._pending.items()):
                    try:
                        current = os.path.getsize(path)
                    except OSError:
                        # Vanished mid-copy, or moved on by the downloader.
                        self._pending.pop(path, None)
                        continue
                    if current != size:
                        # Still growing — restart the clock.
                        self._pending[path] = (now, current, folder)
                        continue
                    if now - last >= folder.settle_seconds:
                        self._pending.pop(path, None)
                        ready.append((path, folder))
            for path, folder in ready:
                try:
                    db.log("watch_settled", "info", path)
                    self._on_ready(path, folder)
                except Exception:  # noqa: BLE001 - one bad file must not stop the watcher
                    log.exception("watch handler failed for %s", path)

    @property
    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"path": p, "since": t, "size": s,
                 "settle_seconds": f.settle_seconds}
                for p, (t, s, f) in self._pending.items()
            ]
