"""User settings. Stored as plain JSON; contains no passwords or secrets."""
from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .naming import sanitize_name
from .networking.node import DEFAULT_TCP_PORT

log = logging.getLogger(__name__)


def default_username() -> str:
    try:
        return sanitize_name(socket.gethostname())
    except OSError:
        return sanitize_name("")


@dataclass
class Settings:
    username: str = field(default_factory=default_username)
    discoverable: bool = True        # announce this PC to others on the LAN
    dark_mode: bool = False
    tcp_port: int = DEFAULT_TCP_PORT  # preferred listening port (falls back if busy)

    def validated(self) -> "Settings":
        port = self.tcp_port if isinstance(self.tcp_port, int) and not isinstance(self.tcp_port, bool) else DEFAULT_TCP_PORT
        if not (1024 <= port <= 65535):
            port = DEFAULT_TCP_PORT
        return Settings(sanitize_name(self.username), bool(self.discoverable), bool(self.dark_mode), port)


def settings_path(profile: Optional[str] = None) -> Path:
    """%APPDATA%\\LANchat on Windows, ~/.lanchat elsewhere. ``profile`` = extra instance on one PC."""
    base = os.environ.get("LANCHAT_HOME")
    if base:
        folder = Path(base)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        folder = Path(os.environ["APPDATA"]) / "LANchat"
    else:
        folder = Path.home() / ".lanchat"
    safe = "".join(c for c in (profile or "") if c.isalnum() or c in "-_")[:32]
    return folder / (f"settings-{safe}.json" if safe else "settings.json")


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("settings file is not an object")
        known = {k: raw[k] for k in ("username", "discoverable", "dark_mode", "tcp_port") if k in raw}
        return Settings(**known).validated()
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Ignoring unreadable settings file %s: %s", path, exc)
        return Settings()


def save_settings(path: Path, settings: Settings) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(settings.validated()), indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic: no half-written file if we crash
        return True
    except OSError as exc:
        log.warning("Could not save settings: %s", exc)
        return False
