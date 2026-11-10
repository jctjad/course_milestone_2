#!/usr/bin/env bash
# Self-check for the basics (BASICS.1 to BASICS.5) listed in /spec/README.md: the small core that
# components C, D and E run on when Specs A and B are not part of the run.
# Usage: bash /spec/basics/checks/check_basics.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

USER_FIELDS='all(k in d["user"] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")) and not any("password" in k for k in d["user"])'
CHAN_FIELDS='all(k in d["channel"] for k in ("id","workspace_id","name","is_private","is_dm","topic","is_archived"))'
MSG_FIELDS='all(k in d["message"] for k in ("id","channel_id","author_id","author","body","parent_id","reply_count","created_at","edited_at","files","reactions","mentions"))'

rules_basics() {
  local all="BASICS.1 BASICS.2 BASICS.3 BASICS.4 BASICS.5" u t ot oid on w g mt mid mn ch pc a b c e f h l s1 s2 s3 s4 s5 s6 m1

  # BASICS.1 auth: register 201 {user, token}; login 200; me 200 on every node; bad or no token 401
  u="chk$(date +%s)$RANDOM"
  a=$(call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\",\"display_name\":\"Check User\"}")
  t=$(field token); s1=$(has "$USER_FIELDS")
  b=$(call POST $N1/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\"}")
  c=$(call POST $N2/api/auth/login "{\"username\":\"$u\",\"password\":\"$PW\"}")
  e=$(call POST $N0/api/auth/login "{\"username\":\"$u\",\"password\":\"wrong-password-9\"}")
  f="$(call GET $N0/api/auth/me "" "$t") $(call GET $N1/api/auth/me "" "$t") $(call GET $N2/api/auth/me "" "$t")"
  h="$(call GET $N0/api/auth/me "" "not-a-real-token-000000") $(call GET $N1/api/auth/me)"
  if [ "$a" = 201 ] && [ "${#t}" -ge 16 ] && [ "$s1" = yes ] && [ "$b" = 409 ] && [ "$c" = 200 ] && [ "$e" = 401 ] \
     && [ "$f" = "200 200 200" ] && [ "$h" = "401 401" ]; then
    ok BASICS.1 "auth: register 201 with user and token; duplicate 409; login 200, wrong password 401; me 200 on every node; bad or no token 401"
  else
    bad BASICS.1 "auth: register $a (token length ${#t}, UserObj fields, no password: $s1; want 201, 16+, yes); duplicate $b (want 409); login on 8002 $c (want 200); wrong password $e (want 401); me on 8000/8001/8002 '$f' (want 200 x3); bad token, no token '$h' (want 401 401)"
  fi
  [ -n "$t" ] || { setup_fail "BASICS.2 BASICS.3 BASICS.4 BASICS.5" "could not register a user, fix BASICS.1 first"; return; }

  # BASICS.2 workspaces: 201 {workspace, general_channel}; detail lists #general; joining #general makes you a member
  ot=$t; call GET $N0/api/auth/me "" "$ot" >/dev/null; oid=$(field user.id); on=$u
  w=$(new_slug)
  a=$(call POST $N0/api/workspaces "{\"slug\":\"$w\",\"name\":\"Check $w\"}" "$ot"); g=$(field general_channel.id)
  s1=$(has 'str(d["workspace"]["owner_id"])==a[0] and d["workspace"]["slug"]==a[1] and d["general_channel"]["is_private"] is False' "$oid" "$w")
  b=$(call GET $N1/api/workspaces/$w "" "$ot"); s2=$(has 'any(str(c["id"])==a[0] for c in d["channels"])' "$g")
  new_user; mt=$TOKEN; mid=$USER_ID; mn=$USER
  c=$(join "$mt" "$g" $N2)
  e=$(call GET $N0/api/channels/$g/members "" "$ot"); s3=$(has 'sorted(str(u["id"]) for u in d["members"])==sorted(a)' "$oid" "$mid")
  f=$(call GET $N0/api/workspaces/no-such-ws-$RANDOM "" "$ot")
  if [ "$a" = 201 ] && [ -n "$g" ] && [ "$s1" = yes ] && [ "$b" = 200 ] && [ "$s2" = yes ] && [ "$c" = 200 ] && [ "$e" = 200 ] && [ "$s3" = yes ] && [ "$f" = 404 ]; then
    ok BASICS.2 "workspaces: 201 with a public #general owned by the creator; detail on another node lists it; joining #general adds the member; unknown slug 404"
  else
    bad BASICS.2 "workspaces: create $a (owner, slug, public general_channel: $s1; want 201 yes); detail on 8001 $b (lists #general: $s2); member joins #general on 8002 $c (want 200); #general members $e (exactly creator and member: $s3); unknown slug $f (want 404)"
  fi
  [ -n "$g" ] || { setup_fail "BASICS.3 BASICS.4 BASICS.5" "could not create a workspace, fix BASICS.2 first"; return; }

  # BASICS.3 channels: owner creates public and private channels; topic; archive blocks posts (423); unarchive
  new_channel "$ot" $w false $N0; a=$ST; ch=$CHAN_ID; s1=$(has "$CHAN_FIELDS"' and d["channel"]["is_private"] is False and d["channel"]["topic"]==""')
  new_channel "$ot" $w true $N1; b=$ST; pc=$CHAN_ID; s2=$(has "$CHAN_FIELDS"' and d["channel"]["is_private"] is True')
  c=$(join "$mt" "$ch" $N2)
  e=$(call PATCH $N0/api/channels/$ch '{"topic":"basics topic"}' "$ot"); s3=$(has 'd["channel"]["topic"]=="basics topic"')
  f=$(call POST $N1/api/channels/$ch/archive "" "$ot"); s4=$(has 'd["channel"]["is_archived"] is True')
  post "$mt" "$ch" "while archived" $N2; h=$ST
  l=$(call POST $N0/api/channels/$ch/unarchive "" "$ot"); s5=$(has 'd["channel"]["is_archived"] is False')
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 201 ] && [ "$s2" = yes ] && [ "$c" = 200 ] && [ "$e" = 200 ] && [ "$s3" = yes ] \
     && [ "$f" = 200 ] && [ "$s4" = yes ] && [ "$h" = 423 ] && [ "$l" = 200 ] && [ "$s5" = yes ]; then
    ok BASICS.3 "channels: public and private create 201 with every ChannelObj field; join 200; topic 200; archive blocks posts with 423; unarchive 200"
  else
    bad BASICS.3 "channels: public $a ($s1), private $b ($s2) (want 201 with id, workspace_id, name, is_private, is_dm, topic, is_archived); member joins $c (want 200); topic $e ($s3); archive $f ($s4); post while archived $h (want 423); unarchive $l ($s5)"
  fi

  # BASICS.4 messages: 201 MessageObj; newest-first history on another node; @username mention; empty body 400;
  #   private channel non-member 403
  post "$ot" "$ch" "first" $N0; a=$ST; m1=$MSG_ID; s1=$(has "$MSG_FIELDS")
  post "$mt" "$ch" "hi @$on" $N1; b=$ST; s2=$(has 'sorted(str(x) for x in d["message"]["mentions"])==[a[0]]' "$oid")
  c=$(call GET $N2/api/channels/$ch/messages "" "$mt"); s3=$(has '[m["body"] for m in d["messages"]][:2]==["hi @"+a[0], "first"]' "$on")
  post "$ot" "$ch" "" $N0; e=$ST
  f=$(call GET $N1/api/channels/$pc/messages "" "$mt")
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 201 ] && [ "$s2" = yes ] && [ "$c" = 200 ] && [ "$s3" = yes ] && [ "$e" = 400 ] && [ "$f" = 403 ]; then
    ok BASICS.4 "messages: 201 with every MessageObj field; @username resolves; history newest first on another node; empty body 400; private non-member 403"
  else
    bad BASICS.4 "messages: post $a (all MessageObj fields: $s1; want 201 yes); '@owner' post $b (mentions = [owner]: $s2); history on 8002 $c (newest first: $s3); empty body $e (want 400); non-member reads a private channel $f (want 403)"
  fi

  # BASICS.5 WebSocket: bad token rejected; subscribe answers subscribed; a post reaches subscribers on every node
  a="$(ws reject "$W0/api/ws?token=not-a-real-token-000000") $(ws reject "$W1/api/ws?token=not-a-real-token-000000") $(ws reject "$W2/api/ws?token=not-a-real-token-000000")"
  listen_all "$mt" "$ch" 1 5
  post "$ot" "$ch" "live" $N1; b=$ST
  wait "${LPIDS[@]}" 2>/dev/null
  c=$(verdict "$ch" "message.created")
  rm -f ${WSF}{0,1,2}.jsonl
  if [ "$a" = "rejected rejected rejected" ] && [ "$b" = 201 ] && [ "$c" = yes ]; then
    ok BASICS.5 "WebSocket: bad token rejected on every node; subscribe gives subscribed; a post reaches all three nodes as message.created head+1"
  else
    bad BASICS.5 "WebSocket: bad token on 8000/8001/8002 '$a' (want rejected x3); post $b (want 201); subscribers on every node get message.created with event_id head+1: $c"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_basics; finish; }
