# LAN Chat — Working Prototype

A minimal LAN-only group chat prototype for Windows/Linux/macOS with Python and Tkinter.

## What it does
- Works over a local network; no internet service is required.
- One PC runs `server.py`.
- Other PCs run `client.py` and connect to the server PC's LAN IP.
- Users choose a username when joining.
- Shows online users and broadcasts group messages in real time.

## Requirements
- Python 3.10+ recommended.
- Tkinter is included with most Windows Python installations. On some Linux distributions install the `python3-tk` package.

## Run it
### 1. On the server PC
Open a terminal in this folder and run:

    python server.py

Find the server PC's LAN IPv4 address, for example with `ipconfig` on Windows. It may look like `192.168.1.20`.

### 2. On each client PC
Copy the folder to the client PC, then run:

    python client.py

Enter the server PC's LAN IP and choose a username.

### 3. Windows Firewall
The first time the server listens on port **5050**, Windows may ask for firewall permission. Allow it on the appropriate **private/local network** if this is your own/authorized network.

## Test on one PC
Run `server.py` in one terminal and open `client.py` twice. Use `127.0.0.1` as the server IP and two different usernames.

## Project structure
- `server.py` — multi-client TCP chat server
- `client.py` — minimal Tkinter desktop client
- `README.md` — setup and usage guide

## Prototype limitations
This is intentionally a small educational model. It does not yet include end-to-end encryption, persistent message history, file transfer, accounts, or automatic LAN discovery. Add those only after the basic networking flow is understood.

Use the application only on networks where you have permission to run local services and communicate with other devices.
