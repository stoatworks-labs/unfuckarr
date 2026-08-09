"""Entry point: ``python -m unfuckarr``."""

from __future__ import annotations

import os
import socket
import struct
import sys
import time

import uvicorn

# SIOCGIFADDR differs per kernel, but the ifreq layout happens to agree where
# it matters: 16 bytes of interface name, then a sockaddr whose IPv4 address
# sits at bytes 20..24 on both Linux and the BSDs (macOS).
_SIOCGIFADDR = 0xC0206921 if sys.platform == "darwin" else 0x8915


def _interface_ipv4(name: str) -> str | None:
    """The IPv4 address of ``name``, or None if it has none (yet)."""
    try:
        import fcntl
    except ImportError:  # Windows — no ioctl; UNFUCKARR_HOST still works.
        raise SystemExit(
            "UNFUCKARR_BIND_INTERFACE needs Linux or macOS; "
            "set UNFUCKARR_HOST to an address instead."
        )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            packed = fcntl.ioctl(
                s.fileno(), _SIOCGIFADDR, struct.pack("256s", name.encode()[:15])
            )
        except OSError:
            # ENODEV (no such interface) or EADDRNOTAVAIL (up, no IPv4 yet).
            # Both mean "not ready": tailscaled brings tailscale0 up after us.
            return None
        return socket.inet_ntoa(packed[20:24])


def resolve_host() -> str:
    """Address to bind, from UNFUCKARR_BIND_INTERFACE or UNFUCKARR_HOST.

    An interface name wins over UNFUCKARR_HOST, because naming one is the more
    deliberate act. The wait loop exists for exactly one reason: a VPN
    interface (tailscale0) comes up asynchronously, and a service that binds
    0.0.0.0 because it raced the VPN is silently exposed everywhere — failing
    the start is the safe outcome.
    """
    interface = os.environ.get("UNFUCKARR_BIND_INTERFACE", "").strip()
    if not interface:
        return os.environ.get("UNFUCKARR_HOST", "0.0.0.0")

    wait = float(os.environ.get("UNFUCKARR_BIND_WAIT", "60"))
    deadline = time.monotonic() + wait
    warned = False
    while True:
        ip = _interface_ipv4(interface)
        if ip:
            print(f"unfuckarr binding to {interface} ({ip})", flush=True)
            return ip
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"interface {interface!r} had no IPv4 address after {wait:.0f}s. "
                "If this is a VPN interface, the VPN is not up inside this "
                "network namespace (a bridge-mode container cannot see the "
                "host's interfaces). Raise UNFUCKARR_BIND_WAIT, or set "
                "UNFUCKARR_HOST to an address instead."
            )
        if not warned:
            print(f"waiting for interface {interface!r}…", flush=True)
            warned = True
        time.sleep(1)


def main() -> None:
    uvicorn.run(
        "unfuckarr.api:app",
        host=resolve_host(),
        port=int(os.environ.get("UNFUCKARR_PORT", "6969")),
        log_level=os.environ.get("UNFUCKARR_LOG_LEVEL", "info").lower(),
        # One worker only: the scan lock, watch observers and job state all
        # live in this process. A second worker would run a second scanner.
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
