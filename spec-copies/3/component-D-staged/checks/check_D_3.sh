#!/usr/bin/env bash
# Self-check for D.3 (a killed Redis). Prints PASS/FAIL per rule in /spec/component-D-staged/D-3-redis-kill.md.
# Usage: bash /spec/component-D-staged/checks/check_D_3.sh      Exit 0 only if every rule passes.
# Run it on its own: it SIGKILLs the process on port 6379 (found by port, never by name).
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# fast_post <status:seconds>... -> true if every result is 201 and took under 2 s
fast_post() { local x; for x in "$@"; do [ "${x%%:*}" = 201 ] && awk -v t="${x#*:}" 'BEGIN{exit !(t<2)}' || return 1; done; }

rules_D_3() {
  local all="D3.1 D3.2 D3.3" ot mt g t0 t1 a b c w back sub s

  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  mt=$TOKEN
  [ "$(redis_ping)" = PONG ] && [ -n "$(pids_on_port 6379)" ] || { setup_fail "$all" "Redis is not answering on 6379, fix D1.1 first"; return; }

  listen_ports "$mt" $g 6 16 8000 8001 8002
  kill_port 6379; t0=$(date +%s%N); sleep 0.3

  # D3.1 writes through every node still succeed within 2 s while Redis is down
  a="$(timed_post "$ot" $g "no redis one" $N0) $(timed_post "$mt" $g "no redis two" $N1) $(timed_post "$ot" $g "no redis three" $N2)"
  if fast_post $a; then
    ok D3.1 "Redis killed: a post through each node still returns 201 within 2 s"
  else
    bad D3.1 "Redis killed: posts on 8000/8001/8002 gave '$a' (status:seconds; want 201 under 2 s each)"
  fi

  # D3.3 (first half) Redis comes back on its own within the respawn limit; then 3 more posts
  if wait_until_ms $t0 $RESPAWN_S redis_ok; then back="$(ms_since $t0) ms"; else back=no; fi
  t1=$(date +%s%N)
  if [ "$back" != no ] && wait_until_ms $t1 $RESPAWN_S numsub_ok; then sub="$(ms_since $t1) ms"; else sub=no; fi
  sleep 0.3
  post "$ot" $g "redis back one" $N0; s=$ST
  post "$mt" $g "redis back two" $N1; s="$s $ST"
  post "$ot" $g "redis back three" $N2; s="$s $ST"
  wait "${LPIDS[@]}" 2>/dev/null

  # D3.2 subscribers on all three nodes got the outage posts (the first 3 events) with consecutive event_ids
  b=$(verdict_ports $g "message.created,message.created,message.created" prefix)
  if [ "$b" = yes ]; then
    ok D3.2 "Redis killed: subscribers on all three nodes still get the outage posts with consecutive event_ids"
  else
    bad D3.2 "Redis killed: subscribers on every node should get the 3 outage posts as message.created head+1..head+3: $b"
  fi

  # D3.3 Redis back, and the whole sequence (outage + after) is dense on every node
  c=$(verdict_ports $g "message.created,message.created,message.created,message.created,message.created,message.created")
  rm -f ${WSF}800{0,1,2}.jsonl
  if [ "$back" != no ] && [ "$sub" != no ] && [ "$s" = "201 201 201" ] && [ "$c" = yes ]; then
    ok D3.3 "Redis came back on its own after $back, nodes re-subscribed after $sub; later posts reach every node; event_ids consecutive across the outage"
  else
    bad D3.3 "Redis respawn: PONG within ${RESPAWN_S}s: $back (want it back with no outside action); 3+ subscribers again within ${RESPAWN_S}s after that: $sub (now $(redis_numsub)); posts after '$s' (want 201 x3); all 6 events on every node with consecutive event_ids: $c"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_D_3; finish; }
