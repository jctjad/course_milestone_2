# C.1: reactions

Routes covered: `POST /api/messages/{id}/reactions`, `DELETE /api/messages/{id}/reactions/{emoji}`, and the
`reactions` field of every `MessageObj`.
Check: `bash /spec/component-C-staged/checks/check_C_1.sh`. The rule numbers below match its output.

Do not start this spec until the basics in `/spec/README.md` work.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_message(message_id)` | B.2 | The message in the URL. Unknown id → `404` |
| `get_channel(channel_id)` | A.1 | The message's channel |
| `can_read_channel(channel, user_id)` | B.2 | Who may react |
| `message_to_json(message)` | B.2 | Extend it so `reactions` is filled in (below) |
| `emit_event(channel_id, kind, payload)` | B.5 | `reaction.added` and `reaction.removed` events |

## Adds

Define these as separate Python functions with these names. C.3 to C.5 and E call them.

| Function | What it does |
|---|---|
| `add_reaction(message_id, user_id, emoji)` | Stores one reaction per (message, user, emoji). Adding it again changes nothing |
| `remove_reaction(message_id, user_id, emoji)` | Removes the caller's reaction if there is one. Removing one that is not there changes nothing |
| `reactions_for(message_id)` | Returns `[{emoji, count, user_ids}]`, one entry per emoji that at least one user has, in the order each emoji was first added. `count` is `len(user_ids)` |

Reactions must be stored where all three nodes can read them.

## Rules:

C1.1 **Adding a reaction.** `POST /api/messages/{id}/reactions` — body `{emoji}` — returns
  `200 {reactions}`, even on the first insert (not `201`). `reactions` is `[{emoji, count, user_ids}]`.
  Two users reacting with the same emoji give one entry with `count` `2` and both ids in `user_ids`.
  It is idempotent per (user, emoji): reacting again returns `200` and the count does not change.
  Also: the `reactions` field of the message in `GET /api/channels/{id}/messages`, on any node, shows the
  same list. Different emojis are separate entries. `emoji` is any non-empty string, for example `👍` or
  `tada`.

C1.2 **Removing a reaction.** `DELETE /api/messages/{id}/reactions/{emoji}` (the emoji is URL-encoded in the
  path) removes only the caller's reaction and returns `200 {reactions}`. The count drops by one; when no
  user is left, the entry is removed from the list. Removing a reaction you do not have returns `200` and
  changes nothing.

C1.3 **Errors.** An unknown message id → `404` on both routes. A missing or empty `emoji` → `400`. A user
  who cannot read the message's channel (not a member of a private channel) → `403` on both routes. No
  token → `401` on both routes.

C1.4 **Reaction events.** Every add that changes something emits
  `{"type": "reaction.added", "event_id": ..., "channel_id": ..., "message_id": ..., "emoji": ..., "user_id": ..., "reactions": [...]}`
  on the message's channel, and every remove that changes something emits `reaction.removed` with the same
  fields. They use the channel's next dense `event_id`, like every other event (B.5), and reach subscribers
  on all three nodes. An add or remove that changes nothing emits nothing.

Every request must answer within 5 seconds.
