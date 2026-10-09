# LANchat — plan

Assumption: old design = central server. Product goal (discover PCs, send request, other side accepts)
needs peer-to-peer. Each LANchat instance = listener + UDP discovery. Old code kept in `legacy/`.

## Design
- Discovery: UDP broadcast + multicast, JSON announce every 3s, stale after 10s, goodbye on exit.
- Messaging: TCP, JSON lines, protocol v1, 16 KiB frame cap, 2000-char text cap.
- Handshake: hello <-> hello -> connect_request -> accept/reject. No chat before accept.
- Groups: star topology. Creator = host, relays messages. Invitee must accept.
- Transports: adapter interface. LAN = real. Bluetooth / Wi-Fi Direct = stub, reported "not implemented".
- UI: Tkinter + sv-ttk (optional, falls back to ttk).

## Phases
- [ ] 1 Inspect + stabilize old code (done: baseline commit, legacy/)
- [ ] 2 Protocol + discovery + tests
- [ ] 3 Node (TCP, requests, groups) + tests
- [ ] 4 UI + settings
- [ ] 5 Nearby transport research + adapter stubs + docs
- [ ] 6 Docs, bat scripts, packaging, ZIP, verify
