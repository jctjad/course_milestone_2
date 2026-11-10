# D.2: a killed HTTP node

Routes covered: none new. This step makes the cluster keep serving while one HTTP node is dead, and bring
it back on its own.
Check: `bash /spec/component-D-staged/checks/check_D_2.sh`. The rule numbers below match its output.

Do not start this spec until `check_D_1.sh` passes.

## Uses

| Function | From | Used here for |
|---|---|---|
| `event_pump()` | D.1 | Delivery to subscribers on the surviving nodes |
| `events_since(channel_id, since_event_id)` | B.5 | Replaying missed events after a reconnect |

**Resume (if Spec B is part of this run, this is B5.4: reuse it).** A client sends
`{"type": "resume", "channel_id": <id>, "since_event_id": <int>}` on any node, including one it was never
connected to. The node replays every stored event of that channel with `event_id > since_event_id`, oldest
first, then sends `{"type": "resumed", "channel_id": <id>, "head_event_id": <int>}`. Keep at least the last
1000 events of every channel.

**Why a node may die.** The grader `SIGKILL`s one of the three HTTP nodes with no warning. Nothing on the
other two may depend on it: no node may be a "primary" that holds state, locks or connections the others
need. A request in flight on the dead node is simply lost.

## Rules:

D2.1 **The other two keep serving.** After the process on port `8001` is killed with `SIGKILL`, ports
  `8000` and `8002` answer `GET /api/health` (`200`), reads (`GET /api/channels/{id}/messages`, `200`) and
  writes (`POST /api/channels/{id}/messages`, `201`), each within 2 seconds.

D2.2 **Live delivery continues.** WebSocket subscribers that were connected to `8000` and `8002` before the
  kill receive every message posted during the outage as `message.created`, with consecutive `event_id`s.

D2.3 **Replay across a reconnect.** A client that saw events up to `event_id` `h` before the kill, and
  reconnects to `8002`, gets every event after `h` by sending `resume` with `since_event_id` `h`, followed by
  `resumed` with the current head.

D2.4 **The node comes back.** Within 5 seconds of the kill, with no outside action, port `8001` answers
  `GET /api/health` with `200` and `node_id` `1`, and it serves the data written during the outage. A message
  posted through `8001` afterwards returns `201` and is readable on the other nodes.

D2.5 **Node 0 is not special.** The same holds when the process on port `8000` is killed: `8001` and
  `8002` keep answering health, reads and writes within 2 seconds, and `8000` comes back on its own within 5
  seconds with `node_id` `0` and takes writes again. Nothing in the cluster may depend on node 0 being alive
  (schema setup, leader threads and the like belong in `start.sh` or in every node).

Every request must answer within 5 seconds.
