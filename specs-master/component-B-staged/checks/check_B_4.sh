#!/usr/bin/env bash
# Self-check for B.4 (mentions). Prints PASS/FAIL per rule in /spec/component-B-staged/B-4-mentions.md.
# Usage: bash /spec/component-B-staged/checks/check_B_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# mentions of the last posted message equal exactly the given ids (no duplicates, any order)
SAME='sorted(str(x) for x in d["message"]["mentions"])==sorted(a)'
GOT='",".join(sorted(str(x) for x in d["message"]["mentions"]))'

rules_B_4() {
  local all="B4.1 B4.2 B4.3" ot oid on g w ch bid bn cid cn yn mem a b c e f s1 s2 s3 s4 s5

  # setup: owner O posts; B is in the channel; C is in the workspace but not the channel; Y is outside
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN; oid=$USER_ID; on=$USER
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  bid=$USER_ID; bn=$USER; local bt=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  cid=$USER_ID; cn=$USER
  new_user || { setup_fail "$all" "could not register a user"; return; }
  yn=$USER
  new_channel "$ot" $w false; ch=$CHAN_ID
  [ -n "$ch" ] && [ "$(join "$bt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join a channel, fix B.1 first"; return; }

  # B4.1 resolving: @username -> id; duplicates once; author never; workspace member outside the channel counts
  post "$ot" $ch "hi @$bn, and @$bn again" $N0; a=$ST; s1=$(has "$SAME" "$bid"); e=$(val "$GOT")
  post "$ot" $ch "(@$cn) and @$on too" $N1; b=$ST; s2=$(has "$SAME" "$cid"); f=$(val "$GOT")
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 201 ] && [ "$s2" = yes ]; then
    ok B4.1 "mentions: @username resolves once; author never included; workspace member outside the channel included"
  else
    bad B4.1 "mentions: '@B, and @B again' gave [$e] (want [$bid], status $a); '(@C) and @author' gave [$f] (want [$cid]: C is in the workspace, the author is never mentioned, status $b)"
  fi

  # B4.2 not mentions: in-word, email-shaped, unknown user, user outside the workspace
  post "$ot" $ch "x@$bn and $bn@example.com and @nosuchuser$RANDOM and @$yn" $N2; a=$ST; s3=$(has 'd["message"]["mentions"]==[]'); e=$(val "$GOT")
  if [ "$a" = 201 ] && [ "$s3" = yes ]; then
    ok B4.2 "not mentions: in-word, email-shaped, unknown users and users outside the workspace add nothing"
  else
    bad B4.2 "non-mentions: 'x@B, B@example.com, @unknown, @outsider' gave [$e] (want [], status $a)"
  fi

  # B4.3 @channel: every channel member except the author, deduplicated with @username
  call GET $N0/api/channels/$ch/members "" "$ot" >/dev/null
  mem=$(val '" ".join(str(u["id"]) for u in d["members"] if str(u["id"])!=a[0])' "$oid")
  post "$ot" $ch "@channel please read" $N0; a=$ST; s4=$(has "$SAME" $mem); e=$(val "$GOT")
  post "$ot" $ch "@channel and @$bn and @$cn" $N1; b=$ST; s5=$(has "$SAME" $mem "$cid"); f=$(val "$GOT")
  if [ -n "$mem" ] && [ "$a" = 201 ] && [ "$s4" = yes ] && [ "$b" = 201 ] && [ "$s5" = yes ]; then
    ok B4.3 "@channel: every channel member except the author, deduplicated with @username"
  else
    bad B4.3 "@channel: gave [$e] (want channel members without the author: [$mem], status $a); '@channel @B @C' gave [$f] (want those plus $cid, once each, status $b)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_B_4; finish; }
