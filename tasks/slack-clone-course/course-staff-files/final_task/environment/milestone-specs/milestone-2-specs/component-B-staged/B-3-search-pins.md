# B.3: search and pins

Routes covered: `GET /api/search`, `POST /api/messages/{id}/pin`, `DELETE /api/messages/{id}/pin`,
  `GET /api/channels/{id}/pins`.
Check: `bash /spec/component-B-staged/checks/check_B_3.sh`. The rule numbers below match its output.

Do not start this spec until `check_B_1.sh` and `check_B_2.sh` both pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_channel(channel_id)` | A.1 | The channel in the URL. Unknown id → `404` |
| `can_read_channel(channel, user_id)` | B.2 | Which messages search may return; who may see or change pins |
| `get_message(message_id)` | B.2 | The message being pinned. Unknown id → `404` |
| `message_to_json(message)` | B.2 | Each search result and each pin's `message` |
| `parse_limit(raw)`, `page(items, limit, cursor)` | B.2 | `limit`, `cursor` and `next_cursor` on both lists, with the same rules as B2.4 and B2.5 |

## Adds

| Function | What it does |
|---|---|
| `search_messages(user_id, q, workspace_id=None)` | Messages whose `body` contains `q` (case-insensitive), from channels the user can read, newest first; only one workspace when `workspace_id` is given |
| `pin_message(message_id, user_id)` | Pins the message once. Pinning again changes nothing and returns the existing pin |
| `unpin_message(message_id)` | Removes the pin if there is one |
| `list_pins(channel_id)` | The channel's pins, newest pin first |
| `pin_to_json(pin)` | `{message_id, pinned_by, pinned_at}`: `pinned_by` is the pinner's user id, `pinned_at` an RFC 3339 UTC string |

## Rules:

B3.1 **Search.** `GET /api/search?q=<text>&limit=&cursor=` returns `200 {results, next_cursor}`: matching
  `MessageObj`s, newest first, only from channels the caller can read (never from a private channel they are
  not a member of, or a workspace they do not belong to). `?workspace_id=<id>` limits results to that
  workspace. Results page like B2.4 (`limit=1` gives one result and a `next_cursor`). No token → `401`.

B3.2 **Pins.** `POST /api/messages/{id}/pin` returns `200 {pin: {message_id, pinned_by, pinned_at}}` and is
  idempotent: pinning twice is `200` both times and the message appears once in the list.
  `GET /api/channels/{id}/pins?limit=&cursor=` returns `200 {pins: [{message, pinned_by, pinned_at}, ...],
  next_cursor}`, newest pin first, paged like B2.4. `DELETE /api/messages/{id}/pin` returns
  `200 {"unpinned": true}` and the message leaves the list.

B3.3 **Pin access.** A non-member of a private channel gets `403` on its pin list and on pinning its
  messages. Pinning an unknown message → `404`; no token → `401`.

Every request must answer within 5 seconds.
