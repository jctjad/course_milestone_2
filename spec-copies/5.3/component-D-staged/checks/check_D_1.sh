#!/usr/bin/env bash
# Self-check for D.1 (Redis fan-out, supervising start.sh). Prints PASS/FAIL per rule in /spec/component-D-staged/D-1-redis.md.
# Usage: bash /spec/component-D-staged/checks/check_D_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_D_1() {
  local all="D1.1 D1.2 D1.3" ot g a b c own s n

  # D1.1 Redis: 6379 answers PING, and the listener on 6379 is redis-server
  a=$(redis_ping); own=$(port_owner 6379)
  if [ "$a" = PONG ] && [[ "$own" == redis-server* ]]; then
    ok D1.1 "Redis: 127.0.0.1:6379 answers PONG and is a redis-server process"
  else
    bad D1.1 "Redis: PING on 127.0.0.1:6379 gave '$a' (want PONG); process on port 6379 is '${own:-none}' (want redis-server, started by /app/start.sh)"
  fi

  # D1.2 cross-node delivery: one post through each node reaches subscribers on all three nodes within 2 s
  new_user || { setup_fail "D1.2" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "D1.2" "could not create a workspace, fix the basics first"; return; }
  g=$GEN_ID
  listen_ports "$ot" $g 3 3 8000 8001 8002
  post "$ot" $g "fan one" $N0; s=$ST
  post "$ot" $g "fan two" $N1; s="$s $ST"
  post "$ot" $g "fan three" $N2; s="$s $ST"
  wait "${LPIDS[@]}" 2>/dev/null
  b=$(verdict_ports $g "message.created,message.created,message.created")
  rm -f ${WSF}800{0,1,2}.jsonl
  if [ "$s" = "201 201 201" ] && [ "$b" = yes ]; then
    ok D1.2 "cross-node delivery: a post through each node reaches subscribers on all three nodes with consecutive event_ids"
  else
    bad D1.2 "cross-node delivery: posts on 8000/8001/8002 '$s' (want 201 x3); every node should get 3 message.created with event_id head+1..head+3 within 2 s: $b"
  fi

  # D1.3 every node holds its own Redis subscription
  n=$(redis_numsub)
  if [ "$n" -ge 3 ]; then
    ok D1.3 "Redis: PUBSUB NUMSUB huddle:events reports $n subscribers (one per node or more)"
  else
    bad D1.3 "Redis: PUBSUB NUMSUB huddle:events reports '$n' subscribers (want at least 3: each node's redis_listener subscribes; -1 means Redis is unreachable)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_D_1; finish; }
