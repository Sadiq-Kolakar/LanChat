# Nearby transports (no shared LAN) - investigation and status

**Status: Bluetooth = NOT implemented. Wi-Fi Direct = NOT implemented. Nothing here was run on hardware.**
The sandbox used to build LANchat is Linux without radios, so no claim of tested support is made.

## What exists (from Microsoft docs and PyPI, checked 2026-10-09)

- **Wi-Fi Direct:** `Windows.Devices.WiFiDirect` (WinRT). `WiFiDirectAdvertisementPublisher` advertises;
  `WiFiDirectDevice.FromIdAsync` connects; `GetConnectionEndpointPairs` yields an endpoint usable with
  normal sockets. Microsoft's docs say the Proximity capability must be enabled (matters for packaged/MSIX apps).
  PyPI has PyWinRT projections (`winrt-Windows.Devices.WiFiDirect`, Windows wheels for CPython 3.9-3.13).
- **Bluetooth:** `Windows.Devices.Bluetooth.Rfcomm` (RFCOMM byte stream; typically needs pairing) and a PyWinRT
  projection exists. Discovery (Bluetooth/BLE advertisements) alone does not give a chat channel.
- **Nearby Share / Quick Share:** no public third-party API; internal protocol undocumented. Not used.

## Realistic design (not built)

Best fit: **form a Wi-Fi Direct link, then run the existing TCP protocol over its endpoint pair.**
Everything above the socket (handshake, accept/reject, chat, groups) is unchanged. Bluetooth needs a
framed RFCOMM stream and a pairing UX; messaging could reuse `PeerConnection` if given a socket-like object.

## Plug-in point

Implement `lanchat.transports.base.Transport` (`start/stop/status/devices/connect`), return
`implemented=True` only when it truly works, and add it to `Engine`'s `ConnectionManager` after LAN.
`ConnectionManager.select()` already prefers LAN, falls back only to transports that are implemented *and*
available, and otherwise reports each transport's real status (shown in Settings).

## Open questions needing real hardware
Driver/adapter support for Wi-Fi Direct, whether a desktop (non-packaged) Python process may use the API,
pairing prompts, how an active Wi-Fi connection coexists with a Wi-Fi Direct group, and Bluetooth-only
PCs without adapters.
