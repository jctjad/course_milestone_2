# B.1: channels

Routes covered: `POST /api/workspaces/{slug}/channels`, `GET /api/workspaces/{slug}`,
  `PATCH /api/channels/{id}`, `POST /api/channels/{id}/leave`, `GET /api/channels/{id}/members`,
  `POST /api/channels/{id}/archive`, `POST /api/channels/{id}/unarchive`.
Check: `bash /spec/component-B-staged/checks/check_B_1.sh`. The rule numbers below match its output.

Do not start this spec until `check_A_1.sh` and `check_A_2.sh` both pass.

## Uses

Call these functions from Component A. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_workspace_by_slug(slug)` | A.1 | The workspace in the URL. Unknown slug → `404` |
| `workspace_to_json(workspace)` | A.1 | The `workspace` object in the workspace detail |
| `get_member_role(workspace_id, user_id)` | A.1 | Who may read the workspace, edit topics, archive |
| `get_channel(channel_id)` | A.1 | The channel in the URL. Unknown id → `404` |
| `create_channel(workspace_id, name, is_private=False)` | A.1 | Storing a new channel. Extend it with `topic=""`; the creator becomes a channel member (A.2) |
| `user_to_json(user)` | A.1 | Each `UserObj` in the channel members list |
| `can_create_channel(role, is_private)` | A.2 | The role checks on channel creation (rule A2.2) |
| `channel_to_json(channel)` | A.2 | Every `channel` object in responses |

## Adds

Define these as separate Python functions with these names. B.2 to B.6 call them.

| Function | What it does |
|---|---|
| `validate_channel_name(name)` | True only for kebab-case of 1 to 32 characters: lowercase letters, digits and single hyphens (`^[a-z0-9]+(-[a-z0-9]+)*$`) |
| `validate_topic(topic)` | True only for a string of 250 characters or fewer |
| `add_channel_member(channel_id, user_id)` | Stores the user as a channel member. Doing it twice keeps one membership. The join route (A.1) must call it |
| `remove_channel_member(channel_id, user_id)` | Removes the membership if there is one |
| `is_channel_member(channel_id, user_id)` | True only if the user is a member of the channel |
| `list_channel_members(channel_id)` | Returns the users who are members of the channel |
| `update_channel(channel_id, **fields)` | Stores changed fields (`topic`, `is_archived`) and returns the channel |
| `visible_channels(workspace_id, user_id, include_archived=False)` | Public channels plus private channels the user is a member of; archived ones only when `include_archived` is true |
| `require_writable(channel)` | Signals `423` if the channel is archived. Every write to a channel calls it (B.2) |

Channels and channel memberships must be stored where all three nodes can read them.

## Rules:

B1.1 **Channel object.** `POST /api/workspaces/{slug}/channels` — body `{name, is_private?, topic?}` —
  returns `201 {channel}`, where `ChannelObj` carries at least `{id, workspace_id, name, is_private, is_dm,
  topic, is_archived}`. `is_dm` and `is_archived` are `false`. `topic` is the value sent, and defaults to
  `""`. The body has no `description` field: a `description` that is sent does not fill `topic`.
  `general_channel` from `POST /api/workspaces` carries the same fields.

B1.2 **Channel validation.** A `name` that is not kebab-case (e.g. `Bad Name`, `bad_name`) or is longer than
  32 characters → `400`. A `topic` longer than 250 characters → `400` (exactly 250 is fine). A duplicate
  `name` in the same workspace → `409`; the same name in a different workspace is fine (`201`).

B1.3 **Workspace detail.** `GET /api/workspaces/{slug}` returns `200 {workspace, channels, read_state}` for a
  workspace member. `channels` lists the public channels and the private channels the caller is a member
  of; private channels the caller is not a member of are left out. `read_state` is a list (it is filled in
  by B.6; until then it may be empty). A non-member of the workspace → `403`; unknown slug → `404`.

B1.4 **Topic and leave.** `PATCH /api/channels/{id}` — body `{topic}` — returns `200 {channel}` with the new
  topic. The caller must be a channel member or the workspace owner (the owner does not need to be a
  member); anyone else → `403`. A topic over 250 characters → `400`. `POST /api/channels/{id}/leave` returns
  `200 {channel}` and is idempotent: leaving again is still `200`. A user who left can join again (`200`).

B1.5 **Channel members.** `GET /api/channels/{id}/members` returns `200 {members: [UserObj, ...]}`. Each
  `UserObj` carries at least `{id, username, display_name, timezone, avatar_url, status_text,
  status_emoji}`. The workspace creator is a member of `#general`; the creator of a channel is a member of
  it; joining adds the caller and leaving removes them. Unknown channel → `404`; no token → `401`.

B1.6 **Archive.** `POST /api/channels/{id}/archive` and `POST /api/channels/{id}/unarchive` are
  workspace-owner only (admins and members → `403`) and return `200 {channel}` with `is_archived` set to
  `true` / `false`. An archived channel is left out of the workspace detail's `channels` unless the request
  adds `?include_archived=1` (`?include_archived=true` works too).

Every request must answer within 5 seconds.
