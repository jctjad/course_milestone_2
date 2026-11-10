#!/usr/bin/env bash
# Self-check for A.3 (invitations). Prints PASS/FAIL per rule in /spec/component-A-staged/A-3-invitations.md.
# Usage: bash /spec/component-A-staged/checks/check_A_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_A_3() {
  local all="A3.1 A3.2 A3.3 A3.4" ot w g mt adt gt ot2 xt xid nt nname yt yid zt c1 mu1 ct mut cn ce c2 cr ca
  local s1 s2 s3 s4 s5 s6 s7 s8 a b c d e f h i j k r

  # setup from A.1 and A.2: owner O with a workspace; M member, AD admin, G guest; X, N, OT outsiders
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "a new user could not join #general of an open workspace, fix A1.7 first"; return; }
  mt=$TOKEN
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] || { setup_fail "$all" "could not add an admin, fix A.2 first"; return; }
  adt=$TOKEN
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID guest)" = 200 ] || { setup_fail "$all" "could not add a guest, fix A.2 first"; return; }
  gt=$TOKEN
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot2=$TOKEN
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  xt=$TOKEN; xid=$USER_ID
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  nt=$TOKEN; nname=$USER

  # A3.1 create: admin/owner only; 201 with code; max_uses defaults to 1; targeted invites are single-use;
  #     member, guest and outsider 403; unknown workspace 404; no token 401
  new_invite "$ot" $w; s1=$ST; c1=$CODE; mu1=$MAX_USES
  new_invite "$ot" $w "{\"invited_user_id\":$(jv $xid),\"max_uses\":5}" $N1; s2=$ST; ct=$CODE; mut=$MAX_USES
  new_invite "$ot" $w "{\"invited_username\":\"$nname\"}" $N2; s3=$ST; cn=$CODE
  new_invite "$adt" $w '{}' $N1; s5=$ST; ca=$CODE
  new_invite "$mt" $w; s4=$ST
  new_invite "$gt" $w; s6=$ST
  new_invite "$ot2" $w; s7=$ST
  new_invite "$ot" no-such-ws-$RANDOM; s8=$ST
  a=$(call POST $N0/api/workspaces/$w/invitations '{}')
  if [ "$s1" = 201 ] && [ -n "$c1" ] && [ "$mu1" = 1 ] && [ "$s2" = 201 ] && [ -n "$ct" ] && [ "$mut" = 1 ] && [ "$s3" = 201 ] && [ -n "$cn" ] \
     && [ "$s5" = 201 ] && [ -n "$ca" ] && [ "$s4" = 403 ] && [ "$s6" = 403 ] && [ "$s7" = 403 ] && [ "$s8" = 404 ] && [ "$a" = 401 ]; then
    ok A3.1 "create invitation: owner and admin 201 with code; max_uses defaults to 1; targeted single-use; others 403"
  else
    bad A3.1 "create: plain $s1 (code '$c1', max_uses '$mu1'), by user id $s2 (max_uses '$mut'), by username $s3, by admin $s5 (want 201, max_uses 1); by member $s4, guest $s6, outsider $s7 (want 403); unknown workspace $s8 (want 404); no token $a (want 401)"
  fi
  if [ -z "$c1" ] || [ -z "$ct" ]; then setup_fail "A3.2 A3.3 A3.4" "could not create invitations, fix A3.1 first"; return; fi

  # A3.2 list: admin/owner see active invitations; member, guest, outsider 403; no token 401
  a=$(call GET $N1/api/workspaces/$w/invitations "" "$ot")
  h=$(has 'set(a) <= {str(i["code"]) for i in d["invitations"]}' "$c1" "$ct" "$cn" "$ca")
  e=$(call GET $N2/api/workspaces/$w/invitations "" "$adt")
  i=$(has 'a[0] in {str(i["code"]) for i in d["invitations"]}' "$c1")
  b=$(call GET $N1/api/workspaces/$w/invitations "" "$mt")
  c=$(call GET $N1/api/workspaces/$w/invitations "" "$gt")
  d=$(call GET $N1/api/workspaces/$w/invitations "" "$ot2")
  f=$(call GET $N1/api/workspaces/$w/invitations)
  if [ "$a" = 200 ] && [ "$h" = yes ] && [ "$e" = 200 ] && [ "$i" = yes ] && [ "$b" = 403 ] && [ "$c" = 403 ] && [ "$d" = 403 ] && [ "$f" = 401 ]; then
    ok A3.2 "owner and admin list active invitations; member, guest, outsider 403; no token 401"
  else
    bad A3.2 "list: owner $a (all codes listed: $h), admin $e (listed: $i) (want 200/yes); member $b, guest $c, outsider $d (want 403); no token $f (want 401)"
  fi

  # A3.3 accept: 200, consumes one use, caller becomes member; wrong user on a targeted invite 403
  #     (by id or by username); unknown/expired/exhausted 404; exhausted codes leave the list; no token 401
  new_user || { setup_fail "A3.3 A3.4" "could not register a user"; return; }; yt=$TOKEN; yid=$USER_ID
  new_user || { setup_fail "A3.3 A3.4" "could not register a user"; return; }; zt=$TOKEN
  k=$(call POST $N0/api/invitations/$c1/accept)
  a=$(call POST $N1/api/invitations/$c1/accept "" "$yt"); r=$(role_of "$ot" $w $yid $N2)
  b=$(call POST $N2/api/invitations/$c1/accept "" "$zt")
  c=$(call POST $N0/api/invitations/$ct/accept "" "$zt")
  d=$(call POST $N1/api/invitations/$ct/accept "" "$xt")
  j="$(call POST $N2/api/invitations/$cn/accept "" "$zt") $(call POST $N0/api/invitations/$cn/accept "" "$nt")"
  e=$(call POST $N0/api/invitations/no-such-code-$RANDOM$RANDOM/accept "" "$zt")
  new_invite "$ot" $w '{"expires_in_seconds":1}'; ce=$CODE
  new_invite "$ot" $w '{"max_uses":2}'; c2=$CODE
  sleep 2
  f=$(call POST $N0/api/invitations/$ce/accept "" "$zt")
  g=$(call POST $N0/api/invitations/$c2/accept "" "$zt")
  new_user; h=$(call POST $N1/api/invitations/$c2/accept "" "$TOKEN")
  new_user; i=$(call POST $N2/api/invitations/$c2/accept "" "$TOKEN")
  call GET $N0/api/workspaces/$w/invitations "" "$ot" >/dev/null
  s1=$(has 'not ({a[0], a[1], a[2]} & {str(i["code"]) for i in d["invitations"]})' "$c1" "$c2" "$ce")
  if [ "$k" = 401 ] && [ "$a" = 200 ] && [ "$r" = member ] && [ "$b" = 404 ] && [ "$c" = 403 ] && [ "$d" = 200 ] && [ "$j" = "403 200" ] \
     && [ "$e" = 404 ] && [ "$f" = 404 ] && [ "$g" = 200 ] && [ "$h" = 200 ] && [ "$i" = 404 ] && [ "$s1" = yes ]; then
    ok A3.3 "accept consumes a use; wrong user on targeted invite 403; unknown, expired, exhausted 404; used-up codes unlisted"
  else
    bad A3.3 "accept: no token $k (want 401), first use $a (role '$r'), reuse $b, wrong user by id $c, right user $d, by username wrong/right '$j', unknown $e, expired $f, max_uses 2: $g/$h/$i; used-up and expired codes gone from list: $s1; want 200 (member), 404, 403, 200, '403 200', 404, 404, 200/200/404, yes"
  fi

  # A3.4 delete: admin/owner only; 200 {"deleted":true}; revoked code is 404 on accept and no longer listed;
  #     unknown code 404; no token 401
  new_invite "$ot" $w; cr=$CODE
  a=$(call DELETE $N1/api/workspaces/$w/invitations/$cr "" "$mt")
  f=$(call DELETE $N1/api/workspaces/$w/invitations/$cr)
  b=$(call DELETE $N1/api/workspaces/$w/invitations/$cr "" "$ot"); e=$(field deleted)
  c=$(call POST $N2/api/invitations/$cr/accept "" "$zt")
  d=$(call GET $N0/api/workspaces/$w/invitations "" "$ot"); h=$(has 'not any(str(i["code"])==a[0] for i in d["invitations"])' "$cr")
  i=$(call DELETE $N2/api/workspaces/$w/invitations/$ca "" "$adt")
  j=$(call DELETE $N0/api/workspaces/$w/invitations/no-such-code-$RANDOM "" "$ot")
  if [ "$a" = 403 ] && [ "$f" = 401 ] && [ "$b" = 200 ] && [ "$e" = True ] && [ "$c" = 404 ] && [ "$h" = yes ] && [ "$i" = 200 ] && [ "$j" = 404 ]; then
    ok A3.4 "delete: owner and admin 200 {deleted:true}; member 403; revoked code 404 and unlisted; unknown code 404"
  else
    bad A3.4 "delete: by member $a (want 403), no token $f (want 401), by owner $b with deleted '$e' (want 200/True), accept revoked $c (want 404), gone from list: $h, by admin $i (want 200), unknown code $j (want 404)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_A_3; finish; }
