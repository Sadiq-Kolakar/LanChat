"""Shared test helpers. All networking tests use loopback (127.0.0.1) only."""
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02):
    """Poll until predicate() is truthy, return its value; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met within %.1fs" % timeout)


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
