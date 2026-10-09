"""LAN transport: UDP discovery + TCP (the only transport that is implemented)."""
from __future__ import annotations

from typing import Callable

from ..discovery import DiscoveryService
from ..netutil import local_ipv4_addresses
from ..networking import LanNode
from .base import NearbyDevice, Transport, TransportKind, TransportStatus


class LanTransport(Transport):
    kind = TransportKind.LAN

    def __init__(
        self,
        node: LanNode,
        discovery: DiscoveryService,
        local_addresses: Callable[[], list[str]] = local_ipv4_addresses,
    ) -> None:
        self.node = node
        self.discovery = discovery
        self._local_addresses = local_addresses
        self._seen_evidence = False  # sticky: something really worked this session

    def start(self) -> None:
        self.discovery.start()

    def stop(self) -> None:
        self.discovery.stop()

    def devices(self) -> list[NearbyDevice]:
        found = [
            NearbyDevice(d.node_id, d.name, d.host, d.port, TransportKind.LAN)
            for d in self.discovery.devices()
        ]
        if found:
            self._seen_evidence = True
        return found

    def connect(self, device: NearbyDevice) -> bool:
        return self.node.connect(device.host, device.port, expected_id=device.device_id)

    def status(self) -> TransportStatus:
        addrs = self._local_addresses()
        if not addrs:
            return TransportStatus(
                self.kind, True, False, False,
                "No LAN connection found",
                "Connect this PC to Wi-Fi or Ethernet (or use a cable/hotspot shared with the other PC). "
                "Manual connect only works once both PCs can reach each other.",
            )
        if any(p.state.value == "connected" for p in self.node.peers()):
            self._seen_evidence = True
        where = f"{addrs[0]}:{self.node.port}"
        if not self.discovery.listening:
            return TransportStatus(
                self.kind, True, True, self._seen_evidence,
                f"LAN ready ({where}) - discovery limited",
                (self.discovery.error or "Discovery socket is not listening.")
                + " You can still connect with the manual IP option.",
            )
        if self._seen_evidence:
            return TransportStatus(self.kind, True, True, True, f"LAN connected ({where})",
                                   "Other LANchat PCs were found or connected on this network.")
        return TransportStatus(
            self.kind, True, True, False, f"LAN ready ({where}) - no other PCs seen yet",
            "If the other PC never appears, check Windows Firewall (see README) and that both PCs "
            "are on the same network. Some Wi-Fi networks block device-to-device traffic.",
        )
