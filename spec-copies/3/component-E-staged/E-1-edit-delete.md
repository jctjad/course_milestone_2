# E.1: editing and deleting messages

Routes covered: `PATCH /api/messages/{id}`, `DELETE /api/messages/{id}`, and the `message.updated` and
`message.deleted` events.
Check: `bash /spec/component-E-staged/checks/check_E_1.sh`. The rule numbers below match its output.

Do not start this spec until the basics in `/spec/README.md` work.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_message(message_id)` | B.2 | The message in the URL. Unknown id → `404` |
| `get_channel(channel_id)`, `get_member_role(workspace_id, user_id)` | A.1 | The workspace owner may delete any message |
| `message_to_json(message)` | B.2 | The `message` in responses and events |
| `emit_event(channel_id, kind, payload)` | B.5 | `message.updated` and `message.deleted` |

## Adds

| Function | What it does |
|---|---|
| `edit_message(message, body)` | Stores the new body and sets `edited_at` to now (RFC 3339 UTC); returns the message |
| `delete_message(message)` | Marks the message deleted. Deleting twice changes nothing |

Deleted messages are left out of `GET /api/channels/{id}/messages` (and of thread replies and search, when
those exist). Reactions on a deleted message no longer count anywhere.

Events: `message.updated` carries `{message}` (the edited `MessageObj`) and `message.deleted` carries
`{message_id}`, plus `type`, `event_id` and `channel_id` like every other event.

## Rules:

E1.1 **Editing.** `PATCH /api/messages/{id}` — body `{body}` — from the author returns `200 {message}` with
  the new `body` and `edited_at` set to an RFC 3339 UTC time (it was `null` before). `GET
  /api/channels/{id}/messages` on every node shows the new body and `edited_at`. An empty `body` → `400`. An
  unknown id → `404`.

E1.2 **Who may edit.** Only the author may edit: anyone else, including the workspace owner, → `403`. No
  token → `401`.

E1.3 **Deleting.** `DELETE /api/messages/{id}` from the author or the workspace owner returns
  `200 {"deleted": true}`, and deleting again still returns `200 {"deleted": true}`. Anyone else → `403`.
  The deleted message no longer appears in `GET /api/channels/{id}/messages` (or appears marked deleted, with
  no reactions). Editing a deleted message → `409`.

E1.4 **Events.** An edit emits `message.updated` and a delete emits `message.deleted`, each with the
  channel's next dense `event_id`, to subscribers on every node. Deleting an already deleted message emits
  nothing.

Every request must answer within 5 seconds.
