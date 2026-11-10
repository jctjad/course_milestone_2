# D.1: Redis fan-out and a supervising start.sh

Routes covered: none new. This step changes how events travel between nodes and how `/app/start.sh` runs
the cluster.
Check: `bash /spec/component-D-staged/checks/check_D_1.sh`. The rule numbers below match its output.

Do not start this spec until the basics in `/spec/README.md` work.

## Uses

Call these functions from the earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `emit_event(channel_id, kind, payload)` | B.5 | Still the only way an event is created. It stores the event (the source of truth), then calls `notify_nodes` |

## Adds

| Function | What it does |
|---|---|
| `notify_nodes(channel_id)` | After an event is committed, `PUBLISH huddle:events <channel_id>` to Redis. Fire and forget: at most 0.2 s, never raises, never blocks or fails the HTTP request when Redis is down |
| `redis_listener()` | One background thread per node: `SUBSCRIBE huddle:events`; each message wakes the event pump at once. On any error, wait 0.5 s and reconnect, forever |
| `event_pump()` | One background thread per node: reads stored events it has not delivered yet (by event row id, in order) and sends them to this node's subscribers. Runs when woken by `redis_listener`, and also at least every 100 ms on its own, so delivery never stops when Redis is down |

**Design (use exactly this; it is what makes D.2 and D.3 pass).** The shared database stays the source of
truth for events and their dense `event_id`s. Redis only wakes the other nodes up early. Because every
node also polls the database every 100 ms, a dead Redis slows delivery slightly but never loses, duplicates
or reorders an event. Speak Redis's protocol over a plain socket (`*1\r\n$4\r\nPING\r\n` → `+PONG`); no
package is needed.

**Supervising `start.sh`.** `/app/start.sh` stays in the foreground and supervises four children: Redis
and the three nodes. Each runs in its own restart loop, so a child that dies is started again within about
1 second without any outside help:

```sh
run_forever() { while true; do "$@"; sleep 0.5; done; }
run_forever redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle &
run_forever python3 /app/server.py 8000 &   # and the same for 8001 and 8002
wait
```

Create `/var/lib/huddle` and the schema once before starting the loops. Never delete or reset data on start
or restart, and keep every file the cluster writes outside `/app` (see the basics).

## Rules:

D1.1 **Redis is part of the cluster.** `127.0.0.1:6379` answers `PING` with `PONG`, and the process
  listening on port `6379` is `redis-server`, started by `/app/start.sh`.

D1.2 **Cross-node delivery.** With subscribers on all three nodes, one message posted through each node (3
  posts) reaches every subscriber as `message.created`, with consecutive `event_id`s, within 2 seconds.

D1.3 **Every node listens on Redis.** `PUBSUB NUMSUB huddle:events` on `127.0.0.1:6379` reports at least 3
  subscribers: each node's `redis_listener` holds its own subscription.

Every request must answer within 5 seconds.
