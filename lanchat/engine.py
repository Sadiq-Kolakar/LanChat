"""Engine: wires node + discovery + transports together for the UI.

The UI only needs ``engine.events`` (a ``queue.Queue`` it can poll from the Tk
thread) and the ``node`` / ``manager`` methods.
"""
from __future__ import annotations

import queue
from typing import Optional

from .discovery import DISCOVERY_PORT, DiscoveryService
from .networking import Event, LanNode
from .settings import Settings
from .transports import BluetoothTransport, ConnectionManager, LanTransport, WifiDirectTransport


class Engine:
    def __init__(
        self,
        settings: Settings,
        *,
        discovery_port: int = DISCOVERY_PORT,
        bind_host: str = "0.0.0.0",
        discovery_bind_host: str = "",
        discovery_targets: Optional[list[tuple[str, int]]] = None,
    ) -> None:
        self.settings = settings.validated()
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.node = LanNode(self.settings.username, host=bind_host, port=self.settings.tcp_port,
                            on_event=self.events.put)
        self.discovery = DiscoveryService(
            self.node.node_id, self.settings.username, 0,
            discovery_port=discovery_port, bind_host=discovery_bind_host,
            targets=discovery_targets, visible=self.settings.discoverable,
            on_change=lambda: self.events.put(Event("devices_changed")),
        )
        self.lan = LanTransport(self.node, self.discovery)
        self.manager = ConnectionManager([self.lan, BluetoothTransport(), WifiDirectTransport()])
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        port = self.node.start()
        self.discovery._tcp_port = port  # announce the port we really got
        self.manager.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.manager.stop()
        self.node.stop()

    def apply_settings(self, new: Settings) -> None:
        new = new.validated()
        if new.username != self.node.name:
            self.node.set_name(new.username)
            self.discovery.set_name(self.node.name)
        self.discovery.set_visible(new.discoverable)
        self.settings = new
