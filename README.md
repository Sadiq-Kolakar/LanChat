# LANchat - offline nearby-PC messaging

Chat with other Windows PCs on the same network **without internet, accounts or a server**.
Launch it, pick a nearby PC, send a request, the other person accepts, chat. Groups included.

> Status: working prototype. LAN discovery + messaging are implemented and tested on
> loopback. **Bluetooth and Wi-Fi Direct are NOT implemented** (see "Honest status").

## Quick start (Windows 10/11)

1. Install Python 3.10+ from python.org (tick **"tcl/tk and IDLE"** and **"Add python.exe to PATH"**).
2. Double-click `start_lanchat.bat` (installs the optional theme package `sv-ttk` on first run).
3. Allow the **Windows Firewall** prompt for **Private networks** (see Firewall below).
4. Other PCs running LANchat on the same network appear under **NEARBY PCs**.
   Select one -> **Connect**. They see "wants to chat" -> **Accept**. Type, press Enter.

Other buttons: **New group** (invite connected PCs; they must accept), **By IP...**
(troubleshooting fallback), **Settings** (name, visibility, dark mode, connection methods).

## Run several clients on one PC

```
start_lanchat.bat --profile alice
start_lanchat.bat --profile bob
start_lanchat.bat --profile carol
```
Each profile has its own settings file and name. The 2nd/3rd instance finds port 50500 busy and
picks a free port automatically; discovery still works (UDP 50501 is shared). Instances on the
same PC discover each other like any other PC.

## Test across two Windows PCs (real LAN test - do this yourself)

1. Same Wi-Fi/Ethernet on both PCs; set the network to **Private**.
2. Run LANchat on both; allow the firewall prompt on both.
3. Each PC should list the other within ~3 seconds. Connect, accept, exchange messages.
4. If it does not appear: Troubleshooting below, then try **By IP...** (address and port are
   shown in the other PC's Settings; run `ipconfig` for the IPv4 address).
5. Two PCs with no router: plug in a cable or share a phone/PC hotspot - both get an address
   (even a 169.254.x.x one) and LANchat works.

## Windows Firewall

LANchat uses **UDP 50501** (discovery) and **TCP 50500** (chat; another port if busy).
Allow `python.exe` (or `LANchat.exe`) on **Private networks** when prompted. Manually, in an
*administrator* PowerShell:

```
New-NetFirewallRule -DisplayName "LANchat TCP" -Direction Inbound -Protocol TCP -LocalPort 50500 -Profile Private -Action Allow
New-NetFirewallRule -DisplayName "LANchat UDP" -Direction Inbound -Protocol UDP -LocalPort 50501 -Profile Private -Action Allow
```
Only open this on networks you trust. LANchat never needs admin rights to run.

### Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Nobody appears | Firewall blocked UDP 50501; network set to Public; different networks/VLANs. |
| Appears but connect fails | TCP port blocked by firewall; use the port shown in the other PC's Settings. |
| Works on cable, not Wi-Fi | Router "AP/client isolation" or guest network blocks device-to-device traffic. |
| Header says "No LAN connection found" | No active network adapter. Connect Wi-Fi/Ethernet. |
| "discovery limited" | UDP 50501 could not be opened (another program uses it). Use **By IP...**. |

## Features

- Automatic discovery (UDP broadcast + multicast, every 3 s, stale after 10 s, instant "bye" on exit).
- Status per PC: Online / Connecting / Wants to chat / Connected; conversations show Disconnected.
- Connection requests with Accept/Reject; **nothing is delivered before acceptance**.
- 1:1 chat and group chat (creator hosts and relays; invitees must accept).
- Change name any time (propagates to peers and groups). Duplicate names are shown as `Name (ip)`.
- Hide yourself from discovery (Settings). Dark mode. Session-only history (nothing written to disk).
- Reconnect button; safe shutdown (sockets and threads closed, "bye" sent).

## Honest status of connection methods

| Method | Status | Evidence |
|---|---|---|
| LAN (UDP discovery + TCP) | **Implemented** | 138 automated tests incl. loopback discovery, messaging, groups, abuse cases. **Not yet tested on two physical PCs by the author** - do the test above. |
| Bluetooth | **Not implemented** | Adapter slot + docs only. See `docs/NEARBY_TRANSPORTS.md`. |
| Wi-Fi Direct | **Not implemented** | Adapter slot + docs only. Never tested on hardware. |
| Windows Nearby Share | **Not usable** | No public API for third-party chat; its protocol is not documented. |

The Settings window shows this live. The app never claims a method works unless it does.

## Security and privacy - read this

- **Traffic is NOT encrypted and peers are NOT authenticated.** On an untrusted network (public
  Wi-Fi, shared offices) others can read or tamper with messages and can impersonate names.
  "It stays on the LAN" is not a security guarantee.
- Display names are labels only. Peers are tracked by a random per-run id; the same id from a
  different IP address is refused; chat is dropped until you accept.
- Limits: 16 KiB per frame, 2000 characters per message, max 50 peers, max 16 half-open sockets,
  5 malformed messages before disconnect, 35 s idle timeout, handshake in 5 s.
- No file transfer. No passwords or secrets stored. Settings file (`%APPDATA%\LANchat\settings.json`)
  holds only name, visibility, dark mode, preferred port.
- In a group, members trust the **host** to relay honestly (names are the host's view).
- Before calling this production-ready: add per-device keys, mutual authentication (e.g. a
  pairing code / TOFU fingerprints) and an encrypted transport (e.g. Noise or TLS-PSK).

## Project layout

```
main.py                    entry point (args: --profile --name --port --debug)
lanchat/
  protocol.py              versioned JSON-lines protocol, validation, framing
  naming.py  settings.py  netutil.py  engine.py
  discovery/service.py     UDP broadcast/multicast discovery
  networking/peer.py       one TCP connection (framing, locked writes, timeouts)
  networking/node.py       listener, handshake, requests, DMs, groups, heartbeat
  transports/              adapter interface; LAN (real); Bluetooth/Wi-Fi Direct (stubs)
  ui/                      Tkinter app, dialogs, theme
tests/                     138 tests (pytest)
docs/                      PROTOCOL.md, NEARBY_TRANSPORTS.md
legacy/                    original client/server prototype, untouched
start_lanchat.bat  build_windows.bat  requirements*.txt
```

## Develop and test

```
pip install -r requirements-dev.txt
python -m pytest -q                 # network tests use 127.0.0.1 only
xvfb-run -a python -m pytest -q     # Linux: also runs the two-window UI smoke test
```
Loopback tests prove the logic; they do **not** prove behaviour of real routers, firewalls or Wi-Fi.

## Build a standalone .exe

Run `build_windows.bat` on Windows -> `dist\LANchat\LANchat.exe` (PyInstaller, one folder, no console).
Copy the folder to other PCs; no Python needed there. Unsigned exes may trigger SmartScreen;
code-signing is the real fix. Build must happen on Windows (PyInstaller does not cross-compile).
Not built or tested in the authoring environment (Linux).

## Legacy prototype

The original server/client chat is kept in `legacy/` (needs a central server; no discovery).
LANchat replaced it with a peer-to-peer design because "discover nearby PCs, request, accept"
does not fit a central server.
