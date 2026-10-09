"""Node tests. Everything runs over 127.0.0.1 loopback inside one process -
these are automated loopback tests, NOT real multi-PC LAN tests."""
import json
import socket
import threading
import time

import pytest

from lanchat import protocol as p
from lanchat.naming import display_labels
from lanchat.networking import LanNode, PeerState

from .conftest import wait_for


class Collector:
    """Thread-safe event sink with simple queries."""

    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def __call__(self, event):
        with self._lock:
            self.events.append(event)

    def of(self, kind, **match):
        with self._lock:
            return [e for e in self.events
                    if e.kind == kind and all(e.data.get(k) == v for k, v in match.items())]


class Harness:
    def __init__(self):
        self.nodes = []

    def node(self, name, **kw):
        sink = Collector()
        kw.setdefault("heartbeat_interval", 0.5)
        n = LanNode(name, host="127.0.0.1", port=0, on_event=sink, **kw)
        n.start()
        n.sink = sink
        self.nodes.append(n)
        return n

    def link(self, a, b):
        """a asks b, b accepts (works for reconnects too: waits for a *new* request)."""
        def requests():
            return [e for e in b.sink.of("incoming_request") if e.data["peer"].peer_id == a.node_id]

        before = len(requests())
        a.connect("127.0.0.1", b.port)
        wait_for(lambda: len(requests()) > before)
        wait_for(lambda: (pi := b.get_peer(a.node_id)) and pi.state == PeerState.PENDING_IN)
        assert b.accept_request(a.node_id)
        wait_for(lambda: (pi := a.get_peer(b.node_id)) and pi.state == PeerState.CONNECTED)
        wait_for(lambda: (pi := b.get_peer(a.node_id)) and pi.state == PeerState.CONNECTED)

    def close(self):
        for n in self.nodes:
            n.stop()


@pytest.fixture
def net():
    h = Harness()
    yield h
    h.close()


class RawPeer:
    """Hand-rolled client for protocol-abuse tests."""

    def __init__(self, port, host="127.0.0.1", source=None):
        self.sock = socket.create_connection((host, port), timeout=3, source_address=source)
        self.buf = p.FrameBuffer()
        self.inbox = []
        self.node_id = p.new_id()

    def send_raw(self, data: bytes):
        self.sock.sendall(data)

    def send(self, mtype, **fields):
        self.sock.sendall(p.encode(mtype, **fields))

    def hello(self, name="Raw", node_id=None):
        self.send("hello", node_id=node_id or self.node_id, name=name, port=40000)

    def read(self, timeout=1.0):
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except (socket.timeout, OSError):
            return None
        if not data:
            return b""
        for line in self.buf.feed(data):
            self.inbox.append(p.decode(line))
        return data

    def wait_type(self, mtype, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.inbox:
                if m["type"] == mtype:
                    return m
            if self.read(0.2) == b"":
                break
        return None

    def is_closed(self, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.read(0.2) == b"":
                return True
        return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ======================================================================
# connection requests
# ======================================================================
def test_request_accept_and_chat_both_ways(net):
    a, b = net.node("Alice"), net.node("Bob")
    net.link(a, b)
    assert a.send_dm(b.node_id, "hi bob")
    assert b.send_dm(a.node_id, "hi alice")
    wait_for(lambda: b.sink.of("chat", text="hi bob"))
    wait_for(lambda: a.sink.of("chat", text="hi alice"))
    ev = b.sink.of("chat", text="hi bob")[0].data
    assert ev["sender_name"] == "Alice" and ev["conv"] == "dm" and ev["peer_id"] == a.node_id


def test_pending_states_visible_to_both_sides(net):
    a, b = net.node("Alice"), net.node("Bob")
    a.connect("127.0.0.1", b.port)
    wait_for(lambda: (pi := a.get_peer(b.node_id)) and pi.state == PeerState.PENDING_OUT)
    wait_for(lambda: (pi := b.get_peer(a.node_id)) and pi.state == PeerState.PENDING_IN)
    assert a.send_dm(b.node_id, "too early") is None  # no chat before acceptance


def test_rejection(net):
    a, b = net.node("Alice"), net.node("Bob")
    a.connect("127.0.0.1", b.port)
    wait_for(lambda: b.sink.of("incoming_request"))
    assert b.reject_request(a.node_id)
    wait_for(lambda: a.sink.of("request_rejected"))
    wait_for(lambda: a.get_peer(b.node_id) is None and b.get_peer(a.node_id) is None)
    assert a.send_dm(b.node_id, "hello?") is None
    assert not b.sink.of("chat")


def test_multiple_clients_connect_to_one_node(net):
    hub = net.node("Hub")
    clients = [net.node(f"C{i}") for i in range(4)]
    for c in clients:
        net.link(c, hub)
    wait_for(lambda: len([x for x in hub.peers() if x.state == PeerState.CONNECTED]) == 4)
    for i, c in enumerate(clients):
        c.send_dm(hub.node_id, f"msg{i}")
    wait_for(lambda: len(hub.sink.of("chat")) == 4)
    assert sorted(e.data["text"] for e in hub.sink.of("chat")) == [f"msg{i}" for i in range(4)]


def test_chat_before_accept_is_ignored_and_connection_dropped(net):
    b = net.node("Bob")
    raw = RawPeer(b.port)
    raw.hello("Mallory")
    assert raw.wait_type("hello")
    for i in range(8):  # never sent connect_request / never accepted
        try:
            raw.send("chat", id=f"m{i}", text="let me in")
        except OSError:
            break
    assert raw.is_closed()
    assert not b.sink.of("chat")
    raw.close()


def test_chat_after_request_but_before_accept_ignored(net):
    b = net.node("Bob")
    raw = RawPeer(b.port)
    raw.hello("Mallory")
    raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    raw.send("chat", id="m1", text="sneaky")
    time.sleep(0.3)
    assert not b.sink.of("chat")
    b.accept_request(raw.node_id)
    raw.wait_type("connect_accept")
    raw.send("chat", id="m2", text="now allowed")
    wait_for(lambda: b.sink.of("chat", text="now allowed"))
    raw.close()


def test_self_connection_rejected(net):
    a = net.node("Alice")
    a.connect("127.0.0.1", a.port)
    wait_for(lambda: a.sink.of("connect_failed") or a.sink.of("notice"))
    assert a.peers() == []


def test_connect_to_dead_port_reports_friendly_failure(net):
    a = net.node("Alice")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    a.connect("127.0.0.1", dead)
    ev = wait_for(lambda: a.sink.of("connect_failed"))[0]
    assert "refused" in ev.data["reason"].lower() or "Timed out" in ev.data["reason"]


def test_connect_invalid_port(net):
    a = net.node("Alice")
    assert a.connect("127.0.0.1", 99999) is False
    assert a.sink.of("connect_failed")


# ======================================================================
# duplicates / spoofing
# ======================================================================
def test_duplicate_usernames_are_separate_peers_with_distinct_labels(net):
    hub = net.node("Hub")
    s1, s2 = net.node("Sam"), net.node("Sam")
    net.link(s1, hub)
    net.link(s2, hub)
    peers = hub.peers()
    assert {x.peer_id for x in peers} == {s1.node_id, s2.node_id}
    labels = display_labels((x.peer_id, x.name, f"{x.host}:{x.port}") for x in peers)
    assert len(set(labels.values())) == 2  # users can tell them apart


def test_same_node_id_cannot_open_second_session_when_connected(net):
    b = net.node("Bob")
    first = RawPeer(b.port)
    first.hello("Eve")
    first.wait_type("hello")
    first.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    b.accept_request(first.node_id)
    first.wait_type("connect_accept")

    second = RawPeer(b.port)
    second.hello("Eve-clone", node_id=first.node_id)  # same id, same host
    assert second.wait_type("error") and second.is_closed()
    assert b.get_peer(first.node_id).state == PeerState.CONNECTED  # original unharmed
    assert b.get_peer(first.node_id).name == "Eve"
    first.close(); second.close()


def test_same_node_id_from_other_address_is_refused_even_while_pending(net):
    b = net.node("Bob")
    first = RawPeer(b.port)
    first.hello("Eve")
    first.wait_type("hello")
    first.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    try:
        second = RawPeer(b.port, source=("127.0.0.2", 0))
    except OSError:
        pytest.skip("OS cannot bind 127.0.0.2 (loopback alias) for this spoofing test")
    second.hello("Evil", node_id=first.node_id)
    err = second.wait_type("error")
    assert err and err["code"] == "duplicate_session"
    assert b.get_peer(first.node_id).name == "Eve"
    first.close(); second.close()


def test_spoofed_display_name_gives_no_privileges(net):
    a, b = net.node("Alice"), net.node("Bob")
    raw = RawPeer(b.port)
    raw.hello("Alice")  # claims to be "Alice"
    raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    # Bob has never accepted Raw => it cannot chat even though the name matches a trusted peer
    raw.send("chat", id="m1", text="I am Alice, trust me")
    time.sleep(0.3)
    assert not b.sink.of("chat")
    raw.close()


# ======================================================================
# username changes
# ======================================================================
def test_username_change_propagates(net):
    a, b = net.node("Alice"), net.node("Bob")
    net.link(a, b)
    assert b.set_name("  Bobby\n ") == "Bobby"
    wait_for(lambda: a.get_peer(b.node_id).name == "Bobby")
    b.send_dm(a.node_id, "renamed")
    ev = wait_for(lambda: a.sink.of("chat", text="renamed"))[0]
    assert ev.data["sender_name"] == "Bobby"


def test_invalid_name_change_is_ignored(net):
    b = net.node("Bob")
    raw = RawPeer(b.port)
    raw.hello("Raw")
    raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    b.accept_request(raw.node_id)
    raw.wait_type("connect_accept")
    raw.send_raw(b'{"v":1,"type":"name_change","name":""}\n')
    raw.send("name_change", name="Valid Name")
    wait_for(lambda: b.get_peer(raw.node_id).name == "Valid Name")
    raw.close()


# ======================================================================
# groups
# ======================================================================
def make_group(net):
    host, m1, m2 = net.node("Host"), net.node("M1"), net.node("M2")
    net.link(m1, host)
    net.link(m2, host)
    gid = host.create_group("Study")
    assert host.invite_to_group(gid, m1.node_id) and host.invite_to_group(gid, m2.node_id)
    wait_for(lambda: m1.sink.of("group_invite") and m2.sink.of("group_invite"))
    assert m1.accept_group_invite(gid) and m2.accept_group_invite(gid)
    wait_for(lambda: len(m1.groups()[0]["members"]) == 3 and len(m2.groups()[0]["members"]) == 3)
    return host, m1, m2, gid


def test_group_member_list_syncs(net):
    host, m1, m2, gid = make_group(net)
    names = sorted(m1.groups()[0]["members"].values())
    assert names == ["Host", "M1", "M2"]
    assert host.groups()[0]["is_host"] and not m1.groups()[0]["is_host"]


def test_group_message_from_member_reaches_everyone_else_once(net):
    host, m1, m2, gid = make_group(net)
    assert m1.send_group(gid, "hello team")
    wait_for(lambda: host.sink.of("chat", text="hello team") and m2.sink.of("chat", text="hello team"))
    time.sleep(0.2)
    assert len(m2.sink.of("chat", text="hello team")) == 1
    assert not m1.sink.of("chat", text="hello team")  # no echo to the sender
    ev = m2.sink.of("chat", text="hello team")[0].data
    assert ev["sender_name"] == "M1" and ev["conv"] == "group" and ev["group_id"] == gid


def test_group_message_from_host_reaches_all_members(net):
    host, m1, m2, gid = make_group(net)
    host.send_group(gid, "from host")
    wait_for(lambda: m1.sink.of("chat", text="from host") and m2.sink.of("chat", text="from host"))


def test_group_invite_requires_acceptance_and_can_be_declined(net):
    host, m1 = net.node("Host"), net.node("M1")
    net.link(m1, host)
    gid = host.create_group("Secret")
    host.invite_to_group(gid, m1.node_id)
    wait_for(lambda: m1.sink.of("group_invite"))
    assert m1.groups() == []                       # not a member until accepted
    host.send_group(gid, "private")
    time.sleep(0.2)
    assert not m1.sink.of("chat")
    assert m1.decline_group_invite(gid)
    wait_for(lambda: host.sink.of("group_invite_declined"))
    assert list(host.groups()[0]["members"]) == [host.node_id]


def test_member_leaves_group(net):
    host, m1, m2, gid = make_group(net)
    assert m1.leave_group(gid)
    wait_for(lambda: len(host.groups()[0]["members"]) == 2 and len(m2.groups()[0]["members"]) == 2)
    host.send_group(gid, "after leave")
    wait_for(lambda: m2.sink.of("chat", text="after leave"))
    assert not m1.sink.of("chat", text="after leave")


def test_host_closes_group(net):
    host, m1, m2, gid = make_group(net)
    host.leave_group(gid)
    wait_for(lambda: m1.sink.of("group_closed") and m2.sink.of("group_closed"))
    assert m1.groups() == [] and m2.groups() == []


def test_member_disconnect_removes_it_from_group(net):
    host, m1, m2, gid = make_group(net)
    m1.stop()
    wait_for(lambda: len(host.groups()[0]["members"]) == 2)
    wait_for(lambda: len(m2.groups()[0]["members"]) == 2)


def test_host_disconnect_closes_group_for_members(net):
    host, m1, m2, gid = make_group(net)
    host.stop()
    wait_for(lambda: m1.sink.of("group_closed") and m2.sink.of("group_closed"))


def test_group_rename_of_member_updates_roster(net):
    host, m1, m2, gid = make_group(net)
    m1.set_name("Renamed")
    wait_for(lambda: "Renamed" in m2.groups()[0]["members"].values())


def test_group_send_by_non_member_is_ignored(net):
    host = net.node("Host")
    gid = host.create_group("Closed")
    raw = RawPeer(host.port)
    raw.hello("Raw"); raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: host.sink.of("incoming_request"))
    host.accept_request(raw.node_id)
    raw.wait_type("connect_accept")
    raw.send("group_send", group_id=gid, text="not a member")
    raw.send("group_accept", group_id=gid)  # never invited
    time.sleep(0.3)
    assert not host.sink.of("chat")
    assert list(host.groups()[0]["members"]) == [host.node_id]
    raw.close()


def test_forged_group_chat_from_non_host_ignored(net):
    host, m1, m2, gid = make_group(net)
    # M2 is connected to the host only; make a raw "peer" connect to M1 and pretend to be the host
    raw = RawPeer(m1.port)
    raw.hello("FakeHost"); raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: any(e.data["peer"].peer_id == raw.node_id for e in m1.sink.of("incoming_request")))
    m1.accept_request(raw.node_id)
    raw.wait_type("connect_accept")
    raw.send("group_chat", group_id=gid, sender_id=host.node_id, sender_name="Host", id="x1", text="forged")
    time.sleep(0.3)
    assert not m1.sink.of("chat", text="forged")
    raw.close()


# ======================================================================
# disconnect / reconnect
# ======================================================================
def test_graceful_disconnect_then_reconnect(net):
    a, b = net.node("Alice"), net.node("Bob")
    net.link(a, b)
    assert a.disconnect(b.node_id)
    wait_for(lambda: a.get_peer(b.node_id) is None and b.get_peer(a.node_id) is None)
    assert b.sink.of("peer_state", reason="peer left") or b.sink.of("peer_state")
    assert a.send_dm(b.node_id, "gone") is None
    net.link(a, b)  # reconnect: needs a fresh accept
    a.send_dm(b.node_id, "back again")
    wait_for(lambda: b.sink.of("chat", text="back again"))


def test_abrupt_peer_close_is_detected(net):
    b = net.node("Bob")
    raw = RawPeer(b.port)
    raw.hello("Raw"); raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    b.accept_request(raw.node_id)
    raw.sock.close()  # no bye
    wait_for(lambda: b.get_peer(raw.node_id) is None)
    final = [e for e in b.sink.of("peer_state") if e.data["peer"].peer_id == raw.node_id][-1]
    assert final.data["peer"].state == PeerState.DISCONNECTED


def test_heartbeat_keeps_idle_connection_alive(net):
    kw = dict(heartbeat_interval=0.2, idle_timeout=1.0)
    a, b = net.node("Alice", **kw), net.node("Bob", **kw)
    net.link(a, b)
    time.sleep(2.5)
    assert a.get_peer(b.node_id).state == PeerState.CONNECTED


def test_silent_peer_times_out(net):
    b = net.node("Bob", idle_timeout=1.0, heartbeat_interval=0.2)
    raw = RawPeer(b.port)
    raw.hello("Raw"); raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    b.accept_request(raw.node_id)
    # raw never sends anything again -> idle timeout
    wait_for(lambda: b.get_peer(raw.node_id) is None, timeout=4)
    raw.close()


def test_unanswered_request_expires(net):
    a = net.node("Alice")
    b = net.node("Bob", request_timeout=0.6)
    a.connect("127.0.0.1", b.port)
    wait_for(lambda: b.sink.of("incoming_request"))
    wait_for(lambda: a.sink.of("request_rejected"), timeout=4)


# ======================================================================
# malformed / oversized input must never hurt the node
# ======================================================================
def test_garbage_does_not_crash_node_and_others_keep_working(net):
    b = net.node("Bob")
    a = net.node("Alice")
    net.link(a, b)
    raw = RawPeer(b.port)
    raw.hello("Raw"); raw.wait_type("hello")
    for junk in (b"not json\n", b"[1,2]\n", b"\xff\xfe\n", b'{"v":1,"type":"zzz"}\n',
                 b'{"v":1,"type":"chat"}\n', b"{}\n"):
        try:
            raw.send_raw(junk)
        except OSError:
            break
    assert raw.is_closed()  # strikes exceeded
    a.send_dm(b.node_id, "still alive")
    wait_for(lambda: b.sink.of("chat", text="still alive"))
    raw.close()


def test_oversized_payload_closes_only_that_connection(net):
    b = net.node("Bob")
    a = net.node("Alice")
    net.link(a, b)
    raw = RawPeer(b.port)
    raw.hello("Raw"); raw.wait_type("hello")
    try:
        raw.send_raw(b'{"v":1,"type":"chat","id":"m","text":"' + b"A" * (p.MAX_FRAME_BYTES * 2) + b'"}\n')
    except OSError:
        pass
    assert raw.is_closed()
    wait_for(lambda: b.get_peer(raw.node_id) is None)
    a.send_dm(b.node_id, "fine")
    wait_for(lambda: b.sink.of("chat", text="fine"))
    raw.close()


def test_oversized_text_within_frame_limit_rejected(net):
    a, b = net.node("Alice"), net.node("Bob")
    net.link(a, b)
    with pytest.raises(ValueError):
        a.send_dm(b.node_id, "x" * (p.MAX_TEXT_CHARS + 1))
    with pytest.raises(ValueError):
        a.send_dm(b.node_id, "   ")


def test_partial_tcp_writes_are_reassembled(net):
    b = net.node("Bob")
    raw = RawPeer(b.port)
    raw.hello("Raw"); raw.wait_type("hello")
    raw.send("connect_request")
    wait_for(lambda: b.sink.of("incoming_request"))
    b.accept_request(raw.node_id)
    raw.wait_type("connect_accept")
    frame = p.encode("chat", id="m1", text="split across packets")
    for i in range(0, len(frame), 3):
        raw.sock.sendall(frame[i:i + 3])
        time.sleep(0.002)
    wait_for(lambda: b.sink.of("chat", text="split across packets"))
    # and two frames glued into one write
    raw.sock.sendall(p.encode("chat", id="m2", text="one") + p.encode("chat", id="m3", text="two"))
    wait_for(lambda: b.sink.of("chat", text="one") and b.sink.of("chat", text="two"))
    raw.close()


def test_handshake_timeout_drops_silent_socket(net):
    b = net.node("Bob", handshake_timeout=0.5)
    raw = RawPeer(b.port)  # connects, never says hello
    assert raw.is_closed(timeout=3)
    raw.close()


def test_too_many_unidentified_sockets_are_limited(net):
    from lanchat.networking import node as nodemod
    b = net.node("Bob", handshake_timeout=30)
    socks = [RawPeer(b.port) for _ in range(nodemod.MAX_UNIDENTIFIED + 5)]
    time.sleep(0.5)
    closed = sum(1 for s in socks if s.read(0.1) == b"")
    assert closed >= 5
    for s in socks:
        s.close()


# ======================================================================
# concurrency + lifecycle
# ======================================================================
def test_concurrent_writers_do_not_interleave_frames(net):
    a, b = net.node("Alice"), net.node("Bob")
    net.link(a, b)
    threads_n, per = 6, 60

    def blast(t):
        for i in range(per):
            a.send_dm(b.node_id, f"t{t}-{i}-" + "x" * 300)

    ts = [threading.Thread(target=blast, args=(t,)) for t in range(threads_n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    wait_for(lambda: len(b.sink.of("chat")) == threads_n * per, timeout=10)
    assert len({e.data["msg_id"] for e in b.sink.of("chat")}) == threads_n * per


def test_port_fallback_when_preferred_port_busy():
    first = LanNode("A", host="127.0.0.1", port=0)
    first.start()
    second = LanNode("B", host="127.0.0.1", port=first.port)
    try:
        second.start()
        assert second.port != first.port and second.port_fell_back
    finally:
        first.stop(); second.stop()


def test_safe_shutdown_closes_everything_and_is_idempotent(net):
    a, b = net.node("Alice"), net.node("Bob")
    net.link(a, b)
    port = a.port
    a.stop()
    a.stop()  # second call must be harmless
    wait_for(lambda: b.get_peer(a.node_id) is None)
    assert not any(t.is_alive() for t in a._threads) if a._threads else True
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)  # listener really closed


def test_shutdown_with_pending_connection_attempts(net):
    a = net.node("Alice")
    a.connect("127.0.0.1", 9)  # nothing there
    a.stop()
