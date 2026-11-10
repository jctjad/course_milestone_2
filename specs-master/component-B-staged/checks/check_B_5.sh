#!/usr/bin/env bash
# Self-check for B.5 (real-time events). Prints PASS/FAIL per rule in /spec/component-B-staged/B-5-events.md.
# Usage: bash /spec/component-B-staged/checks/check_B_5.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WSF=/tmp/check_B_ws
# listen_all <token> <channel> <count> <seconds> -> one listener per node in the background; waits until
# each has its "subscribed" frame (up to 3s). Sets LPIDS.
listen_all() {
  local i; LPIDS=()
  for i in 0 1 2; do
    rm -f $WSF$i.jsonl
    ws listen "ws://127.0.0.1:800$i/api/ws?token=$1" "$2" "$3" "$4" $WSF$i.jsonl & LPIDS+=($!)
  done
  for _ in $(seq 1 30); do
    grep -q '"subscribed"' ${WSF}{0,1,2}.jsonl 2>/dev/null && [ "$(grep -l '"subscribed"' ${WSF}{0,1,2}.jsonl 2>/dev/null | wc -l)" -eq 3 ] && return
    sleep 0.1
  done
}
# verdict <channel> <kinds,comma,separated> -> "yes" if every node saw subscribed(head) then exactly those
# kinds with event_id head+1, head+2, ... and the right channel_id; otherwise a short description
verdict() {
  python3 - "$1" "$2" ${WSF}{0,1,2}.jsonl <<'PY'
import json, sys
cid, kinds, files = sys.argv[1], sys.argv[2].split(","), sys.argv[3:]
seen, problems = [], []
for n, f in enumerate(files):
    try:
        frames = [json.loads(l) for l in open(f) if l.strip()]
    except Exception:
        frames = []
    sub = [x for x in frames if x.get("type") == "subscribed"]
    ev = [x for x in frames if x.get("type") not in ("subscribed", "resumed", "resume.gap", "error")]
    if not sub:
        problems.append(f"node {n}: no subscribed frame"); continue
    h = sub[0].get("head_event_id")
    got = [(x.get("type"), x.get("event_id"), str(x.get("channel_id"))) for x in ev]
    want = [(k, h + i + 1, cid) for i, k in enumerate(kinds)] if isinstance(h, int) else None
    seen.append([(t, e) for t, e, _ in got])
    if got != want:
        problems.append(f"node {n}: head {h}, got {[(t, e) for t, e, _ in got]}")
if not problems and any(s != seen[0] for s in seen):
    problems.append("nodes delivered different sequences")
print("yes" if not problems else "; ".join(problems)[:600])
PY
}

# SUMMARY_PY: reads ws.py frames from stdin, prints "type:event_id" per frame (head_event_id for resumed)
read -r -d '' SUMMARY_PY <<'PY'
import json, sys
fr = [json.loads(l) for l in sys.stdin if l.startswith("{")]
print(" ".join("%s:%s" % (f.get("type"), f.get("event_id", f.get("head_event_id"))) for f in fr))
PY

rules_B_5() {
  local all="B5.1 B5.2 B5.3 B5.4" ot mt jt g w ch h h1 h2 m1 s a b c e f k

  # setup: owner O with a channel ch that member M joined; J is a workspace member who will join and leave
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  mt=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  jt=$TOKEN
  new_channel "$ot" $w false; ch=$CHAN_ID
  [ -n "$ch" ] && [ "$(join "$mt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join a channel, fix B.1 first"; return; }

  # B5.1 connect: bad token rejected on every node; subscribe -> subscribed with the same head everywhere
  a="$(ws reject "$W0/api/ws?token=bogus-token-000") $(ws reject "$W1/api/ws?token=bogus-token-000") $(ws reject "$W2/api/ws?token=bogus-token-000")"
  h="$(head_of "$ot" $ch $W0) $(head_of "$mt" $ch $W1) $(head_of "$ot" $ch $W2)"
  if [ "$a" = "rejected rejected rejected" ] && [[ "$h" =~ ^([0-9]+)\ \1\ \1$ ]]; then
    ok B5.1 "websocket: bad token rejected on every node; subscribe answers subscribed with the same head_event_id"
  else
    bad B5.1 "websocket: bad token on 8000/8001/8002 '$a' (want rejected each); head_event_id on 8000/8001/8002 '$h' (want the same integer three times)"
  fi

  # B5.2 live events: a post and a reply reach subscribers on all three nodes
  listen_all "$ot" $ch 2 6
  post "$mt" $ch "live one" $N1; m1=$MSG_ID; s=$ST
  post "$ot" $ch "live reply" $N2 "$m1"; s="$s $ST"
  wait "${LPIDS[@]}" 2>/dev/null
  a=$(verdict $ch "message.created,message.reply")
  if [ "$s" = "201 201" ] && [ "$a" = yes ]; then
    ok B5.2 "live events: message.created and message.reply reach subscribers on all three nodes with the next event_ids"
  else
    bad B5.2 "live events: posts '$s' (want '201 201'); each node should get message.created then message.reply with event_id head+1, head+2: $a"
  fi

  # B5.3 dense ordering: 8 real changes across nodes = 8 consecutive events; no-op join/leave emit nothing
  listen_all "$mt" $ch 8 10
  post "$ot" $ch "dense one" $N0; s=$ST
  post "$mt" $ch "dense two" $N1; s="$s $ST"
  post "$ot" $ch "dense reply" $N2 "$MSG_ID"; s="$s $ST"
  s="$s $(join "$jt" $ch $N0) $(join "$jt" $ch $N1)"
  s="$s $(call POST $N1/api/channels/$ch/leave "" "$jt") $(call POST $N2/api/channels/$ch/leave "" "$jt")"
  s="$s $(call PATCH $N2/api/channels/$ch '{"topic":"dense topic"}' "$ot")"
  s="$s $(call POST $N0/api/channels/$ch/archive "" "$ot") $(call POST $N1/api/channels/$ch/unarchive "" "$ot")"
  wait "${LPIDS[@]}" 2>/dev/null
  a=$(verdict $ch "message.created,message.created,message.reply,member.joined,member.left,channel.updated,channel.archived,channel.unarchived")
  h1=$(python3 -c 'import json,sys
for l in open(sys.argv[1]):
    f=json.loads(l)
    if f.get("type")=="subscribed": print(f.get("head_event_id")); break' $WSF"0.jsonl" 2>/dev/null)
  if [ "$s" = "201 201 201 200 200 200 200 200 200 200" ] && [ "$a" = yes ]; then
    ok B5.3 "dense ordering: 8 changes across nodes give 8 consecutive event_ids, same on every node; no-op join/leave emit nothing"
  else
    bad B5.3 "dense: request statuses '$s' (want 201 x3 then 200 x7); expected created, created, reply, joined, left, updated, archived, unarchived with consecutive ids on every node (repeat join/leave must add nothing): $a"
  fi

  # B5.4 resume: replay after since_event_id on another node, then resumed; from head: only resumed
  if [[ "$h1" =~ ^[0-9]+$ ]]; then
    k=$((h1 + 8))
    a=$(ws resume "$W2/api/ws?token=$mt" $ch $((h1 + 2)) 4 | python3 -c "$SUMMARY_PY")
    b=$(ws resume "$W1/api/ws?token=$ot" $ch $k 4 | python3 -c "$SUMMARY_PY")
    e="message.reply:$((h1+3)) member.joined:$((h1+4)) member.left:$((h1+5)) channel.updated:$((h1+6)) channel.archived:$((h1+7)) channel.unarchived:$((h1+8)) resumed:$k"
    if [ "$a" = "$e" ] && [ "$b" = "resumed:$k" ]; then
      ok B5.4 "resume: replays every event after since_event_id in order on another node, then resumed; from the head only resumed"
    else
      bad B5.4 "resume from $((h1+2)) on 8002 gave '$a' (want '$e'); resume from head $k on 8001 gave '$b' (want 'resumed:$k')"
    fi
  else
    bad B5.4 "setup: no head_event_id from B5.3's subscription, fix B5.1 to B5.3 first"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_B_5; finish; }
