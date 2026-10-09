"""Automatic LAN discovery over UDP (broadcast + multicast).

How it works
------------
* Every ``ANNOUNCE_INTERVAL`` seconds each visible LANchat sends a tiny JSON
  packet ("I am <name>, reach me on TCP port <port>") to the broadcast address
  *and* to a multicast group, out of every local network interface.
* Everyone listens on ``DISCOVERY_PORT`` and keeps a table of devices.
* The sender's IP address is taken from the UDP packet itself, never from the
  packet body (so a packet cannot claim to come from another address).
* A device that stays silent for ``STALE_AFTER`` seconds is dropped. A polite
  shutdown sends a "bye" packet so the entry disappears immediately.

Discovery only *finds* PCs. Chatting always needs the TCP connection request
and the other user's acceptance (see ``networking``).
"""
from __future__ import annotations

import json
import logging
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..naming import sanitize_name
from ..netutil import local_ipv4_addresses
from ..protocol import is_node_id

log = logging.getLogger(__name__)

DISCOVERY_PORT = 50501
MULTICAST_GROUP = "239.255.50.50"
ANNOUNCE_INTERVAL = 3.0
STALE_AFTER = 10.0
MAX_PACKET_BYTES = 512
MAX_DEVICES = 256
APP_TAG = "LANchat"
DISCOVERY_VERSION = 1

_IS_WINDOWS = sys.platform.startswith("win")


# --- packet format ------------------------------------------------------------
@dataclass(frozen=True)
class Announcement:
    node_id: str
    name: str
    port: int
    bye: bool = False


def build_announcement(node_id: str, name: str, port: int, bye: bool = False) -> bytes:
    body = {"app": APP_TAG, "dv": DISCOVERY_VERSION, "id": node_id, "name": name, "port": port}
    if bye:
        body["bye"] = True
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_announcement(data: bytes) -> Optional[Announcement]:
    """Parse and filter a discovery datagram. Returns None for anything not ours/valid."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PACKET_BYTES:
        return None
    try:
        obj = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.get("app") != APP_TAG:
        return None
    dv = obj.get("dv")
    if isinstance(dv, bool) or not isinstance(dv, int) or dv < 1 or dv > DISCOVERY_VERSION:
        return None
    node_id, name, port = obj.get("id"), obj.get("name"), obj.get("port")
    if not is_node_id(node_id):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    return Announcement(node_id, sanitize_name(name), port, obj.get("bye") is True)


@dataclass
class Device:
    node_id: str
    name: str
    host: str
    port: int
    last_seen: float


# --- service ------------------------------------------------------------------
class DiscoveryService:
    def __init__(
        self,
        node_id: str,
        name: str,
        tcp_port: int,
        *,
        discovery_port: int = DISCOVERY_PORT,
        targets: Optional[Sequence[tuple[str, int]]] = None,
        bind_host: str = "",
        visible: bool = True,
        announce_interval: float = ANNOUNCE_INTERVAL,
        stale_after: float = STALE_AFTER,
        local_addresses: Callable[[], list[str]] = local_ipv4_addresses,
        on_change: Optional[Callable[[], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.node_id = node_id
        self._name = name
        self._tcp_port = tcp_port
        self.discovery_port = discovery_port
        self._targets = list(targets) if targets is not None else [
            ("255.255.255.255", discovery_port),
            (MULTICAST_GROUP, discovery_port),
        ]
        self._bind_host = bind_host
        self._visible = visible
        self._interval = announce_interval
        self._stale_after = stale_after
        self._local_addresses = local_addresses
        self._on_change = on_change
        self._clock = clock

        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._recv_sock: Optional[socket.socket] = None
        self._senders: dict[str, socket.socket] = {}
        self._joined: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._last_iface_refresh = 0.0
        self.listening = False
        self.error: Optional[str] = None
        self.send_errors = 0

    # -- lifecycle --
    def start(self) -> bool:
        """Start listening and announcing. Never raises; check ``error`` / ``listening``."""
        self._stop.clear()
        try:
            self._open_receiver()
            self.listening = True
        except OSError as exc:
            self.listening = False
            self.error = f"Discovery port {self.discovery_port} unavailable: {exc}"
            log.warning(self.error)
        if self.listening:
            t = threading.Thread(target=self._recv_loop, name="lanchat-discovery-rx", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._announce_loop, name="lanchat-discovery-tx", daemon=True)
        t.start()
        self._threads.append(t)
        return self.listening

    def stop(self) -> None:
        if self._stop.is_set():
            return
        if self._visible:
            self._send(build_announcement(self.node_id, self._name, self._tcp_port, bye=True))
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()
        for s in [self._recv_sock, *self._senders.values()]:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._recv_sock = None
        self._senders.clear()
        self.listening = False

    # -- settings --
    def set_name(self, name: str) -> None:
        self._name = name
        self._wake.set()  # announce immediately

    def set_visible(self, visible: bool) -> None:
        if visible == self._visible:
            return
        self._visible = visible
        if visible:
            self._wake.set()
        else:
            self._send(build_announcement(self.node_id, self._name, self._tcp_port, bye=True))

    @property
    def visible(self) -> bool:
        return self._visible

    # -- device table --
    def devices(self) -> list[Device]:
        """Currently online devices (stale ones are pruned first)."""
        self._prune()
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: (d.name.casefold(), d.node_id))

    def get(self, node_id: str) -> Optional[Device]:
        with self._lock:
            return self._devices.get(node_id)

    def _prune(self, now: Optional[float] = None) -> bool:
        now = self._clock() if now is None else now
        with self._lock:
            stale = [k for k, d in self._devices.items() if now - d.last_seen > self._stale_after]
            for k in stale:
                del self._devices[k]
        if stale:
            self._notify()
        return bool(stale)

    def _notify(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:  # never let a UI callback kill the thread
                log.exception("discovery on_change callback failed")

    def handle_datagram(self, data: bytes, addr: tuple[str, int], now: Optional[float] = None) -> bool:
        """Process one received datagram. Returns True if the device table changed."""
        ann = parse_announcement(data)
        if ann is None or ann.node_id == self.node_id:
            return False
        now = self._clock() if now is None else now
        host = addr[0]
        changed = False
        with self._lock:
            existing = self._devices.get(ann.node_id)
            if ann.bye:
                if existing is not None and existing.host == host:
                    del self._devices[ann.node_id]
                    changed = True
            else:
                if existing is not None and existing.host != host and now - existing.last_seen <= self._stale_after:
                    return False  # first-seen address wins; blocks id hijacking while it is alive
                if existing is None and len(self._devices) >= MAX_DEVICES:
                    return False
                if existing is None:
                    self._devices[ann.node_id] = Device(ann.node_id, ann.name, host, ann.port, now)
                    changed = True
                else:
                    changed = (existing.name, existing.port, existing.host) != (ann.name, ann.port, host)
                    existing.name, existing.port, existing.host, existing.last_seen = ann.name, ann.port, host, now
        if changed:
            self._notify()
        return changed

    # -- sockets --
    def _open_receiver(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if not _IS_WINDOWS and hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # several clients per PC
            sock.bind((self._bind_host, self.discovery_port))
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            raise
        self._recv_sock = sock
        if self._bind_host in ("", "0.0.0.0"):
            self._join_multicast(self._local_addresses())

    def _join_multicast(self, addresses: list[str]) -> None:
        if self._recv_sock is None:
            return
        for iface in addresses or ["0.0.0.0"]:
            if iface in self._joined:
                continue
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP), socket.inet_aton(iface))
                self._recv_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self._joined.add(iface)
            except OSError as exc:  # multicast is optional - broadcast still works
                log.debug("multicast join on %s failed: %s", iface, exc)

    def _refresh_senders(self) -> None:
        addresses = self._local_addresses()
        for ip in list(self._senders):
            if ip not in addresses and ip != "":
                self._senders.pop(ip).close()
        for ip in addresses or [""]:
            if ip in self._senders:
                continue
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                if ip:
                    s.bind((ip, 0))
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.settimeout(1.0)
            except OSError as exc:
                log.debug("sender socket for %r failed: %s", ip, exc)
                s.close()
                continue
            self._senders[ip] = s
        if self._bind_host in ("", "0.0.0.0"):
            self._join_multicast(addresses)

    def _send(self, packet: bytes) -> None:
        now = self._clock()
        if not self._senders or now - self._last_iface_refresh > 10.0:
            self._last_iface_refresh = now
            try:
                self._refresh_senders()
            except OSError as exc:
                log.debug("refresh senders failed: %s", exc)
        for s in list(self._senders.values()):
            for target in self._targets:
                try:
                    s.sendto(packet, target)
                except OSError as exc:
                    self.send_errors += 1
                    log.debug("announce to %s failed: %s", target, exc)

    # -- threads --
    def _recv_loop(self) -> None:
        sock = self._recv_sock
        while not self._stop.is_set() and sock is not None:
            try:
                data, addr = sock.recvfrom(MAX_PACKET_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.2)
                continue
            try:
                self.handle_datagram(data, addr)
            except Exception:
                log.exception("bad discovery datagram ignored")

    def _announce_loop(self) -> None:
        while not self._stop.is_set():
            if self._visible:
                self._send(build_announcement(self.node_id, self._name, self._tcp_port))
            self._prune()
            self._wake.wait(self._interval)
            self._wake.clear()
