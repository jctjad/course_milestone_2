# Auth.3: invitations

Routes covered: `POST /api/workspaces/{slug}/invitations`, `GET /api/workspaces/{slug}/invitations`, 
  `POST /api/invitations/{code}/accept`, `DELETE /api/workspaces/{slug}/invitations/{code}`.
Check: `bash /spec/component-A-staged/checks/check_A_3.sh`. The rule numbers below match its output.

Do not start this spec until `check_A_1.sh` and `check_A_2.sh` both pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `get_user_by_username(username)` | A.1 | Turning `invited_username` into the targeted user's id |
| `get_workspace_by_slug(slug)` | A.1 | The workspace in the URL. Unknown slug → `404` |
| `get_member_role(workspace_id, user_id)` | A.1 | The caller's role on create, list and delete |
| `is_admin_or_owner(role)` | A.2 | Admin/owner checks on create, list and delete. Anyone else → `403` |
| `add_workspace_member(workspace_id, user_id, role)` | A.1 | Adding the accepting user to the workspace as a `member` |
| `member_to_json(member)` | A.1 | The `member` object returned when an invitation is accepted |
| `workspace_to_json(workspace)` | A.1 | The `workspace` object returned when an invitation is accepted |

## Adds

Define these as separate Python functions with these names. A.4 calls them.

| Function | What it does |
|---|---|
| `create_invitation(workspace_id, created_by, expires_in_seconds=None, max_uses=1, invited_user_id=None)` | Stores and returns a new invitation with a unique, hard-to-guess `code`. A targeted invitation (`invited_user_id` set) always has `max_uses` of `1` |
| `get_invitation(code)` | Returns the invitation with this code, or `None` if it does not exist |
| `is_invitation_usable(invitation)` | True only if the invitation is not expired, not revoked, and has uses left |
| `list_active_invitations(workspace_id)` | Returns every usable invitation in the workspace |
| `redeem_invitation(invitation, user_id)` | Consumes one use and adds the user as a workspace `member`. A user who is already a member keeps their current role |
| `revoke_invitation(invitation)` | Marks the invitation as revoked so it can no longer be accepted |
| `invitation_to_json(invitation)` | Returns the `invitation` object sent in responses, with at least `{code, workspace_id, max_uses, uses, expires_at, invited_user_id}` |

Invitations and their use counts must be stored where all three nodes can read them. An
invitation created on one node can be listed, accepted or revoked on any other.

## Rules:

A3.1 **Creating Workspace Invitation.** `POST /api/workspaces/{slug}/invitations` — admin/owner only; body
  `{expires_in_seconds?, max_uses?, invited_username?, invited_user_id?}` and returns `201 {invitation}` 
  with a `code`. `max_uses` defaults to `1`; targeted invitations are single-use (`max_uses` is `1` even
  if a larger value is sent).
  Also: a `member`, `guest` or non-member → `403`; an unknown `slug` → `404`; no token → `401`.

A3.2 **Viewing Workspace Invitations.** `GET /api/workspaces/{slug}/invitations` lists active invitations 
  for admins/owners as `200 {invitations: [...]}`.
  Also: a `member`, `guest` or non-member → `403`; no token → `401`.

A3.3 **Code Invitation.** `POST /api/invitations/{code}/accept` returns `200`, consumes one use and makes
  the caller a workspace `member`; a different user accepting 
  an `invited_user_id`-targeted invite gets `403`, while an unknown, expired, exhausted, or revoked
  code gets `404`.
  Also: an `invited_username`-targeted invite behaves the same way (anyone else → `403`); used-up and
  expired invitations no longer appear in the list (rule A3.2); no token → `401`.

A3.4 **Deleting Code Invitation.** `DELETE /api/workspaces/{slug}/invitations/{code}` is admin/owner-only and 
  revokes an invitation with `200 {"deleted":true}`.
  Also: a `member` → `403`; an unknown `code` → `404`; no token → `401`; a revoked code no longer appears
  in the list.

Every request must answer within 5 seconds.
