"""LANchat main window (Tkinter).

Layout: left sidebar (branding, connection status, nearby PCs, conversations),
right area (header, pending requests, chat history, message box).
All network events arrive through ``engine.events`` and are handled on the Tk
thread by ``pump`` - background threads never touch widgets.
"""
from __future__ import annotations

import queue
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .. import APP_NAME, __version__
from ..engine import Engine
from ..naming import display_labels
from ..networking import Event, PeerInfo, PeerState
from ..protocol import MAX_TEXT_CHARS
from ..settings import Settings, save_settings
from ..transports import NearbyDevice, Selection, TransportKind
from . import theme
from .dialogs import ManualConnectDialog, PickPeersDialog, SettingsDialog


@dataclass
class Conversation:
    conv_id: str
    kind: str                 # "dm" or "group"
    ref_id: str               # peer id or group id
    title: str
    messages: list[tuple[str, str, str, str]] = field(default_factory=list)  # (time, sender, text, kind)
    unread: int = 0
    last_addr: Optional[tuple[str, int]] = None
    closed: bool = False      # group closed / left


def _dot(root: tk.Misc, color: str, size: int = 12) -> tk.PhotoImage:
    img = tk.PhotoImage(master=root, width=size, height=size)
    r, c = size / 2 - 1, size / 2
    for y in range(size):
        for x in range(size):
            if (x + 0.5 - c) ** 2 + (y + 0.5 - c) ** 2 <= r * r:
                img.put(color, (x, y))
    return img


class MainWindow:
    def __init__(self, root: tk.Tk, engine: Engine, settings_file: Optional[Path] = None) -> None:
        self.root = root
        self.engine = engine
        self.node = engine.node
        self.settings_file = settings_file
        self.convs: dict[str, Conversation] = {}
        self.current: Optional[str] = None
        self.labels: dict[str, str] = {}
        self.device_cache: dict[str, NearbyDevice] = {}
        self.dialing: set[tuple[str, int]] = set()
        self._manual: set[tuple[str, int]] = set()
        self.requests: dict[str, ttk.Frame] = {}
        self._suppress_select = False
        self._notice_job: Optional[str] = None
        self._selection: Optional[Selection] = None
        self._selection_at = 0.0
        self._closing = False

        root.title(APP_NAME)
        root.geometry("1020x680")
        root.minsize(780, 500)
        self.palette = theme.apply_theme(root, engine.settings.dark_mode)
        self._make_icons()
        self._build()
        self._apply_palette()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)
        root.after(300, self.tick)
        self._startup_notices()
        self.open_conv(None)

    # ------------------------------------------------------------------ build
    def _make_icons(self) -> None:
        pal = self.palette
        self.icons = {k: _dot(self.root, pal[k]) for k in
                      ("online", "connecting", "connected", "wants", "offline", "error")}

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ---- sidebar ----
        side = ttk.Frame(root, padding=(14, 14, 10, 10), width=300)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        side.rowconfigure(3, weight=2)
        side.rowconfigure(5, weight=1)
        self.side = side
        ttk.Label(side, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(side, text="Nearby chat - no internet needed", style="Sub.TLabel").grid(row=1, column=0, sticky="w")

        chip = ttk.Frame(side)
        chip.grid(row=2, column=0, sticky="ew", pady=(10, 8))
        self.chip_icon = ttk.Label(chip)
        self.chip_icon.grid(row=0, column=0, sticky="n", padx=(0, 6), pady=(3, 0))
        self.chip_title = ttk.Label(chip, text="", style="Section.TLabel")
        self.chip_title.grid(row=0, column=1, sticky="w")
        self.chip_text = ttk.Label(chip, text="", style="Sub.TLabel", wraplength=240, justify="left")
        self.chip_text.grid(row=1, column=1, sticky="w")
        self.you_label = ttk.Label(chip, text="", style="Sub.TLabel")
        self.you_label.grid(row=2, column=1, sticky="w")

        dev_frame = ttk.Frame(side)
        dev_frame.grid(row=3, column=0, sticky="nsew")
        dev_frame.rowconfigure(1, weight=1)
        dev_frame.columnconfigure(0, weight=1)
        ttk.Label(dev_frame, text="NEARBY PCs", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.dev_tree = ttk.Treeview(dev_frame, columns=("status",), show="tree", selectmode="browse", height=6)
        self.dev_tree.column("#0", width=170, stretch=True)
        self.dev_tree.column("status", width=96, anchor="e", stretch=False)
        self.dev_tree.grid(row=1, column=0, sticky="nsew")
        self.dev_tree.bind("<<TreeviewSelect>>", lambda e: self._update_connect_button())
        self.dev_tree.bind("<Double-1>", lambda e: self.on_connect_selected())
        self.dev_empty = ttk.Label(dev_frame, text="Looking for nearby LANchat PCs...\nThey appear here automatically.",
                                   style="Sub.TLabel", justify="center")
        btns = ttk.Frame(side)
        btns.grid(row=4, column=0, sticky="ew", pady=(8, 10))
        btns.columnconfigure((0, 1), weight=1)
        self.connect_btn = ttk.Button(btns, text="Connect", command=self.on_connect_selected,
                                      style=theme.ACCENT_STYLE)
        self.connect_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(btns, text="By IP...", command=self.on_manual).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        conv_frame = ttk.Frame(side)
        conv_frame.grid(row=5, column=0, sticky="nsew")
        conv_frame.rowconfigure(1, weight=1)
        conv_frame.columnconfigure(0, weight=1)
        ttk.Label(conv_frame, text="CONVERSATIONS", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.conv_tree = ttk.Treeview(conv_frame, columns=("n",), show="tree", selectmode="browse", height=5)
        self.conv_tree.column("#0", width=200, stretch=True)
        self.conv_tree.column("n", width=40, anchor="e", stretch=False)
        self.conv_tree.grid(row=1, column=0, sticky="nsew")
        self.conv_tree.bind("<<TreeviewSelect>>", self._on_conv_select)
        self.conv_empty = ttk.Label(conv_frame, text="No conversations yet.", style="Sub.TLabel")

        bottom = ttk.Frame(side)
        bottom.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure((0, 1), weight=1)
        ttk.Button(bottom, text="New group", command=self.on_new_group).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(bottom, text="Settings", command=self.on_settings).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # ---- main area ----
        main = ttk.Frame(root, padding=(0, 14, 14, 8))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        head = ttk.Frame(main)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.title_lbl = ttk.Label(head, text="", style="Header.TLabel")
        self.title_lbl.grid(row=0, column=0, sticky="w")
        self.sub_lbl = ttk.Label(head, text="", style="Sub.TLabel")
        self.sub_lbl.grid(row=1, column=0, sticky="w")
        self.head_btns = ttk.Frame(head)
        self.head_btns.grid(row=0, column=1, rowspan=2, sticky="e")
        self.btn_a = ttk.Button(self.head_btns, text="", command=lambda: None)
        self.btn_b = ttk.Button(self.head_btns, text="", command=lambda: None)

        self.requests_frame = ttk.Frame(main)
        self.requests_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.requests_frame.columnconfigure(0, weight=1)

        chat_wrap = ttk.Frame(main)
        chat_wrap.grid(row=2, column=0, sticky="nsew", pady=(8, 8))
        chat_wrap.rowconfigure(0, weight=1)
        chat_wrap.columnconfigure(0, weight=1)
        self.chat = tk.Text(chat_wrap, wrap="word", state="disabled", relief="flat", borderwidth=0,
                            padx=14, pady=10, font=theme.font(11), cursor="arrow", spacing1=2, spacing3=2)
        sb = ttk.Scrollbar(chat_wrap, command=self.chat.yview)
        self.chat.configure(yscrollcommand=sb.set)
        self.chat.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.empty = ttk.Label(chat_wrap, text="", style="Sub.TLabel", justify="center", anchor="center")

        entry_row = ttk.Frame(main)
        entry_row.grid(row=3, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        self.entry_var = tk.StringVar()
        self.entry = ttk.Entry(entry_row, textvariable=self.entry_var, font=theme.font(11))
        self.entry.grid(row=0, column=0, sticky="ew", ipady=4)
        self.entry.bind("<Return>", lambda e: self.send_message())
        self.send_btn = ttk.Button(entry_row, text="Send", command=self.send_message, style=theme.ACCENT_STYLE)
        self.send_btn.grid(row=0, column=1, padx=(8, 0))

        self.status_var = tk.StringVar(value="")
        self.status_lbl = ttk.Label(main, textvariable=self.status_var, style="Sub.TLabel")
        self.status_lbl.grid(row=4, column=0, sticky="w", pady=(6, 0))

    def _apply_palette(self) -> None:
        pal = self.palette
        self.chat.configure(bg=pal["panel"], fg=pal["text"], insertbackground=pal["text"],
                            selectbackground=pal["accent"], selectforeground=pal["on_accent"])
        self.chat.tag_configure("name_me", foreground=pal["me"], font=theme.font(10, "bold"), spacing1=8)
        self.chat.tag_configure("name_other", foreground=pal["other"], font=theme.font(10, "bold"), spacing1=8)
        self.chat.tag_configure("time", foreground=pal["muted"], font=theme.font(8))
        self.chat.tag_configure("body", lmargin1=2, lmargin2=2)
        self.chat.tag_configure("sys", foreground=pal["muted"], font=theme.font(9, "italic"),
                                justify="center", spacing1=6, spacing3=4)
        for key, tree in (("dev", self.dev_tree), ("conv", self.conv_tree)):
            for name in ("online", "connecting", "connected", "wants", "offline"):
                tree.tag_configure(name, foreground=pal["text"] if name != "offline" else pal["muted"])
        self.status_lbl.configure(foreground=pal["muted"])

    # ------------------------------------------------------------------ helpers
    def notify(self, text: str, error: bool = False, seconds: int = 8) -> None:
        self.status_var.set(text)
        self.status_lbl.configure(foreground=self.palette["error"] if error else self.palette["muted"])
        if self._notice_job:
            self.root.after_cancel(self._notice_job)
        self._notice_job = self.root.after(seconds * 1000, lambda: self.status_var.set(""))

    def _startup_notices(self) -> None:
        notes = []
        if self.node.port_fell_back:
            notes.append(f"Port {self.engine.settings.tcp_port} was busy - using {self.node.port} "
                         "(normal when running several LANchats on one PC).")
        if not self.engine.discovery.listening and self.engine.discovery.error:
            notes.append(self.engine.discovery.error)
        if notes:
            self.notify(" ".join(notes), error=not self.engine.discovery.listening, seconds=15)

    def _now(self) -> str:
        return time.strftime("%H:%M")

    def label(self, peer_id: str, fallback: str = "?") -> str:
        return self.labels.get(peer_id, fallback)

    # ------------------------------------------------------------------ conversations
    def ensure_dm(self, peer: PeerInfo) -> Conversation:
        cid = f"dm:{peer.peer_id}"
        conv = self.convs.get(cid)
        if conv is None:
            conv = self.convs[cid] = Conversation(cid, "dm", peer.peer_id, peer.name)
        conv.last_addr = (peer.host, peer.port)
        return conv

    def ensure_group(self, snap: dict) -> Conversation:
        cid = f"grp:{snap['group_id']}"
        conv = self.convs.get(cid)
        if conv is None:
            conv = self.convs[cid] = Conversation(cid, "group", snap["group_id"], snap["name"])
            self.add_sys(conv, "You created this group." if snap["is_host"] else "You joined this group.")
        conv.title = snap["name"]
        conv.closed = False
        return conv

    def add_message(self, conv: Conversation, sender: str, text: str, kind: str) -> None:
        entry = (self._now(), sender, text, kind)
        conv.messages.append(entry)
        if self.current == conv.conv_id:
            self._render_entry(entry)
        else:
            if kind == "other":
                conv.unread += 1
                self.root.bell()
            self.refresh_lists()

    def add_sys(self, conv: Conversation, text: str) -> None:
        self.add_message(conv, "", text, "sys")

    def _render_entry(self, entry: tuple[str, str, str, str]) -> None:
        stamp, sender, text, kind = entry
        at_bottom = self.chat.yview()[1] > 0.97
        self.chat.configure(state="normal")
        if kind == "sys":
            self.chat.insert("end", f"{text}\n", "sys")
        else:
            self.chat.insert("end", sender, "name_me" if kind == "me" else "name_other")
            self.chat.insert("end", f"   {stamp}\n", "time")
            self.chat.insert("end", f"{text}\n", "body")
        self.chat.configure(state="disabled")
        if at_bottom or kind == "me":
            self.chat.see("end")

    def current_conv(self) -> Optional[Conversation]:
        return self.convs.get(self.current) if self.current else None

    def is_active(self, conv: Conversation) -> bool:
        if conv.kind == "dm":
            p = self.node.get_peer(conv.ref_id)
            return bool(p and p.state == PeerState.CONNECTED)
        return (not conv.closed) and any(g["group_id"] == conv.ref_id for g in self.node.groups())

    def open_conv(self, conv_id: Optional[str]) -> None:
        self.current = conv_id
        conv = self.current_conv()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        if conv:
            conv.unread = 0
            for entry in conv.messages:
                self._render_entry(entry)
            self.chat.see("end")
            self.empty.place_forget()
            self.entry.focus_set()
        else:
            self.empty.configure(text="Select a nearby PC and press Connect to start chatting.\n\n"
                                      "The other person has to accept your request first.")
            self.empty.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.refresh_lists()
        self.update_header()

    def _on_conv_select(self, _e: object = None) -> None:
        if self._suppress_select:
            return
        sel = self.conv_tree.selection()
        if sel and sel[0] != self.current:
            self.open_conv(sel[0])

    def update_header(self) -> None:
        conv = self.current_conv()
        for b in (self.btn_a, self.btn_b):
            b.pack_forget()
        if not conv:
            self.title_lbl.config(text="LANchat")
            self.sub_lbl.config(text="Nothing selected")
            self.entry.state(["disabled"]); self.send_btn.state(["disabled"])
            return
        active = self.is_active(conv)
        if conv.kind == "dm":
            peer = self.node.get_peer(conv.ref_id)
            title = self.label(conv.ref_id, conv.title)
            if active and peer:
                sub = f"Connected via LAN  |  {peer.host}:{peer.port}"
                self.btn_a.config(text="Disconnect", command=lambda: self.node.disconnect(conv.ref_id))
                self.btn_a.pack(side="right")
            else:
                sub = "Disconnected"
                if conv.last_addr:
                    self.btn_a.config(text="Reconnect", command=lambda: self._reconnect(conv))
                    self.btn_a.pack(side="right")
        else:
            title = conv.title
            snap = next((g for g in self.node.groups() if g["group_id"] == conv.ref_id), None)
            if snap and active:
                names = ", ".join(snap["members"].values())
                sub = f"Group  |  {len(snap['members'])} members: {names}  |  via LAN"
                self.btn_a.config(text="Leave group" if not snap["is_host"] else "Close group",
                                  command=lambda: self.node.leave_group(conv.ref_id))
                self.btn_a.pack(side="right")
                if snap["is_host"]:
                    self.btn_b.config(text="Invite...", command=lambda: self.on_invite(conv.ref_id))
                    self.btn_b.pack(side="right", padx=(0, 8))
            else:
                sub = "Group closed or no longer available"
        self.title_lbl.config(text=title)
        self.sub_lbl.config(text=sub)
        state = ["!disabled"] if active else ["disabled"]
        self.entry.state(state); self.send_btn.state(state)

    def _reconnect(self, conv: Conversation) -> None:
        if conv.last_addr:
            host, port = conv.last_addr
            self.node.connect(host, port, expected_id=conv.ref_id)
            self.notify(f"Sending a new connection request to {conv.title}...")

    # ------------------------------------------------------------------ sidebar lists
    def refresh_lists(self) -> None:
        self._refresh_devices()
        self._refresh_convs()
        self._update_connect_button()

    def _device_rows(self) -> list[tuple[str, str, str, str, Optional[NearbyDevice]]]:
        devs = {d.device_id: d for d in self.engine.manager.devices()}
        peers = {p.peer_id: p for p in self.node.peers()}
        entries = []
        for pid in set(devs) | set(peers):
            name = peers[pid].name if pid in peers else devs[pid].name
            host = peers[pid].host if pid in peers else devs[pid].host
            entries.append((pid, name, host))
        self.labels = dict(display_labels(entries))
        for conv in self.convs.values():
            if conv.kind == "dm":
                conv.title = self.labels.get(conv.ref_id, conv.title)
        self.device_cache = devs
        rows = []
        for pid, name, host in sorted(entries, key=lambda e: (e[1].casefold(), e[0])):
            p = peers.get(pid)
            if p is None:
                status, icon = "Online", "online"
            elif p.state == PeerState.CONNECTED:
                status, icon = "Connected", "connected"
            elif p.state == PeerState.PENDING_IN:
                status, icon = "Wants to chat", "wants"
            else:
                status, icon = "Connecting...", "connecting"
            rows.append((pid, self.labels[pid], status, icon, devs.get(pid)))
        peer_hosts = {p.host for p in peers.values()}
        for host, port in sorted(self.dialing):
            if host not in peer_hosts:
                rows.append((f"dial:{host}:{port}", f"{host}:{port}", "Connecting...", "connecting", None))
        return rows

    def _refresh_devices(self) -> None:
        rows = self._device_rows()
        keep = self.dev_tree.selection()
        self._suppress_select = True
        try:
            self.dev_tree.delete(*self.dev_tree.get_children())
            for iid, label, status, icon, _dev in rows:
                self.dev_tree.insert("", "end", iid=iid, text="  " + label, values=(status,),
                                     image=self.icons[icon], tags=(icon,))
            if keep and self.dev_tree.exists(keep[0]):
                self.dev_tree.selection_set(keep[0])
        finally:
            self._suppress_select = False
        if rows:
            self.dev_empty.place_forget()
        else:
            self.dev_empty.place(relx=0.5, rely=0.5, anchor="center")

    def _refresh_convs(self) -> None:
        self._suppress_select = True
        try:
            self.conv_tree.delete(*self.conv_tree.get_children())
            for cid, conv in self.convs.items():
                active = self.is_active(conv)
                icon = "connected" if active else "offline"
                prefix = "" if conv.kind == "dm" else "Group: "
                text = "  " + prefix + (self.labels.get(conv.ref_id, conv.title) if conv.kind == "dm" else conv.title)
                self.conv_tree.insert("", "end", iid=cid, text=text, image=self.icons[icon],
                                      values=(f"({conv.unread})" if conv.unread else "",),
                                      tags=("connected" if active else "offline",))
            if self.current and self.conv_tree.exists(self.current):
                self.conv_tree.selection_set(self.current)
        finally:
            self._suppress_select = False
        if self.convs:
            self.conv_empty.place_forget()
        else:
            self.conv_empty.place(relx=0.5, rely=0.5, anchor="center")

    def _update_connect_button(self) -> None:
        sel = self.dev_tree.selection()
        text, enabled = "Connect", False
        if sel and not sel[0].startswith("dial:"):
            peer = self.node.get_peer(sel[0])
            if peer is None:
                enabled = sel[0] in self.device_cache
            elif peer.state == PeerState.CONNECTED:
                text, enabled = "Open chat", True
            elif peer.state == PeerState.PENDING_IN:
                text, enabled = "Accept", True
        self.connect_btn.config(text=text)
        self.connect_btn.state(["!disabled"] if enabled else ["disabled"])

    def _update_chip(self) -> None:
        now = time.monotonic()
        if self._selection is None or now - self._selection_at > 3:
            self._selection, self._selection_at = self.engine.manager.select(), now
        sel = self._selection
        lan = self.engine.lan.status()
        if sel.transport is None:
            icon, title = "error", "No connection"
        elif lan.verified:
            icon, title = "online", f"Connected via {sel.method}"
        else:
            icon, title = "connecting", f"{sel.method} ready"
        self.chip_icon.configure(image=self.icons[icon])
        self.chip_title.config(text=title)
        extra = "" if self.engine.settings.discoverable else "  (hidden from others)"
        self.chip_text.config(text=sel.message + extra)
        self.you_label.config(text=f"You: {self.node.name}")

    # ------------------------------------------------------------------ actions
    def on_connect_selected(self) -> None:
        sel = self.dev_tree.selection()
        if not sel or sel[0].startswith("dial:"):
            return
        pid = sel[0]
        peer = self.node.get_peer(pid)
        if peer and peer.state == PeerState.CONNECTED:
            self.open_conv(f"dm:{pid}")
        elif peer and peer.state == PeerState.PENDING_IN:
            self.node.accept_request(pid)
        elif peer is None and pid in self.device_cache:
            dev = self.device_cache[pid]
            ok, why = self.engine.manager.connect(dev)
            self.notify(f"Request sent to {self.label(pid, dev.name)} - waiting for them to accept..."
                        if ok else why, error=not ok)
        self.refresh_lists()

    def on_manual(self) -> None:
        def go(host: str, port: int) -> None:
            self._manual.add((host, port))
            self.node.connect(host, port)
        ManualConnectDialog(self.root, go)

    def on_settings(self) -> None:
        def save(new: Settings) -> None:
            dark_changed = new.dark_mode != self.engine.settings.dark_mode
            self.engine.apply_settings(new)
            if self.settings_file:
                if not save_settings(self.settings_file, new):
                    self.notify("Could not save settings to disk (they apply for this session).", error=True)
            if dark_changed:
                self.palette = theme.apply_theme(self.root, new.dark_mode)
                self._make_icons()
                self._apply_palette()
            self._selection = None
            self.refresh_lists()
            self.update_header()
        SettingsDialog(self.root, self.engine.settings, self.engine.manager.statuses(), self.node.port, save)

    def _connected_peers(self, exclude: set[str] = frozenset()) -> list[tuple[str, str]]:
        return [(p.peer_id, self.label(p.peer_id, p.name)) for p in self.node.peers()
                if p.state == PeerState.CONNECTED and p.peer_id not in exclude]

    def on_new_group(self) -> None:
        def go(name: str, chosen: list[str]) -> None:
            gid = self.node.create_group(name)
            for pid in chosen:
                self.node.invite_to_group(gid, pid)
            snap = next(g for g in self.node.groups() if g["group_id"] == gid)
            conv = self.ensure_group(snap)
            self.open_conv(conv.conv_id)
            if chosen:
                self.add_sys(conv, f"Invitations sent to {len(chosen)} PC(s). They must accept to join.")
        PickPeersDialog(self.root, "New group", self._connected_peers(), True, "Create group", go)

    def on_invite(self, group_id: str) -> None:
        snap = next((g for g in self.node.groups() if g["group_id"] == group_id), None)
        if not snap:
            return
        peers = self._connected_peers(exclude=set(snap["members"]))
        conv = self.convs.get(f"grp:{group_id}")

        def go(_name: str, chosen: list[str]) -> None:
            for pid in chosen:
                self.node.invite_to_group(group_id, pid)
            if conv and chosen:
                self.add_sys(conv, f"Invitations sent to {len(chosen)} PC(s).")
        if not peers:
            messagebox.showinfo("Invite", "Every connected PC is already in this group, "
                                          "or no PC is connected yet.", parent=self.root)
            return
        PickPeersDialog(self.root, "Invite to group", peers, False, "Send invitations", go)

    def send_message(self) -> None:
        conv = self.current_conv()
        text = self.entry_var.get().strip()
        if not conv or not text:
            return
        if len(text) > MAX_TEXT_CHARS:
            self.notify(f"Message too long ({len(text)}/{MAX_TEXT_CHARS} characters).", error=True)
            return
        try:
            if conv.kind == "dm":
                mid = self.node.send_dm(conv.ref_id, text)
            else:
                mid = self.node.send_group(conv.ref_id, text)
        except ValueError as exc:
            self.notify(f"Message not sent: {exc}", error=True)
            return
        if mid is None:
            self.notify("Message not sent: you are not connected.", error=True)
            self.update_header()
            return
        self.entry_var.set("")
        self.add_message(conv, "You", text, "me")

    # ------------------------------------------------------------------ requests banner
    def add_request(self, key: str, text: str, yes: str, no: str,
                    on_yes: Callable[[], None], on_no: Callable[[], None]) -> None:
        self.remove_request(key)
        row = tk.Frame(self.requests_frame, bg=self.palette["banner"], padx=10, pady=6)
        row.grid(sticky="ew", pady=(0, 4))
        row.columnconfigure(0, weight=1)
        tk.Label(row, text=text, bg=self.palette["banner"], fg=self.palette["text"], anchor="w",
                 justify="left", wraplength=560, font=theme.font(10)).grid(row=0, column=0, sticky="w")

        def wrap(fn: Callable[[], None]) -> Callable[[], None]:
            def run() -> None:
                self.remove_request(key)
                fn()
                self.refresh_lists()
            return run
        ttk.Button(row, text=yes, command=wrap(on_yes), style=theme.ACCENT_STYLE).grid(row=0, column=1, padx=(8, 4))
        ttk.Button(row, text=no, command=wrap(on_no)).grid(row=0, column=2)
        self.requests[key] = row  # type: ignore[assignment]

    def remove_request(self, key: str) -> None:
        row = self.requests.pop(key, None)
        if row is not None:
            row.destroy()

    # ------------------------------------------------------------------ events
    def pump(self) -> None:
        if self._closing:
            return
        try:
            for _ in range(200):
                self.handle(self.engine.events.get_nowait())
        except queue.Empty:
            pass
        except Exception as exc:  # a UI bug must not stop the pump
            self.notify(f"Internal UI error: {exc}", error=True)
        self.root.after(100, self.pump)

    def tick(self) -> None:
        if self._closing:
            return
        self.refresh_lists()
        self._update_chip()
        self.update_header()
        self.root.after(1000, self.tick)

    def handle(self, ev: Event) -> None:
        d, kind = ev.data, ev.kind
        if kind == "devices_changed":
            self.refresh_lists()
        elif kind == "connecting":
            self.dialing.add((d["host"], d["port"]))
            self.refresh_lists()
        elif kind == "connect_failed":
            key = (d["host"], d["port"])
            self.dialing.discard(key)
            self.notify(f"Could not connect to {d['host']}:{d['port']} - {d['reason']}", error=True, seconds=12)
            if key in self._manual:
                self._manual.discard(key)
                messagebox.showerror("Connection failed", f"{d['host']}:{d['port']}\n\n{d['reason']}", parent=self.root)
            self.refresh_lists()
        elif kind == "incoming_request":
            peer: PeerInfo = d["peer"]
            pid = peer.peer_id
            self.refresh_lists()
            self.add_request(f"req:{pid}", f"{self.label(pid, peer.name)} ({peer.host}) wants to chat with you.",
                             "Accept", "Reject",
                             lambda: self.node.accept_request(pid), lambda: self.node.reject_request(pid))
            self.root.bell()
        elif kind == "peer_state":
            self._on_peer_state(d["peer"], d.get("reason", ""))
        elif kind == "request_rejected":
            peer = d["peer"]
            self.notify(f"{peer.name} declined your request.", error=True)
        elif kind == "chat":
            self._on_chat(d)
        elif kind == "group_update":
            conv = self.ensure_group(d["group"])
            if self.current is None:
                self.open_conv(conv.conv_id)
            self.refresh_lists()
            self.update_header()
        elif kind == "group_closed":
            conv = self.convs.get(f"grp:{d['group_id']}")
            if conv:
                conv.closed = True
                self.add_sys(conv, d["reason"])
            self.refresh_lists()
            self.update_header()
        elif kind == "group_invite":
            gid = d["group_id"]
            self.add_request(f"inv:{gid}", f"{d['host_name']} invited you to the group \"{d['group_name']}\".",
                             "Join", "Decline",
                             lambda: self.node.accept_group_invite(gid), lambda: self.node.decline_group_invite(gid))
            self.root.bell()
        elif kind == "group_invite_cancelled":
            self.remove_request(f"inv:{d['group_id']}")
        elif kind == "group_invite_declined":
            conv = self.convs.get(f"grp:{d['group_id']}")
            if conv:
                self.add_sys(conv, f"{d['name']} declined the invitation.")
        elif kind == "notice":
            self.notify(d["text"])

    def _on_peer_state(self, peer: PeerInfo, reason: str) -> None:
        pid = peer.peer_id
        self.dialing = {k for k in self.dialing if k[0] != peer.host}
        if peer.state == PeerState.CONNECTED:
            self.remove_request(f"req:{pid}")
            conv = self.ensure_dm(peer)
            if reason == "accepted":
                self.status_var.set("")  # drop the stale "waiting for them to accept" notice
                self.add_sys(conv, f"Connected to {peer.name} via LAN ({peer.host}:{peer.port}).")
                if self.current is None or self.current.startswith("dm:"):
                    self.open_conv(conv.conv_id)
        elif peer.state in (PeerState.DISCONNECTED, PeerState.REJECTED):
            self.remove_request(f"req:{pid}")
            conv = self.convs.get(f"dm:{pid}")
            if conv and peer.state == PeerState.DISCONNECTED:
                self.add_sys(conv, f"Disconnected ({reason or 'connection closed'}).")
        self.refresh_lists()
        self.update_header()

    def _on_chat(self, d: dict) -> None:
        if d["conv"] == "dm":
            conv = self.convs.get(f"dm:{d['peer_id']}")
            if conv is None:
                peer = self.node.get_peer(d["peer_id"])
                if peer is None:
                    return
                conv = self.ensure_dm(peer)
            sender = self.label(d["peer_id"], d["sender_name"])
        else:
            conv = self.convs.get(f"grp:{d['group_id']}")
            if conv is None:
                return
            sender = d["sender_name"]
        self.add_message(conv, sender, d["text"], "other")

    # ------------------------------------------------------------------ shutdown
    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.root.withdraw()
        try:
            self.engine.stop()
        finally:
            self.root.destroy()
