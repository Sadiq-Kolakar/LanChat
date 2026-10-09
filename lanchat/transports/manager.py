"""ConnectionManager: chooses a transport and reports the truth about each one."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .base import NearbyDevice, Transport, TransportKind, TransportStatus


@dataclass(frozen=True)
class Selection:
    transport: Optional[Transport]
    method: str                          # e.g. "LAN" or "None"
    message: str                         # user-facing explanation
    others: tuple[TransportStatus, ...]  # status of every non-selected transport


class ConnectionManager:
    """Order of preference = order of ``transports`` (LAN first)."""

    def __init__(self, transports: Sequence[Transport]) -> None:
        self.transports = list(transports)

    def start(self) -> None:
        for t in self.transports:
            t.start()

    def stop(self) -> None:
        for t in self.transports:
            t.stop()

    def statuses(self) -> list[TransportStatus]:
        return [t.status() for t in self.transports]

    def get(self, kind: TransportKind) -> Optional[Transport]:
        return next((t for t in self.transports if t.kind == kind), None)

    def select(self) -> Selection:
        """Pick the first transport that is implemented AND available now.

        Verified LAN is naturally preferred (it is first). If nothing is usable we
        say so and list the nearby options with their real status.
        """
        statuses = [(t, t.status()) for t in self.transports]
        for t, s in statuses:
            if s.implemented and s.available:
                others = tuple(x for _, x in statuses if x.kind != s.kind)
                return Selection(t, s.kind.value, s.summary, others)
        lines = [f"{s.kind.value}: {s.summary}" for _, s in statuses]
        return Selection(None, "None",
                         "No usable connection. " + " | ".join(lines),
                         tuple(s for _, s in statuses))

    def devices(self) -> list[NearbyDevice]:
        out: list[NearbyDevice] = []
        for t in self.transports:
            s = t.status()
            if s.implemented and s.available:
                out.extend(t.devices())
        return out

    def connect(self, device: NearbyDevice) -> tuple[bool, str]:
        """Start a connection using the transport the device was found on."""
        t = self.get(device.transport)
        if t is None:
            return False, f"No {device.transport.value} transport is installed."
        s = t.status()
        if not s.implemented:
            return False, f"{device.transport.value} is not implemented in this version."
        if not s.available:
            return False, f"{device.transport.value} is not available: {s.summary}"
        return (True, "") if t.connect(device) else (False, "Could not start the connection.")
