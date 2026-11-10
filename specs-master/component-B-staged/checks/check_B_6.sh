#!/usr/bin/env bash
# Self-check for B.6 (read state). Prints PASS/FAIL per rule in /spec/component-B-staged/B-6-read-state.md.
# Usage: bash /spec/component-B-staged/checks/check_B_6.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# rs -> "last_read_event_id unread_count mention_count" of the last read_state response
RS='" ".join(str(d["read_state"][k]) for k in ("last_read_event_id","unread_count","mention_count"))'

rules_B_6() {
  local all="B6.1 B6.2 B6.3 B6.4" ot ut un g w ch at p h h2 s a b c e f i j k

  # setup: owner O posts in channel ch; U is the reader (a channel member); AD admin has private channel p
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  ut=$TOKEN; un=$USER
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] || { setup_fail "$all" "could not add an admin, fix A.2 first"; return; }
  at=$TOKEN
  new_channel "$ot" $w false; ch=$CHAN_ID
  new_channel "$at" $w true; p=$CHAN_ID
  [ -n "$ch" ] && [ -n "$p" ] && [ "$(join "$ut" $ch)" = 200 ] || { setup_fail "$all" "could not create and join channels, fix B.1 first"; return; }
  h=$(head_of "$ut" $ch)
  [[ "$h" =~ ^[0-9]+$ ]] || { setup_fail "$all" "could not read head_event_id over the WebSocket, fix B.5 first ($h)"; return; }

  # B6.1 shape; unread counts other people's messages after the cursor; private non-member 403; no token 401
  a=$(call GET $N0/api/channels/$ch/read "" "$ut")
  b=$(has '(lambda r: str(r["channel_id"])==a[0] and all(isinstance(r[k],int) for k in ("last_read_event_id","unread_count","mention_count")))(d["read_state"])' "$ch")
  c=$(call POST $N1/api/channels/$ch/read "{\"last_read_event_id\":$h}" "$ut")
  for k in 1 2 3; do post "$ot" $ch "news $k" "http://127.0.0.1:800$((k-1))"; done
  post "$ut" $ch "my own words" $N0
  e=$(call GET $N2/api/channels/$ch/read "" "$ut"); i=$(field read_state.unread_count)
  f=$(call GET $N0/api/channels/$p/read "" "$ut"); j=$(call GET $N0/api/channels/$ch/read)
  if [ "$a" = 200 ] && [ "$b" = yes ] && [ "$c" = 200 ] && [ "$e" = 200 ] && [ "$i" = 3 ] && [ "$f" = 403 ] && [ "$j" = 401 ]; then
    ok B6.1 "read state: right shape; unread_count counts others' messages after the cursor; private non-member 403; no token 401"
  else
    bad B6.1 "read state: GET $a (shape right: $b), mark read $c; after 3 messages from others and 1 own, unread_count '$i' (want 3, status $e); private non-member $f (want 403); no token $j (want 401)"
  fi

  # B6.2 cursor: marking up to the head clears unread; never moves backwards; same on every node
  h2=$(head_of "$ut" $ch $W1)
  a=$(call POST $N1/api/channels/$ch/read "{\"last_read_event_id\":$h2}" "$ut"); i=$(val "$RS")
  b=$(call POST $N2/api/channels/$ch/read "{\"last_read_event_id\":$h}" "$ut"); j=$(val "$RS")
  c=$(call GET $N0/api/channels/$ch/read "" "$ut"); k=$(val "$RS")
  if [ "$a" = 200 ] && [ "$i" = "$h2 0 0" ] && [ "$b" = 200 ] && [ "$j" = "$h2 0 0" ] && [ "$c" = 200 ] && [ "$k" = "$h2 0 0" ]; then
    ok B6.2 "read cursor: marking to the head clears unread; an older id never moves it back; seen on every node"
  else
    bad B6.2 "cursor (last_read unread mentions): mark to head $h2 gave '$i' ($a), older id $h gave '$j' ($b), GET on 8000 gave '$k' ($c); want '$h2 0 0' each"
  fi

  # B6.3 mention_count: once per message from others (@username or @channel); own mention never counts
  post "$ot" $ch "hey @$un" $N0
  post "$ot" $ch "@$un @$un twice" $N1
  post "$ot" $ch "@channel heads up" $N2
  post "$ot" $ch "no mention here" $N0
  post "$ut" $ch "talking to @$un myself" $N1
  k=$h2
  a=$(call GET $N2/api/channels/$ch/read "" "$ut"); i=$(val "$RS")
  h2=$(head_of "$ut" $ch $W2)
  b=$(call POST $N0/api/channels/$ch/read "{\"last_read_event_id\":$h2}" "$ut"); j=$(val "$RS")
  if [ "$a" = 200 ] && [ "$i" = "$k 4 3" ] && [ "$b" = 200 ] && [ "$j" = "$h2 0 0" ]; then
    ok B6.3 "mention_count: once per message from others by @username or @channel; own mentions never count; reset by marking read"
  else
    bad B6.3 "mentions: after 3 mentioning messages and 1 plain from others plus 1 self-mention, got '$i' (want '$k 4 3': cursor unchanged, unread 4, mentions 3; status $a); after marking read to $h2 got '$j' (want '$h2 0 0')"
  fi

  # B6.4 workspace detail embeds one read_state per channel with the same values
  post "$ot" $ch "last one @$un" $N1
  call GET $N0/api/channels/$ch/read "" "$ut" >/dev/null; i=$(val "$RS")
  a=$(call GET $N2/api/workspaces/$w "" "$ut")
  b=$(has 'len(d["read_state"])==len(d["channels"]) and {str(r["channel_id"]) for r in d["read_state"]}=={str(c["id"]) for c in d["channels"]} and any(str(r["channel_id"])==a[0] and " ".join(str(r[k]) for k in ("last_read_event_id","unread_count","mention_count"))==a[1] for r in d["read_state"])' "$ch" "$i")
  if [ "$a" = 200 ] && [ "$b" = yes ] && [ "$i" = "$h2 1 1" ]; then
    ok B6.4 "workspace detail: read_state has one entry per channel with the same values as GET /read"
  else
    bad B6.4 "workspace detail $a: one read_state per channel and the entry for the channel equal to GET /read ('$i', want '$h2 1 1'): $b"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_B_6; finish; }
