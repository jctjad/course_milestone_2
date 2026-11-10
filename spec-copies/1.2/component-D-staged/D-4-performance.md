# D.4: WebSocket speed under load

Routes covered: none new. This step makes live delivery fast and complete when all three nodes take writes
at once.
Check: `bash /spec/component-D-staged/checks/check_D_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_D_1.sh` to `check_D_3.sh` pass.

## Uses

| Function | From | Used here for |
|---|---|---|
| `emit_event(channel_id, kind, payload)` | B.5 | Every post under load |
| `event_pump()` | D.1 | Delivery under load |

**What makes it slow.** One database connection or one global lock shared by all requests; an event pump
that sleeps a fixed 100 ms even when Redis has just woken it; writing to sockets while holding a database
transaction; and a single slow subscriber holding up the rest (send to each socket with a short timeout,
and drop a socket that fails). Keep write transactions short: insert the message and its event, commit,
then notify.

## Rules:

D4.1 **Latency and completeness.** While writers on all three nodes post messages at the same time (about
  30 messages per second in total), every WebSocket subscriber on every node receives each
  `message.created`. Across all subscribers, the time from sending the post to receiving its
  `message.created` has a median of at most 300 ms and a 95th percentile of at most 800 ms, and no more than
  1% of the expected deliveries are missing. Every post returns `201`.

Every request must answer within 5 seconds.
