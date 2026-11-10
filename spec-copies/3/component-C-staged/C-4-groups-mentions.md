# C.4: user groups, @group-handle and @here

Routes covered: `GET` and `POST /api/workspaces/{slug}/groups`, `PATCH` and
  `DELETE /api/workspaces/{slug}/groups/{handle}`, and the `mentions` field of new messages.
Check: `bash /spec/component-C-staged/checks/check_C_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_C_1.sh` to `check_C_3.sh` pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_workspace_by_slug(slug)` | A.1 | The workspace in the URL. Unknown slug → `404` |
| `get_member_role(workspace_id, user_id)` | A.1 | Only workspace members may use groups; the owner may edit any group |
| `is_channel_member(channel_id, user_id)` | B.1 | `@group-handle` and `@here` only reach channel members |
| `list_channel_members(channel_id)` | B.1 | Expanding `@here` |
| `extract_mentions(body, channel, author_id)` | B.4 | Extend it with `@group-handle` and `@here`. Do not write a second mention parser |

## Adds

| Function | What it does |
|---|---|
| `validate_group_handle(handle)` | True only for a valid handle (see Handles below) |
| `create_group(workspace_id, creator_id, handle, name, user_ids)` | Stores a group; signals `409` on a duplicate handle in the workspace |
| `get_group(workspace_id, handle)` | Returns the group, or None |
| `update_group(group, **fields)` / `delete_group(group)` | Store changed `name` / `user_ids`; delete the group |
| `group_to_json(group)` | Returns `GroupObj = {id, handle, name, member_user_ids}`. Never a `members` key |
| `online_user_ids()` | Ids of users with at least one open WebSocket on any node (see Presence below) |

**Presence (for `@here`).** Use exactly this design; earlier runs lost a lot of time tuning others:

- On a WebSocket connect, insert a row `(conn_id, user_id, last_seen)` into a shared `presence` table.
- While the connection is open, its node updates `last_seen` every 0.5 seconds from one background thread
  per node (one `UPDATE` for all of that node's connections, not one per socket).
- When the connection closes or errors, delete its row in a `finally` block.
- `online_user_ids()` returns the users with a row whose `last_seen` is under 1.5 seconds old.

Do not send WebSocket pings from the server for this; a closed socket is detected when its read fails.

**Handles.** A handle is 1 to 32 characters of lowercase letters, digits, `-` and `_`, starting with a
letter or digit (`^[a-z0-9][a-z0-9_-]{0,31}$`), sent without the `@` (body `"eng-team"`, mention
`@eng-team`). `here` and `channel` are reserved and cannot be handles.

**Parsing group mentions.** Extend `extract_mentions`: after an `@` (same start rule as B.4), take the
longest run of handle characters. If it names a group in the channel's workspace, expand the group.
Otherwise fall back to the B.4 `@username` rule. So `@eng-team` is a group, and `@bob,` is still a user.

## Rules:

C4.1 **Creating a group.** `POST /api/workspaces/{slug}/groups` — body `{handle, name, user_ids}` — from any
  workspace member returns `201 {group}`, where `member_user_ids` lists exactly the ids sent in `user_ids`
  and there is no `members` key. A duplicate `handle` in the same workspace → `409`. A missing or invalid
  `handle` (for example `Bad Handle`, `@eng`, `here`) → `400`. A user who is not a workspace member → `403`.
  No token → `401`.

C4.2 **Listing groups.** `GET /api/workspaces/{slug}/groups` returns `200 {groups: [GroupObj, ...]}` on every
  node for any workspace member. A user who is not a workspace member → `403`. No token → `401`.

C4.3 **Updating and deleting.** `PATCH /api/workspaces/{slug}/groups/{handle}` — any subset of
  `{name, user_ids}` — returns `200 {group}` with the changes. Only the group's creator or the workspace owner
  may update or delete; anyone else → `403`. An unknown handle → `404` on `PATCH`.
  `DELETE /api/workspaces/{slug}/groups/{handle}` returns `200 {"deleted": true}`, and deleting again is
  still `200 {"deleted": true}`; the group is gone from the list.

C4.4 **@group-handle.** `@<handle>` in a message adds the group's members who are also members of the
  destination channel, excluding the author. Group members who are not in the channel are left out. Ids from
  `@group`, `@username` and `@channel` are deduplicated together.

C4.5 **@here.** `@here` adds every channel member, except the author, who has an open WebSocket on any node
  when the message is posted. Members with no open WebSocket are left out. A user whose WebSocket has closed
  is left out again within 2 seconds.

`mention_count` (read state) is not part of C: it counts from each message's `mentions`, which never
contains the author.

Every request must answer within 5 seconds.
