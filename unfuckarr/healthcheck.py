"""Container healthcheck: ``python -m unfuckarr.healthcheck``.

Probes the address the server actually bound, not 127.0.0.1 — with
``UNFUCKARR_BIND_INTERFACE`` set, nothing listens on loopback and a
hard-coded localhost probe reports a healthy server as dead.
"""

from __future__ import annotations

import os
import sys
import urllib.request

from unfuckarr.__main__ import resolve_host


def target_url() -> str:
    # Don't sit out the bind-wait: if the interface has no address now, the
    # server cannot be listening on it either, and failing fast is the point.
    os.environ["UNFUCKARR_BIND_WAIT"] = "0"
    host = resolve_host()
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = os.environ.get("UNFUCKARR_PORT", "6969")
    return f"http://{host}:{port}/health"


def main() -> None:
    try:
        ok = urllib.request.urlopen(target_url(), timeout=4).status == 200
    except Exception:
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
