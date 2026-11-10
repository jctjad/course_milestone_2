#!/usr/bin/env bash
# Self-check for C.4 (groups, @group-handle, @here). Prints PASS/FAIL per rule in
# /spec/component-C-staged/C-4-groups-mentions.md.
# Usage: bash /spec/component-C-staged/checks/check_C_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# mentions of the last posted message equal exactly the given ids (no duplicates, any order)
SAME='sorted(str(x) for x in d["message"]["mentions"])==sorted(a)'
GOT='",".join(sorted(str(x) for x in d["message"]["mentions"]))'
GMEM='sorted(str(x) for x in d["group"]["member_user_ids"])==sorted(a) and "members" not in d["group"]'

rules_C_4() {
  local all="C4.1 C4.2 C4.3 C4.4 C4.5" ot oid w g at aid bt bid bn ct cid xt ch h1 h2 G a b c e f h l s1 s2 s3 s4 s5 pa po

  # setup: owner O; A and B are in the workspace and channel ch; C is in the workspace only; X is outside
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN; oid=$USER_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  at=$TOKEN; aid=$USER_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  bt=$TOKEN; bid=$USER_ID; bn=$USER
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  ct=$TOKEN; cid=$USER_ID
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  xt=$TOKEN
  new_channel "$ot" $w false; ch=$CHAN_ID
  [ -n "$ch" ] && [ "$(join "$at" $ch)" = 200 ] && [ "$(join "$bt" $ch)" = 200 ] \
    || { setup_fail "$all" "could not create and join a channel, fix the basics first"; return; }
  G=$N0/api/workspaces/$w/groups

  # C4.1 create: any member -> 201 {group} with member_user_ids; duplicate 409; missing handle 400; outsider 403
  h1="eng$RANDOM"
  a=$(call POST $G "{\"handle\":\"$h1\",\"name\":\"Eng\",\"user_ids\":$(ids_json $aid $bid $cid)}" "$at")
  s1=$(has "$GMEM"' and d["group"]["name"]=="Eng" and d["group"]["id"] not in (None, "")' "$aid" "$bid" "$cid")
  [ "$(field group.handle)" = "$h1" ] || s1="no (handle '$(field group.handle)', want '$h1')"
  b=$(call POST $N1/api/workspaces/$w/groups "{\"handle\":\"$h1\",\"name\":\"Again\",\"user_ids\":[]}" "$bt")
  c=$(call POST $N2/api/workspaces/$w/groups '{"name":"No Handle","user_ids":[]}' "$bt")
  e=$(call POST $G "{\"handle\":\"x$RANDOM\",\"name\":\"Outsider\",\"user_ids\":[]}" "$xt")
  f="$(call POST $G '{"handle":"Bad Handle","name":"Bad","user_ids":[]}' "$at") $(call POST $G '{"handle":"@eng","name":"Bad","user_ids":[]}' "$at") $(call POST $G '{"handle":"here","name":"Bad","user_ids":[]}' "$at")"
  h=$(call POST $G "{\"handle\":\"y$RANDOM\",\"name\":\"No Token\",\"user_ids\":[]}")
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 409 ] && [ "$c" = 400 ] && [ "$e" = 403 ] && [ "$f" = "400 400 400" ] && [ "$h" = 401 ]; then
    ok C4.1 "create group: 201 {group} with handle, name, member_user_ids (no members key); duplicate 409; missing or invalid handle 400; outsider 403; no token 401"
  else
    bad C4.1 "create group: got $a (GroupObj right, member_user_ids = sent user_ids, no members key: $s1, want 201 yes); duplicate handle $b (want 409); missing handle $c (want 400); handles 'Bad Handle', '@eng', 'here' gave '$f' (want 400 each); non-member $e (want 403); no token $h (want 401)"
  fi

  # C4.2 list: 200 {groups} on any node for members; outsider 403
  a=$(call GET $N1/api/workspaces/$w/groups "" "$ct")
  s1=$(has 'any(x["handle"]==a[0] and sorted(str(i) for i in x["member_user_ids"])==sorted(a[1:]) for x in d["groups"])' "$h1" "$aid" "$bid" "$cid")
  b=$(call GET $N2/api/workspaces/$w/groups "" "$xt")
  c=$(call GET $N0/api/workspaces/$w/groups)
  if [ "$a" = 200 ] && [ "$s1" = yes ] && [ "$b" = 403 ] && [ "$c" = 401 ]; then
    ok C4.2 "list groups: 200 {groups} with member_user_ids on another node; outsider 403; no token 401"
  else
    bad C4.2 "list groups: member on 8001 $a (lists the group with its members: $s1, want 200 yes); non-member $b (want 403); no token $c (want 401)"
  fi

  # C4.3 update/delete: creator or owner only; PATCH unknown 404; DELETE 200 {"deleted":true} twice; gone from list
  a=$(call PATCH $N0/api/workspaces/$w/groups/$h1 '{"name":"Hijack"}' "$bt")
  b=$(call PATCH $N2/api/workspaces/$w/groups/$h1 '{"name":"Eng Two"}' "$ot"); s1=$(has 'd["group"]["name"]=="Eng Two"')
  c=$(call PATCH $N1/api/workspaces/$w/groups/$h1 "{\"user_ids\":$(ids_json $aid $bid)}" "$at"); s2=$(has "$GMEM" "$aid" "$bid")
  e=$(call PATCH $N0/api/workspaces/$w/groups/nosuch$RANDOM '{"name":"Nope"}' "$ot")
  f=$(call DELETE $N1/api/workspaces/$w/groups/$h1 "" "$bt")
  h=$(call DELETE $N2/api/workspaces/$w/groups/$h1 "" "$at"); s3=$(has 'd.get("deleted") is True')
  l=$(call DELETE $N0/api/workspaces/$w/groups/$h1 "" "$at"); s4=$(has 'd.get("deleted") is True')
  call GET $N1/api/workspaces/$w/groups "" "$at" >/dev/null; s5=$(has 'not any(x["handle"]==a[0] for x in d["groups"])' "$h1")
  if [ "$a" = 403 ] && [ "$b" = 200 ] && [ "$s1" = yes ] && [ "$c" = 200 ] && [ "$s2" = yes ] && [ "$e" = 404 ] \
     && [ "$f" = 403 ] && [ "$h" = 200 ] && [ "$s3" = yes ] && [ "$l" = 200 ] && [ "$s4" = yes ] && [ "$s5" = yes ]; then
    ok C4.3 "update/delete group: creator or owner only; PATCH unknown 404; DELETE 200 deleted:true, again 200; gone from list"
  else
    bad C4.3 "update/delete: other member PATCH $a (want 403); owner PATCH name $b ($s1); creator PATCH user_ids $c ($s2) (want 200 yes each); unknown handle $e (want 404); other member DELETE $f (want 403); creator DELETE $h ($s3), again $l ($s4) (want 200 deleted:true each); gone from list: $s5"
  fi

  # C4.4 @group: group members who are also in the channel, minus the author; deduped with @username
  h2="team-$RANDOM"
  a=$(call POST $G "{\"handle\":\"$h2\",\"name\":\"Team\",\"user_ids\":$(ids_json $aid $bid $cid $oid)}" "$ot")
  post "$ot" $ch "@$h2 standup now" $N0; b=$ST; s1=$(has "$SAME" "$aid" "$bid"); e=$(val "$GOT")
  post "$at" $ch "@$h2 and @$bn again" $N1; c=$ST; s2=$(has "$SAME" "$oid" "$bid"); f=$(val "$GOT")
  if [ "$a" = 201 ] && [ "$b" = 201 ] && [ "$s1" = yes ] && [ "$c" = 201 ] && [ "$s2" = yes ]; then
    ok C4.4 "@group: group members in the channel minus the author; deduplicated with @username"
  else
    bad C4.4 "@group (group = A, B, C, O; channel = O, A, B): create $a (want 201); O posts '@group' $b gave [$e] (want [$aid,$bid]: C is not in the channel, O is the author); A posts '@group @B' $c gave [$f] (want [$oid,$bid] once each)"
  fi

  # C4.5 @here: non-author channel members with an open WebSocket (any node); closed sockets drop out
  if watch_ws "$at" $W1 8 $TMPC.wsa; then pa=$WPID; else pa=""; fi
  if watch_ws "$ot" $W2 8 $TMPC.wso; then po=$WPID; else po=""; fi
  sleep 1
  post "$ot" $ch "@here look" $N0; a=$ST; s1=$(has "$SAME" "$aid"); e=$(val "$GOT")
  [ -n "$pa" ] && { kill "$pa" 2>/dev/null; wait "$pa" 2>/dev/null; }; sleep 2
  post "$ot" $ch "@here again" $N1; b=$ST; s2=$(has "$SAME"); f=$(val "$GOT")
  [ -n "$po" ] && kill "$po" 2>/dev/null; wait $pa $po 2>/dev/null; rm -f $TMPC.wsa $TMPC.wso
  if [ -n "$pa" ] && [ -n "$po" ] && [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 201 ] && [ "$s2" = yes ]; then
    ok C4.5 "@here: only non-author channel members with an open WebSocket on any node; a closed socket drops out"
  else
    bad C4.5 "@here (A connected on 8001, O the author on 8002, B not connected): WebSockets opened: A '${pa:+yes}', O '${po:+yes}'; '@here' $a gave [$e] (want [$aid]); after A disconnects, '@here' $b gave [$f] (want [])"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_C_4; finish; }
