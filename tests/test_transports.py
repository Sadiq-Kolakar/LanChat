"""Transport layer tests (fake/loopback only). Bluetooth / Wi-Fi Direct are
checked for *honest reporting*, not for working - they are not implemented."""
from lanchat.discovery import DiscoveryService, build_announcement
from lanchat.networking import LanNode
from lanchat.transports import (BluetoothTransport, ConnectionManager, LanTransport,
                                NearbyDevice, Transport, TransportKind, TransportStatus,
                                WifiDirectTransport)

from .conftest import free_udp_port, wait_for

A = "a" * 32


def make_lan(addresses):
    node = LanNode("Me", host="127.0.0.1", port=0)
    node.start()
    disc = DiscoveryService(node.node_id, "Me", node.port, discovery_port=free_udp_port(),
                            bind_host="127.0.0.1", targets=[], local_addresses=lambda: [])
    return node, disc, LanTransport(node, disc, local_addresses=lambda: addresses)


def test_lan_status_without_network_is_unavailable():
    node, disc, lan = make_lan([])
    try:
        s = lan.status()
        assert s.implemented and not s.available and not s.verified
        assert "No LAN" in s.summary
    finally:
        node.stop()


def test_lan_status_ready_then_verified_after_device_seen():
    node, disc, lan = make_lan(["192.168.1.5"])
    try:
        disc.start()
        s = lan.status()
        assert s.available and not s.verified and "no other PCs seen" in s.summary
        disc.handle_datagram(build_announcement(A, "Alice", 50500), ("192.168.1.9", 1))
        assert [d.name for d in lan.devices()] == ["Alice"]
        s = lan.status()
        assert s.verified and "LAN connected" in s.summary
    finally:
        disc.stop(); node.stop()


def test_lan_status_discovery_failure_is_reported_not_hidden():
    node, disc, lan = make_lan(["192.168.1.5"])  # discovery never started => not listening
    try:
        s = lan.status()
        assert s.available and "discovery limited" in s.summary
    finally:
        node.stop()


def test_lan_connect_starts_real_connection():
    node, disc, lan = make_lan(["127.0.0.1"])
    other = LanNode("Other", host="127.0.0.1", port=0)
    other.start()
    try:
        dev = NearbyDevice(other.node_id, "Other", "127.0.0.1", other.port, TransportKind.LAN)
        assert lan.connect(dev)
        wait_for(lambda: node.get_peer(other.node_id) is not None)
    finally:
        node.stop(); other.stop()


def test_nearby_transports_never_claim_to_work():
    for t in (BluetoothTransport(), WifiDirectTransport()):
        s = t.status()
        assert not s.implemented and not s.available and not s.verified
        assert s.summary == "Not implemented"
        assert t.devices() == []
        dev = NearbyDevice("x", "x", "0.0.0.0", 1, t.kind)
        assert t.connect(dev) is False


class FakeTransport(Transport):
    def __init__(self, kind, implemented=True, available=True, verified=False):
        self.kind = kind
        self._s = TransportStatus(kind, implemented, available, verified, f"{kind.value} summary")
        self.connected = []

    def start(self): pass
    def stop(self): pass
    def status(self): return self._s
    def devices(self): return [NearbyDevice("d", "Dev", "1.1.1.1", 1, self.kind)]
    def connect(self, device):
        self.connected.append(device); return True


def test_manager_prefers_lan_when_available():
    lan = FakeTransport(TransportKind.LAN, verified=True)
    bt = FakeTransport(TransportKind.BLUETOOTH)
    sel = ConnectionManager([lan, bt]).select()
    assert sel.transport is lan and sel.method == "LAN"


def test_manager_falls_back_only_to_usable_transport():
    lan = FakeTransport(TransportKind.LAN, available=False)
    bt = FakeTransport(TransportKind.BLUETOOTH, implemented=False, available=False)
    wd = FakeTransport(TransportKind.WIFI_DIRECT)  # pretend this one works
    sel = ConnectionManager([lan, bt, wd]).select()
    assert sel.transport is wd and sel.method == "Wi-Fi Direct"


def test_manager_reports_none_and_lists_reasons_when_nothing_usable():
    mgr = ConnectionManager([FakeTransport(TransportKind.LAN, available=False),
                             BluetoothTransport(), WifiDirectTransport()])
    sel = mgr.select()
    assert sel.transport is None and sel.method == "None"
    assert "Bluetooth: Not implemented" in sel.message and "Wi-Fi Direct: Not implemented" in sel.message


def test_manager_refuses_unimplemented_transport_connect():
    mgr = ConnectionManager([FakeTransport(TransportKind.LAN), BluetoothTransport()])
    ok, why = mgr.connect(NearbyDevice("x", "x", "0", 1, TransportKind.BLUETOOTH))
    assert not ok and "not implemented" in why


def test_manager_connect_routes_to_device_transport():
    lan = FakeTransport(TransportKind.LAN)
    mgr = ConnectionManager([lan])
    dev = NearbyDevice("d", "Dev", "1.1.1.1", 1, TransportKind.LAN)
    assert mgr.connect(dev) == (True, "")
    assert lan.connected == [dev]


def test_manager_devices_skips_unavailable_transports():
    lan = FakeTransport(TransportKind.LAN, available=False)
    assert ConnectionManager([lan]).devices() == []
