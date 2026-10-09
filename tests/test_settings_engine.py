import json

from lanchat.engine import Engine
from lanchat.settings import Settings, load_settings, save_settings, settings_path
from lanchat.networking import PeerState

from .conftest import free_udp_port, wait_for


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    assert save_settings(path, Settings("Alice", False, True, 51000))
    s = load_settings(path)
    assert (s.username, s.discoverable, s.dark_mode, s.tcp_port) == ("Alice", False, True, 51000)


def test_settings_file_has_no_secrets_only_known_keys(tmp_path):
    path = tmp_path / "s.json"
    save_settings(path, Settings("Alice"))
    assert set(json.loads(path.read_text())) == {"username", "discoverable", "dark_mode", "tcp_port"}


def test_corrupt_or_hostile_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert load_settings(path).tcp_port == 50500
    path.write_text(json.dumps({"username": 123, "tcp_port": "x", "discoverable": "yes", "evil": 1}))
    s = load_settings(path)
    assert s.username and s.tcp_port == 50500 and s.discoverable is True
    path.write_text("[1]")
    assert load_settings(path).username


def test_missing_settings_file_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "nope.json").discoverable is True


def test_settings_validation_clamps():
    s = Settings("  \x00 ", True, False, 80).validated()
    assert s.username == "Guest" and s.tcp_port == 50500


def test_profile_paths_differ(monkeypatch, tmp_path):
    monkeypatch.setenv("LANCHAT_HOME", str(tmp_path))
    assert settings_path().name == "settings.json"
    assert settings_path("bob").name == "settings-bob.json"
    assert settings_path("../../evil").name == "settings-evil.json"  # no path traversal


def test_two_engines_discover_each_other_and_chat_on_loopback():
    pa, pb = free_udp_port(), free_udp_port()
    kw = dict(bind_host="127.0.0.1", discovery_bind_host="127.0.0.1")
    a = Engine(Settings("Alice", tcp_port=0 + 50600), discovery_port=pa, discovery_targets=[("127.0.0.1", pb)], **kw)
    b = Engine(Settings("Bob", tcp_port=50601), discovery_port=pb, discovery_targets=[("127.0.0.1", pa)], **kw)
    a.discovery._interval = b.discovery._interval = 0.1
    a.discovery._local_addresses = b.discovery._local_addresses = lambda: []
    a.start(); b.start()
    try:
        wait_for(lambda: [d.name for d in a.manager.devices()] == ["Bob"])
        dev = a.manager.devices()[0]
        assert a.manager.connect(dev) == (True, "")
        wait_for(lambda: any(e.kind == "incoming_request" for e in list(b.events.queue)))
        b.node.accept_request(a.node.node_id)
        wait_for(lambda: (p := a.node.get_peer(b.node.node_id)) and p.state == PeerState.CONNECTED)
        a.node.send_dm(b.node.node_id, "over engine")
        wait_for(lambda: any(e.kind == "chat" and e.data["text"] == "over engine" for e in list(b.events.queue)))
    finally:
        a.stop(); b.stop()


def test_apply_settings_renames_and_toggles_visibility():
    e = Engine(Settings("Old"), bind_host="127.0.0.1", discovery_port=free_udp_port(),
               discovery_bind_host="127.0.0.1", discovery_targets=[])
    e.discovery._local_addresses = lambda: []
    e.start()
    try:
        e.apply_settings(Settings("New Name", discoverable=False))
        assert e.node.name == "New Name" and e.discovery.visible is False
    finally:
        e.stop()
