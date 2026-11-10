# Auth.4: join mode

Routes covered: `PATCH /api/workspaces/{slug}`, `POST /api/channels/{id}/join`.
Check: `bash /spec/component-A-staged/checks/check_A_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_A_1.sh`, `check_A_2.sh`, and `check_A_3.sh` all pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_workspace_by_slug(slug)` | A.1 | The workspace in the URL. Unknown slug → `404` |
| `workspace_to_json(workspace)` | A.1 | The `workspace` object in the `PATCH` response |
| `get_member_role(workspace_id, user_id)` | A.1 | The caller's role on `PATCH`, and telling members from non-members on channel join |
| `is_admin_or_owner(role)` | A.2 | The admin/owner check on `PATCH`. Anyone else → `403` |
| `add_workspace_member(workspace_id, user_id, role)` | A.1 | Adding a non-member as a `member` when they join a channel in an `open` workspace |
| `get_channel(channel_id)` | A.1 | The channel being joined. Unknown id → `404` |
| `redeem_invitation(invitation, user_id)` | A.3 | The only way a non-member gets into an `invite_only` workspace |

## Adds

| Function | What it does |
|---|---|
| `validate_join_mode(join_mode)` | True only for `"open"` or `"invite_only"` |
| `update_workspace(workspace_id, name=None, join_mode=None)` | Stores the changed fields and returns the updated workspace. Fields left as `None` are not changed |
| `ensure_workspace_member(workspace, user_id)` | Returns the user's role if they are already a member. If not, and `join_mode` is `"open"`, adds them as a `member` and returns `"member"`. If not, and `join_mode` is `"invite_only"`, returns `None` |

`POST /api/channels/{id}/join` checks in this order: unknown channel → `404`; private channel
the caller was not invited to → `403` (before adding anyone to the workspace); then
`ensure_workspace_member`, answering `403` when it returns `None`; then the role checks from A.2. A change to `join_mode` made
on one node must apply to channel joins on every node.

## Rules:

A4.1 **Join Mode.** `PATCH /api/workspaces/{slug}` — body any subset of `{name, join_mode}` — admin/owner only;
  `200 {workspace}` on success.
  Also: fields not in the body stay unchanged; a `join_mode` other than `"open"` or `"invite_only"` → `400`
  and nothing changes; a `member`, `guest` or non-member → `403`; an unknown `slug` → `404`; no token → `401`.

A4.2 **Open-Workspace Membership.** When `join_mode == "open"`, any authenticated user can call 
`POST /api/channels/{id}/join` on a *public* channel in that workspace and is implicitly added as a `member` 
of the workspace (returns `200`).
  Also: a *private* channel is still `403` for them, and they are not added to the workspace.

A4.3 **Invite-Only Membership.** When `join_mode == "invite_only"`, non-members of the workspace get `403` on 
  the channel-join unless they redeem a workspace invitation first.
  Also: this applies to every public channel, not only `#general`; existing members still join with
  `200`; setting `join_mode` back to `"open"` lets non-members join again.

Every request must answer within 5 seconds.
