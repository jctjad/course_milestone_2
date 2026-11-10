#!/usr/bin/env bash
# Self-check for B.3 (search and pins). Prints PASS/FAIL per rule in /spec/component-B-staged/B-3-search-pins.md.
# Usage: bash /spec/component-B-staged/checks/check_B_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

RIDS='",".join(str(m["id"]) for m in d["results"])'
PIDS='",".join(str(x["message"]["id"]) for x in d["pins"])'

rules_B_3() {
  local all="B3.1 B3.2 B3.3" ot mid mt at xt wid w g w2id g2 ch p tok s1 s2 s3 s4 m1 m2 pm cur ids s a b c e f h i j k

  # setup: owner O (workspace w), M member, AD admin with private channel p; X owns another workspace w2
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; wid=$WS_ID; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  mt=$TOKEN; mid=$USER_ID
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] || { setup_fail "$all" "could not add an admin, fix A.2 first"; return; }
  at=$TOKEN
  new_user && xt=$TOKEN && new_ws "$xt" || { setup_fail "$all" "could not create a second workspace"; return; }
  w2id=$WS_ID; g2=$GEN_ID
  new_channel "$ot" $w false; ch=$CHAN_ID
  new_channel "$at" $w true; p=$CHAN_ID
  [ -n "$ch" ] && [ -n "$p" ] && [ "$(join "$mt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join channels, fix B.1 first"; return; }

  # B3.1 search: newest first, case-insensitive, only readable channels, workspace filter, paging
  tok="zq$RANDOM$RANDOM"
  post "$ot" $ch "first $tok here" $N0; s1=$MSG_ID
  post "$mt" $ch "second $tok here" $N1; s2=$MSG_ID
  post "$at" $p "secret $tok here" $N2; s3=$MSG_ID
  post "$xt" $g2 "elsewhere $tok here" $N0; s4=$MSG_ID
  post "$ot" $ch "unrelated words" $N1
  a=$(call GET "$N0/api/search?q=$tok" "" "$mt"); ids=$(val "$RIDS")
  b=$(call GET "$N1/api/search?q=$(echo "$tok" | tr a-z A-Z)" "" "$mt"); i=$(val "$RIDS")
  c=$(call GET "$N2/api/search?q=$tok&workspace_id=$wid" "" "$at"); j=$(val "$RIDS")
  join "$xt" $g >/dev/null
  e=$(call GET "$N0/api/search?q=$tok&workspace_id=$w2id" "" "$xt"); k=$(val "$RIDS")
  f=$(call GET "$N1/api/search?q=$tok&limit=1" "" "$mt"); s=$(val "$RIDS"); cur=$(field next_cursor)
  h=$(call GET "$N2/api/search?q=$tok&limit=1&cursor=$(enc "$cur")" "" "$mt"); s="$s|$(val "$RIDS")"
  if [ "$a$b$c$e$f$h" = 200200200200200200 ] && [ "$ids" = "$s2,$s1" ] && [ "$i" = "$s2,$s1" ] && [ "$j" = "$s3,$s2,$s1" ] \
     && [ "$k" = "$s4" ] && [ "$s" = "$s2|$s1" ] && [ "$(call GET "$N0/api/search?q=$tok")" = 401 ]; then
    ok B3.1 "search: newest first, case-insensitive, only readable channels, workspace_id filter, paged; no token 401"
  else
    bad B3.1 "search: member got '$ids', uppercase query '$i' (want '$s2,$s1': not the private or other-workspace ones); private channel's member with workspace_id '$j' (want '$s3,$s2,$s1'); other workspace filter '$k' (want '$s4'); limit=1 pages '$s' (want '$s2|$s1'); statuses $a/$b/$c/$e/$f/$h; also no token should be 401"
  fi

  # B3.2 pins: pin returns {pin}, idempotent; list newest pin first, paged; unpin
  post "$ot" $ch "pin me one" $N0; m1=$MSG_ID
  post "$ot" $ch "pin me two" $N0; m2=$MSG_ID
  a=$(call POST $N1/api/messages/$m1/pin "" "$mt")
  i=$(has '(lambda x: str(x["message_id"])==a[0] and str(x["pinned_by"])==a[1] and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)", x["pinned_at"]) is not None)(d["pin"])' "$m1" "$mid")
  b=$(call POST $N2/api/messages/$m1/pin "" "$mt")
  sleep 1
  c=$(call POST $N0/api/messages/$m2/pin "" "$ot")
  e=$(call GET "$N1/api/channels/$ch/pins" "" "$mt"); ids=$(val "$PIDS"); j=$(has 'all(k in x for x in d["pins"] for k in ("message","pinned_by","pinned_at")) and d["next_cursor"] is None')
  call GET "$N2/api/channels/$ch/pins?limit=1" "" "$mt" >/dev/null; s=$(val "$PIDS"); cur=$(field next_cursor)
  call GET "$N0/api/channels/$ch/pins?limit=1&cursor=$(enc "$cur")" "" "$mt" >/dev/null; s="$s|$(val "$PIDS")"; k=$(field next_cursor)
  f=$(call DELETE $N1/api/messages/$m1/pin "" "$mt"); h=$(field unpinned)
  call GET "$N2/api/channels/$ch/pins" "" "$mt" >/dev/null; cur=$(val "$PIDS")
  if [ "$a" = 200 ] && [ "$i" = yes ] && [ "$b" = 200 ] && [ "$c" = 200 ] && [ "$e" = 200 ] && [ "$ids" = "$m2,$m1" ] && [ "$j" = yes ] \
     && [ "$s" = "$m2|$m1" ] && [ "$k" = null ] && [ "$f" = 200 ] && [ "$h" = true ] && [ "$cur" = "$m2" ]; then
    ok B3.2 "pins: 200 {pin}, idempotent; list newest pin first and paged; unpin 200 {unpinned:true} removes it"
  else
    bad B3.2 "pins: pin $a (fields right: $i), again $b, second pin $c (want 200); list $e gave '$ids' (want '$m2,$m1' once each; pin fields and null next_cursor: $j); limit=1 pages '$s' (want '$m2|$m1', last cursor '$k' want null); unpin $f with unpinned '$h' (want 200/true); list after '$cur' (want '$m2')"
  fi

  # B3.3 pin access: private non-member 403 on list and pin; unknown message 404; no token 401
  post "$at" $p "private pin" $N0; pm=$MSG_ID
  a=$(call GET "$N0/api/channels/$p/pins" "" "$mt")
  b=$(call POST $N1/api/messages/$pm/pin "" "$mt")
  c=$(call POST $N2/api/messages/999999999/pin "" "$mt")
  e=$(call GET "$N0/api/channels/$ch/pins")
  f=$(call GET "$N0/api/channels/$p/pins" "" "$at")
  if [ "$a" = 403 ] && [ "$b" = 403 ] && [ "$c" = 404 ] && [ "$e" = 401 ] && [ "$f" = 200 ]; then
    ok B3.3 "pin access: private non-member 403 on list and pin; unknown message 404; no token 401"
  else
    bad B3.3 "pin access: non-member list $a, pin $b (want 403); unknown message $c (want 404); no token $e (want 401); the private channel's member lists pins $f (want 200)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_B_3; finish; }
