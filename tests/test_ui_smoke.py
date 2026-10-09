"""UI smoke test: two real Tk windows talk to each other over loopback.
Skipped automatically when no display is available (use `xvfb-run -a pytest` on Linux).
Set LANCHAT_SCREENSHOT_DIR to also save PNG screenshots (needs ImageMagick `import`)."""
import os
import subprocess
import time
import tkinter as tk
from tkinter import ttk

import pytest

from lanchat.engine import Engine
from lanchat.settings import Settings

from .conftest import free_udp_port


def _display_ok():
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except tk.TclError:
        return False


pytestmark = pytest.mark.skipif(not _display_ok(), reason="no display available for Tk")


def make_window(root, name, my_port, other_port, tcp_port, x):
    from lanchat.ui.app import MainWindow
    eng = Engine(Settings(name, tcp_port=tcp_port), bind_host="127.0.0.1", discovery_port=my_port,
                 discovery_bind_host="127.0.0.1", discovery_targets=[("127.0.0.1", other_port)])
    eng.discovery._interval = 0.1
    eng.discovery._local_addresses = lambda: []
    eng.lan._local_addresses = lambda: ["127.0.0.1"]
    eng.start()
    root.geometry(f"+{x}+0")
    return MainWindow(root, eng, None)


def spin(roots, cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for r in roots:
            r.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def shot(name):
    d = os.environ.get("LANCHAT_SCREENSHOT_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        subprocess.run(["import", "-window", "root", os.path.join(d, name)], check=False)


def press(frame, index):
    [w for w in frame.winfo_children() if isinstance(w, ttk.Button)][index].invoke()


def test_two_windows_discover_connect_and_chat():
    ra, rb = tk.Tk(), tk.Tk()
    pa, pb = free_udp_port(), free_udp_port()
    a = make_window(ra, "Alice-PC", pa, pb, 50710, 0)
    b = make_window(rb, "Bob-PC", pb, pa, 50711, 1040)
    roots = [ra, rb]
    try:
        shot("1_empty_or_searching.png")
        assert spin(roots, lambda: len(a.dev_tree.get_children()) == 1 and len(b.dev_tree.get_children()) == 1)
        shot("2_discovered.png")
        iid = a.dev_tree.get_children()[0]
        a.dev_tree.selection_set(iid)
        ra.update()
        assert str(a.connect_btn.cget("text")) == "Connect"
        a.on_connect_selected()
        assert spin(roots, lambda: len(b.requests) == 1)         # request banner on Bob's side
        shot("3_request.png")
        press(list(b.requests.values())[0], 0)                    # Bob presses Accept
        assert spin(roots, lambda: a.convs and b.convs and a.is_active(next(iter(a.convs.values()))))
        a.entry_var.set("hello Bob")
        a.send_message()
        assert spin(roots, lambda: "hello Bob" in b.chat.get("1.0", "end"))
        b.open_conv(next(iter(b.convs)))
        b.entry_var.set("hi Alice, 👋")
        b.send_message()
        assert spin(roots, lambda: "hi Alice" in a.chat.get("1.0", "end"))
        shot("4_chatting.png")

        # group flow through the real UI methods
        gid = a.node.create_group("Study group")
        for p in a.node.peers():
            a.node.invite_to_group(gid, p.peer_id)
        assert spin(roots, lambda: any(k.startswith("inv:") for k in b.requests))
        shot("5_group_invite.png")
        press(b.requests[next(k for k in b.requests if k.startswith("inv:"))], 0)  # Join
        assert spin(roots, lambda: any(c.kind == "group" for c in b.convs.values()))
        gconv = next(c for c in a.convs.values() if c.kind == "group")
        a.open_conv(gconv.conv_id)
        a.entry_var.set("group hello")
        a.send_message()
        bconv = next(c for c in b.convs.values() if c.kind == "group")
        b.open_conv(bconv.conv_id)
        assert spin(roots, lambda: "group hello" in b.chat.get("1.0", "end"))
        shot("6_group.png")

        # disconnect shows in both UIs
        a.node.disconnect(next(iter(a.node.peers())).peer_id)
        assert spin(roots, lambda: "Disconnected" in str(a.sub_lbl.cget("text")) or a.node.peers() == [])
    finally:
        for w, r in ((a, ra), (b, rb)):
            try:
                w.close()
            except tk.TclError:
                pass
