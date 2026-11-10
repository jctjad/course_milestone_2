# The basics (build these first)

`/app` is empty. The components in this run sit on top of a small core that you build first. Build only
what is listed here. Where a spec's **Uses** table says a function comes from `A.1`, `B.2` and so on, it
means the function of that name below.
Check: `bash /spec/basics/checks/check_basics.sh` (BASICS.1 to BASICS.5) and
`bash /spec/basics/checks/check_baseline.sh` (M0.1 to M0.4, the baseline conditions in `/spec/README.md`).

## Cluster

`/app/start.sh` stays in the foreground and starts three separate node processes on `127.0.0.1:8000`,
`:8001` and `:8002` (killing one must leave the other two running). `GET /api/health` returns
`200 {"status": "ok", "node_id": <0, 1 or 2>}` (an integer matching the port). All three nodes share one
store: use one SQLite file in WAL mode, open a connection per request and always close it
(`try`/`finally`), keep the busy wait at 2 seconds or less, and create the schema once in `start.sh` before
the nodes start (`CREATE TABLE IF NOT EXISTS`). **`start.sh` must never delete or reset existing data:** the
cluster is restarted during checks and grading, and every user, message and file must survive a restart.
**Keep every file that changes while the cluster runs outside `/app`:** the database (with its `-wal` and
`-shm` files), uploaded files, pid files and logs go under `/var/lib/huddle/` (create it in `start.sh`) or
`/tmp`. `/app` holds only your code: it is copied when you submit, and a file that changes during that copy
makes the whole copy fail.

## Routes

| Route | Result |
|---|---|
| `POST /api/auth/register` `{username, password, display_name?}` | `201 {user, token}`; username letters/digits/underscore; password 8+ chars; token 16+ chars; duplicate `409`; invalid `400` |
| `POST /api/auth/login` `{username, password}` | `200 {user, token}`; wrong password or unknown user `401` |
| `GET /api/auth/me` | `200 {user}` |
| `POST /api/workspaces` `{slug, name}` | `201 {workspace, general_channel}`: the creator is the `owner` and a member of a new public `#general`; slug `^[a-z0-9-]{2,32}$`, else `400`; duplicate `409` |
| `GET /api/workspaces/{slug}` | `200 {workspace, channels}` for members: public channels plus private ones the caller is in, archived ones left out; non-member `403`; unknown `404` |
| `POST /api/workspaces/{slug}/channels` `{name, is_private?}` | `201 {channel}`; the creator becomes a member; only the owner may create private channels; name kebab-case up to 32 chars, else `400`; duplicate `409` |
| `POST /api/channels/{id}/join` | `200`; joining a public channel makes the caller a workspace `member` if they were not one; private channel `403`; unknown `404` |
| `GET /api/channels/{id}/members` | `200 {members: [UserObj, ...]}` |
| `PATCH /api/channels/{id}` `{topic}` | `200 {channel}`; channel members or the owner; topic up to 250 chars |
| `POST /api/channels/{id}/archive`, `/unarchive` | `200 {channel}`; owner only; posting to an archived channel → `423` |
| `POST /api/channels/{id}/messages` `{body}` | `201 {message}`; empty body `400`; `@username` mentions (letters/digits/underscore after an `@` that starts the body or follows a non-word character; workspace members only; never the author; deduplicated); `@channel` = every channel member except the author |
| `GET /api/channels/{id}/messages` | `200 {messages, next_cursor}` newest first; private channel non-member `403` |
| `ws://127.0.0.1:<port>/api/ws?token=<token>` | Bad token: reject the upgrade. `{"type":"subscribe","channel_id":<id>}` → `{"type":"subscribed","channel_id":<id>,"head_event_id":<int>}`. Each new message emits `{"type":"message.created","event_id":<next>,"channel_id":<id>,"message":MessageObj}` to subscribers on all three nodes |

## Objects

`UserObj` has at least `{id, username, display_name, timezone, avatar_url, status_text, status_emoji}` and
never a password. `ChannelObj` has `{id, workspace_id, name, is_private, is_dm, topic, is_archived}`
(`topic` defaults to `""`). `MessageObj` has `{id, channel_id, author_id, author, body, parent_id,
reply_count, created_at, edited_at, files, reactions, mentions}` (`files` and `reactions` start as `[]`).

## Events

`event_id` is dense per channel across all three nodes: store each event with one shared counter per
channel, updated in the same transaction. Every node polls the shared event store about every 100 ms and
sends new events to its own subscribers.

## Functions

Define these with these names; the specs call them.

| Function | Spec name | What it does |
|---|---|---|
| `user_from_token(token)` | A.1 | The token's user, or None |
| `user_to_json(user)` | A.1 | `UserObj` |
| `get_user_by_username(username)` | A.1 | The user, or None |
| `get_workspace_by_slug(slug)` | A.1 | The workspace, or None |
| `get_member_role(workspace_id, user_id)` | A.1 | `"owner"`, `"member"` or None |
| `create_channel(workspace_id, name, is_private=False)` | A.1 | Stores and returns a channel |
| `get_channel(channel_id)` | A.1 | The channel, or None |
| `channel_to_json(channel)` | A.2 | `ChannelObj` |
| `add_channel_member(channel_id, user_id)` | B.1 | Adds a membership (doing it twice keeps one) |
| `is_channel_member(channel_id, user_id)` | B.1 | True if the user is in the channel |
| `list_channel_members(channel_id)` | B.1 | The channel's members |
| `validate_topic(topic)` | B.1 | True for 250 characters or fewer |
| `update_channel(channel_id, **fields)` | B.1 | Stores `topic` / `is_archived`, returns the channel |
| `require_writable(channel)` | B.1 | Signals `423` if archived |
| `can_read_channel(channel, user_id)` | B.2 | Public: workspace members. Private: channel members |
| `create_message(channel_id, author_id, body, ...)` | B.2 | Stores and returns a message |
| `get_message(message_id)` | B.2 | The message, or None |
| `message_to_json(message)` | B.2 | `MessageObj` |
| `extract_mentions(body, channel, author_id)` | B.4 | The deduplicated mentioned user ids |
| `emit_event(channel_id, kind, payload)` | B.5 | Stores the event with the next `event_id`, returns the frame |
