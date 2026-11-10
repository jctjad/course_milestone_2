# B.2: messages and history

Routes covered: `POST /api/channels/{id}/messages`, `GET /api/channels/{id}/messages`,
  `GET /api/messages/{parent_id}/replies`.
Check: `bash /spec/component-B-staged/checks/check_B_2.sh`. The rule numbers below match its output.

Do not start this spec until `check_B_1.sh` passes.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_channel(channel_id)` | A.1 | The channel in the URL. Unknown id → `404` |
| `get_member_role(workspace_id, user_id)` | A.1 | Whether the caller belongs to the channel's workspace |
| `user_to_json(user)` | A.1 | The `author` object inside each message |
| `is_channel_member(channel_id, user_id)` | B.1 | Who may post, and who may read a private channel |
| `require_writable(channel)` | B.1 | Posting to an archived channel → `423` |

## Adds

Define these as separate Python functions with these names. B.3 to B.6 call them.

| Function | What it does |
|---|---|
| `can_read_channel(channel, user_id)` | Public channel: true for workspace members. Private channel: true only for channel members |
| `create_message(channel_id, author_id, body, parent_id=None)` | Stores and returns a new message. A reply (with `parent_id`) increases the parent's `reply_count` |
| `get_message(message_id)` | Returns the message, or `None` |
| `message_to_json(message)` | Returns `MessageObj`: at least `{id, channel_id, author_id, author, body, parent_id, reply_count, created_at, edited_at, files, reactions, mentions}`. No `event_id` |
| `parse_limit(raw)` | `50` when missing; an integer from 1 to 200 otherwise; anything else signals `400` |
| `encode_cursor(position)` / `decode_cursor(cursor)` | Turn a stored position into an opaque string and back. The same cursor must work on every node. `decode_cursor` signals `400` for a string it did not make |
| `page(items, limit, cursor)` | Shared paging helper for every list in B.2 and B.3: returns `(items_on_this_page, next_cursor)`, with `next_cursor` `null` on the last page |

## Rules:

B2.1 **Post a message.** `POST /api/channels/{id}/messages` — body `{body, parent_id?, file_ids?, blocks?}`
  — returns `201 {message}`. `MessageObj` carries at least `{id, channel_id, author_id, author, body,
  parent_id, reply_count, created_at, edited_at, files, reactions, mentions}`: `author_id` is the caller,
  `author` is the caller's `UserObj`, `parent_id` and `edited_at` are `null`, `reply_count` is `0`, `files`,
  `reactions` and `mentions` are lists, and `created_at` is an RFC 3339 / ISO 8601 UTC string (e.g.
  `2026-05-16T08:07:34.123Z`). Messages do not carry `event_id` (only WebSocket events do, B.5).

B2.2 **Posting errors.** An empty or missing `body` → `400`. Only channel members may post: a non-member of a
  private channel, or a user outside the workspace, → `403`. Unknown channel → `404`; no token → `401`.

B2.3 **Archived channels.** Writes to an archived channel return `423`: posting a message and
  `PATCH /api/channels/{id}`. After `unarchive`, posting works again (`201`).

B2.4 **History paging.** `GET /api/channels/{id}/messages?limit=&cursor=` returns `200 {messages, next_cursor}`
  newest first. Without `cursor` it returns the first page. `next_cursor` is an opaque string while more
  messages exist and `null` on the last page; following it visits every message exactly once. A cursor from
  one node works on the other two, and new messages posted after a cursor was issued do not change the
  page it returns. `limit` defaults to `50`. Offset paging (`?page=`, `?offset=`) is not supported.

B2.5 **Paging validation.** `limit` must be an integer from 1 to 200: `0`, `201`, `-1` or `abc` → `400`;
  `200` is fine. A malformed or unknown cursor → `400`. Reading a private channel you are not a member of →
  `403`.

B2.6 **Threads.** A message posted with `parent_id` is a reply: `201`, its `parent_id` is the parent's id, and
  the parent's `reply_count` goes up by one per reply (seen in the parent's `MessageObj` in the history).
  `GET /api/messages/{parent_id}/replies?limit=&cursor=` returns `200 {replies: [MessageObj, ...],
  next_cursor}` oldest first, paged the same way. Unknown parent → `404`.

Every request must answer within 5 seconds.
