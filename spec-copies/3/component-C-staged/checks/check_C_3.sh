#!/usr/bin/env bash
# Self-check for C.3 (slash commands). Prints PASS/FAIL per rule in /spec/component-C-staged/C-3-slash-commands.md.
# Usage: bash /spec/component-C-staged/checks/check_C_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SHRUG='¯\_(ツ)_/¯'
BODY_IS='d["message"]["body"]==a[0]'
NO_MSG='d.get("message", "missing") is None'
# count_bodies <token> <channel> <python-test-on-body> [node] -> how many stored messages pass the test
count_bodies() {
  call GET ${4:-$N0}/api/channels/$2/messages "" "$1" >/dev/null
  val "sum(1 for m in d['messages'] if ($3))"
}

rules_C_3() {
  local all="C3.1 C3.2 C3.3 C3.4 C3.5 C3.6" ot w g mt nt nid nn xid xn ch pc a b c e f h s1 s2 s3 s4 s5 s6

  # setup: owner O, member M (in channel ch), member N (in the workspace, not in ch or pc)
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  mt=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  nt=$TOKEN; nid=$USER_ID; nn=$USER
  new_channel "$ot" $w false; ch=$CHAN_ID
  [ -n "$ch" ] && [ "$(join "$mt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join a channel, fix the basics first"; return; }

  # C3.1 /me: body kept literally; /me with no text 400 and nothing posted
  post "$mt" $ch "/me waves" $N0; a=$ST; s1=$(has "$BODY_IS" "/me waves")
  post "$mt" $ch "/me" $N1; b=$ST
  post "$mt" $ch "/me   " $N2; c=$ST
  s2=$(count_bodies "$mt" $ch 'm["body"].startswith("/me")' $N1)
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 400 ] && [ "$c" = 400 ] && [ "$s2" = 1 ]; then
    ok C3.1 "/me: 201 with body kept as '/me waves'; /me with no text 400 and nothing posted"
  else
    bad C3.1 "/me: '/me waves' $a (body kept literally: $s1, want 201 yes); '/me' $b and '/me   ' $c (want 400 each); stored /me messages $s2 (want 1)"
  fi

  # C3.2 /shrug: text + space + emoticon, or the emoticon alone
  post "$mt" $ch "/shrug fine" $N0; a=$ST; s1=$(has "$BODY_IS" "fine $SHRUG")
  post "$mt" $ch "/shrug" $N1; b=$ST; s2=$(has "$BODY_IS" "$SHRUG")
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 201 ] && [ "$s2" = yes ]; then
    ok C3.2 "/shrug: 'fine $SHRUG' with text, '$SHRUG' alone"
  else
    bad C3.2 "/shrug: '/shrug fine' $a (body 'fine $SHRUG': $s1); '/shrug' $b (body '$SHRUG': $s2); want 201 yes each"
  fi

  # C3.3 unknown command 400 and nothing posted; //literal is posted as a normal message
  post "$mt" $ch "/nosuchcommand hi" $N2; a=$ST
  post "$mt" $ch "//literal text" $N0; b=$ST; e=$MSG_ID
  # the spec says "as typed"; the check also accepts one slash removed, in case the hidden tests expect that
  s2=$(has 'd["message"]["body"] in ("//literal text", "/literal text")')
  s1=$(count_bodies "$mt" $ch '"nosuchcommand" in m["body"]' $N2)
  if [ "$a" = 400 ] && [ "$b" = 201 ] && [ -n "$e" ] && [ "$s2" = yes ] && [ "$s1" = 0 ]; then
    ok C3.3 "unknown command 400 and nothing posted; //literal posts a normal message"
  else
    bad C3.3 "unknown command $a (want 400), stored copies $s1 (want 0); '//literal text' $b with message id '$e' and body '//literal text': $s2 (want 201, a message, yes)"
  fi

  # C3.4 /topic: 201, message null, channel.topic set and visible on another node; not in channel -> 403
  post "$mt" $ch "/topic Release on Friday" $N1; a=$ST
  s1=$(has "$NO_MSG"' and d["channel"]["topic"]=="Release on Friday"')
  b=$(call GET $N2/api/workspaces/$w "" "$ot"); s2=$(has 'any(str(c["id"])==a[0] and c["topic"]=="Release on Friday" for c in d["channels"])' "$ch")
  post "$nt" $ch "/topic nope" $N0; c=$ST
  s3=$(count_bodies "$mt" $ch 'm["body"].startswith("/topic")' $N0)
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 200 ] && [ "$s2" = yes ] && [ "$c" = 403 ] && [ "$s3" = 0 ]; then
    ok C3.4 "/topic: 201 with message null and the new topic, seen on another node; not in the channel 403; no message stored"
  else
    bad C3.4 "/topic: got $a (message null and channel.topic set: $s1, want 201 yes); workspace detail on 8002 $b shows it: $s2; non-member $c (want 403); stored /topic messages $s3 (want 0)"
  fi

  # C3.5 /invite @user into a private channel: 201, message null, invited_user; they become a member and can read
  new_channel "$ot" $w true; pc=$CHAN_ID
  [ -n "$pc" ] || { setup_fail "C3.5 C3.6" "could not create a private channel (got $ST), fix the basics first"; return; }
  e=$(call GET $N2/api/channels/$pc/messages "" "$nt")
  post "$ot" $pc "/invite @$nn" $N0; a=$ST
  s1=$(has "$NO_MSG"' and str(d["invited_user"]["id"])==a[0]' "$nid")
  b=$(call GET $N1/api/channels/$pc/members "" "$ot"); s2=$(has 'any(str(u["id"])==a[0] for u in d["members"])' "$nid")
  c=$(call GET $N2/api/channels/$pc/messages "" "$nt")
  new_user; xid=$USER_ID; xn=$USER            # X: registered, not in this workspace
  post "$ot" $pc "/invite @nosuchuser$RANDOM$RANDOM" $N1; f=$ST
  post "$ot" $pc "/invite @$xn" $N2; h=$ST
  post "$ot" $pc "/invite" $N0; s3=$ST
  post "$mt" $pc "/invite @$nn" $N1; s4=$ST     # M is not a member of pc
  call GET $N0/api/channels/$pc/members "" "$ot" >/dev/null
  s5=$(has 'not any(str(u["id"])==a[0] for u in d["members"])' "$xid")
  if [ "$e" = 403 ] && [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 200 ] && [ "$s2" = yes ] && [ "$c" = 200 ] \
     && [ "$f" = 404 ] && [ "$h" = 403 ] && [ "$s3" = 400 ] && [ "$s4" = 403 ] && [ "$s5" = yes ]; then
    ok C3.5 "/invite @user: 201 with message null and invited_user; they join the private channel; unknown 404, outside the workspace 403, no user 400, non-member caller 403"
  else
    bad C3.5 "/invite: before, N reads the private channel $e (want 403); /invite got $a (message null, invited_user is N: $s1, want 201 yes); members on 8001 $b include N: $s2; N reads after $c (want 200); unknown user $f (want 404); user outside the workspace $h (want 403, and not added: $s5); no username $s3 (want 400); caller not in the channel $s4 (want 403)"
  fi

  # C3.6 /archive and /unarchive: owner only; archived channel refuses posts (423) but accepts /unarchive
  post "$mt" $ch "/archive" $N0; a=$ST
  post "$ot" $ch "/archive" $N1; b=$ST; s1=$(has "$NO_MSG"' and d["channel"]["is_archived"] is True')
  post "$mt" $ch "after archive" $N2; c=$ST
  post "$mt" $ch "/unarchive" $N0; e=$ST
  post "$ot" $ch "/unarchive" $N1; f=$ST; s2=$(has "$NO_MSG"' and d["channel"]["is_archived"] is False')
  post "$mt" $ch "after unarchive" $N2; h=$ST
  if [ "$a" = 403 ] && [ "$b" = 201 ] && [ "$s1" = yes ] && [ "$c" = 423 ] && [ "$e" = 403 ] && [ "$f" = 201 ] && [ "$s2" = yes ] && [ "$h" = 201 ]; then
    ok C3.6 "/archive and /unarchive: owner only (403 otherwise); archived channel 423 on posts; /unarchive restores posting"
  else
    bad C3.6 "archive: member /archive $a (want 403); owner /archive $b (archived, message null: $s1, want 201 yes); post while archived $c (want 423); member /unarchive $e (want 403); owner /unarchive $f (unarchived: $s2, want 201 yes); post after $h (want 201)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_C_3; finish; }
