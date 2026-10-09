"""Nearby (no shared LAN) transports - NOT IMPLEMENTED, reported honestly.

What was investigated (details in docs/NEARBY_TRANSPORTS.md):

* Wi-Fi Direct: Windows exposes ``Windows.Devices.WiFiDirect`` (WinRT). A
  ``WiFiDirectDevice`` can yield an endpoint pair that normal sockets can use,
  so a working design would be "form a Wi-Fi Direct link, then run the same TCP
  protocol over it". Python projections of that namespace exist on PyPI.
* Bluetooth: ``Windows.Devices.Bluetooth.Rfcomm`` gives an RFCOMM byte stream
  (usually needs pairing). Discovery alone does NOT give a chat connection.

None of this has been written or tested here: it needs two Windows PCs with the
right radios and drivers. These classes therefore never claim availability.
They only keep the adapter slot, so a future implementation can plug in
without touching the UI or the chat engine.
"""
from __future__ import annotations

import importlib.util
import sys

from .base import NearbyDevice, Transport, TransportKind, TransportStatus


def _winrt_package_present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class _UnimplementedTransport(Transport):
    kind: TransportKind
    _module = ""
    _why = ""

    def start(self) -> None:  # nothing to start
        return None

    def stop(self) -> None:
        return None

    def devices(self) -> list[NearbyDevice]:
        return []

    def connect(self, device: NearbyDevice) -> bool:
        return False

    def status(self) -> TransportStatus:
        on_windows = sys.platform.startswith("win")
        pkg = _winrt_package_present(self._module) if on_windows else False
        extra = (
            " The Python WinRT package is installed, but no adapter code exists yet."
            if pkg else
            (" This PC is not running Windows." if not on_windows else "")
        )
        return TransportStatus(
            self.kind, implemented=False, available=False, verified=False,
            summary="Not implemented",
            detail=self._why + extra,
        )


class BluetoothTransport(_UnimplementedTransport):
    kind = TransportKind.BLUETOOTH
    _module = "winrt.windows.devices.bluetooth.rfcomm"
    _why = (
        "Bluetooth discovery alone does not create a chat connection. A real transport needs RFCOMM "
        "sockets (Windows.Devices.Bluetooth.Rfcomm), device pairing and testing on two PCs. "
        "No such code has been written or tested."
    )


class WifiDirectTransport(_UnimplementedTransport):
    kind = TransportKind.WIFI_DIRECT
    _module = "winrt.windows.devices.wifidirect"
    _why = (
        "Windows offers Wi-Fi Direct through Windows.Devices.WiFiDirect, but this app has no adapter for it "
        "and it has never been tested on real hardware."
    )
