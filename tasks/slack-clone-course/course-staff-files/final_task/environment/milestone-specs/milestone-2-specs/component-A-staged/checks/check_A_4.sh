#!/usr/bin/env bash
# Self-check for A.4 (join mode). Prints PASS/FAIL per rule in /spec/component-A-staged/A-4-join_mode.md.
# Usage: bash /spec/component-A-staged/checks/check_A_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_A_4() {
  local all="A4.1 A4.2 A4.3" ot w g at mt gt w2 g2 pub priv w3 g3 pub3 mt3 xt xid yid nm nm2 s a b c d e f h i j r1 r2 n1 j1 n2 j2

  # setup from A.1-A.2: owner O with a workspace; A admin, M member, G guest there
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] \
    || { setup_fail "$all" "could not add an admin, fix A1.7 and A.2 first"; return; }
  at=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A1.7 first"; return; }
  mt=$TOKEN
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID guest)" = 200 ] \
    || { setup_fail "$all" "could not add a guest, fix A.2 first"; return; }
  gt=$TOKEN
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  xt=$TOKEN

  # A4.1 PATCH workspace: admin/owner only; any subset of {name, join_mode}; 200 {workspace};
  #     unknown join_mode 400 (nothing changes); member, guest, outsider 403; unknown workspace 404; no token 401
  nm="Renamed $RANDOM"; nm2="Both $RANDOM"
  a=$(call PATCH $N0/api/workspaces/$w "{\"name\":\"$nm\"}" "$ot"); n1=$(field workspace.name); j1=$(field workspace.join_mode)
  b=$(call PATCH $N1/api/workspaces/$w '{"join_mode":"invite_only"}' "$at"); n2=$(field workspace.name); j2=$(field workspace.join_mode)
  c=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}' "$mt")
  e=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}' "$gt")
  f=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}' "$xt")
  s=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}')
  i=$(call PATCH $N0/api/workspaces/$w '{"join_mode":"closed"}' "$ot")
  j=$(call PATCH $N0/api/workspaces/no-such-ws-$RANDOM '{"name":"Nope"}' "$ot")
  d=$(call GET $N2/api/workspaces "" "$ot"); h=$(has 'any(str(x["slug"])==a[0] and x["join_mode"]=="invite_only" and x["name"]==a[1] for x in d["workspaces"])' "$w" "$nm")
  r1=$(call PATCH $N1/api/workspaces/$w "{\"name\":\"$nm2\",\"join_mode\":\"open\"}" "$ot")
  r2="$(field workspace.name)|$(field workspace.join_mode)"
  if [ "$a" = 200 ] && [ "$n1" = "$nm" ] && [ "$j1" = open ] && [ "$b" = 200 ] && [ "$j2" = invite_only ] && [ "$n2" = "$nm" ] \
     && [ "$c" = 403 ] && [ "$e" = 403 ] && [ "$f" = 403 ] && [ "$s" = 401 ] && [ "$i" = 400 ] && [ "$j" = 404 ] && [ "$h" = yes ] \
     && [ "$r1" = 200 ] && [ "$r2" = "$nm2|open" ]; then
    ok A4.1 "PATCH workspace: owner/admin change name and join_mode; bad join_mode 400; others 403; unknown 404; no token 401"
  else
    bad A4.1 "PATCH: owner name $a ('$n1', join_mode '$j1'), admin join_mode $b ('$j2', name '$n2'), both fields $r1 ('$r2') (want 200 each); member $c, guest $e, outsider $f (want 403); no token $s (want 401); join_mode closed $i (want 400, unchanged on 8002: $h); unknown workspace $j (want 404)"
  fi

  # A4.2 open workspace: any user joining a public channel gets 200 and becomes a member;
  #     a private channel is still 403 and does not make them a member
  new_ws "$ot" || { setup_fail "A4.2 A4.3" "could not create a workspace, fix A.1 first"; return; }
  w2=$SLUG; g2=$GEN_ID
  new_channel "$ot" $w2 false; pub=$CHAN_ID
  new_channel "$ot" $w2 true; priv=$CHAN_ID
  new_user; xid=$USER_ID; a=$(call POST $N1/api/channels/$g2/join "" "$TOKEN"); r1=$(role_of "$ot" $w2 $xid $N2)
  new_user; yid=$USER_ID; b=$(call POST $N2/api/channels/$pub/join "" "$TOKEN"); r2=$(role_of "$ot" $w2 $yid $N0)
  new_user; xid=$USER_ID; c=$(call POST $N0/api/channels/$priv/join "" "$TOKEN"); d=$(role_of "$ot" $w2 $xid $N1)
  if [ "$a" = 200 ] && [ "$r1" = member ] && [ -n "$pub" ] && [ "$b" = 200 ] && [ "$r2" = member ] && [ -n "$priv" ] && [ "$c" = 403 ] && [ -z "$d" ]; then
    ok A4.2 "open workspace: outsiders join public channels with 200 and become members; private channel 403"
  else
    bad A4.2 "open join: #general $a (role '$r1'), other public channel '$pub' $b (role '$r2') (want 200 and member each); private channel '$priv' $c (want 403, role '$d' should be empty)"
  fi

  # A4.3 invite_only workspace: outsiders get 403 on every public channel until they accept an invitation;
  #     existing members still join; switching back to open lets outsiders in again
  new_ws "$ot" || { setup_fail "A4.3" "could not create a workspace, fix A.1 first"; return; }
  w3=$SLUG; g3=$GEN_ID
  new_channel "$ot" $w3 false; pub3=$CHAN_ID
  new_member "$g3" || { setup_fail "A4.3" "could not add a member, fix A1.7 first"; return; }
  mt3=$TOKEN
  s=$(call PATCH $N0/api/workspaces/$w3 '{"join_mode":"invite_only"}' "$ot")
  [ "$s" = 200 ] || { bad A4.3 "setup: owner could not set join_mode to invite_only (got $s), fix A4.1 first"; return; }
  new_user; xid=$USER_ID; xt=$TOKEN
  a=$(call POST $N1/api/channels/$g3/join "" "$xt"); e=$(call POST $N2/api/channels/$pub3/join "" "$xt"); r1=$(role_of "$ot" $w3 $xid $N2)
  f=$(call POST $N0/api/channels/$pub3/join "" "$mt3")
  new_invite "$ot" $w3
  b=$(call POST $N2/api/invitations/$CODE/accept "" "$xt")
  c=$(call POST $N1/api/channels/$g3/join "" "$xt")
  s=$(call PATCH $N0/api/workspaces/$w3 '{"join_mode":"open"}' "$ot")
  new_user; d=$(call POST $N2/api/channels/$g3/join "" "$TOKEN")
  if [ "$a" = 403 ] && [ "$e" = 403 ] && [ -z "$r1" ] && [ "$f" = 200 ] && [ "$b" = 200 ] && [ "$c" = 200 ] && [ "$s" = 200 ] && [ "$d" = 200 ]; then
    ok A4.3 "invite_only: outsider 403 until invited, member still joins; back to open lets outsiders in"
  else
    bad A4.3 "invite_only: outsider join #general $a and other public channel $e (want 403, role '$r1' should be empty); existing member join $f (want 200); accept invite $b, join after $c (want 200); back to open $s, new outsider join $d (want 200)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_A_4; finish; }
