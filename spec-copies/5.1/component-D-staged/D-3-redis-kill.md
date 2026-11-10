# D.3: a killed Redis

Routes covered: none new. This step makes the cluster survive a dead Redis and pick it up again when it
comes back.
Check: `bash /spec/component-D-staged/checks/check_D_3.sh`. The rule numbers below match its output.

Do not start this spec until `check_D_1.sh` and `check_D_2.sh` pass.

## Uses

| Function | From | Used here for |
|---|---|---|
| `notify_nodes(channel_id)` | D.1 | Must not block or fail a write while Redis is down |
| `redis_listener()` | D.1 | Reconnects on its own when Redis is back |
| `event_pump()` | D.1 | Keeps delivering (by polling the database) while Redis is down |

**What breaks most often.** A write that waits on a Redis connection that no longer exists (connect or
send without a timeout), a listener thread that dies on the first error and never reconnects, and a
"Redis is down" flag that is never cleared. Give every Redis socket a timeout of 0.2 s, catch every error
in `notify_nodes`, and keep `redis_listener` in its reconnect loop forever.

## Rules:

D3.1 **Writes keep working.** After the `redis-server` process on port `6379` is killed with `SIGKILL`, a
  message posted through each of the three nodes returns `201` within 2 seconds.

D3.2 **Delivery keeps working.** WebSocket subscribers on all three nodes that were connected before the
  kill receive the messages posted during the outage as `message.created`, with consecutive `event_id`s.

D3.3 **Redis comes back and is used again.** Within 5 seconds of the kill, with no outside action, port
  `6379` answers `PING` with `PONG` again. Messages posted after that reach every subscriber, and the whole
  sequence (before, during and after the outage) has consecutive `event_id`s with no gaps or duplicates on
  every node.
  Also: within 5 seconds of Redis answering again, every node has subscribed again
  (`PUBSUB NUMSUB huddle:events` reports at least 3).

Every request must answer within 5 seconds.
