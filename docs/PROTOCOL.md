# LANchat protocol v1

**Discovery (UDP 50501)** - JSON datagram <= 512 bytes, to 255.255.255.255 and 239.255.50.50 every 3 s:
`{"app":"LANchat","dv":1,"id":"<32 hex>","name":"...","port":50500}`; `"bye":true` on exit.
Source IP comes from the datagram. Unknown app/version/invalid fields are dropped. First-seen
address wins for an id while it is alive. Entries expire after 10 s.

**Messaging (TCP)** - one JSON object per line, UTF-8, `\n` terminated, max 16 KiB per line.
Every object has `"v":1` and `"type"`. Unknown types/fields are rejected/dropped.

| type | fields | notes |
|---|---|---|
| hello | node_id, name, port | both directions; initiator first |
| connect_request / connect_accept / connect_reject | - | initiator -> listener -> user decides |
| chat | id, text | DM, only after accept |
| name_change | name | |
| ping / bye / error | - / - / code, text | keep-alive, graceful close, diagnostics |
| group_invite | group_id, group_name | host -> invitee, must be accepted |
| group_accept / group_decline / group_leave | group_id | member -> host |
| group_members | group_id, group_name, members[{id,name}] | host -> members |
| group_send | group_id, text | member -> host |
| group_chat | group_id, sender_id, sender_name, id, text | host -> members (sender from host's table) |
| group_closed | group_id | host -> members |

State machine per connection: HANDSHAKE -> (PENDING_OUT | PENDING_IN) -> CONNECTED -> closed.
Anything but hello/ping/bye/error/connect_* before CONNECTED is a protocol violation (strike).
