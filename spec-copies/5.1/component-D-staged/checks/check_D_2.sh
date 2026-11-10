#!/usr/bin/env bash
# Self-check for D.2 (a killed HTTP node). Prints PASS/FAIL per rule in /spec/component-D-staged/D-2-node-kill.md.
# Usage: bash /spec/component-D-staged/checks/check_D_2.sh      Exit 0 only if every rule passes.
# Run it on its own: it SIGKILLs the process on port 8001 (found by port, never by name).
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# timed <method> <url> [token] -> prints "status:seconds" (max 2 s)
timed() {
  local r args=(-s -o /dev/null -w '%{http_code}:%{time_total}' --max-time 2 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H "Authorization: Bearer $3")
  r=$(curl "${args[@]}" 2>/dev/null || true)
  echo "${r:-000:2}"
}
# fast <status:seconds>... -> true if every result has a 2xx status and took under 2 s
fast() { local x; for x in "$@"; do [[ "${x%%:*}" =~ ^2 ]] && awk -v t="${x#*:}" 'BEGIN{exit !(t<2)}' || return 1; done; }

rules_D_2() {
  local all="D2.1 D2.2 D2.3 D2.4 D2.5" ot mt g h0 t0 a b c e f r1 r2 w1 w2 w3 back nid s1 s2

  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  mt=$TOKEN
  post "$ot" $g "before the kill" $N0
  h0=$(head_of "$mt" $g $W2)
  [[ "$h0" =~ ^[0-9]+$ ]] || { setup_fail "$all" "could not read head_event_id over WebSocket (got '$h0'), fix the basics first"; return; }
  [ -n "$(pids_on_port 8001)" ] || { setup_fail "$all" "nothing is listening on port 8001"; return; }

  listen_ports "$mt" $g 3 8 8000 8002
  kill_port 8001; t0=$(date +%s%N); sleep 0.3

  # D2.1 8000 and 8002 keep answering health, reads and writes, each within 2 s
  a="$(timed GET $N0/api/health) $(timed GET $N2/api/health)"
  r1=$(timed GET $N0/api/channels/$g/messages "$mt"); r2=$(timed GET $N2/api/channels/$g/messages "$mt")
  w1=$(timed_post "$ot" $g "outage one" $N0); w2=$(timed_post "$mt" $g "outage two" $N2); w3=$(timed_post "$ot" $g "outage three" $N0)
  if fast $a $r1 $r2 $w1 $w2 $w3; then
    ok D2.1 "8001 killed: 8000 and 8002 still answer health, reads and writes within 2 s"
  else
    bad D2.1 "8001 killed (status:seconds): health on 8000/8002 '$a', reads '$r1' '$r2' (want 200 under 2 s), writes '$w1' '$w2' '$w3' (want 201 under 2 s)"
  fi

  # D2.2 subscribers on 8000 and 8002 receive the outage posts with consecutive event_ids
  wait "${LPIDS[@]}" 2>/dev/null
  b=$(verdict_ports $g "message.created,message.created,message.created")
  rm -f ${WSF}800{0,2}.jsonl
  if [ "$b" = yes ]; then
    ok D2.2 "live delivery during the outage: subscribers on 8000 and 8002 get every post with consecutive event_ids"
  else
    bad D2.2 "live delivery during the outage: subscribers on 8000 and 8002 should get 3 message.created with event_id head+1..head+3: $b"
  fi

  # D2.3 replay: resume on 8002 from the head seen before the kill
  c=$(ws resume "$W2/api/ws?token=$mt" $g $h0 4 | python3 -c '
import json, sys
h0 = int(sys.argv[1]); fr = [json.loads(l) for l in sys.stdin if l.strip().startswith("{")]
ev = [(f.get("type"), f.get("event_id")) for f in fr]
want = [("message.created", h0 + i) for i in (1, 2, 3)] + [("resumed", None)]
ok = [(t, e) for t, e in ev if t != "resumed"] == want[:3] and ev[-1:] == [("resumed", None)] and fr[-1].get("head_event_id") == h0 + 3
print("yes" if ok else "got %s" % [(t, e if t != "resumed" else fr[-1].get("head_event_id")) for t, e in ev][:8])' "$h0")
  if [ "$c" = yes ]; then
    ok D2.3 "replay across a reconnect: resume on 8002 replays the outage events in order, then resumed with the head"
  else
    bad D2.3 "replay: resume on 8002 since $h0 should give message.created $((h0+1)), $((h0+2)), $((h0+3)) then resumed with head_event_id $((h0+3)): $c"
  fi

  # D2.4 8001 comes back within the respawn limit, with the outage data, and takes writes again
  if wait_until_ms $t0 $RESPAWN_S health_ok $N1; then back="$(ms_since $t0) ms"; else back=no; fi
  if [ "$back" != no ]; then
    call GET $N1/api/health >/dev/null; nid=$(val 'd["node_id"]')
    e=$(call GET $N1/api/channels/$g/messages "" "$mt"); s1=$(has 'all(any(m["body"]==b for m in d["messages"]) for b in ("outage one","outage two","outage three"))')
    post "$mt" $g "after the respawn" $N1; f=$ST
    call GET $N2/api/channels/$g/messages "" "$mt" >/dev/null; s2=$(has 'any(m["body"]=="after the respawn" for m in d["messages"])')
  else
    nid=-; e=-; s1=-; f=-; s2=-
    restart_cluster || true     # leave a working cluster for the next check
  fi
  if [ "$back" != no ] && [ "$nid" = 1 ] && [ "$e" = 200 ] && [ "$s1" = yes ] && [ "$f" = 201 ] && [ "$s2" = yes ]; then
    ok D2.4 "8001 came back on its own after $back, with node_id 1 and the outage data, and takes writes again"
  else
    bad D2.4 "respawn: 8001 back within ${RESPAWN_S}s: $back (want it back with no outside action); node_id '$nid' (want 1); reads on 8001 $e with the outage messages: $s1; post on 8001 $f (want 201), seen on 8002: $s2"
  fi

  # D2.5 node 0 is not special: kill 8000; 8001 and 8002 keep serving; 8000 comes back with node_id 0
  [ -n "$(pids_on_port 8000)" ] || { bad D2.5 "setup: nothing is listening on port 8000"; return; }
  kill_port 8000; t0=$(date +%s%N); sleep 0.3
  a="$(timed GET $N1/api/health) $(timed GET $N2/api/health)"
  r1=$(timed GET $N1/api/channels/$g/messages "$mt"); r2=$(timed GET $N2/api/channels/$g/messages "$mt")
  w1=$(timed_post "$ot" $g "node zero down" $N1); w2=$(timed_post "$mt" $g "node zero still down" $N2)
  if wait_until_ms $t0 $RESPAWN_S health_ok $N0; then back="$(ms_since $t0) ms"; else back=no; fi
  if [ "$back" != no ]; then
    call GET $N0/api/health >/dev/null; nid=$(val 'd["node_id"]')
    post "$mt" $g "node zero is back" $N0; f=$ST
  else
    nid=-; f=-
    restart_cluster || true     # leave a working cluster for the next check
  fi
  if fast $a $r1 $r2 $w1 $w2 && [ "$back" != no ] && [ "$nid" = 0 ] && [ "$f" = 201 ]; then
    ok D2.5 "8000 killed: 8001 and 8002 kept serving; 8000 came back on its own after $back with node_id 0 and takes writes"
  else
    bad D2.5 "8000 killed (status:seconds): health on 8001/8002 '$a', reads '$r1' '$r2' (want 200 under 2 s), writes '$w1' '$w2' (want 201 under 2 s); 8000 back within ${RESPAWN_S}s: $back; node_id '$nid' (want 0); post on 8000 after $f (want 201)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_D_2; finish; }
