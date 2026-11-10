#!/usr/bin/env bash
# Self-check for B.1 (channels). Prints PASS/FAIL per rule in /spec/component-B-staged/B-1-channels.md.
# Usage: bash /spec/component-B-staged/checks/check_B_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CHAN_FIELDS='all(k in c for k in ("id","workspace_id","name","is_private","is_dm","topic","is_archived"))'
USER_FIELDS='all(k in u for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji"))'

rules_B_1() {
  local all="B1.1 B1.2 B1.3 B1.4 B1.5 B1.6" ot oid w wid g w2 at aid mt mid xt c1 n1 p s a b c d e f h i j k

  # setup (Component A): owner O with workspace w; AD admin, M member (both joined #general); X outsider
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN; oid=$USER_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; wid=$WS_ID; g=$GEN_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w2=$SLUG
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] || { setup_fail "$all" "could not add an admin, fix A.1 and A.2 first"; return; }
  at=$TOKEN; aid=$USER_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  mt=$TOKEN; mid=$USER_ID
  new_user || { setup_fail "$all" "could not register a user"; return; }
  xt=$TOKEN

  # B1.1 channel object: all ChannelObj fields; defaults; topic kept; description ignored; general_channel too
  new_channel "$mt" $w false $N1 '"topic":"hello topic"'; s=$ST; c1=$CHAN_ID; n1=$CHAN_NAME
  a=$(has "(lambda c: $CHAN_FIELDS and c['is_dm'] is False and c['is_archived'] is False and c['is_private'] is False and c['topic']=='hello topic' and c['name']==a[0] and str(c['workspace_id'])==a[1])(d['channel'])" "$n1" "$wid")
  new_channel "$mt" $w false $N2 '"description":"not a topic"'; b=$ST
  c=$(has "(lambda c: $CHAN_FIELDS and c['topic']=='')(d['channel'])")
  d=$(call POST $N0/api/workspaces "{\"slug\":\"$(new_slug)\",\"name\":\"Fields\"}" "$ot")
  e=$(has "(lambda c: $CHAN_FIELDS and c['is_dm'] is False and c['is_archived'] is False and c['topic']=='')(d['general_channel'])")
  if [ "$s" = 201 ] && [ "$a" = yes ] && [ "$b" = 201 ] && [ "$c" = yes ] && [ "$d" = 201 ] && [ "$e" = yes ]; then
    ok B1.1 "create channel: 201 with every ChannelObj field; topic kept, defaults to \"\"; description ignored"
  else
    bad B1.1 "channel object: create with topic $s (all fields, is_dm/is_archived false, topic kept: $a), with description $b (topic stays \"\": $c), new workspace $d (general_channel has all fields: $e); want 201/yes each"
  fi
  [ -n "$c1" ] || { setup_fail "B1.2 B1.3 B1.4 B1.5 B1.6" "could not create a channel, fix B1.1 first"; return; }

  # B1.2 validation: bad or long name 400; topic > 250 is 400, 250 ok; duplicate 409; same name elsewhere ok
  a=$(call POST $N0/api/workspaces/$w/channels '{"name":"Bad Name"}' "$ot")
  b=$(call POST $N0/api/workspaces/$w/channels '{"name":"bad_name"}' "$ot")
  c=$(call POST $N0/api/workspaces/$w/channels "{\"name\":\"$(printf 'a%.0s' $(seq 1 33))\"}" "$ot")
  d=$(call POST $N1/api/workspaces/$w/channels "{\"name\":\"t-$RANDOM$RANDOM\",\"topic\":\"$(printf 'x%.0s' $(seq 1 251))\"}" "$ot")
  e=$(call POST $N1/api/workspaces/$w/channels "{\"name\":\"t-$RANDOM$RANDOM\",\"topic\":\"$(printf 'x%.0s' $(seq 1 250))\"}" "$ot")
  f=$(call POST $N2/api/workspaces/$w/channels "{\"name\":\"$n1\"}" "$ot")
  h=$(call POST $N2/api/workspaces/$w2/channels "{\"name\":\"$n1\"}" "$ot")
  if [ "$a" = 400 ] && [ "$b" = 400 ] && [ "$c" = 400 ] && [ "$d" = 400 ] && [ "$e" = 201 ] && [ "$f" = 409 ] && [ "$h" = 201 ]; then
    ok B1.2 "channel validation: bad or long name 400; topic over 250 400; duplicate 409; same name in another workspace 201"
  else
    bad B1.2 "validation: 'Bad Name' $a, 'bad_name' $b, 33 chars $c, topic 251 $d (want 400 each); topic 250 $e (want 201); duplicate name $f (want 409); same name in another workspace $h (want 201)"
  fi

  # B1.3 workspace detail: {workspace, channels, read_state}; private channels only for their members
  new_channel "$at" $w true; p=$CHAN_ID
  a=$(call GET $N1/api/workspaces/$w "" "$mt")
  b=$(has 'isinstance(d["channels"],list) and isinstance(d["read_state"],list) and str(d["workspace"]["slug"])==a[0] and {a[1],a[2]} <= {str(c["id"]) for c in d["channels"]} and a[3] not in {str(c["id"]) for c in d["channels"]}' "$w" "$g" "$c1" "$p")
  c=$(call GET $N2/api/workspaces/$w "" "$at"); e=$(has 'a[0] in {str(c["id"]) for c in d["channels"]}' "$p")
  f=$(call GET $N0/api/workspaces/$w "" "$xt")
  h=$(call GET $N0/api/workspaces/no-such-ws-$RANDOM "" "$ot")
  if [ -n "$p" ] && [ "$a" = 200 ] && [ "$b" = yes ] && [ "$c" = 200 ] && [ "$e" = yes ] && [ "$f" = 403 ] && [ "$h" = 404 ]; then
    ok B1.3 "workspace detail: workspace, channels, read_state; private channels only for members; outsider 403; unknown 404"
  else
    bad B1.3 "detail: member $a (shape right, public channels listed, private '$p' left out: $b), private channel's member $c (listed: $e) (want 200/yes); outsider $f (want 403); unknown slug $h (want 404)"
  fi

  # B1.4 topic and leave: member or owner may PATCH, others 403; long topic 400; leave idempotent; rejoin
  a=$(call PATCH $N0/api/channels/$c1 '{"topic":"by member"}' "$mt"); i=$(field channel.topic)
  b=$(call PATCH $N1/api/channels/$c1 '{"topic":"by owner"}' "$ot"); j=$(field channel.topic)
  c=$(call PATCH $N2/api/channels/$c1 '{"topic":"by admin"}' "$at")
  d=$(call PATCH $N0/api/channels/$c1 "{\"topic\":\"$(printf 'x%.0s' $(seq 1 251))\"}" "$mt")
  k=$(call PATCH $N0/api/channels/999999999 '{"topic":"x"}' "$ot")
  e=$(join "$at" $c1 $N1)
  f=$(call POST $N2/api/channels/$c1/leave "" "$at"); h=$(field channel.id)
  s="$(call POST $N0/api/channels/$c1/leave "" "$at") $(join "$at" $c1 $N1)"
  if [ "$a" = 200 ] && [ "$i" = "by member" ] && [ "$b" = 200 ] && [ "$j" = "by owner" ] && [ "$c" = 403 ] && [ "$d" = 400 ] && [ "$k" = 404 ] \
     && [ "$e" = 200 ] && [ "$f" = 200 ] && [ "$h" = "$c1" ] && [ "$s" = "200 200" ]; then
    ok B1.4 "topic: member and owner 200, non-member admin 403, long topic 400; leave 200 {channel}, again 200, rejoin 200"
  else
    bad B1.4 "topic: member $a ('$i'), owner $b ('$j') (want 200), non-member admin $c (want 403), 251 chars $d (want 400), unknown channel $k (want 404); join $e, leave $f (channel.id '$h') (want 200, $c1), leave again + rejoin '$s' (want '200 200')"
  fi

  # B1.5 channel members: UserObj list; creator of workspace in #general; joins add, leaves remove
  a=$(call GET $N1/api/channels/$g/members "" "$mt")
  b=$(has "all((lambda u: $USER_FIELDS)(u) for u in d['members']) and set(a) <= {str(u['id']) for u in d['members']}" "$oid" "$aid" "$mid")
  c=$(call GET $N2/api/channels/$c1/members "" "$mt"); e=$(has 'set(a) <= {str(u["id"]) for u in d["members"]}' "$mid" "$aid")
  call POST $N0/api/channels/$c1/leave "" "$at" >/dev/null
  f=$(call GET $N0/api/channels/$c1/members "" "$mt"); h=$(has 'a[0] not in {str(u["id"]) for u in d["members"]} and a[1] in {str(u["id"]) for u in d["members"]}' "$aid" "$mid")
  i=$(call GET $N0/api/channels/$c1/members); j=$(call GET $N0/api/channels/999999999/members "" "$mt")
  if [ "$a" = 200 ] && [ "$b" = yes ] && [ "$c" = 200 ] && [ "$e" = yes ] && [ "$f" = 200 ] && [ "$h" = yes ] && [ "$i" = 401 ] && [ "$j" = 404 ]; then
    ok B1.5 "channel members: UserObj list; workspace creator in #general; creator and joiners listed; leavers removed"
  else
    bad B1.5 "members: #general $a (every UserObj field, owner/admin/member listed: $b), channel $c (creator and joiner listed: $e), after leave $f (leaver gone: $h) (want 200/yes); no token $i (want 401); unknown channel $j (want 404)"
  fi

  # B1.6 archive: owner only; hidden from detail unless include_archived=1 or true; unarchive restores
  a="$(call POST $N0/api/channels/$c1/archive "" "$at") $(call POST $N0/api/channels/$c1/archive "" "$mt")"
  b=$(call POST $N1/api/channels/$c1/archive "" "$ot"); i=$(field channel.is_archived)
  c=$(call GET $N2/api/workspaces/$w "" "$ot"); e=$(has 'a[0] not in {str(c["id"]) for c in d["channels"]}' "$c1")
  call GET "$N0/api/workspaces/$w?include_archived=1" "" "$ot" >/dev/null; f=$(has 'any(str(c["id"])==a[0] and c["is_archived"] is True for c in d["channels"])' "$c1")
  call GET "$N1/api/workspaces/$w?include_archived=true" "" "$ot" >/dev/null; h=$(has 'a[0] in {str(c["id"]) for c in d["channels"]}' "$c1")
  j=$(call POST $N2/api/channels/$c1/unarchive "" "$at")
  k=$(call POST $N2/api/channels/$c1/unarchive "" "$ot"); s=$(field channel.is_archived)
  call GET $N0/api/workspaces/$w "" "$ot" >/dev/null; d=$(has 'a[0] in {str(c["id"]) for c in d["channels"]}' "$c1")
  if [ "$a" = "403 403" ] && [ "$b" = 200 ] && [ "$i" = true ] && [ "$c" = 200 ] && [ "$e" = yes ] && [ "$f" = yes ] && [ "$h" = yes ] \
     && [ "$j" = 403 ] && [ "$k" = 200 ] && [ "$s" = false ] && [ "$d" = yes ]; then
    ok B1.6 "archive/unarchive: owner only; archived hidden unless include_archived=1 or true; unarchive restores"
  else
    bad B1.6 "archive: by admin/member '$a' (want '403 403'), by owner $b (is_archived '$i'); hidden from detail $c/$e, shown with =1: $f, =true: $h; unarchive by admin $j (want 403), by owner $k (is_archived '$s'), back in detail: $d"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_B_1; finish; }
