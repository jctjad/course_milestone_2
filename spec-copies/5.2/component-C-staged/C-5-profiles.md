# C.5: profiles

Routes covered: `GET /api/users/{id}`, `PATCH /api/users/me`, and the global `user.updated` WebSocket frame.
Check: `bash /spec/component-C-staged/checks/check_C_5.sh`. The rule numbers below match its output.

Do not start this spec until `check_C_1.sh` to `check_C_4.sh` pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `user_to_json(user)` | A.1 | Every `user` in responses and in `user.updated` |
| `online_user_ids()` | C.4 | Knowing that open WebSockets exist on every node |

## Adds

| Function | What it does |
|---|---|
| `validate_timezone(tz)` | True only for an IANA name that `zoneinfo.ZoneInfo` accepts (e.g. `America/New_York`, `UTC`) |
| `validate_profile(fields)` | Checks each sent field's type and length (see the limits below); signals `400` on the first bad one |
| `update_profile(user_id, fields)` | Stores only the fields that were sent and returns the user |
| `broadcast_global(frame)` | Sends a frame to every open WebSocket on every node, whether or not it subscribed to anything. Cross-node: store it in the shared store and let each node's poller deliver it |

Profile fields: `display_name`, `timezone`, `avatar_url`, `status_text`, `status_emoji`.

| Field | Allowed |
|---|---|
| `display_name` | a string of 1 to 80 characters (empty → `400`) |
| `timezone` | an IANA name that `validate_timezone` accepts |
| `status_text` | a string of 0 to 100 characters |
| `status_emoji` | a string of 0 to 64 characters |
| `avatar_url` | a string of 0 to 2048 characters |

Any other value for these fields (a wrong type, or a length outside the range) → `400`. Unknown fields are
ignored.

## Rules:

C5.1 **Reading a profile.** `GET /api/users/{id}` returns `200 {user}` on every node, with at least
  `{id, username, display_name, timezone, avatar_url, status_text, status_emoji}` and no password field. An
  unknown id → `404`.

C5.2 **Updating a profile.** `PATCH /api/users/me` — any subset of the profile fields — returns `200 {user}`
  with the new values. They are stored: `GET /api/users/{id}` and `GET /api/auth/me` on other nodes show
  them. Fields that were not sent keep their values.
  Also: no token → `401` on `GET /api/users/{id}` and `PATCH /api/users/me`.

C5.3 **Validation.** An invalid timezone (for example `Mars/Olympus_Mons`), an empty `display_name`, or a
  field longer than its limit (for example a 1000-character `status_text`) → `400`, and nothing in that
  request is stored (other fields sent with it do not change either).

C5.4 **user.updated.** After a successful `PATCH /api/users/me`, every open WebSocket on every node, even
  one that subscribed to no channel, receives `{"type": "user.updated", "user": UserObj}` with the new
  values within 3 seconds. The frame has no `channel_id` and no `event_id`, and it does not advance any
  channel's `event_id`.

Every request must answer within 5 seconds.
