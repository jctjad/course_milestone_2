#!/usr/bin/env bash
# Self-check for B.2 (messages and history). Prints PASS/FAIL per rule in /spec/component-B-staged/B-2-messages.md.
# Usage: bash /spec/component-B-staged/checks/check_B_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

IDS='",".join(str(m["id"]) for m in d[a[0]])'

rules_B_2() {
  local all="B2.1 B2.2 B2.3 B2.4 B2.5 B2.6" ot w g mt mid at xt ch c2 p h t par r1 r2 r3 cur ids want s a b c e f i j k n
  local -a m

  # setup: owner O, workspace w; M member; AD admin with a private channel p; X outsider; public channel ch (M joined)
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  mt=$TOKEN; mid=$USER_ID; n=$USER
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] || { setup_fail "$all" "could not add an admin, fix A.2 first"; return; }
  at=$TOKEN
  new_user || { setup_fail "$all" "could not register a user"; return; }
  xt=$TOKEN
  new_channel "$ot" $w false; ch=$CHAN_ID
  new_channel "$at" $w true; p=$CHAN_ID
  [ -n "$ch" ] && [ -n "$p" ] && [ "$(join "$mt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join channels, fix B.1 first"; return; }

  # B2.1 post: 201 {message} with every MessageObj field and the right values; no event_id
  post "$mt" $ch "hello world" $N1; s=$ST
  a=$(has 'all(k in d["message"] for k in ("id","channel_id","author_id","author","body","parent_id","reply_count","created_at","edited_at","files","reactions","mentions"))')
  b=$(has '(lambda m: m["body"]=="hello world" and str(m["channel_id"])==a[0] and str(m["author_id"])==a[1] and str(m["author"]["id"])==a[1] and m["author"]["username"]==a[2] and m["parent_id"] is None and m["edited_at"] is None and m["reply_count"]==0 and all(isinstance(m[k],list) for k in ("files","reactions","mentions")) and "event_id" not in m)(d["message"])' "$ch" "$mid" "$n")
  c=$(has 're.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)", d["message"]["created_at"]) is not None')
  if [ "$s" = 201 ] && [ "$a" = yes ] && [ "$b" = yes ] && [ "$c" = yes ]; then
    ok B2.1 "post: 201 with every MessageObj field, right author and defaults, UTC created_at, no event_id"
  else
    bad B2.1 "post: got $s; every field present: $a; values right (body, channel_id, author_id/author, null parent_id/edited_at, reply_count 0, list fields, no event_id): $b; created_at RFC 3339 UTC: $c"
  fi

  # B2.2 posting errors: empty/missing body 400; non-member of private 403; outsider 403; unknown 404; no token 401
  a=$(call POST $N0/api/channels/$ch/messages '{"body":""}' "$mt")
  b=$(call POST $N0/api/channels/$ch/messages '{}' "$mt")
  post "$mt" $p "sneaky" $N1; c=$ST
  post "$xt" $ch "outsider" $N2; e=$ST
  f=$(call POST $N0/api/channels/999999999/messages '{"body":"x"}' "$mt")
  h=$(call POST $N0/api/channels/$ch/messages '{"body":"x"}')
  if [ "$a" = 400 ] && [ "$b" = 400 ] && [ "$c" = 403 ] && [ "$e" = 403 ] && [ "$f" = 404 ] && [ "$h" = 401 ]; then
    ok B2.2 "posting errors: empty or missing body 400; non-member 403; unknown channel 404; no token 401"
  else
    bad B2.2 "posting: empty body $a, no body $b (want 400); non-member of private $c, outsider $e (want 403); unknown channel $f (want 404); no token $h (want 401)"
  fi

  # B2.3 archived channel: post and topic PATCH 423; unarchive restores posting
  new_channel "$ot" $w false; c2=$CHAN_ID; join "$mt" $c2 >/dev/null
  i=$(call POST $N0/api/channels/$c2/archive "" "$ot")
  post "$mt" $c2 "into the archive" $N1; a=$ST
  b=$(call PATCH $N2/api/channels/$c2 '{"topic":"nope"}' "$mt")
  j=$(call POST $N0/api/channels/$c2/unarchive "" "$ot")
  post "$mt" $c2 "back again" $N2; c=$ST
  if [ "$i" = 200 ] && [ "$a" = 423 ] && [ "$b" = 423 ] && [ "$j" = 200 ] && [ "$c" = 201 ]; then
    ok B2.3 "archived channel: post and topic change 423; after unarchive posting is 201"
  else
    bad B2.3 "archived: archive $i, post $a (want 423), topic PATCH $b (want 423), unarchive $j, post after $c (want 201)"
  fi

  # B2.4 history paging: newest first, opaque cursors, every message once, cross-node, stable under new posts, default 50
  new_channel "$ot" $w false; h=$CHAN_ID; m=()
  for k in 1 2 3 4 5 6 7; do post "$ot" $h "page msg $k" $([ $((k%2)) = 0 ] && echo $N1 || echo $N0); m+=("$MSG_ID"); done
  a=$(call GET "$N0/api/channels/$h/messages?limit=3" "" "$ot"); ids=$(val "$IDS" messages); cur=$(field next_cursor)
  want="${m[6]},${m[5]},${m[4]}"
  b=$(call GET "$N1/api/channels/$h/messages?limit=3&cursor=$(enc "$cur")" "" "$ot"); ids="$ids|$(val "$IDS" messages)"; cur=$(field next_cursor)
  want="$want|${m[3]},${m[2]},${m[1]}"
  post "$ot" $h "arrives later" $N2; k=$MSG_ID
  c=$(call GET "$N2/api/channels/$h/messages?limit=3&cursor=$(enc "$cur")" "" "$ot"); ids="$ids|$(val "$IDS" messages)"; e=$(field next_cursor)
  want="$want|${m[0]}"
  f=$(call GET "$N0/api/channels/$h/messages" "" "$ot"); i=$(val "$IDS" messages)
  for n in $(seq 1 43); do
    post "$ot" $h "bulk $n" $N0; done
  call GET "$N1/api/channels/$h/messages" "" "$ot" >/dev/null; j=$(has 'len(d["messages"])==50 and isinstance(d["next_cursor"],str) and d["next_cursor"]!=""')
  if [ "$a$b$c$f" = 200200200200 ] && [ "$ids" = "$want" ] && [ "$e" = null ] && [ "$i" = "$k,${m[6]},${m[5]},${m[4]},${m[3]},${m[2]},${m[1]},${m[0]}" ] && [ "$j" = yes ]; then
    ok B2.4 "history: newest first; cursors page every message once on any node, unaffected by new posts; default limit 50"
  else
    bad B2.4 "history: pages $a/$b/$c gave '$ids' (want '$want', pages fetched from 8000/8001/8002 with a new message posted in between), last next_cursor '$e' (want null); no limit $f gave '$i'; 51 messages without limit give 50 and a next_cursor: $j"
  fi

  # B2.5 paging validation: bad limits 400, 200 ok; bad cursor 400; private non-member 403
  a="$(call GET "$N0/api/channels/$h/messages?limit=0" "" "$ot") $(call GET "$N0/api/channels/$h/messages?limit=201" "" "$ot") $(call GET "$N1/api/channels/$h/messages?limit=-1" "" "$ot") $(call GET "$N2/api/channels/$h/messages?limit=abc" "" "$ot")"
  b=$(call GET "$N0/api/channels/$h/messages?limit=200" "" "$ot")
  c=$(call GET "$N0/api/channels/$h/messages?cursor=not-a-real-cursor" "" "$ot")
  e=$(call GET "$N1/api/channels/$p/messages" "" "$mt")
  if [ "$a" = "400 400 400 400" ] && [ "$b" = 200 ] && [ "$c" = 400 ] && [ "$e" = 403 ]; then
    ok B2.5 "paging validation: limit 0/201/-1/abc 400, 200 ok; malformed cursor 400; private non-member 403"
  else
    bad B2.5 "validation: limit 0/201/-1/abc '$a' (want 400 each), limit 200 $b (want 200), bad cursor $c (want 400), private non-member read $e (want 403)"
  fi

  # B2.6 threads: replies carry parent_id, raise reply_count; replies endpoint oldest first and paged
  new_channel "$ot" $w false; t=$CHAN_ID; join "$mt" $t >/dev/null
  post "$ot" $t "thread parent" $N0; par=$MSG_ID
  post "$mt" $t "reply one" $N1 "$par"; a=$ST; r1=$MSG_ID; s=$(field message.parent_id)
  post "$mt" $t "reply two" $N2 "$par"; r2=$MSG_ID
  post "$ot" $t "reply three" $N0 "$par"; r3=$MSG_ID
  call GET "$N1/api/channels/$t/messages" "" "$ot" >/dev/null; b=$(has 'any(str(m["id"])==a[0] and m["reply_count"]==3 for m in d["messages"])' "$par")
  c=$(call GET "$N2/api/messages/$par/replies?limit=2" "" "$mt"); ids=$(val "$IDS" replies); cur=$(field next_cursor)
  e=$(call GET "$N0/api/messages/$par/replies?limit=2&cursor=$(enc "$cur")" "" "$mt"); ids="$ids|$(val "$IDS" replies)"; f=$(field next_cursor)
  i=$(call GET "$N0/api/messages/999999999/replies" "" "$mt")
  if [ "$a" = 201 ] && [ "$s" = "$par" ] && [ "$b" = yes ] && [ "$c" = 200 ] && [ "$e" = 200 ] && [ "$ids" = "$r1,$r2|$r3" ] && [ "$f" = null ] && [ "$i" = 404 ]; then
    ok B2.6 "threads: replies 201 with parent_id; parent reply_count counts them; replies oldest first and paged; unknown parent 404"
  else
    bad B2.6 "threads: reply $a (parent_id '$s', want $par), parent shows reply_count 3: $b; replies pages $c/$e gave '$ids' (want '$r1,$r2|$r3'), last next_cursor '$f' (want null); unknown parent $i (want 404)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_B_2; finish; }
