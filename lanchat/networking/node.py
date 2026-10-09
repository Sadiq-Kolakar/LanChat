"""LanNode: the peer-to-peer chat engine (no UI code in here).

Every running LANchat is a *node*: it listens for TCP connections and can dial
other nodes. The UI talks to the node through plain method calls and receives
:class:`Event` objects through a callback (called from background threads - the
UI must hand them over to its own thread, e.g. with a ``queue.Queue``).

Connection flow (protocol v1)::

    A (initiator)                         B (listener)
    ------------------------------------------------------------
    TCP connect ------------------------>
    hello  ------------------------------>  validate, reject duplicates
           <------------------------------  hello
    connect_request --------------------->  B's user sees "A wants to chat"
           <---------- connect_accept       (or connect_reject -> close)
    chat / chat ... both directions          (chat is ignored before accept)

Groups use a star topology: the creator is the *host* and relays messages.
Members must accept an invite and only ever talk to the host.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..naming import sanitize_name
from ..protocol import ProtocolError, new_id, validate_text
from .peer import PeerConnection, PeerState

log = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 50500
MAX_PEERS = 50
MAX_UNIDENTIFIED = 16        # sockets that have not completed hello yet
MAX_PENDING_INVITES = 16
REQUEST_TIMEOUT = 60.0       # unanswered connection requests expire
TRANSPORT_LAN = "LAN"

_IS_WINDOWS = sys.platform.startswith("win")


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PeerInfo:
    peer_id: str
    name: str
    host: str
    port: int
    state: PeerState
    inbound: bool
    method: str = TRANSPORT_LAN


@dataclass
class Group:
    group_id: str
    name: str
    host_id: str
    is_host: bool
    members: dict[str, str] = field(default_factory=dict)  # node_id -> display name

    def snapshot(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id, "name": self.name, "host_id": self.host_id,
            "is_host": self.is_host, "members": dict(self.members),
        }


class LanNode:
    def __init__(
        self,
        name: str,
        *,
        node_id: Optional[str] = None,
        host: str = "0.0.0.0",
        port: int = DEFAULT_TCP_PORT,
        on_event: Optional[Callable[[Event], None]] = None,
        heartbeat_interval: float = 10.0,
        idle_timeout: float = 35.0,
        handshake_timeout: float = 5.0,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.node_id = node_id or new_id()
        self._name = sanitize_name(name)
        self._host = host
        self._requested_port = port
        self.port = 0
        self.port_fell_back = False
        self._on_event = on_event or (lambda e: None)
        self._heartbeat_interval = heartbeat_interval
        self._idle_timeout = idle_timeout
        self._handshake_timeout = handshake_timeout
        self._request_timeout = request_timeout

        self._lock = threading.RLock()
        self._conns: set[PeerConnection] = set()
        self._peers: dict[str, PeerConnection] = {}
        self._groups: dict[str, Group] = {}
        self._invites_in: dict[str, dict[str, str]] = {}   # group_id -> {host_id, name}
        self._invited_out: dict[str, set[str]] = {}        # group_id -> peer ids we invited
        self._dialing: set[tuple[str, int]] = set()
        self._listener: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._started = False

    # ======================================================================
    # lifecycle
    # ======================================================================
    @property
    def name(self) -> str:
        return self._name

    def start(self) -> int:
        """Open the TCP listener. Returns the port actually used.

        If the preferred port is busy (e.g. a second LANchat on the same PC) an
        OS-chosen free port is used instead; discovery announces whichever it is.
        """
        if self._started:
            return self.port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if not _IS_WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # quick restart on Linux/macOS
        try:
            sock.bind((self._host, self._requested_port))
        except OSError:
            if self._requested_port == 0:
                sock.close()
                raise
            self.port_fell_back = True
            sock.bind((self._host, 0))
        sock.listen(32)
        sock.settimeout(0.5)
        self._listener = sock
        self.port = sock.getsockname()[1]
        self._started = True
        self._spawn(self._accept_loop, "lanchat-accept")
        self._spawn(self._heartbeat_loop, "lanchat-heartbeat")
        return self.port

    def stop(self) -> None:
        """Say goodbye, close every socket and join the background threads."""
        if self._stopping.is_set() or not self._started:
            return
        self._stopping.set()
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            if conn.peer_id:
                try:
                    conn.send("bye")
                except ProtocolError:
                    pass
            conn.close("shutting down")
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        for t in list(self._threads):
            if t is not threading.current_thread():
                t.join(timeout=2.0)
        self._threads.clear()

    def _spawn(self, target: Callable[..., None], name: str, *args: Any) -> threading.Thread:
        t = threading.Thread(target=target, name=name, args=args, daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self._on_event(Event(kind, data))
        except Exception:
            log.exception("event callback failed")

    # ======================================================================
    # queries
    # ======================================================================
    def _info(self, conn: PeerConnection) -> PeerInfo:
        return PeerInfo(conn.peer_id or "", conn.peer_name, conn.host, conn.peer_port, conn.state, conn.inbound)

    def peers(self) -> list[PeerInfo]:
        with self._lock:
            return [self._info(c) for c in self._peers.values() if not c.closed]

    def get_peer(self, peer_id: str) -> Optional[PeerInfo]:
        with self._lock:
            conn = self._peers.get(peer_id)
            return self._info(conn) if conn and not conn.closed else None

    def groups(self) -> list[dict[str, Any]]:
        with self._lock:
            return [g.snapshot() for g in self._groups.values()]

    def pending_group_invites(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {k: dict(v) for k, v in self._invites_in.items()}

    # ======================================================================
    # user actions
    # ======================================================================
    def set_name(self, name: str) -> str:
        """Change our display name and tell connected peers / group members."""
        self._name = sanitize_name(name)
        with self._lock:
            targets = [c for c in self._peers.values() if c.state == PeerState.CONNECTED]
            for g in self._groups.values():
                if self.node_id in g.members:
                    g.members[self.node_id] = self._name
            hosted = [g for g in self._groups.values() if g.is_host]
        for conn in targets:
            conn.send("name_change", name=self._name)
        for g in hosted:
            self._broadcast_members(g)
        return self._name

    def connect(self, host: str, port: int, expected_id: Optional[str] = None) -> bool:
        """Start a connection request to ``host:port`` (non-blocking). Progress arrives as events."""
        if not (1 <= int(port) <= 65535):
            self._emit("connect_failed", host=host, port=port, reason="Port must be 1-65535")
            return False
        key = (host, int(port))
        with self._lock:
            if expected_id and expected_id in self._peers and not self._peers[expected_id].closed:
                self._emit("notice", text="Already connected or waiting for this device.")
                return False
            if key in self._dialing:
                return False
            self._dialing.add(key)
        self._spawn(self._dial, "lanchat-dial", host, int(port))
        return True

    def accept_request(self, peer_id: str) -> bool:
        with self._lock:
            conn = self._peers.get(peer_id)
        if not conn or conn.state != PeerState.PENDING_IN or conn.closed:
            return False
        if not conn.send("connect_accept"):
            return False
        conn.set_state(PeerState.CONNECTED)
        self._emit("peer_state", peer=self._info(conn), reason="accepted")
        return True

    def reject_request(self, peer_id: str) -> bool:
        with self._lock:
            conn = self._peers.get(peer_id)
        if not conn or conn.state != PeerState.PENDING_IN:
            return False
        conn.send("connect_reject")
        conn.close("you rejected the request")
        return True

    def disconnect(self, peer_id: str) -> bool:
        with self._lock:
            conn = self._peers.get(peer_id)
        if not conn:
            return False
        if conn.state == PeerState.CONNECTED:
            conn.send("bye")
        conn.close("you disconnected")
        return True

    def send_dm(self, peer_id: str, text: str) -> Optional[str]:
        """Send a private message. Returns the message id, or None if not connected.
        Raises ValueError (ProtocolError) for empty / too-long text."""
        text = validate_text(text)
        with self._lock:
            conn = self._peers.get(peer_id)
        if not conn or conn.state != PeerState.CONNECTED:
            return None
        mid = new_id()
        return mid if conn.send("chat", id=mid, text=text) else None

    # -- groups --
    def create_group(self, name: str) -> str:
        gid = new_id()
        group = Group(gid, sanitize_name(name, "Group"), self.node_id, True, {self.node_id: self._name})
        with self._lock:
            self._groups[gid] = group
            self._invited_out[gid] = set()
        self._emit("group_update", group=group.snapshot())
        return gid

    def invite_to_group(self, group_id: str, peer_id: str) -> bool:
        with self._lock:
            group = self._groups.get(group_id)
            conn = self._peers.get(peer_id)
            if not group or not group.is_host or not conn or conn.state != PeerState.CONNECTED:
                return False
            if peer_id in group.members:
                return False
            self._invited_out.setdefault(group_id, set()).add(peer_id)
        return conn.send("group_invite", group_id=group_id, group_name=group.name)

    def accept_group_invite(self, group_id: str) -> bool:
        with self._lock:
            invite = self._invites_in.pop(group_id, None)
            conn = self._peers.get(invite["host_id"]) if invite else None
            if not invite or not conn or conn.state != PeerState.CONNECTED:
                return False
            self._groups[group_id] = Group(
                group_id, invite["name"], invite["host_id"], False,
                {invite["host_id"]: conn.peer_name, self.node_id: self._name},
            )
            snap = self._groups[group_id].snapshot()
        conn.send("group_accept", group_id=group_id)
        self._emit("group_update", group=snap)
        return True

    def decline_group_invite(self, group_id: str) -> bool:
        with self._lock:
            invite = self._invites_in.pop(group_id, None)
            conn = self._peers.get(invite["host_id"]) if invite else None
        if conn and conn.state == PeerState.CONNECTED:
            conn.send("group_decline", group_id=group_id)
        return invite is not None

    def leave_group(self, group_id: str) -> bool:
        with self._lock:
            group = self._groups.pop(group_id, None)
            self._invited_out.pop(group_id, None)
            if not group:
                return False
            if group.is_host:
                targets = [self._peers[m] for m in group.members if m in self._peers and m != self.node_id]
            else:
                targets = [self._peers[group.host_id]] if group.host_id in self._peers else []
        for conn in targets:
            if conn.state == PeerState.CONNECTED:
                conn.send("group_closed" if group.is_host else "group_leave", group_id=group_id)
        self._emit("group_closed", group_id=group_id, name=group.name,
                   reason="You closed the group" if group.is_host else "You left the group")
        return True

    def send_group(self, group_id: str, text: str) -> Optional[str]:
        text = validate_text(text)
        with self._lock:
            group = self._groups.get(group_id)
            if not group:
                return None
            host_conn = None if group.is_host else self._peers.get(group.host_id)
        mid = new_id()
        if group.is_host:
            self._relay_group_chat(group, self.node_id, self._name, mid, text, skip=self.node_id)
            return mid
        if not host_conn or host_conn.state != PeerState.CONNECTED:
            return None
        return mid if host_conn.send("group_send", group_id=group_id, text=text) else None

    # ======================================================================
    # connection setup
    # ======================================================================
    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                sock, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                unidentified = sum(1 for c in self._conns if c.peer_id is None)
                full = len(self._conns) >= MAX_PEERS or unidentified >= MAX_UNIDENTIFIED
            if full:
                sock.close()
                continue
            conn = PeerConnection(sock, addr, True, idle_timeout=self._idle_timeout,
                                  handshake_timeout=self._handshake_timeout)
            with self._lock:
                self._conns.add(conn)
            self._spawn(conn.run, "lanchat-peer-in", self._on_message, self._on_closed)

    def _dial(self, host: str, port: int) -> None:
        self._emit("connecting", host=host, port=port)
        try:
            sock = socket.create_connection((host, port), timeout=5.0)
        except OSError as exc:
            with self._lock:
                self._dialing.discard((host, port))
            self._emit("connect_failed", host=host, port=port, reason=_friendly_error(exc))
            return
        conn = PeerConnection(sock, (host, port), False, idle_timeout=self._idle_timeout,
                              handshake_timeout=self._handshake_timeout)
        with self._lock:
            self._conns.add(conn)
        conn.set_state(PeerState.HANDSHAKE)
        conn.send("hello", node_id=self.node_id, name=self._name, port=self.port)
        try:
            conn.run(self._on_message, self._on_closed)  # this thread becomes the reader
        finally:
            with self._lock:
                self._dialing.discard((host, port))

    # ======================================================================
    # incoming messages
    # ======================================================================
    def _on_message(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        mtype = msg["type"]
        if conn.peer_id is None:
            if mtype == "hello":
                self._on_hello(conn, msg)
            elif mtype in ("ping", "error", "bye"):
                pass
            else:
                conn.strike(f"{mtype} before hello")
            return

        if mtype == "ping":
            return
        if mtype == "bye":
            conn.close("peer left")
            return
        if mtype == "error":
            self._emit("notice", text=f"{conn.peer_name}: {msg.get('text') or msg['code']}")
            return
        if mtype == "hello":
            conn.strike("duplicate hello")
            return

        # ---- connection request flow ----
        if mtype == "connect_request":
            if conn.inbound and conn.state == PeerState.HANDSHAKE:
                conn.set_state(PeerState.PENDING_IN)
                self._emit("incoming_request", peer=self._info(conn))
                self._emit("peer_state", peer=self._info(conn), reason="request")
            else:
                conn.strike("unexpected connect_request")
            return
        if mtype == "connect_accept":
            if conn.requested_out and conn.state == PeerState.PENDING_OUT:
                conn.set_state(PeerState.CONNECTED)
                self._emit("peer_state", peer=self._info(conn), reason="accepted")
            else:
                conn.strike("unexpected connect_accept")
            return
        if mtype == "connect_reject":
            if conn.requested_out and conn.state == PeerState.PENDING_OUT:
                conn.set_state(PeerState.REJECTED)
                self._emit("request_rejected", peer=self._info(conn))
                conn.close("request rejected")
            else:
                conn.strike("unexpected connect_reject")
            return

        # ---- everything below needs an accepted connection ----
        if conn.state != PeerState.CONNECTED:
            conn.strike(f"{mtype} before accept")
            return

        handler = {
            "chat": self._on_chat,
            "name_change": self._on_name_change,
            "group_invite": self._on_group_invite,
            "group_accept": self._on_group_accept,
            "group_decline": self._on_group_decline,
            "group_members": self._on_group_members,
            "group_send": self._on_group_send,
            "group_chat": self._on_group_chat,
            "group_leave": self._on_group_leave,
            "group_closed": self._on_group_closed,
        }.get(mtype)
        if handler is None:
            conn.strike(f"unexpected {mtype}")
            return
        handler(conn, msg)

    def _on_hello(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        peer_id = msg["node_id"]
        if peer_id == self.node_id:
            conn.send("error", code="self", text="That is this same LANchat instance")
            conn.close("tried to connect to itself")
            return
        with self._lock:
            existing = self._peers.get(peer_id)
            if existing is not None and not existing.closed:
                if existing.host != conn.host:
                    # Same id from a different address: possible spoof. Keep the first one.
                    reject = "duplicate_session"
                elif existing.state == PeerState.CONNECTED:
                    reject = "already_connected"
                else:
                    reject = ""
                    existing.close("replaced by a newer connection")
            else:
                reject = ""
            if not reject:
                conn.peer_id = peer_id
                conn.peer_name = msg["name"]
                conn.peer_port = msg["port"]
                self._peers[peer_id] = conn
        if reject:
            conn.send("error", code=reject, text="A session with this device already exists")
            conn.close(f"rejected: {reject}")
            self._emit("notice", text=f"Ignored a duplicate connection from {msg['name']} ({conn.host}).")
            return

        if conn.inbound:
            conn.send("hello", node_id=self.node_id, name=self._name, port=self.port)
            conn.set_state(PeerState.HANDSHAKE)
        else:
            conn.requested_out = True
            conn.set_state(PeerState.PENDING_OUT)
            conn.send("connect_request")
        self._emit("peer_state", peer=self._info(conn), reason="hello")

    def _on_chat(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        self._emit("chat", conv="dm", peer_id=conn.peer_id, sender_id=conn.peer_id,
                   sender_name=conn.peer_name, text=msg["text"], msg_id=msg["id"])

    def _on_name_change(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        conn.peer_name = msg["name"]
        with self._lock:
            hosted = [g for g in self._groups.values() if g.is_host and conn.peer_id in g.members]
            for g in hosted:
                g.members[conn.peer_id] = conn.peer_name  # type: ignore[index]
        self._emit("peer_state", peer=self._info(conn), reason="renamed")
        for g in hosted:
            self._broadcast_members(g)

    # ======================================================================
    # groups (incoming)
    # ======================================================================
    def _on_group_invite(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        gid = msg["group_id"]
        with self._lock:
            if gid in self._groups or gid in self._invites_in or len(self._invites_in) >= MAX_PENDING_INVITES:
                return
            self._invites_in[gid] = {"host_id": conn.peer_id or "", "name": msg["group_name"]}
        self._emit("group_invite", group_id=gid, group_name=msg["group_name"],
                   host_id=conn.peer_id, host_name=conn.peer_name)

    def _on_group_accept(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        gid, pid = msg["group_id"], conn.peer_id or ""
        with self._lock:
            group = self._groups.get(gid)
            if not group or not group.is_host or pid not in self._invited_out.get(gid, set()):
                conn.strike("group_accept without invite")
                return
            self._invited_out[gid].discard(pid)
            if len(group.members) >= 64:
                return
            group.members[pid] = conn.peer_name
        self._broadcast_members(group)

    def _on_group_decline(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        with self._lock:
            invited = self._invited_out.get(msg["group_id"])
            if not invited or conn.peer_id not in invited:
                return
            invited.discard(conn.peer_id)
        self._emit("group_invite_declined", group_id=msg["group_id"], peer_id=conn.peer_id, name=conn.peer_name)

    def _on_group_members(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        gid = msg["group_id"]
        members = {m["id"]: m["name"] for m in msg["members"]}
        with self._lock:
            group = self._groups.get(gid)
            if not group or group.is_host or group.host_id != conn.peer_id:
                conn.strike("group_members from non-host")
                return
            if self.node_id not in members:
                del self._groups[gid]
                removed, snap = True, None
            else:
                removed = False
                group.name = msg["group_name"]
                group.members = members
                snap = group.snapshot()
        if removed:
            self._emit("group_closed", group_id=gid, name=group.name, reason="You were removed from the group")
        else:
            self._emit("group_update", group=snap)

    def _on_group_send(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        with self._lock:
            group = self._groups.get(msg["group_id"])
            allowed = bool(group and group.is_host and conn.peer_id in group.members)
            sender_name = group.members.get(conn.peer_id or "", "") if group else ""
        if not allowed or group is None:
            conn.strike("group_send by non-member")
            return
        # sender id/name come from *our* connection table, never from the message body
        self._relay_group_chat(group, conn.peer_id or "", sender_name, new_id(), msg["text"], skip=conn.peer_id)

    def _on_group_chat(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        with self._lock:
            group = self._groups.get(msg["group_id"])
            ok = bool(group and not group.is_host and group.host_id == conn.peer_id)
        if not ok or group is None:
            conn.strike("group_chat from non-host")
            return
        if msg["sender_id"] == self.node_id:
            return
        self._emit("chat", conv="group", group_id=group.group_id, sender_id=msg["sender_id"],
                   sender_name=msg["sender_name"], text=msg["text"], msg_id=msg["id"])

    def _on_group_leave(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        with self._lock:
            group = self._groups.get(msg["group_id"])
            if not group or not group.is_host or conn.peer_id not in group.members:
                return
            group.members.pop(conn.peer_id or "", None)
        self._broadcast_members(group)

    def _on_group_closed(self, conn: PeerConnection, msg: dict[str, Any]) -> None:
        with self._lock:
            group = self._groups.get(msg["group_id"])
            if not group or group.is_host or group.host_id != conn.peer_id:
                return
            del self._groups[group.group_id]
        self._emit("group_closed", group_id=group.group_id, name=group.name, reason="The host closed the group")

    # ======================================================================
    # group helpers (host side)
    # ======================================================================
    def _broadcast_members(self, group: Group) -> None:
        with self._lock:
            members = [{"id": i, "name": n} for i, n in group.members.items()]
            targets = [self._peers[i] for i in group.members if i != self.node_id and i in self._peers]
            snap = group.snapshot()
        for conn in targets:
            if conn.state == PeerState.CONNECTED:
                conn.send("group_members", group_id=group.group_id, group_name=group.name, members=members)
        self._emit("group_update", group=snap)

    def _relay_group_chat(self, group: Group, sender_id: str, sender_name: str,
                          mid: str, text: str, skip: Optional[str]) -> None:
        with self._lock:
            targets = [self._peers[i] for i in group.members
                       if i not in (self.node_id, skip) and i in self._peers]
        for conn in targets:
            if conn.state == PeerState.CONNECTED:
                conn.send("group_chat", group_id=group.group_id, sender_id=sender_id,
                          sender_name=sender_name, id=mid, text=text)
        if sender_id != self.node_id:
            self._emit("chat", conv="group", group_id=group.group_id, sender_id=sender_id,
                       sender_name=sender_name, text=text, msg_id=mid)

    # ======================================================================
    # cleanup + housekeeping
    # ======================================================================
    def _on_closed(self, conn: PeerConnection) -> None:
        with self._lock:
            self._conns.discard(conn)
            pid = conn.peer_id
            owned = bool(pid) and self._peers.get(pid or "") is conn
            if owned:
                del self._peers[pid or ""]
            hosted_changed: list[Group] = []
            lost_groups: list[Group] = []
            cancelled: list[str] = []
            if owned and pid:
                for g in list(self._groups.values()):
                    if g.is_host and pid in g.members:
                        g.members.pop(pid, None)
                        hosted_changed.append(g)
                    elif not g.is_host and g.host_id == pid:
                        lost_groups.append(self._groups.pop(g.group_id))
                for gid, inv in list(self._invites_in.items()):
                    if inv["host_id"] == pid:
                        del self._invites_in[gid]
                        cancelled.append(gid)
                for invited in self._invited_out.values():
                    invited.discard(pid)
        was_rejected = conn.state == PeerState.REJECTED
        if owned or conn.peer_id:
            conn.state = PeerState.REJECTED if was_rejected else PeerState.DISCONNECTED
            if owned:
                self._emit("peer_state", peer=self._info(conn), reason=conn.close_reason or "disconnected")
        elif not conn.inbound and not self._stopping.is_set():
            self._emit("connect_failed", host=conn.host, port=conn.address[1],
                       reason=conn.close_reason or "Connection closed before it was established")
        for g in hosted_changed:
            self._broadcast_members(g)
        for g in lost_groups:
            self._emit("group_closed", group_id=g.group_id, name=g.name, reason="Lost connection to the group host")
        for gid in cancelled:
            self._emit("group_invite_cancelled", group_id=gid)

    def _heartbeat_loop(self) -> None:
        while not self._stopping.wait(self._heartbeat_interval):
            with self._lock:
                conns = [c for c in self._conns if c.peer_id]
            now = time.monotonic()
            for conn in conns:
                if conn.state == PeerState.PENDING_IN and now - conn.state_since > self._request_timeout:
                    self.reject_request(conn.peer_id or "")
                    self._emit("notice", text=f"Request from {conn.peer_name} expired.")
                    continue
                if conn.state == PeerState.PENDING_OUT and now - conn.state_since > self._request_timeout + 10:
                    conn.close("no answer to the connection request")
                    continue
                conn.send("ping")


def _friendly_error(exc: OSError) -> str:
    text = str(exc)
    if isinstance(exc, socket.timeout) or "timed out" in text:
        return "Timed out. Check the address and that LANchat is running there (and allowed by its firewall)."
    if "refused" in text.lower():
        return "Connection refused. LANchat is not listening on that address/port."
    return text or exc.__class__.__name__
