"""One TCP connection to one remote LANchat, with safe framing and writes."""
from __future__ import annotations

import logging
import select
import socket
import threading
import time
from enum import Enum
from typing import Callable, Optional

from ..protocol import FrameBuffer, ProtocolError, decode, encode

log = logging.getLogger(__name__)

SEND_TIMEOUT = 10.0       # a stalled peer cannot block a writer forever
MAX_STRIKES = 5           # protocol violations tolerated before we hang up


class PeerState(str, Enum):
    CONNECTING = "connecting"        # TCP connect in progress (outbound)
    HANDSHAKE = "handshake"          # TCP up, waiting for hello / request
    PENDING_OUT = "pending_out"      # we asked, waiting for the other user
    PENDING_IN = "pending_in"        # they asked, waiting for our user
    CONNECTED = "connected"          # accepted - chat allowed
    REJECTED = "rejected"
    DISCONNECTED = "disconnected"


class PeerConnection:
    """Wraps a socket. Thread-safe ``send``; a single reader thread runs ``run``."""

    def __init__(
        self,
        sock: socket.socket,
        address: tuple[str, int],
        inbound: bool,
        *,
        idle_timeout: float = 35.0,
        handshake_timeout: float = 5.0,
    ) -> None:
        self.sock = sock
        self.sock.settimeout(SEND_TIMEOUT)
        self.address = address
        self.inbound = inbound
        self.state = PeerState.HANDSHAKE if inbound else PeerState.CONNECTING
        self.peer_id: Optional[str] = None
        self.peer_name: str = ""
        self.peer_port: int = 0
        self.requested_out = False           # we sent connect_request on this connection
        self.strikes = 0
        self.close_reason = ""
        self.created = time.monotonic()
        self.state_since = self.created
        self.last_rx = self.created
        self._idle_timeout = idle_timeout
        self._handshake_timeout = handshake_timeout
        self._write_lock = threading.Lock()  # concurrent writers must not interleave frames
        self._closed = threading.Event()

    # -- state --
    def set_state(self, state: PeerState) -> None:
        self.state = state
        self.state_since = time.monotonic()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def host(self) -> str:
        return self.address[0]

    # -- writing --
    def send(self, mtype: str, **fields) -> bool:
        """Send one message. Returns False (and closes) if the socket is unusable.

        A failed/timed-out ``sendall`` may leave half a frame on the wire, so the
        connection is dropped instead of being reused.
        """
        if self.closed:
            return False
        data = encode(mtype, **fields)  # ProtocolError here = bug in caller, let it raise
        try:
            with self._write_lock:
                self.sock.sendall(data)
            return True
        except OSError as exc:
            self.close(f"send failed: {exc}")
            return False

    def strike(self, reason: str) -> bool:
        """Count a protocol violation. Returns True when the limit is reached (and closes)."""
        self.strikes += 1
        log.debug("strike %d from %s: %s", self.strikes, self.address, reason)
        if self.strikes >= MAX_STRIKES:
            self.close("too many protocol violations")
            return True
        return False

    # -- lifecycle --
    def close(self, reason: str = "") -> None:
        if self._closed.is_set():
            return
        self.close_reason = self.close_reason or reason
        self._closed.set()
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except OSError:
                pass

    # -- reading (runs in its own thread) --
    def run(
        self,
        on_message: Callable[["PeerConnection", dict], None],
        on_closed: Callable[["PeerConnection"], None],
    ) -> None:
        buf = FrameBuffer()
        try:
            while not self.closed:
                try:
                    ready, _, _ = select.select([self.sock], [], [], 0.5)
                except (OSError, ValueError):
                    break
                now = time.monotonic()
                if not ready:
                    if self.peer_id is None and now - self.created > self._handshake_timeout:
                        self.close("handshake timed out")
                    elif now - self.last_rx > self._idle_timeout:
                        self.close("connection timed out")
                    continue
                try:
                    data = self.sock.recv(4096)
                except OSError:
                    break
                if not data:
                    self.close("closed by peer")
                    break
                self.last_rx = now
                try:
                    lines = buf.feed(data)
                except ProtocolError:
                    self._best_effort_error("frame_too_large", "Frame too large")
                    self.close("oversized frame")
                    break
                for line in lines:
                    if self.closed:
                        break
                    try:
                        msg = decode(line)
                    except ProtocolError as exc:
                        self._best_effort_error("bad_message", str(exc))
                        if self.strike(f"malformed: {exc}"):
                            break
                        continue
                    try:
                        on_message(self, msg)
                    except Exception:  # a handler bug must never kill the reader
                        log.exception("message handler failed")
        finally:
            self.close("connection lost")
            on_closed(self)

    def _best_effort_error(self, code: str, text: str) -> None:
        try:
            self.send("error", code=code, text=text)
        except ProtocolError:
            pass
