"""Entry point: ``python -m unfuckarr``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "unfuckarr.api:app",
        host=os.environ.get("UNFUCKARR_HOST", "0.0.0.0"),
        port=int(os.environ.get("UNFUCKARR_PORT", "6969")),
        log_level=os.environ.get("UNFUCKARR_LOG_LEVEL", "info").lower(),
        # One worker only: the scan lock, watch observers and job state all
        # live in this process. A second worker would run a second scanner.
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
