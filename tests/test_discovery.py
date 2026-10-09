"""Discovery tests. Packet parsing/filtering is pure; the live tests use loopback UDP only
(NOT a real broadcast across a physical LAN)."""
import json

import pytest

from lanchat.discovery import DiscoveryService, build_announcement, parse_announcement
from lanchat.discovery import service as svc

from .conftest import FakeClock, free_udp_port, wait_for

A = "a" * 32
B = "b" * 32
C = "c" * 32


# ---------- packet parsing / filtering ----------
def test_build_and_parse_roundtrip():
    ann = parse_announcement(build_announcement(A, "Alice", 50500))
    assert (ann.node_id, ann.name, ann.port, ann.bye) == (A, "Alice", 50500, False)


def test_bye_packet():
    assert parse_announcement(build_announcement(A, "Alice", 50500, bye=True)).bye is True


def _pkt(**over):
    body = {"app": "LANchat", "dv": 1, "id": A, "name": "Alice", "port": 50500}
    body.update(over)
    return json.dumps(body).encode()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"garbage",
        b"\xff\xfe",
        b"[]",
        _pkt(app="OtherApp"),
        _pkt(dv=2),
        _pkt(dv=0),
        _pkt(dv="1"),
        _pkt(id="short"),
        _pkt(id=None),
        _pkt(name=""),
        _pkt(name=5),
        _pkt(port=0),
        _pkt(port=70000),
        _pkt(port="50500"),
        _pkt(port=True),
        _pkt(name="x" * 600),  # oversized datagram
    ],
)
def test_parse_rejects_invalid_or_foreign_packets(data):
    assert parse_announcement(data) is None


def test_parse_sanitizes_name():
    assert parse_announcement(_pkt(name="  Eve\n\x00 PC ")).name == "Eve PC"


def test_parse_ignores_non_bye_truthy_values():
    assert parse_announcement(_pkt(bye="yes")).bye is False


# ---------- device table (no sockets) ----------
def make(clock=None, **kw):
    clock = clock or FakeClock()
    s = DiscoveryService(C, "Me", 50500, clock=clock, local_addresses=lambda: [], **kw)
    return s, clock


def test_adds_device_with_source_ip_not_packet_claim():
    s, _ = make()
    assert s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 50501))
    (d,) = s.devices()
    assert (d.node_id, d.name, d.host, d.port) == (A, "Alice", "10.0.0.5", 50500)


def test_ignores_own_announcements():
    s, _ = make()
    assert not s.handle_datagram(build_announcement(C, "Me", 50500), ("10.0.0.9", 50501))
    assert s.devices() == []


def test_ignores_junk_datagrams():
    s, _ = make()
    assert not s.handle_datagram(b"hello", ("10.0.0.5", 1))
    assert s.devices() == []


def test_stale_devices_expire():
    s, clock = make()
    s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 1))
    clock.advance(svc.STALE_AFTER - 1)
    assert len(s.devices()) == 1
    clock.advance(2)
    assert s.devices() == []


def test_refresh_keeps_device_alive():
    s, clock = make()
    for _ in range(5):
        s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 1))
        clock.advance(svc.STALE_AFTER - 1)
    assert len(s.devices()) == 1


def test_bye_removes_device_only_from_same_host():
    s, _ = make()
    s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 1))
    assert not s.handle_datagram(build_announcement(A, "Alice", 50500, bye=True), ("10.0.0.99", 1))
    assert len(s.devices()) == 1
    assert s.handle_datagram(build_announcement(A, "Alice", 50500, bye=True), ("10.0.0.5", 1))
    assert s.devices() == []


def test_id_hijack_from_other_host_ignored_while_alive_but_allowed_after_stale():
    s, clock = make()
    s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 1))
    assert not s.handle_datagram(build_announcement(A, "Evil", 6666), ("10.0.0.66", 1))
    assert s.devices()[0].host == "10.0.0.5" and s.devices()[0].name == "Alice"
    clock.advance(svc.STALE_AFTER + 1)  # legit DHCP change after the old entry died
    assert s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.77", 1))
    assert s.devices()[0].host == "10.0.0.77"


def test_name_change_updates_entry():
    s, _ = make()
    s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 1))
    assert s.handle_datagram(build_announcement(A, "Alicia", 50500), ("10.0.0.5", 1))
    assert s.devices()[0].name == "Alicia"


def test_duplicate_usernames_are_distinct_devices():
    s, _ = make()
    s.handle_datagram(build_announcement(A, "Sam", 50500), ("10.0.0.5", 1))
    s.handle_datagram(build_announcement(B, "Sam", 50500), ("10.0.0.6", 1))
    assert [d.node_id for d in s.devices()] == [A, B]


def test_device_cap(monkeypatch):
    monkeypatch.setattr(svc, "MAX_DEVICES", 3)
    s, _ = make()
    for i in range(5):
        s.handle_datagram(build_announcement(f"{i:032x}", f"P{i}", 50500), ("10.0.0.%d" % (i + 1), 1))
    assert len(s.devices()) == 3


def test_on_change_called_on_add_and_expiry():
    calls = []
    clock = FakeClock()
    s = DiscoveryService(C, "Me", 1, clock=clock, local_addresses=lambda: [], on_change=lambda: calls.append(1))
    s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 1))
    assert len(calls) == 1
    s.handle_datagram(build_announcement(A, "Alice", 50500), ("10.0.0.5", 1))  # no change
    assert len(calls) == 1
    clock.advance(svc.STALE_AFTER + 1)
    s.devices()
    assert len(calls) == 2


# ---------- live loopback UDP ----------
@pytest.fixture
def pair():
    pa, pb = free_udp_port(), free_udp_port()
    kw = dict(bind_host="127.0.0.1", announce_interval=0.1, stale_after=2.0, local_addresses=lambda: [])
    a = DiscoveryService(A, "Alice", 50500, discovery_port=pa, targets=[("127.0.0.1", pb)], **kw)
    b = DiscoveryService(B, "Bob", 50501, discovery_port=pb, targets=[("127.0.0.1", pa)], **kw)
    yield a, b
    a.stop()
    b.stop()


def test_live_discovery_both_directions(pair):
    a, b = pair
    assert a.start() and b.start()
    wait_for(lambda: [d.node_id for d in a.devices()] == [B])
    wait_for(lambda: [d.node_id for d in b.devices()] == [A])
    d = a.devices()[0]
    assert (d.name, d.host, d.port) == ("Bob", "127.0.0.1", 50501)


def test_live_bye_on_stop_removes_entry_quickly(pair):
    a, b = pair
    a.start(); b.start()
    wait_for(lambda: a.devices())
    b.stop()
    wait_for(lambda: a.devices() == [], timeout=1.5)  # well before stale_after (2.0s)... bye is immediate


def test_live_invisible_node_not_announced_but_still_discovers(pair):
    a, b = pair
    b.set_visible(False)
    a.start(); b.start()
    wait_for(lambda: [d.node_id for d in b.devices()] == [A])  # hidden node still sees others
    import time; time.sleep(0.5)
    assert a.devices() == []                                    # but nobody sees it


def test_live_going_visible_again(pair):
    a, b = pair
    b.set_visible(False)
    a.start(); b.start()
    wait_for(lambda: b.devices())
    b.set_visible(True)
    wait_for(lambda: [d.node_id for d in a.devices()] == [B])


def test_live_rename_propagates(pair):
    a, b = pair
    a.start(); b.start()
    wait_for(lambda: b.devices())
    a.set_name("Alicia")
    wait_for(lambda: b.devices()[0].name == "Alicia")


def test_port_in_use_reports_error_without_crashing():
    import socket
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", 0))  # no SO_REUSEADDR => our bind must fail
    port = blocker.getsockname()[1]
    s = DiscoveryService(A, "Alice", 1, discovery_port=port, bind_host="127.0.0.1",
                         targets=[], local_addresses=lambda: [])
    try:
        assert s.start() is False
        assert s.error and not s.listening
    finally:
        s.stop()
        blocker.close()
