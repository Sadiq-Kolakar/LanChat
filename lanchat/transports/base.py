"""Transport adapter interface.

The UI and chat logic talk to a :class:`ConnectionManager`, which talks to
``Transport`` adapters. A transport knows how to (1) find nearby devices and
(2) open a byte stream to one of them. Everything above that (handshake,
accept/reject, chat, groups) is transport independent.

Honesty rule: a transport that is not implemented must say so in ``status()``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class TransportKind(str, Enum):
    LAN = "LAN"
    BLUETOOTH = "Bluetooth"
    WIFI_DIRECT = "Wi-Fi Direct"


@dataclass(frozen=True)
class NearbyDevice:
    device_id: str
    name: str
    host: str
    port: int
    transport: TransportKind


@dataclass(frozen=True)
class TransportStatus:
    kind: TransportKind
    implemented: bool        # is there working code for this transport at all?
    available: bool          # can it be used right now on this PC / network?
    verified: bool           # has it actually produced a device or connection this session?
    summary: str             # short, user-facing
    detail: str = ""         # longer explanation / troubleshooting


class Transport(ABC):
    kind: TransportKind

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def status(self) -> TransportStatus: ...

    @abstractmethod
    def devices(self) -> list[NearbyDevice]: ...

    @abstractmethod
    def connect(self, device: NearbyDevice) -> bool:
        """Begin a connection request. Progress/results arrive as node events."""
