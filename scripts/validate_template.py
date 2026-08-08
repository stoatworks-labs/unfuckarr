#!/usr/bin/env python3
"""Validate the Unraid Community Applications template.

CA fails quietly: a template with a malformed category token is accepted and
simply listed uncategorised, and a missing required element drops the app from
the feed with no error anyone sees. So this runs in CI.

Category tokens are the trap. A subcategory takes NO trailing colon —
``Tools:Utilities:`` is not a token and is discarded, while ``Tools:`` (bare
top level) is fine. ``MediaApp:Video Tools:Utilities`` is a space-separated
list of two valid tokens.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "unraid" / "unfuckarr.xml"

REQUIRED = ["Name", "Repository", "Registry", "Overview", "Category", "WebUI", "Icon"]
TOP_LEVEL = {"Backup", "Cloud", "Downloaders", "GameServers", "HomeAutomation",
             "MediaApp", "MediaServer", "Network", "Other", "Productivity",
             "Security", "Tools"}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if not TEMPLATE.exists():
        fail(f"{TEMPLATE} is missing")
    root = ET.parse(TEMPLATE).getroot()

    if root.tag != "Container":
        fail(f"root element is <{root.tag}>, expected <Container>")
    if root.get("version") != "2":
        fail("Container version must be 2")

    missing = [t for t in REQUIRED if root.find(t) is None]
    if missing:
        fail(f"missing required elements: {', '.join(missing)}")

    if root.find("Support") is None and root.find("Project") is None:
        fail("one of <Support> or <Project> is required")

    for token in (root.find("Category").text or "").split():
        head, _, tail = token.partition(":")
        if head not in TOP_LEVEL:
            fail(f"category token {token!r} has an unknown top level {head!r}")
        if tail.endswith(":"):
            fail(f"category token {token!r} has a trailing colon on its "
                 "subcategory — CA drops it and the app lists uncategorised")
        if not tail and not token.endswith(":"):
            fail(f"bare top-level token {token!r} needs a trailing colon")

    registry = (root.find("Registry").text or "")
    repo = (root.find("Repository").text or "").split(":")[0]
    if not registry.rstrip("/").endswith(repo.split("/", 1)[-1]):
        fail(f"<Registry> must be the per-image URL for {repo}, not the "
             f"organisation packages page (got {registry})")

    ports = [c for c in root.findall("Config") if c.get("Type") == "Port"]
    if not ports:
        fail("no Port config — the WebUI would be unreachable")
    web = root.find("WebUI").text or ""
    for p in ports:
        if f"[PORT:{p.get('Target')}]" not in web:
            fail(f"<WebUI> does not reference the container port {p.get('Target')}")

    for c in root.findall("Config"):
        for attr in ("Name", "Target", "Type"):
            if not c.get(attr):
                fail(f"a <Config> is missing {attr}")

    print(f"template OK — {root.find('Name').text}, "
          f"category {root.find('Category').text!r}, "
          f"{len(root.findall('Config'))} config entries")


if __name__ == "__main__":
    main()
