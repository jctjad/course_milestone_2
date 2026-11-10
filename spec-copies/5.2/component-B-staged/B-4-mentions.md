# B.4: mentions

Routes covered: `POST /api/channels/{id}/messages` (the `mentions` field of the returned message).
Check: `bash /spec/component-B-staged/checks/check_B_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_B_1.sh` to `check_B_3.sh` all pass.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `get_user_by_username(username)` | A.1 | Turning `@username` into a user id |
| `get_member_role(workspace_id, user_id)` | A.1 | Dropping users who are not in the channel's workspace |
| `list_channel_members(channel_id)` | B.1 | Expanding `@channel` |
| `create_message(...)` | B.2 | Storing the resolved `mentions` with the message |
| `message_to_json(message)` | B.2 | The `mentions` list in every `MessageObj` |

## Adds

| Function | What it does |
|---|---|
| `extract_mentions(body, channel, author_id)` | Returns the deduplicated list of user ids mentioned in `body` (rules below). `create_message` calls it and stores the result |

A mention is `@` followed by a username (letters, digits, underscore). The `@` must be at the start of the
body or come right after a character that is not a letter, digit, underscore or `.`. So `hi @bob,` and
`(@bob)` mention bob, but `x@bob` and `bob@example.com` do not.

## Rules:

B4.1 **Resolving mentions.** Each `@username` that names a user in the channel's workspace adds that user's
  id to the message's `mentions`. The list has no duplicates (`@bob @bob` gives bob once) and never contains
  the author (mentioning yourself adds nothing). A workspace member who is not in the channel can still be
  mentioned by `@username`.

B4.2 **Things that are not mentions.** In-word or email-shaped text (`x@bob`, `bob@example.com`), an unknown
  username, and a user who is not a member of the channel's workspace add nothing to `mentions`.

B4.3 **@channel.** `@channel` adds every channel member except the author. Ids from `@channel` and
  `@username` are deduplicated together.

Every request must answer within 5 seconds.
