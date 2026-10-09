"""Small modal dialogs: settings, manual connect, new group, invite."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Sequence

from ..naming import MAX_NAME_CHARS, sanitize_name
from ..networking.node import DEFAULT_TCP_PORT
from ..settings import Settings
from ..transports import TransportStatus
from . import theme


class _Dialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, width: int = 420) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.body = ttk.Frame(self, padding=18)
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())
        self._width = width

    def finish(self) -> None:
        self.update_idletasks()
        p = self.master.winfo_toplevel()
        x = p.winfo_rootx() + (p.winfo_width() - self.winfo_reqwidth()) // 2
        y = p.winfo_rooty() + (p.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def buttons(self, ok_text: str, on_ok: Callable[[], None]) -> ttk.Frame:
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(16, 0))
        ok = ttk.Button(row, text=ok_text, command=on_ok, style=theme.ACCENT_STYLE)
        ok.pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda e: on_ok())
        return row


class SettingsDialog(_Dialog):
    def __init__(self, parent: tk.Misc, settings: Settings, statuses: Sequence[TransportStatus],
                 actual_port: int, on_save: Callable[[Settings], None]) -> None:
        super().__init__(parent, "Settings")
        self._on_save = on_save
        self._settings = settings
        b = self.body
        ttk.Label(b, text="Your name", style="Section.TLabel").pack(anchor="w")
        self.name_var = tk.StringVar(value=settings.username)
        e = ttk.Entry(b, textvariable=self.name_var, width=34)
        e.pack(fill="x", pady=(2, 0))
        ttk.Label(b, text=f"Shown to nearby PCs. Max {MAX_NAME_CHARS} characters. It is a label only - "
                          "it does not identify or authenticate you.", style="Sub.TLabel",
                  wraplength=380, justify="left").pack(anchor="w", pady=(2, 10))
        self.vis_var = tk.BooleanVar(value=settings.discoverable)
        ttk.Checkbutton(b, text="Visible to nearby PCs (announce this PC on the network)",
                        variable=self.vis_var).pack(anchor="w")
        ttk.Label(b, text="When off, you can still see others and connect to them, but nobody sees you.",
                  style="Sub.TLabel", wraplength=380, justify="left").pack(anchor="w", pady=(0, 8), padx=(24, 0))
        self.dark_var = tk.BooleanVar(value=settings.dark_mode)
        ttk.Checkbutton(b, text="Dark mode", variable=self.dark_var).pack(anchor="w", pady=(0, 10))

        ttk.Separator(b).pack(fill="x", pady=6)
        ttk.Label(b, text="Connection methods", style="Section.TLabel").pack(anchor="w")
        for s in statuses:
            mark = "OK" if (s.implemented and s.available) else ("-" if s.implemented else "x")
            row = ttk.Frame(b)
            row.pack(fill="x", pady=(4, 0))
            ttk.Label(row, text=f"[{mark}] {s.kind.value}", width=18).pack(side="left", anchor="n")
            ttk.Label(row, text=s.summary + (f"\n{s.detail}" if s.detail else ""), style="Sub.TLabel",
                      wraplength=240, justify="left").pack(side="left", fill="x")
        ttk.Label(b, text=f"Listening on TCP port {actual_port} (preferred {settings.tcp_port}). "
                          "Port changes need a restart.", style="Sub.TLabel", wraplength=380,
                  justify="left").pack(anchor="w", pady=(10, 0))
        self.buttons("Save", self._save)
        self.finish()
        e.focus_set()
        e.selection_range(0, "end")

    def _save(self) -> None:
        new = Settings(sanitize_name(self.name_var.get()), self.vis_var.get(), self.dark_var.get(),
                       self._settings.tcp_port)
        self._on_save(new)
        self.destroy()


class ManualConnectDialog(_Dialog):
    def __init__(self, parent: tk.Misc, on_connect: Callable[[str, int], None]) -> None:
        super().__init__(parent, "Connect by IP address")
        self._on_connect = on_connect
        b = self.body
        ttk.Label(b, text="Troubleshooting option - normally PCs appear automatically.",
                  style="Sub.TLabel", wraplength=340).pack(anchor="w", pady=(0, 10))
        ttk.Label(b, text="IP address or PC name", style="Section.TLabel").pack(anchor="w")
        self.host = tk.StringVar()
        e = ttk.Entry(b, textvariable=self.host, width=30)
        e.pack(fill="x", pady=(2, 8))
        ttk.Label(b, text="Port", style="Section.TLabel").pack(anchor="w")
        self.port = tk.StringVar(value=str(DEFAULT_TCP_PORT))
        ttk.Entry(b, textvariable=self.port, width=10).pack(anchor="w", pady=(2, 0))
        ttk.Label(b, text="Find the other PC's address and port in its Settings window.",
                  style="Sub.TLabel", wraplength=340).pack(anchor="w", pady=(6, 0))
        self.error = ttk.Label(b, text="", foreground="#dc2626")
        self.error.pack(anchor="w")
        self.buttons("Send request", self._go)
        self.finish()
        e.focus_set()

    def _go(self) -> None:
        host = self.host.get().strip()
        try:
            port = int(self.port.get().strip())
        except ValueError:
            port = 0
        if not host:
            self.error.config(text="Enter an IP address or PC name.")
        elif not 1 <= port <= 65535:
            self.error.config(text="Port must be a number from 1 to 65535.")
        else:
            self.destroy()
            self._on_connect(host, port)


class PickPeersDialog(_Dialog):
    """Name (optional) + checklist of connected PCs. Used for 'New group' and 'Invite'."""

    def __init__(self, parent: tk.Misc, title: str, peers: Sequence[tuple[str, str]],
                 ask_name: bool, ok_text: str, on_ok: Callable[[str, list[str]], None]) -> None:
        super().__init__(parent, title)
        self._on_ok = on_ok
        self._ask_name = ask_name
        b = self.body
        self.name_var = tk.StringVar()
        entry: Optional[ttk.Entry] = None
        if ask_name:
            ttk.Label(b, text="Group name", style="Section.TLabel").pack(anchor="w")
            entry = ttk.Entry(b, textvariable=self.name_var, width=34)
            entry.pack(fill="x", pady=(2, 10))
        ttk.Label(b, text="Invite connected PCs" if peers else "No connected PCs yet",
                  style="Section.TLabel").pack(anchor="w")
        if not peers:
            ttk.Label(b, text="Connect to a nearby PC first. You can create the group now and "
                              "invite people later.", style="Sub.TLabel", wraplength=340,
                      justify="left").pack(anchor="w", pady=(2, 0))
        self.vars: list[tuple[str, tk.BooleanVar]] = []
        for pid, label in peers:
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(b, text=label, variable=v).pack(anchor="w", pady=1)
            self.vars.append((pid, v))
        ttk.Label(b, text="Invited PCs must accept before they can see group messages.",
                  style="Sub.TLabel", wraplength=340).pack(anchor="w", pady=(8, 0))
        self.buttons(ok_text, self._go)
        self.finish()
        if entry:
            entry.focus_set()

    def _go(self) -> None:
        name = sanitize_name(self.name_var.get(), "Group") if self._ask_name else ""
        chosen = [pid for pid, v in self.vars if v.get()]
        self.destroy()
        self._on_ok(name, chosen)
