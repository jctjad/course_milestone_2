#!/usr/bin/env bash
# Self-check for C.1 (reactions). Prints PASS/FAIL per rule in /spec/component-C-staged/C-1-reactions.md.
# Usage: bash /spec/component-C-staged/checks/check_C_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Reactions rendered as sorted "emoji:count:id,id" entries joined by "|" (entries with count 0 are dropped)
RX_RESP='"|".join(sorted("%s:%s:%s" % (r["emoji"], r["count"], ",".join(sorted(str(u) for u in r["user_ids"]))) for r in d["reactions"] if r["count"] > 0))'
RX_MSG='"|".join(sorted("%s:%s:%s" % (r["emoji"], r["count"], ",".join(sorted(str(u) for u in r["user_ids"]))) for r in next(m for m in d["messages"] if str(m["id"])==a[0])["reactions"] if r["count"] > 0))'
# rx <entry>... -> the same rendering for expected entries;  ids <id>... -> sorted ids joined by ","
rx()  { printf '%s\n' "$@" | LC_ALL=C sort | paste -sd'|' -; }
ids() { printf '%s\n' "$@" | LC_ALL=C sort | paste -sd, -; }

# first_added <message-id> <emoji> <user-id> <frames-file> -> "yes" if the first reaction.added frame has the right fields
read -r -d '' FIRST_ADDED_PY <<'PY'
import json, sys
mid, emoji, uid, f = sys.argv[1:5]
try:
    x = [x for x in map(json.loads, open(f)) if x.get("type") == "reaction.added"][0]
    ok = str(x.get("message_id")) == mid and x.get("emoji") == emoji and str(x.get("user_id")) == uid and isinstance(x.get("reactions"), list)
    print("yes" if ok else "first reaction.added was " + json.dumps(x)[:300])
except Exception:
    print("no reaction.added frame")
PY

rules_C_1() {
  local all="C1.1 C1.2 C1.3 C1.4" E1="👍" E2="tada" ot oid mt mid w g pc pm m m2 a b c e f h l s1 s2 s3 s4 s5 w1 w2 w3 w4

  # setup: owner O posts a message in #general; member M is in the workspace
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN; oid=$USER_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  mt=$TOKEN; mid=$USER_ID
  post "$ot" $g "react to me" $N0; m=$MSG_ID
  [ "$ST" = 201 ] && [ -n "$m" ] || { setup_fail "$all" "could not post a message (got $ST), fix the basics first"; return; }

  # C1.1 add: 200 on first insert; same emoji from two users = one entry, count 2; idempotent; shown in history
  a=$(react "$ot" $m "$E1" $N0); s1=$(val "$RX_RESP"); w1=$(rx "$E1:1:$oid")
  b=$(react "$mt" $m "$E1" $N1); s2=$(val "$RX_RESP"); w2=$(rx "$E1:2:$(ids $oid $mid)")
  c=$(react "$ot" $m "$E1" $N2); s3=$(val "$RX_RESP")
  e=$(react "$mt" $m "$E2" $N0); s4=$(val "$RX_RESP"); w4=$(rx "$E1:2:$(ids $oid $mid)" "$E2:1:$mid")
  l=$(call GET $N2/api/channels/$g/messages "" "$mt"); s5=$(val "$RX_MSG" "$m")
  if [ "$a" = 200 ] && [ "$s1" = "$w1" ] && [ "$b" = 200 ] && [ "$s2" = "$w2" ] && [ "$c" = 200 ] && [ "$s3" = "$w2" ] \
     && [ "$e" = 200 ] && [ "$s4" = "$w4" ] && [ "$l" = 200 ] && [ "$s5" = "$w4" ]; then
    ok C1.1 "add reaction: 200 {reactions} on first insert; two users share one entry; repeat is idempotent; history shows it"
  else
    bad C1.1 "add reaction (emoji:count:user_ids): first $a '$s1' (want 200 '$w1'); second user $b '$s2' (want 200 '$w2'); repeat $c '$s3' (want 200, unchanged); other emoji $e '$s4' (want 200 '$w4'); history on 8002 $l '$s5' (want 200 '$w4')"
  fi

  # C1.2 remove: only the caller's; removing one you do not have changes nothing; last one removes the entry
  a=$(unreact "$mt" $m "$E1" $N1); s1=$(val "$RX_RESP"); w1=$(rx "$E1:1:$oid" "$E2:1:$mid")
  b=$(unreact "$mt" $m "$E1" $N2); s2=$(val "$RX_RESP")
  c=$(unreact "$ot" $m "$E1" $N0); s3=$(val "$RX_RESP"); w3=$(rx "$E2:1:$mid")
  l=$(call GET $N1/api/channels/$g/messages "" "$ot"); s5=$(val "$RX_MSG" "$m")
  if [ "$a" = 200 ] && [ "$s1" = "$w1" ] && [ "$b" = 200 ] && [ "$s2" = "$w1" ] && [ "$c" = 200 ] && [ "$s3" = "$w3" ] \
     && [ "$l" = 200 ] && [ "$s5" = "$w3" ]; then
    ok C1.2 "remove reaction: 200 {reactions}, only the caller's; removing again changes nothing; empty entry is gone"
  else
    bad C1.2 "remove reaction (emoji:count:user_ids): M removes $a '$s1' (want 200 '$w1'); M again $b '$s2' (want 200, unchanged); O removes the last $c '$s3' (want 200 '$w3'); history on 8001 $l '$s5' (want 200 '$w3')"
  fi

  # C1.3 errors: unknown message 404; missing or empty emoji 400; private channel non-member 403; no token 401
  new_channel "$ot" $w true; pc=$CHAN_ID
  post "$ot" "$pc" "private message" $N0; pm=$MSG_ID
  if [ -z "$pc" ] || [ -z "$pm" ]; then
    setup_fail "C1.3" "could not create a private channel and post in it (got $ST), fix the basics first"
  else
    a=$(react "$ot" 999999999 "$E1" $N0); b=$(unreact "$ot" 999999999 "$E1" $N1)
    c=$(call POST $N2/api/messages/$m/reactions '{}' "$ot"); e=$(react "$ot" $m "" $N0)
    f=$(react "$mt" $pm "$E1" $N1); h=$(unreact "$mt" $pm "$E1" $N2)
    s1=$(react "" $m "$E1" $N0); s2=$(unreact "" $m "$E2" $N1)
    if [ "$a" = 404 ] && [ "$b" = 404 ] && [ "$c" = 400 ] && [ "$e" = 400 ] && [ "$f" = 403 ] && [ "$h" = 403 ] && [ "$s1" = 401 ] && [ "$s2" = 401 ]; then
      ok C1.3 "reaction errors: unknown message 404; missing or empty emoji 400; private channel non-member 403; no token 401"
    else
      bad C1.3 "reaction errors: unknown message POST $a, DELETE $b (want 404 each); no emoji $c, empty emoji $e (want 400 each); non-member of a private channel POST $f, DELETE $h (want 403 each); no token POST $s1, DELETE $s2 (want 401 each)"
    fi
  fi

  # C1.4 events: changes emit reaction.added / reaction.removed with dense event_ids on every node; no-ops emit nothing
  post "$ot" $g "events for me" $N0; m2=$MSG_ID
  [ -n "$m2" ] || { setup_fail "C1.4" "could not post a message (got $ST), fix the basics first"; return; }
  listen_all "$mt" $g 3 6
  a="$(react "$ot" $m2 "$E1" $N0) $(react "$ot" $m2 "$E1" $N1) $(react "$mt" $m2 "$E1" $N2)"
  a="$a $(unreact "$mt" $m2 "$E1" $N0) $(unreact "$mt" $m2 "$E1" $N1)"
  wait "${LPIDS[@]}" 2>/dev/null
  b=$(verdict $g "reaction.added,reaction.added,reaction.removed")
  c=$(python3 -c "$FIRST_ADDED_PY" "$m2" "$E1" "$oid" ${WSF}0.jsonl)
  rm -f ${WSF}{0,1,2}.jsonl
  if [ "$a" = "200 200 200 200 200" ] && [ "$b" = yes ] && [ "$c" = yes ]; then
    ok C1.4 "reaction events: reaction.added / reaction.removed with the next event_ids on every node; no-op add/remove emit nothing"
  else
    bad C1.4 "reaction events: statuses '$a' (want 200 x5); add, repeat add, second user add, remove, repeat remove should give reaction.added, reaction.added, reaction.removed with event_id head+1..head+3 on every node: $b; first event has message_id, emoji, user_id, reactions: $c"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_C_1; finish; }
