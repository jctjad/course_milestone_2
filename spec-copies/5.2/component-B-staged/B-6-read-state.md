# B.6: read state

Routes covered: `GET /api/channels/{id}/read`, `POST /api/channels/{id}/read`, and the `read_state` list in
  `GET /api/workspaces/{slug}`.
Check: `bash /spec/component-B-staged/checks/check_B_6.sh`. The rule numbers below match its output.

Do not start this spec until `check_B_1.sh` to `check_B_5.sh` all pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_channel(channel_id)` | A.1 | The channel in the URL. Unknown id → `404` |
| `can_read_channel(channel, user_id)` | B.2 | Who may read or set read state |
| `visible_channels(workspace_id, user_id)` | B.1 | Which channels get an entry in the workspace detail's `read_state` |
| `events_since(channel_id, since_event_id)` | B.5 | Counting unread messages and mentions after the cursor |

## Adds

| Function | What it does |
|---|---|
| `get_last_read(user_id, channel_id)` | The stored `last_read_event_id`, or `0` if the user never set one |
| `set_last_read(user_id, channel_id, event_id)` | Stores `max(current, event_id)`: the cursor never moves backwards |
| `read_state_json(user_id, channel_id)` | `{channel_id, last_read_event_id, unread_count, mention_count}` |

Counting, for one user and one channel, over events with `event_id > last_read_event_id`:

- `unread_count`: `message.created` and `message.reply` events whose author is someone else.
- `mention_count`: those same events whose message's `mentions` contains the user. The author's own
  messages never count, so mentioning yourself never raises your own `mention_count`.

## Rules:

B6.1 **Read state.** `GET /api/channels/{id}/read` returns `200 {read_state: {channel_id,
  last_read_event_id, unread_count, mention_count}}`. After the user marks the channel read up to the head
  and someone else posts 3 messages, `unread_count` is `3`. The user's own messages do not count. A
  non-member of a private channel → `403`; no token → `401`.

B6.2 **Moving the cursor.** `POST /api/channels/{id}/read` — body `{last_read_event_id}` — stores it and
  returns `200 {read_state}` with updated counts (marking up to the head gives `unread_count` `0`). It never
  moves backwards: posting a smaller id returns `200` and keeps the larger one. A cursor set on one node is
  seen on the others.

B6.3 **Mention count.** Messages from others that mention the user (by `@username` or `@channel`) after the
  cursor are counted in `mention_count`, once per message. Marking the channel read sets it back to `0`.
  Mentioning yourself never raises your own `mention_count`.

B6.4 **Workspace detail.** `read_state` in `GET /api/workspaces/{slug}` is a list with one read-state object
  per channel in `channels`, with the same shape and the same values as `GET /api/channels/{id}/read`.

Every request must answer within 5 seconds.
