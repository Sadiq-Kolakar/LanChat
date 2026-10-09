"""LANchat wire protocol, version 1.

Transport: TCP. Framing: one JSON object per line (UTF-8, ``\\n`` terminated).
Every message carries ``"v"`` (protocol version) and ``"type"``.

All incoming data is untrusted: :func:`decode` validates type, field types,
sizes and sanitizes names, and returns a *new* dict that only holds known fields.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .naming import sanitize_name

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024      # one JSON line, including the newline
MAX_TEXT_CHARS = 2000            # one chat message
MAX_GROUP_MEMBERS = 64

_NODE_ID = re.compile(r"^[0-9a-f]{32}$")
_SHORT_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


class ProtocolError(ValueError):
    """Raised for any malformed, oversized or unsupported message."""


def new_id() -> str:
    """Random 128-bit hex id (used for node ids, groups and messages)."""
    return uuid.uuid4().hex


def is_node_id(value: Any) -> bool:
    """True if ``value`` looks like a LANchat node id (32 lowercase hex chars)."""
    return isinstance(value, str) and bool(_NODE_ID.match(value))


# --- field validators ---------------------------------------------------------
def _node_id(v: Any) -> str:
    if not isinstance(v, str) or not _NODE_ID.match(v):
        raise ProtocolError("bad node id")
    return v


def _short_id(v: Any) -> str:
    if not isinstance(v, str) or not _SHORT_ID.match(v):
        raise ProtocolError("bad id")
    return v


def _name(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ProtocolError("bad name")
    return sanitize_name(v)


def _text(v: Any) -> str:
    if not isinstance(v, str):
        raise ProtocolError("text must be a string")
    v = v.strip()
    if not v:
        raise ProtocolError("empty text")
    if len(v) > MAX_TEXT_CHARS:
        raise ProtocolError("text too long")
    return v


def _port(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not (1 <= v <= 65535):
        raise ProtocolError("bad port")
    return v


def _short_text(v: Any) -> str:
    return v.strip()[:120] if isinstance(v, str) else ""


def _members(v: Any) -> list[dict[str, str]]:
    if not isinstance(v, list) or len(v) > MAX_GROUP_MEMBERS:
        raise ProtocolError("bad members")
    out = []
    for item in v:
        if not isinstance(item, dict):
            raise ProtocolError("bad member")
        out.append({"id": _node_id(item.get("id")), "name": _name(item.get("name"))})
    return out


# message type -> {field: validator}. Missing required field => ProtocolError.
_REQUIRED: dict[str, dict[str, Any]] = {
    "hello": {"node_id": _node_id, "name": _name, "port": _port},
    "connect_request": {},
    "connect_accept": {},
    "connect_reject": {},
    "chat": {"id": _short_id, "text": _text},
    "name_change": {"name": _name},
    "ping": {},
    "bye": {},
    "error": {"code": _short_id, "text": _short_text},
    "group_invite": {"group_id": _short_id, "group_name": _name},
    "group_accept": {"group_id": _short_id},
    "group_decline": {"group_id": _short_id},
    "group_members": {"group_id": _short_id, "group_name": _name, "members": _members},
    "group_send": {"group_id": _short_id, "text": _text},
    "group_chat": {
        "group_id": _short_id,
        "sender_id": _node_id,
        "sender_name": _name,
        "id": _short_id,
        "text": _text,
    },
    "group_leave": {"group_id": _short_id},
    "group_closed": {"group_id": _short_id},
}

MESSAGE_TYPES = frozenset(_REQUIRED)


def validate(msg: Any) -> dict[str, Any]:
    """Validate a decoded JSON value. Returns a clean dict or raises ProtocolError."""
    if not isinstance(msg, dict):
        raise ProtocolError("message must be an object")
    version = msg.get("v")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProtocolError("missing protocol version")
    if version > PROTOCOL_VERSION or version < 1:
        raise ProtocolError("unsupported protocol version")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or mtype not in _REQUIRED:
        raise ProtocolError("unknown message type")
    clean: dict[str, Any] = {"v": version, "type": mtype}
    for field, check in _REQUIRED[mtype].items():
        if field not in msg:
            raise ProtocolError(f"missing field: {field}")
        clean[field] = check(msg[field])
    return clean


def encode(mtype: str, **fields: Any) -> bytes:
    """Build one framed message. Validates our own output too."""
    msg = {"v": PROTOCOL_VERSION, "type": mtype, **fields}
    validate(msg)  # never send something we would reject ourselves
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("message too large")
    return data


def decode(line: bytes) -> dict[str, Any]:
    """Decode one frame (without or with trailing newline)."""
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("invalid JSON") from exc
    return validate(obj)


class FrameBuffer:
    """Reassembles newline-delimited frames from arbitrary TCP chunks.

    TCP is a byte stream: one ``recv`` can return half a message or three
    messages. ``feed`` returns complete lines only and keeps the remainder.
    Raises :class:`ProtocolError` if a line grows beyond ``MAX_FRAME_BYTES``
    (peer is misbehaving - caller should drop the connection).
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        lines: list[bytes] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if len(line) + 1 > MAX_FRAME_BYTES:
                raise ProtocolError("frame too large")
            if line.strip():
                lines.append(line)
        if len(self._buf) >= MAX_FRAME_BYTES:
            raise ProtocolError("frame too large")
        return lines
