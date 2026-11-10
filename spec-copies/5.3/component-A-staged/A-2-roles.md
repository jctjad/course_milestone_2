# Auth.2: roles

Routes covered: `POST /api/channels/{id}/join`, `POST /api/workspaces/{slug}/channels`, 
  `PATCH /api/workspaces/{slug}/members/{user_id}`, `POST /api/workspaces/{slug}/transfer_ownership`.
Check: `bash /spec/component-A-staged/checks/check_A_2.sh`. The rule numbers below match its output.

Do not start this spec until `check_A_1.sh` prints `RESULT: 7 passed, 0 failed`.

## Uses

Call these functions from A.1. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `user_from_token(token)` | The caller of every route. Missing or invalid token → `401` |
| `get_workspace_by_slug(slug)` | The workspace in the URL. Unknown slug → `404` |
| `get_member_role(workspace_id, user_id)` | The caller's role and the target's role in every permission check |
| `member_to_json(member)` | The `member` object in the `PATCH` response |
| `create_channel(workspace_id, name, is_private=False)` | Storing a channel created by `POST /api/workspaces/{slug}/channels`. Also store the creator as a member of the new channel |
| `get_channel(channel_id)` | The channel being joined. Unknown id → `404` |

## Adds

Define these as separate Python functions with these names. A.3 and A.4 call them.

| Function | What it does |
|---|---|
| `validate_role(role)` | True only for `"owner"`, `"admin"`, `"member"` or `"guest"` |
| `is_admin_or_owner(role)` | True only for `"admin"` or `"owner"` |
| `can_create_channel(role, is_private)` | True only if this role may create this kind of channel: `guest` never, `member` public only, `admin`/`owner` both |
| `can_change_role(actor_role, target_role, new_role)` | True only if a member with `actor_role` may change a `target_role` member to `new_role`. Never true when `target_role` is `"owner"`; an `admin` may not change another `admin` |
| `set_member_role(workspace_id, user_id, role)` | Stores the member's new role and returns the updated membership |
| `transfer_ownership(workspace_id, new_owner_id)` | Makes `new_owner_id` the `owner`, makes the previous owner an `admin`, and updates the workspace's `owner_id` |
| `channel_to_json(channel)` | Returns the `channel` object (`ChannelObj`) sent in responses, with at least `{id, workspace_id, name, is_private, is_dm, topic, is_archived}` (`is_dm` and `is_archived` start `false`, `topic` starts `""`). `general_channel` in A.1 uses it too |

Every route that checks permissions must read the role with `get_member_role`, so a role
change made on one node applies on every node.

## Rules:

A2.1 **Per-Workspace Roles.** `owner`, `admin`, `member`, `guest`. Users can have different
  roles in different workspaces.

A2.2 **Role-Based Channel Permissions.** `guest` workspace members cannot join public channels
  (→ `403` on `POST /api/channels/{id}/join`) and cannot create channels (→ `403` on
  `POST /api/workspaces/{slug}/channels`). Regular `member`s can create public channels but cannot
  create private ones (→ `403` for `{is_private: true}`); private-channel creation is admin-or-owner.
  A successful create (body `{name, is_private?}`) returns `201 {channel}`.
  Also: **nobody joins a private channel without an invite** (rule A1.7). A `member`, `guest`, `admin`,
  the workspace `owner`, and a non-member all get `403` on `POST /api/channels/{id}/join` for a private
  channel they were not invited to, and a non-member is not added to the workspace. A non-member of the
  workspace cannot create channels in it (→ `403`).
  **The user who creates a channel is automatically a member of it**, so the creator of a private
  channel calling `POST /api/channels/{id}/join` on it gets `200` (already a member, rule A1.7). Do not
  block every private-channel join: block only callers who are not already channel members.

A2.3 **Role Changes.** `PATCH /api/workspaces/{slug}/members/{user_id}` — body `{role}` — returns
  `200 {member:{user_id, role, ...}}`. The current owner's role cannot be changed via PATCH (→ `403`).
  Also: a `role` that is not one of the four roles (e.g. `"superuser"`) → `400` and nothing changes; a
  `user_id` that is not a member, or an unknown `slug` → `404`; no token → `401`.

A2.4 **Promoting/Demoting.** Promoting/demoting between `admin` and `member`/`guest` is owner-or-admin; 
  admins cannot modify another admin or change `owner` (→ `403`); setting `role` to `owner` via PATCH 
  is rejected (→ `400`) — use `transfer_ownership`.
  Also: the owner can promote a `member` to `admin` and demote an `admin` back; `member`s and `guest`s
  cannot change anyone's role (→ `403`).

A2.5 **Transfer Ownership.** `POST /api/workspaces/{slug}/transfer_ownership` — body `{new_owner_id}`; 
  owner-only. The target must already be an `admin` (otherwise `400`); on success the target becomes 
  `owner` and the previous owner becomes `admin`.
  Also: returns `200`; an `admin` or `member` calling it → `403`, including the previous owner after the
  transfer; no token → `401`; the workspace's `owner_id` becomes the new owner.

Every request must answer within 5 seconds.
