# Auth.0: baseline cluster

Routes covered: `GET /api/health`, `GET /api/ws` (bad-token rejection only).
Check: `bash /spec/component-A-staged/checks/check_A_0.sh`. The rule numbers below match its output.

Build this first, before A.1. It is the smallest cluster every later spec runs on, and these four
conditions must still hold when you submit.

Your service is launched with `/app/start.sh`, which must stay in the foreground and keep the cluster
alive. The three HTTP nodes listen on `127.0.0.1:8000`, `:8001`, and `:8002`, and each is its own
process.

## Uses

Nothing. This is the first spec.

## Adds

| Function | What it does |
|---|---|
| `node_id()` | Returns this node's id: `0` on port `8000`, `1` on `8001`, `2` on `8002` (pass it to each node at startup, for example `--node-id 1`) |

`/app/start.sh` starts three separate node processes. Do not run all three ports in one process: killing
the process on port `8001` must leave `8000` and `8002` running.

**Store and files.** All three nodes share one store: use one SQLite file in WAL mode, open a connection per
request and always close it (`try`/`finally`), keep the busy wait at 2 seconds or less, and create the schema
once in `start.sh` before the nodes start (`CREATE TABLE IF NOT EXISTS`). **`start.sh` must never delete or
reset existing data:** the cluster is restarted during checks and grading, and every user, message and file
must survive a restart. **Keep every file that changes while the cluster runs outside `/app`:** the database
(with its `-wal` and `-shm` files), uploaded files, pid files and logs go under `/var/lib/huddle/` (create it
in `start.sh`) or `/tmp`. `/app` holds only your code: it is copied when you submit, and a file that changes
during that copy makes the whole copy fail.

## Rules:

A0.1 **Health endpoint.** `GET /api/health` on `127.0.0.1:8000` returns `200` with the JSON body
  `{"status": "ok", "node_id": 0}`. No token is needed.

A0.2 **Every node reports its own id.** Port `8001` answers `GET /api/health` with `200` and
  `node_id` `1`, and port `8002` with `node_id` `2`. Across the cluster the set of node ids is exactly
  `{0, 1, 2}`. `node_id` is a JSON integer, not a string.

A0.3 **WebSocket rejects a bad token.** A WebSocket connection to `ws://127.0.0.1:<port>/api/ws?token=<invalid>`
  on any of the three ports is rejected: the server never answers the upgrade with `101 Switching Protocols`.

A0.4 **Crash tolerance.** If the process that owns port `8001` is killed with `SIGKILL`, ports `8000`
  and `8002` keep answering `GET /api/health` with `200` and `"status": "ok"`.
  Also: the check restarts the cluster afterwards if `8001` does not come back on its own, so the
  cluster must start cleanly from `/app/start.sh` while data from earlier runs is still on disk.

Every request must answer within 5 seconds.
