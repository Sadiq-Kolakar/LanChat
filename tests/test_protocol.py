import json

import pytest

from lanchat import protocol as p
from lanchat.naming import display_labels, sanitize_name

NID = "a" * 32


def test_encode_decode_roundtrip():
    frame = p.encode("chat", id="m1", text="hello")
    assert frame.endswith(b"\n") and frame.count(b"\n") == 1
    msg = p.decode(frame)
    assert msg == {"v": 1, "type": "chat", "id": "m1", "text": "hello"}


def test_newlines_inside_text_do_not_break_framing():
    frame = p.encode("chat", id="m1", text="line1\nline2")
    assert frame.count(b"\n") == 1  # JSON escapes the inner newline
    assert p.decode(frame)["text"] == "line1\nline2"


def test_unicode_roundtrip():
    msg = p.decode(p.encode("chat", id="m1", text="नमस्ते 👋 ಕನ್ನಡ"))
    assert msg["text"] == "नमस्ते 👋 ಕನ್ನಡ"


def test_hello_roundtrip_and_name_sanitized():
    frame = p.encode("hello", node_id=NID, name="  Alice\x00\x07   PC  ", port=50500)
    msg = p.decode(frame)
    assert msg["name"] == "Alice PC"
    assert msg["port"] == 50500


def test_framebuffer_partial_reads_byte_by_byte():
    buf = p.FrameBuffer()
    data = p.encode("chat", id="m1", text="hi") + p.encode("ping")
    lines = []
    for i in range(len(data)):
        lines += buf.feed(data[i : i + 1])
    assert [p.decode(l)["type"] for l in lines] == ["chat", "ping"]


def test_framebuffer_multiple_messages_one_chunk_and_remainder():
    buf = p.FrameBuffer()
    a, b = p.encode("ping"), p.encode("bye")
    lines = buf.feed(a + b + b"{\"v\":1,")
    assert len(lines) == 2
    assert buf.feed(b"\"type\":\"ping\"}\n") == [b'{"v":1,"type":"ping"}']


def test_framebuffer_skips_blank_lines():
    assert p.FrameBuffer().feed(b"\n\n  \n") == []


def test_framebuffer_oversized_without_newline():
    buf = p.FrameBuffer()
    with pytest.raises(p.ProtocolError):
        buf.feed(b"x" * (p.MAX_FRAME_BYTES + 1))


def test_framebuffer_oversized_line_with_newline():
    buf = p.FrameBuffer()
    with pytest.raises(p.ProtocolError):
        buf.feed(b"x" * (p.MAX_FRAME_BYTES + 10) + b"\n")


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff\xfe\xfa",
        b"[1,2,3]",
        b'"string"',
        b"null",
        b"{}",
        b'{"v":1}',
        b'{"v":1,"type":"nope"}',
        b'{"type":"ping"}',
        b'{"v":"1","type":"ping"}',
        b'{"v":true,"type":"ping"}',
        b'{"v":2,"type":"ping"}',
        b'{"v":0,"type":"ping"}',
        b'{"v":1,"type":"chat","id":"m1"}',                      # missing text
        b'{"v":1,"type":"chat","id":"m1","text":5}',             # wrong type
        b'{"v":1,"type":"chat","id":"m1","text":"   "}',         # empty
        b'{"v":1,"type":"chat","id":"bad id!","text":"x"}',      # bad id
        b'{"v":1,"type":"hello","node_id":"short","name":"a","port":1}',
        b'{"v":1,"type":"hello","node_id":"' + NID.encode() + b'","name":"a","port":70000}',
        b'{"v":1,"type":"hello","node_id":"' + NID.encode() + b'","name":"a","port":true}',
        b'{"v":1,"type":"group_members","group_id":"g","group_name":"n","members":"x"}',
        b'{"v":1,"type":"group_members","group_id":"g","group_name":"n","members":[1]}',
    ],
)
def test_decode_rejects_malformed(raw):
    with pytest.raises(p.ProtocolError):
        p.decode(raw)


def test_text_over_limit_rejected_both_ways():
    too_long = "x" * (p.MAX_TEXT_CHARS + 1)
    with pytest.raises(p.ProtocolError):
        p.encode("chat", id="m1", text=too_long)
    raw = json.dumps({"v": 1, "type": "chat", "id": "m1", "text": too_long}).encode()
    with pytest.raises(p.ProtocolError):
        p.decode(raw)


def test_text_at_limit_ok():
    assert p.decode(p.encode("chat", id="m1", text="x" * p.MAX_TEXT_CHARS))["text"]


def test_frame_over_limit_rejected():
    with pytest.raises(p.ProtocolError):
        p.decode(b"x" * (p.MAX_FRAME_BYTES + 1))


def test_unknown_fields_are_dropped():
    msg = p.decode(b'{"v":1,"type":"ping","admin":true,"extra":[1]}')
    assert msg == {"v": 1, "type": "ping"}


def test_group_members_validation_and_cap():
    ok = [{"id": NID, "name": "A"}]
    msg = p.decode(p.encode("group_members", group_id="g1", group_name="Team", members=ok))
    assert msg["members"] == ok
    with pytest.raises(p.ProtocolError):
        p.encode("group_members", group_id="g1", group_name="Team",
                 members=[{"id": NID, "name": "A"}] * (p.MAX_GROUP_MEMBERS + 1))


def test_encode_refuses_invalid_output():
    with pytest.raises(p.ProtocolError):
        p.encode("chat", id="m1", text="")
    with pytest.raises(p.ProtocolError):
        p.encode("no_such_type")


def test_sanitize_name():
    assert sanitize_name("  Bob\n\tSmith ") == "Bob Smith"
    assert sanitize_name("") == "Guest"
    assert sanitize_name(None) == "Guest"
    assert sanitize_name("x" * 100) == "x" * 24
    assert sanitize_name("\x00\x01") == "Guest"


def test_display_labels_disambiguate_duplicates():
    labels = display_labels([("1", "Alice", "10.0.0.1"), ("2", "alice", "10.0.0.2"), ("3", "Bob", "10.0.0.3")])
    assert labels["1"] == "Alice (10.0.0.1)"
    assert labels["2"] == "alice (10.0.0.2)"
    assert labels["3"] == "Bob"
