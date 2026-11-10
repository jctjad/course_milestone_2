# C.3: slash commands

Routes covered: `POST /api/channels/{id}/messages` when `body` starts with `/`.
Check: `bash /spec/component-C-staged/checks/check_C_3.sh`. The rule numbers below match its output.

Do not start this spec until `check_C_1.sh` and `check_C_2.sh` pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `create_message(...)` | B.2 | `/me`, `/shrug` and `//literal` post normal messages |
| `require_writable(channel)` | B.1 | Every command except `/unarchive` → `423` in an archived channel |
| `validate_topic(topic)` | B.1 | `/topic`: over 250 characters → `400` |
| `update_channel(channel_id, **fields)` | B.1 | `/topic`, `/archive`, `/unarchive` |
| `get_member_role(workspace_id, user_id)` | A.1 | `/archive` and `/unarchive` are workspace-owner only; `/topic` follows B1.4 |
| `get_user_by_username(username)` | A.1 | `/invite @user` |
| `add_channel_member(channel_id, user_id)` | B.1 | `/invite @user` |
| `channel_to_json(channel)`, `user_to_json(user)` | A.2, A.1 | The `channel` and `invited_user` in responses |
| `emit_event(channel_id, kind, payload)` | B.5 | The same events the matching REST routes emit (`channel.updated`, `channel.archived`, `member.joined`, ...) |

## Adds

| Function | What it does |
|---|---|
| `run_slash_command(channel, user, body)` | Called by the post route before storing anything, when `body` starts with `/` but not `//`. Returns the HTTP status and response body, or signals `400` for an unknown command |

A command is the text from `/` up to the first space. The rest, trimmed, is its argument.

| Command | Result |
|---|---|
| `/me <text>` | A normal message whose `body` is exactly `"/me <text>"`, not stripped or reformatted. `201 {message}` |
| `/shrug [text]` | A normal message whose `body` is `"<text> ¯\_(ツ)_/¯"` (a space, then the emoticon), or just `"¯\_(ツ)_/¯"` with no text. The emoticon has one backslash. `201 {message}` |
| `/topic <text>` | Sets the channel topic, same permissions and limits as `PATCH /api/channels/{id}` (B1.4). `201 {"message": null, "channel": ChannelObj}` |
| `/invite @username` | Any member of this channel may add a member of the channel's workspace to it, even a private channel. `201 {"message": null, "invited_user": UserObj}` |
| `/archive` | Same as `POST /api/channels/{id}/archive` (owner only). `201 {"message": null, "channel": ChannelObj}` |
| `/unarchive` | Same as `POST /api/channels/{id}/unarchive` (owner only). Works in an archived channel. `201 {"message": null, "channel": ChannelObj}` |

Side-effect commands (`/topic`, `/invite`, `/archive`, `/unarchive`) store no message.

## Rules:

C3.1 **/me.** `/me waves` returns `201` with a message whose `body` is exactly `/me waves`. `/me` with no
  text (or only spaces) → `400`, and nothing is posted.

C3.2 **/shrug.** `/shrug fine` returns `201` with `body` `fine ¯\_(ツ)_/¯`; `/shrug` alone gives
  `¯\_(ツ)_/¯`.

C3.3 **Unknown commands and //.** An unknown command (for example `/nosuchcommand hi`) → `400`, and nothing
  is posted. A body starting with `//` is not a command: it is posted as a normal message (`201`), with the
  body stored exactly as typed (`//literal` has body `//literal`).

C3.4 **/topic.** `/topic Release on Friday` returns `201` with `"message": null` and `channel.topic` equal to
  `Release on Friday`. The new topic shows in `GET /api/workspaces/{slug}` on every node. No message is
  stored. A workspace member who is not in the channel (and is not the owner) → `403`.

C3.5 **/invite.** `/invite @username` in a private channel returns `201` with `"message": null` and
  `invited_user.id` equal to that user's id. The user is now in `GET /api/channels/{id}/members` and can
  read the channel (`200`).
  Also: an unknown username → `404`; a user who is not a member of the channel's workspace → `403`;
  `/invite` with no username → `400`; a caller who is not a member of the channel → `403`. None of these
  add anyone to the channel.

C3.6 **/archive and /unarchive.** `/archive` from the workspace owner returns `201` with `"message": null` and
  `channel.is_archived` `true`; after it, a normal post → `423`. `/unarchive` in the archived channel returns
  `201` with `is_archived` `false`, and posting works again (`201`). A workspace member who is not the owner
  → `403` for both.

Every request must answer within 5 seconds.
