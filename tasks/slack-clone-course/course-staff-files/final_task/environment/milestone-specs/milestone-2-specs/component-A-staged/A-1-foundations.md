# Auth.1: foundations

Routes covered: `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/workspaces`,
  `GET /api/workspaces`, `POST /api/channels/{id}/join`, `GET /api/workspaces/{slug}/members`.
Check: `bash /spec/component-A-staged/checks/check_A_1.sh`. The rule numbers below match its output.

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on
`127.0.0.1:8000`, `:8001`, and `:8002`.

## Uses

The A.0 cluster (`/app/start.sh`, three node processes, `GET /api/health`). No functions.

## Adds

Define these as separate Python functions with these names. A.2, A.3 and A.4 call them.

| Function | What it does |
|---|---|
| `validate_username(username)` | True only for letters, digits and underscore, with no spaces or special chars |
| `validate_password(password)` | True only for 8 or more characters |
| `hash_password(password)` | Returns the stored form of a password. Never store the plain password |
| `create_user(username, password, display_name=None)` | Stores and returns a new user; signals a conflict on a duplicate |
| `get_user_by_username(username)` | Returns the stored user, or None if there is none. Login uses it to find the user |
| `issue_token(user_id)` | Creates, stores and returns a new bearer token of 16 or more characters |
| `verify_password(password, stored_hash)` | True only if the password matches the stored hash |
| `user_from_token(token)` | Returns the token's user, or None if missing/invalid |
| `user_to_json(user)` | Returns the `user` object (`UserObj`) sent in responses, with at least `{id, username, display_name, timezone, avatar_url, status_text, status_emoji}` (use `null` or `""` for values the user has not set). Never includes the password |
| `get_workspace_by_slug(slug)` | Returns the workspace with this slug, or None if it does not exist |
| `workspace_to_json(workspace)` | Returns the `workspace` object sent in responses, with at least `{id, slug, name, owner_id, join_mode}` |
| `get_member_role(workspace_id, user_id)` | Returns the user's role in the workspace (`"owner"`, `"admin"`, `"member"` or `"guest"`), or None if they are not a member |
| `add_workspace_member(workspace_id, user_id, role)` | Stores the user as a workspace member with the given role and returns the membership |
| `member_to_json(member)` | Returns the `member` object sent in responses, with at least `{user_id, role}` |
| `create_channel(workspace_id, name, is_private=False)` | Stores a new channel in the workspace and returns it |
| `get_channel(channel_id)` | Returns the channel with this id, or None if it does not exist |

Users, tokens, workspaces, memberships and channels must be stored where all three nodes can read them.

## Rules:

A1.1 **Register Authentication.** `POST /api/auth/register` — body `{username, password, display_name?}`. `username` is a short URL-safe slug (letters/digits/underscore, no spaces or special chars). `password` >= 8 characters. Returns `201 {user, token}` where `token` is a bearer token of length >= 16. Duplicate `username` → `409`. Invalid username (e.g. contains a space) or short password → `400`. The endpoint takes no email field.
  Also: `user` carries at least `{id, username, display_name, timezone, avatar_url, status_text, status_emoji}`;
  `user.display_name` is the value sent; the `user` object never contains a password field; a
  username with a special character (e.g. `bad!name`) or a body with no `password` → `400`.

A1.2 **Login Authentication.** `POST /api/auth/login` — body `{username, password}`. Returns `200 {user, token}`. Wrong password → `401`.
  Also: an unknown username → `401`; a user registered on one node can log in on any node.

A1.3 **User Authentication.** `GET /api/auth/me` — returns `200 {user}` for the bearer-token holder. Missing or invalid token → `401`.

A1.4 **Workspace Object.** `WorkspaceObj` carries at least `{id, slug, name, owner_id, join_mode}`. `join_mode` is `"open"` or `"invite_only"` and defaults to `"open"` on creation.
  Also: every workspace in the `GET /api/workspaces` list carries the same fields.

A1.5 **Workspace Creation.** `POST /api/workspaces` — body `{slug, name}`. `slug` is kebab-case (regex `^[a-z0-9-]{2,32}$`). Returns `201 {workspace, general_channel}` — every new workspace auto-creates a `#general` channel and the creator is its `owner`. Duplicate `slug` → `409`.
  Also: `general_channel` is public (`is_private: false`) and the creator is a member of it; a `slug` that does not match the regex
  (e.g. `Bad_Slug`, 1 character, or 33 characters) → `400`; no token → `401`.

A1.6 **Workspace Retrieval.** `GET /api/workspaces` — `200 {workspaces: [...]}` filtered to ones the caller is a member of or owns. `GET /api/workspaces/{slug}/members` — `200 {members: [{user_id, role, ...}]}` listing every workspace member with their role.
  Also: no token on either route → `401`; an unknown `slug` → `404`.

A1.7 **Joining Workspaces.** `POST /api/channels/{id}/join` — `200`; non-existent → `404`; private channel without invite → `403`. On an `open` workspace, callers who are not yet workspace members get auto-onboarded (see workspace section above).
  Also: joining a channel you already joined is still `200` and does not add a second membership
  (the user appears once in `members`); no token → `401`.

A1.8 **Shared store that starts on a new machine.** The grader starts `/app/start.sh` in a new container that
  has only `/app` and the packages in `requirements.txt`, so `start.sh` must create every folder and file it
  needs (for example `mkdir -p /var/lib/huddle`). The check tests this first: it stops `start.sh` and the
  nodes, moves `/var/lib/huddle/` to `/var/lib/huddle.before-check/` and any SQLite files under `/tmp` to
  `/tmp/huddle.before-check/`, and runs `/app/start.sh`. All three nodes must answer `GET /api/health` within
  10 seconds (if they do not, the check puts your data back and restarts the cluster on it).
  Then: all three nodes keep their data in one SQLite file (see **Store and files** in A.0), and writes made
  at the same time on different nodes are all kept. 30 users registered at once (10 through each port) all
  get `201`, and each one's token then works for `GET /api/auth/me` on a different port.
  Also: the SQLite file is under `/var/lib/huddle/` or `/tmp`, and a username registered a moment ago is
  found in that file or its `-wal` file. A JSON or other flat file that each node reads and rewrites does
  not meet this rule: it is slow once data grows, and two nodes writing at once overwrite each other.

Every request must answer within 5 seconds.
