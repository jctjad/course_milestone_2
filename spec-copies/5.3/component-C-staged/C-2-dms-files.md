# C.2: DMs and files

Routes covered: `POST /api/dms`, `POST /api/files`, `GET /api/files/{id}`, `GET /api/files/{id}/download`,
  and `file_ids` on `POST /api/channels/{id}/messages`.
Check: `bash /spec/component-C-staged/checks/check_C_2.sh`. The rule numbers below match its output.

Do not start this spec until `check_C_1.sh` passes.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | The caller of every route. Missing or invalid token → `401` |
| `create_channel(workspace_id, name, is_private=False)` | A.1 | Storing the DM channel |
| `add_channel_member(channel_id, user_id)` | B.1 | Both users become members of the DM |
| `can_read_channel(channel, user_id)` | B.2 | Only the two users may read or post in a DM |
| `create_message(...)` | B.2 | Extend it with `file_ids` |
| `message_to_json(message)` | B.2 | Extend it so `files` lists the attached `FileObj`s |

## Adds

| Function | What it does |
|---|---|
| `get_or_create_dm(user_a, user_b)` | Returns the one DM channel for this pair of users, creating it the first time. The pair is unordered: `(a, b)` and `(b, a)` give the same channel, on every node |
| `save_file(uploader_id, filename, content_type, data)` | Stores the bytes where all three nodes can read them (a BLOB in the shared database or a file under one shared directory) and returns the file record |
| `get_file(file_id)` | Returns the file record, or None |
| `file_to_json(file)` | Returns `FileObj = {id, uploader_id, filename, content_type, size, created_at}`: `size` in bytes, `created_at` an RFC 3339 UTC string |
| `attach_files(message_id, author_id, file_ids)` | Checks every id (rule C2.5), then links the files to the message. Signals `400` on any bad id, before the message is stored |

**Uploads.** Read the whole request body (`Content-Length`) exactly once before parsing, even for a 10 MiB
file. Do not run the JSON body parser on `POST /api/files`: it would consume the multipart body.
Python 3.13 has no `cgi` module: parse `multipart/form-data` with `email.parser.BytesParser`, or split on
the boundary yourself. The `file` part's filename and content type are the ones the client sent.

**DM channels.** A DM is a channel with `is_dm: true`, `is_private: true` and `workspace_id: null`, named
for example `dm-<lower id>-<higher id>`. It belongs to no workspace, so it is never listed in a workspace's
`channels`. Any two registered users may open a DM; they do not need to share a workspace.

## Rules:

C2.1 **DMs.** `POST /api/dms` — body `{user_id}` — returns `200 {channel_id}` (not `201`). The same
  `channel_id` comes back when the other user opens the DM, and on repeated calls, from any node. Both users
  can post in it (`201`) and read it with `GET /api/channels/{id}/messages` (`200`). A third user who reads
  it → `403`. An unknown `user_id` → `404`.
  Also: two users who share no workspace can open a DM (`200`). A DM with yourself → `400`. No token →
  `401`.

C2.2 **Upload.** `POST /api/files` — `multipart/form-data` with a `file` part — returns `201 {file}` where
  `file` is `FileObj = {id, uploader_id, filename, content_type, size, created_at}`: `uploader_id` is the
  caller, `filename` and `content_type` are what the client sent, `size` is the byte count.
  `GET /api/files/{id}` returns `200 {file}` with the same object on every node. An unknown id → `404`. A
  request with no `file` part → `400`. No token → `401` on `POST /api/files` and `GET /api/files/{id}`.

C2.3 **Download.** `GET /api/files/{id}/download` returns the exact bytes that were uploaded (text and
  binary), on every node, with `Content-Type` equal to the uploaded `content_type` and a
  `Content-Disposition` header that carries the filename (for example
  `attachment; filename="report.txt"`). An unknown id → `404`.

C2.4 **Size limit.** A file of exactly 10 MiB (10485760 bytes) is accepted (`201`). One byte more → `413`,
  and nothing is stored.

C2.5 **Attaching files.** `POST /api/channels/{id}/messages` — body `{body, file_ids}` — returns `201` with
  the files' `FileObj`s in the message's `files` (also in `GET .../messages`). Every id must be a file the
  caller uploaded that is not attached to any message yet. Otherwise (someone else's file, a file already
  attached, an unknown id) → `400`, and no message is posted.

Every request must answer within 5 seconds.
