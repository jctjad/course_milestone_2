#!/usr/bin/env bash
# Self-check for A.2 (roles). Prints PASS/FAIL per rule in /spec/component-A-staged/A-2-roles.md.
# Usage: bash /spec/component-A-staged/checks/check_A_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_A_2() {
  local all="A2.1 A2.2 A2.3 A2.4 A2.5" ot oid w1 g1 w2 g2 bt bid ct cid dt did et eid xt xid pub priv
  local s a b c d e f g h i j r1 r2 r3 mrole muid

  # setup from A.1: owner O with two workspaces; B is a member of both (joined via #general)
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN; oid=$USER_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w1=$SLUG; g1=$GEN_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w2=$SLUG; g2=$GEN_ID
  new_member "$g1" && [ "$(call POST $N0/api/channels/$g2/join "" "$TOKEN")" = 200 ] \
    || { setup_fail "$all" "a new user could not join #general of an open workspace, fix A1.7 first"; return; }
  bt=$TOKEN; bid=$USER_ID

  # A2.1 per-workspace roles: B becomes admin in w1 but stays member in w2
  s=$(set_role "$ot" $w1 $bid admin); mrole=$(field member.role); muid=$(field member.user_id)
  r1=$(role_of "$ot" $w1 $bid $N1); r2=$(role_of "$ot" $w2 $bid $N2)
  if [ "$s" = 200 ] && [ "$r1" = admin ] && [ "$r2" = member ]; then
    ok A2.1 "the same user is admin in one workspace and member in another"
  else
    bad A2.1 "per-workspace roles: PATCH to admin got $s; expected admin in w1 and member in w2, got '$r1' and '$r2'"
  fi

  # A2.2 channel permissions: guest C, member D, admin B, outsider X
  new_member "$g1" || { setup_fail "A2.2 A2.3 A2.4 A2.5" "could not add a member to the workspace"; return; }
  ct=$TOKEN; cid=$USER_ID
  new_member "$g1" || { setup_fail "A2.2 A2.3 A2.4 A2.5" "could not add a member to the workspace"; return; }
  dt=$TOKEN; did=$USER_ID
  new_user || { setup_fail "A2.2 A2.3 A2.4 A2.5" "could not register a user"; return; }
  xt=$TOKEN; xid=$USER_ID
  if [ "$(set_role "$ot" $w1 $cid guest)" != 200 ]; then
    bad A2.2 "setup: owner could not PATCH a member to guest"
  else
    new_channel "$ot" $w1 false; pub=$CHAN_ID
    if ! is2xx "$ST" || [ -z "$pub" ]; then
      bad A2.2 "setup: owner could not create a public channel (got $ST, channel.id '$pub')"
    else
      a=$(call POST $N1/api/channels/$pub/join "" "$ct")
      new_channel "$ct" $w1 false $N1; b=$ST
      new_channel "$ct" $w1 true $N1; f=$ST
      new_channel "$dt" $w1 false $N2; c=$ST
      new_channel "$dt" $w1 true $N2; d=$ST
      new_channel "$xt" $w1 false $N0; g=$ST
      new_channel "$bt" $w1 true $N0; e=$ST; priv=$CHAN_ID
      # nobody gets into a private channel without an invite: not a member, admin, owner, guest or outsider;
      # the creator is already a member, so their own join is 200
      if [ -n "$priv" ]; then
        i=$(call POST $N2/api/channels/$priv/join "" "$bt")
        r3="$(call POST $N1/api/channels/$priv/join "" "$dt") $(call POST $N2/api/channels/$priv/join "" "$ot")"
        r3="$r3 $(call POST $N0/api/channels/$priv/join "" "$ct") $(call POST $N1/api/channels/$priv/join "" "$xt")"
        new_member "$g1" && [ "$(set_role "$ot" $w1 $USER_ID admin)" = 200 ] && r3="$r3 $(call POST $N2/api/channels/$priv/join "" "$TOKEN")"
        h=$(role_of "$ot" $w1 $xid)
      else r3="no private channel id"; h=""; i=""; fi
      if [ "$a" = 403 ] && [ "$b" = 403 ] && [ "$f" = 403 ] && [ "$c" = 201 ] && [ "$d" = 403 ] && [ "$g" = 403 ] && [ "$e" = 201 ] \
         && [ "$i" = 200 ] && [ "$r3" = "403 403 403 403 403" ] && [ -z "$h" ]; then
        ok A2.2 "guest cannot join or create channels; member creates public only; admin creates private; only the creator is in a private channel"
      else
        bad A2.2 "channel permissions: guest join $a, guest create public $b and private $f (want 403 each); member public $c (want 201), member private $d (want 403); outsider create $g (want 403); admin private $e (want 201); creator joins own private channel $i (want 200: creator is a member); private join by member/owner/guest/outsider/other admin '$r3' (want 403 each); outsider became '$h' (want not a member)"
      fi
    fi
  fi

  # A2.3 role changes: PATCH {role} returns {member:{user_id, role}}; owner's role cannot change;
  #     unknown role 400; unknown user or workspace 404; no token 401
  a=$(set_role "$bt" $w1 $oid member); b=$(set_role "$ot" $w1 $oid admin); r1=$(role_of "$ot" $w1 $oid)
  c=$(set_role "" $w1 $bid member)
  d=$(set_role "$ot" $w1 $did superuser); r2=$(role_of "$ot" $w1 $did)
  e=$(set_role "$ot" $w1 999999999 member)
  f=$(set_role "$ot" no-such-ws-$RANDOM $did member)
  if [ "$s" = 200 ] && [ "$mrole" = admin ] && [ "$muid" = "$bid" ] && [ "$a" = 403 ] && [ "$b" = 403 ] && [ "$r1" = owner ] \
     && [ "$c" = 401 ] && [ "$d" = 400 ] && [ "$r2" = member ] && [ "$e" = 404 ] && [ "$f" = 404 ]; then
    ok A2.3 "PATCH returns 200 with member; owner's role is 403; bad role 400; unknown user or workspace 404; no token 401"
  else
    bad A2.3 "role changes: PATCH got $s with member.role '$mrole', member.user_id '$muid'; admin->owner $a, owner->self $b (want 403 each, owner now '$r1'); no token $c (want 401); role superuser $d (want 400, role now '$r2'); unknown user $e, unknown workspace $f (want 404)"
  fi

  # A2.4 promoting/demoting: owner and admin can change member<->guest; owner can change admins;
  #     an admin cannot change another admin; role "owner" is 400; members and guests cannot PATCH
  a=$(set_role "$bt" $w1 $did guest); r1=$(role_of "$ot" $w1 $did); b=$(set_role "$bt" $w1 $did member)
  g=$(set_role "$ot" $w1 $did admin); h=$(set_role "$ot" $w1 $did member); r3=$(role_of "$ot" $w1 $did)
  new_member "$g1" && [ "$(set_role "$ot" $w1 $USER_ID admin)" = 200 ] \
    || { setup_fail "A2.4 A2.5" "could not add a second admin"; return; }
  et=$TOKEN; eid=$USER_ID
  c=$(set_role "$bt" $w1 $eid member); r2=$(role_of "$ot" $w1 $eid)
  d=$(set_role "$ot" $w1 $did owner)
  e=$(set_role "$dt" $w1 $cid member)
  f=$(set_role "$ct" $w1 $did guest)
  if [ "$a" = 200 ] && [ "$r1" = guest ] && [ "$b" = 200 ] && [ "$g" = 200 ] && [ "$h" = 200 ] && [ "$r3" = member ] \
     && [ "$c" = 403 ] && [ "$r2" = admin ] && [ "$d" = 400 ] && [ "$e" = 403 ] && [ "$f" = 403 ]; then
    ok A2.4 "admin demotes/promotes members; owner changes admins; admin cannot touch admin; role owner 400; members/guests 403"
  else
    bad A2.4 "promote/demote: admin member->guest $a ('$r1'), guest->member $b; owner member->admin $g, admin->member $h ('$r3') (want 200 each); admin edits admin $c (want 403, role now '$r2'); role owner $d (want 400); member PATCH $e, guest PATCH $f (want 403)"
  fi

  # A2.5 transfer ownership: owner-only; target must be admin; old owner becomes admin; owner_id updates
  a=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $eid)}" "$bt")
  i=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $bid)}" "$dt")
  j=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $bid)}")
  b=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $did)}" "$ot")
  c=$(call POST $N1/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $bid)}" "$ot")
  r1=$(role_of "$bt" $w1 $bid $N2); r2=$(role_of "$bt" $w1 $oid $N2)
  d=$(call GET $N0/api/workspaces "" "$bt"); h=$(has 'any(str(w["slug"])==a[0] and str(w["owner_id"])==a[1] for w in d["workspaces"])' "$w1" "$bid")
  e=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $eid)}" "$ot")
  if [ "$a" = 403 ] && [ "$i" = 403 ] && [ "$j" = 401 ] && [ "$b" = 400 ] && [ "$c" = 200 ] && [ "$r1" = owner ] && [ "$r2" = admin ] \
     && [ "$h" = yes ] && [ "$e" = 403 ]; then
    ok A2.5 "transfer_ownership: owner-only, target must be admin, old owner becomes admin, owner_id updates"
  else
    bad A2.5 "transfer: by admin $a, by member $i (want 403), no token $j (want 401), to member $b (want 400), to admin $c (want 200); new owner '$r1' (want owner), old owner '$r2' (want admin); owner_id updated: $h; old owner tries again $e (want 403)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_A_2; finish; }
