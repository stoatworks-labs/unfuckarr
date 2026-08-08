#!/usr/bin/env python3
"""Stub Sonarr, Radarr and Emby, so the UI can be worked on — and screenshotted
— without three real servers.

Deliberately a real HTTP server rather than faked application state: it answers
the same endpoints the clients actually call, so the connection panel going
green means the client code genuinely worked, not that a flag was set.

    python scripts/demo_services.py &        # listens on 8989
    UNFUCKARR_CONFIG_DIR=./demo python scripts/seed_demo.py --with-services

It answers only the handshake endpoints (`system/status`, `rootfolder`,
`System/Info`, `Users`, `Items`). It is NOT a library fixture — the file
inventory comes from the seed script, and nothing here is a substitute for
testing against a real *arr.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8989

ROUTES = {
    "/api/v3/system/status": {
        "version": "4.0.15.2941", "instanceName": "Sonarr", "appName": "Sonarr",
    },
    "/api/v3/rootfolder": [
        {"id": 1, "path": "/media/tv", "accessible": True},
    ],
    "/System/Info": {
        "Version": "4.9.1.2", "ServerName": "emby", "Id": "demo",
    },
    "/Users": [
        {"Id": "demo-admin", "Name": "allan", "Policy": {"IsAdministrator": True}},
    ],
    "/Items": {"Items": [], "TotalRecordCount": 0},
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = self.path.split("?")[0]
        # Radarr and Emby are served from the same process on the same port;
        # the path is what distinguishes them.
        if path in ROUTES:
            payload = ROUTES[path]
            if path == "/api/v3/system/status" and "radarr" in self.headers.get(
                    "X-Api-Key", ""):
                payload = {**payload, "instanceName": "Radarr", "appName": "Radarr",
                           "version": "5.14.0.9383"}
            self._send(payload)
            return
        self._send({"error": "not stubbed", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        self._send({})

    def log_message(self, *args) -> None:
        pass  # quiet


if __name__ == "__main__":
    print(f"stub Sonarr/Radarr/Emby on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
