"""Small networking helpers (standard library only)."""
from __future__ import annotations

import socket


def local_ipv4_addresses() -> list[str]:
    """Best-effort list of this PC's non-loopback IPv4 addresses.

    Link-local (169.254.x.x) addresses are kept on purpose: two PCs joined by a
    cable or an ad-hoc link without DHCP use them and can still chat.
    The preferred (default-route) address is listed first.
    """
    found: list[str] = []

    def add(addr: str) -> None:
        if addr and not addr.startswith("127.") and addr != "0.0.0.0" and addr not in found:
            found.append(addr)

    # Default-route address. connect() on UDP sends no packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(str(info[4][0]))
    except OSError:
        pass
    return found


def primary_ipv4() -> str | None:
    addrs = local_ipv4_addresses()
    return addrs[0] if addrs else None
