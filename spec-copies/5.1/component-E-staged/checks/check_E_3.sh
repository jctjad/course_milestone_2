#!/usr/bin/env bash
# Self-check for E.3 (IRC channels and bridging). Prints PASS/FAIL per rule in /spec/component-E-staged/E-3-irc-bridge.md.
# Usage: bash /spec/component-E-staged/checks/check_E_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_E_3() {
  local all="E3.1 E3.2 E3.3 E3.4 E3.5" ot mt mid w g gname ch a b c e s1 s2 txt txt2 line pl t0
  listening 6667 || { setup_fail "$all" "nothing is listening on port 6667: start the IRC gateway from /app/start.sh"; return; }
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  w=$SLUG; g=$GEN_ID; gname=$(field general_channel.name); gname=${gname#\#}
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  mt=$TOKEN; mid=$USER_ID
  ch=$(irc_chan $w $gname)

  # E3.1 JOIN #slug/channel -> JOIN, 353, 366; unknown or hyphenated -> 403
  a=$(irc join "$mt" $(new_nick) "$ch"); b=$(irc join "$mt" $(new_nick) "#$w/nosuch$RANDOM"); c=$(irc join "$mt" $(new_nick) "#$w-$gname")
  if [[ "$a" =~ ^JOIN.*353.*366$ ]] && [ "$b" = 403 ] && [ "$c" = 403 ]; then
    ok E3.1 "JOIN $ch: JOIN, 353, 366; unknown and hyphenated channels 403"
  else
    bad E3.1 "JOIN $ch gave '$a' (want JOIN ... 353 ... 366); unknown channel '$b', hyphenated #$w-$gname '$c' (want 403 each)"
  fi

  # E3.2 NAMES, WHO, LIST, TOPIC, MODE are accepted (no 421)
  e=$(irc cmds "$mt" $(new_nick) "$ch")
  if [ "$e" = "421s=0" ]; then
    ok E3.2 "NAMES, WHO, LIST, TOPIC and MODE are accepted"
  else
    bad E3.2 "NAMES/WHO/LIST/TOPIC/MODE after JOIN: got '$e' (want 421s=0: none of them unknown)"
  fi

  # E3.3 IRC -> web: stored as the token's user; one dense event_id stream with web posts
  txt="from irc $RANDOM"
  listen_ports "$mt" $g 3 8 8000 8001 8002
  post "$ot" $g "web before irc" $N0; s1=$ST
  a=$(irc say "$mt" $(new_nick) "$ch" "$txt")
  post "$ot" $g "web after irc" $N2; s1="$s1 $ST"
  wait "${LPIDS[@]}" 2>/dev/null
  b=$(verdict_ports $g "message.created,message.created,message.created")
  rm -f ${WSF}800{0,1,2}.jsonl
  c=$(call GET $N1/api/channels/$g/messages "" "$mt"); s2=$(has 'any(m["body"]==a[0] and str(m["author_id"])==a[1] for m in d["messages"])' "$txt" "$mid")
  if [ "$s1" = "201 201" ] && [ "$a" = sent ] && [ "$b" = yes ] && [ "$c" = 200 ] && [ "$s2" = yes ]; then
    ok E3.3 "IRC to web: PRIVMSG is stored as the token's user and shares one dense event_id stream with web posts"
  else
    bad E3.3 "IRC to web: web posts '$s1' (want 201 201), IRC say '$a' (want sent); 3 message.created with consecutive event_ids on every node: $b; history on 8001 $c has '$txt' by the token's user: $s2"
  fi

  # E3.4 web -> IRC: a web post reaches an IRC client in the channel as :nick!user@host PRIVMSG #chan :body
  txt="to irc $RANDOM"; rm -f $TMPC.irc
  irc listen "$mt" $(new_nick) "$ch" 5 $TMPC.irc & pl=$!
  for _ in $(seq 1 30); do grep -q '^joined' $TMPC.irc 2>/dev/null && break; sleep 0.1; done
  post "$ot" $g "$txt" $N1; s1=$ST
  line=""
  for _ in $(seq 1 20); do line=$(grep -F " :$txt" $TMPC.irc 2>/dev/null | head -1); [ -n "$line" ] && break; sleep 0.1; done
  kill $pl 2>/dev/null; wait $pl 2>/dev/null
  e=$(head -1 $TMPC.irc 2>/dev/null); rm -f $TMPC.irc
  if [ "$e" = joined ] && [ "$s1" = 201 ] && [[ "$line" =~ ^:[^!\ ]+![^@\ ]+@[^\ ]+\ PRIVMSG\ ${ch//\//\\/}\ :$txt$ ]]; then
    ok E3.4 "web to IRC: a web post reaches IRC clients in the channel as :nick!user@host PRIVMSG within 2 s"
  else
    bad E3.4 "web to IRC: IRC listener '$e' (want joined); web post $s1 (want 201); IRC line within 2 s: '${line:-none}' (want ':<nick>!<user>@<host> PRIVMSG $ch :$txt')"
  fi

  # E3.5 bridging with port 8001 killed: web post through 8000 reaches IRC; IRC message reaches a web subscriber on 8002
  [ -n "$(pids_on_port 8001)" ] || { bad E3.5 "setup: nothing is listening on port 8001"; return; }
  txt="outage to irc $RANDOM"; txt2="outage from irc $RANDOM"; rm -f $TMPC.irc
  irc listen "$mt" $(new_nick) "$ch" 8 $TMPC.irc & pl=$!
  for _ in $(seq 1 30); do grep -q '^joined' $TMPC.irc 2>/dev/null && break; sleep 0.1; done
  kill_port 8001; t0=$(date +%s%N); sleep 0.3
  listen_ports "$mt" $g 2 5 8002
  post "$ot" $g "$txt" $N0; s1=$ST
  a=$(irc say "$mt" $(new_nick) "$ch" "$txt2")
  line=""
  for _ in $(seq 1 20); do line=$(grep -F " :$txt" $TMPC.irc 2>/dev/null | head -1); [ -n "$line" ] && break; sleep 0.1; done
  kill $pl 2>/dev/null; wait $pl 2>/dev/null; rm -f $TMPC.irc
  wait "${LPIDS[@]}" 2>/dev/null
  b=$(verdict_ports $g "message.created,message.created"); grep -qF "$txt2" ${WSF}8002.jsonl 2>/dev/null && c=yes || c=no
  rm -f ${WSF}8002.jsonl
  # put the cluster back: 8001 may come back on its own; otherwise restart it (one start.sh only)
  wait_until_ms $t0 2 health_ok $N1 || restart_cluster || true
  if [ "$s1" = 201 ] && [ -n "$line" ] && [ "$a" = sent ] && [ "$b" = yes ] && [ "$c" = yes ]; then
    ok E3.5 "8001 killed: web posts still reach IRC, and IRC messages still reach web subscribers on 8002"
  else
    bad E3.5 "8001 killed: web post on 8000 $s1 (want 201), reached IRC: ${line:+yes}${line:-no}; IRC say '$a' (want sent), reached the subscriber on 8002: $c; that subscriber got both as message.created with consecutive event_ids: $b"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_E_3; finish; }
