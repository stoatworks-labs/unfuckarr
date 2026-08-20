"""Holding our own share of the GPU's encode engine to a target.

The point is to be able to leave shrinking running permanently without it
being the reason a film stutters. An encode that takes twice as long but never
competes with Emby is strictly better than one that finishes sooner and makes
the server feel broken.

**How the share is measured.** Linux exposes per-process DRM engine accounting
in ``/proc/<pid>/fdinfo/*``: for amdgpu, ``drm-engine-enc`` is a cumulative
count of nanoseconds that process has spent on the video *encode* engine.
Sampled over a wall-clock interval it is a direct percentage — measured on the
target hardware (Radeon 880M, gfx1150), a 4K HEVC encode running flat out
reports **958 ms of engine time per wall-second**, i.e. ~96%.

**Why not the obvious metrics.** ``gpu_busy_percent`` in sysfs and ``VCN Load``
in debugfs both read **0** on this GPU during a saturating 4K encode — the
first tracks the graphics engine rather than the video one, and debugfs is not
mounted in a container anyway. fdinfo is the only thing that works, and it has
the considerable advantage of being per-process and readable without any
privilege, because the process being measured is our own child.

**How the share is held.** SIGSTOP and SIGCONT. Measured on the same hardware:
stopped, the encode engine goes to a true 0 ms/s and the process sits in state
``T``; resumed, it returns to 966 ms/s and the finished file is valid. So the
controller runs the encoder for a fraction of each period and stops it for the
rest, correcting the fraction from what the counter actually reports rather
than assuming the relationship is linear.

What this cannot do is measure *other* processes' encode usage — Emby is in a
different PID namespace. It does not need to: holding our own share at 50%
leaves the other 50% for whoever wants it, which is the guarantee that was
asked for.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# One wall-second of engine time. The counter is in nanoseconds, so a process
# with sole use of the engine approaches this per second of wall clock.
NS_PER_SECOND = 1_000_000_000

# How often the controller re-measures and re-applies. Long enough that a
# stop/start pair is not itself a cost, short enough to react inside a minute.
PERIOD = 2.0

# Below this measured share, assume there is no GPU encode happening at all —
# a software encode, or a build with no engine accounting — and get out of the
# way entirely rather than throttling something this cannot see.
IDLE_SHARE = 0.02


def engine_ns(pid: int, engine: str = "enc") -> int | None:
    """Cumulative nanoseconds ``pid`` has spent on a DRM engine.

    Returns None when the process has no DRM fd yet (VAAPI opens the render
    node a moment after start) or the kernel does not report the counter.

    Several fds can refer to the same DRM client and each reports the same
    cumulative figure, so they are summed per client id rather than per fd —
    adding them up naively double-counts and reads as several hundred percent.
    """
    key = f"drm-engine-{engine}:"
    per_client: dict[str, int] = {}
    try:
        entries = os.listdir(f"/proc/{pid}/fdinfo")
    except OSError:
        return None

    for name in entries:
        client, value = None, None
        try:
            with open(f"/proc/{pid}/fdinfo/{name}") as fh:
                for line in fh:
                    if line.startswith("drm-client-id:"):
                        client = line.split(":", 1)[1].strip()
                    elif line.startswith(key):
                        value = int(line.split(":", 1)[1].strip().split()[0])
        except (OSError, ValueError):
            continue
        if value is not None:
            per_client[client or name] = value

    if not per_client:
        return None
    return sum(per_client.values())


@dataclass
class Governor:
    """Holds one process's encode-engine share near ``target``.

    ``target`` is a fraction: 0.5 means "use about half the encode engine, and
    leave the other half alone". 0 or >= 1 disables throttling entirely.
    """

    target: float = 0.5
    period: float = PERIOD
    _on_fraction: float = field(default=1.0, init=False)
    _measured: float = field(default=0.0, init=False)
    _inert: bool = field(default=False, init=False)
    _samples: int = field(default=0, init=False)

    @property
    def measured_share(self) -> float:
        """The last measured share of the encode engine, 0..1."""
        return self._measured

    @property
    def active(self) -> bool:
        """False when there is nothing here to govern."""
        return not self._inert and 0 < self.target < 1

    def run(self, pid: int, stop: threading.Event) -> None:
        """Govern ``pid`` until ``stop`` is set or the process goes away.

        Always leaves the process running: a job that is cancelled or fails
        while stopped would otherwise be left in state T for ever, holding a
        semaphore and looking like a hang.
        """
        try:
            self._loop(pid, stop)
        finally:
            _resume(pid)

    def _loop(self, pid: int, stop: threading.Event) -> None:
        if not 0 < self.target < 1:
            return
        # The first period runs flat out, deliberately. It establishes whether
        # this job touches the encode engine at all — a software encode never
        # will, and throttling it to half speed for a GPU it is not using
        # would be pure loss.
        self._on_fraction = 1.0

        while not stop.is_set():
            before = engine_ns(pid)
            started = time.monotonic()

            on = max(0.0, min(1.0, self._on_fraction)) * self.period
            if on > 0 and not _sleep_while_running(pid, on, stop):
                return
            off = self.period - on
            if off > 0.01:
                if not _pause(pid):
                    return
                if stop.wait(off):
                    _resume(pid)
                    return
                if not _resume(pid):
                    return

            after = engine_ns(pid)
            elapsed = time.monotonic() - started
            if before is None or after is None or elapsed <= 0:
                # No counter yet. Keep running flat out rather than throttling
                # on the basis of a number that does not exist.
                self._samples += 1
                if self._samples > 5:
                    self._inert = True
                    return
                continue

            self._measured = (after - before) / (elapsed * NS_PER_SECOND)
            self._samples += 1

            if self._samples >= 2 and self._measured < IDLE_SHARE \
                    and self._on_fraction >= 0.99:
                # Running flat out and still not touching the encode engine:
                # this is not a GPU encode. Stop interfering.
                self._inert = True
                return

            self._correct()

    def _correct(self) -> None:
        """Nudge the on-fraction towards the measured target.

        Proportional, and damped: the relationship between how long the
        process is allowed to run and how much engine time it gets is close to
        linear but not exactly, and overshooting produces a visible stutter of
        its own.
        """
        if self._measured <= 0:
            self._on_fraction = min(1.0, self._on_fraction * 1.5)
            return
        ideal = self._on_fraction * (self.target / self._measured)
        self._on_fraction = max(0.05, min(1.0,
                                          self._on_fraction + 0.5 * (ideal - self._on_fraction)))


def _sleep_while_running(pid: int, seconds: float, stop: threading.Event) -> bool:
    if stop.wait(seconds):
        return False
    return _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pause(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGSTOP)
        return True
    except OSError:
        return False


def _resume(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGCONT)
        return True
    except OSError:
        return False
