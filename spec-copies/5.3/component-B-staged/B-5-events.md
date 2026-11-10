# B.5: real-time events (WebSocket)

Routes covered: `ws://127.0.0.1:8000/api/ws?token=<bearer>` (and `:8001`, `:8002`), plus the events that
  the routes from A.1, B.1 and B.2 must now publish.
Check: `bash /spec/component-B-staged/checks/check_B_5.sh`. The rule numbers below match its output.

Do not start this spec until `check_B_1.sh` to `check_B_4.sh` all pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The `token` query parameter. Invalid → reject the connection |
| `can_read_channel(channel, user_id)` | B.2 | Who may subscribe to a channel |
| `message_to_json(message)` | B.2 | The `message` inside `message.created` and `message.reply` events |
| `channel_to_json(channel)` | A.2 | The `channel` inside `channel.*` events |

## Adds

| Function | What it does |
|---|---|
| `emit_event(channel_id, kind, payload)` | Gives the event the channel's next `event_id`, stores it where every node can read it, and returns the frame. The id must be dense per channel across all three nodes: use one shared counter updated in the same transaction as the stored event |
| `events_since(channel_id, since_event_id)` | Stored events with `event_id > since_event_id`, oldest first |
| `head_event_id(channel_id)` | The channel's latest `event_id`, or `0` if it has none |

Call `emit_event` from these routes, once per real change:

| Action | Event `type` | Extra fields |
|---|---|---|
| New top-level message (B.2) | `message.created` | `message` |
| New reply (B.2) | `message.reply` | `message` |
| Join that adds a new channel member (A.1) | `member.joined` | `user_id` |
| Leave that removes a member (B.1) | `member.left` | `user_id` |
| `PATCH /api/channels/{id}` (B.1) | `channel.updated` | `channel` |
| Archive / unarchive (B.1) | `channel.archived` / `channel.unarchived` | `channel` |

`message.updated`, `message.deleted`, `reaction.added` and `reaction.removed` are also durable kinds; they
are not part of this run. Joining or leaving again when nothing changes emits nothing.

**Fan-out across nodes.** A post on port `8000` must reach subscribers connected to `8001` and `8002`. One
way: every node polls the shared event store about every 100 ms and sends new events to its own
subscribers. Keep at least the last 1000 events of each channel for `resume`.

**WebSocket without extra packages.** If no WebSocket library is installed, the handshake and frames can be
written with the standard library: answer the upgrade with `101` and
`Sec-WebSocket-Accept = base64(sha1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))`; client frames are
masked, server frames are not; one JSON object per text frame.

## Rules:

B5.1 **Connect and subscribe.** A connection with an invalid token is rejected on every node (never
  upgraded). With a valid token, `{"type": "subscribe", "channel_id": <id>}` gets
  `{"type": "subscribed", "channel_id": <id>, "head_event_id": <int>}` right away, and all three nodes
  report the same `head_event_id` for the channel.

B5.2 **Live events on every node.** After subscribing on all three nodes, a message posted through any node
  reaches every subscriber as `{"type": "message.created", "event_id": head + 1, "channel_id": <id>, ...}`.
  A reply arrives as `message.reply`.

B5.3 **Dense ordering.** Each event from the table above advances the channel's `event_id` by exactly 1: no
  gaps, no duplicates, in the order the changes happened, and every node delivers the same sequence. A join
  or leave that changes nothing emits no event.

B5.4 **Resume.** `{"type": "resume", "channel_id": <id>, "since_event_id": <int>}` on any node (even one the
  client was never connected to) replays every event after `since_event_id` in order, then sends
  `{"type": "resumed", "channel_id": <id>, "head_event_id": <int>}`. Resuming from the head replays nothing
  and just sends `resumed`. If `since_event_id` is older than the kept events, the server sends
  `{"type": "resume.gap", "channel_id": <id>, "earliest_event_id": <int>}` instead.

Every request must answer within 5 seconds.
