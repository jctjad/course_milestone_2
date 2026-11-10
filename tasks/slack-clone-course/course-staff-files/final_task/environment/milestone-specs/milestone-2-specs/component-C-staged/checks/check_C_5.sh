#!/usr/bin/env bash
# Self-check for C.5 (profiles). Prints PASS/FAIL per rule in /spec/component-C-staged/C-5-profiles.md.
# Usage: bash /spec/component-C-staged/checks/check_C_5.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

FIELDS='all(k in d["user"] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")) and not any("password" in k for k in d["user"])'
PROFILE='{"display_name":"Check Person","timezone":"America/New_York","status_text":"in a meeting","status_emoji":":calendar:","avatar_url":"https://example.com/a.png"}'
HAS_PROFILE='all(d["user"].get(k)==v for k, v in {"display_name":"Check Person","timezone":"America/New_York","status_text":a[0],"status_emoji":":calendar:","avatar_url":"https://example.com/a.png"}.items())'

rules_C_5() {
  local all="C5.1 C5.2 C5.3 C5.4" ut uid vt nm a b c e f h s1 s2 s3 s4 p1 p2

  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ut=$TOKEN; uid=$USER_ID
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  vt=$TOKEN

  # C5.1 GET /api/users/{id}: 200 {user} with every profile field on another node; unknown 404
  a=$(call GET $N1/api/users/$uid "" "$vt"); s1=$(has "$FIELDS"' and str(d["user"]["id"])==a[0]' "$uid")
  b=$(call GET $N2/api/users/999999999 "" "$vt")
  if [ "$a" = 200 ] && [ "$s1" = yes ] && [ "$b" = 404 ]; then
    ok C5.1 "GET user: 200 {user} with every profile field and no password, on another node; unknown 404"
  else
    bad C5.1 "GET user: got $a on 8001 (right id, all UserObj fields, no password: $s1, want 200 yes); unknown id $b (want 404)"
  fi

  # C5.2 PATCH /api/users/me: 200 with the new values; stored on every node; unsent fields keep their values
  a=$(call PATCH $N0/api/users/me "$PROFILE" "$ut"); s1=$(has "$HAS_PROFILE" "in a meeting")
  b=$(call GET $N2/api/users/$uid "" "$vt"); s2=$(has "$HAS_PROFILE" "in a meeting")
  c=$(call PATCH $N1/api/users/me '{"status_text":"lunch"}' "$ut"); s3=$(has "$HAS_PROFILE" "lunch")
  e=$(call GET $N2/api/auth/me "" "$ut"); s4=$(has "$HAS_PROFILE" "lunch")
  f=$(call GET $N0/api/users/$uid); h=$(call PATCH $N1/api/users/me '{"status_text":"no token"}')
  if [ "$a" = 200 ] && [ "$s1" = yes ] && [ "$b" = 200 ] && [ "$s2" = yes ] && [ "$c" = 200 ] && [ "$s3" = yes ] && [ "$e" = 200 ] && [ "$s4" = yes ] \
     && [ "$f" = 401 ] && [ "$h" = 401 ]; then
    ok C5.2 "PATCH me: 200 with all five fields; stored on every node; a partial PATCH keeps the other fields; no token 401"
  else
    bad C5.2 "PATCH me: all fields $a ($s1); seen by another user on 8002 $b ($s2); status_text only $c (others unchanged: $s3); /auth/me on 8002 $e ($s4); want 200 yes each; no token GET user $f, PATCH me $h (want 401 each)"
  fi

  # C5.3 validation: invalid timezone 400 and nothing from that request is stored
  a=$(call PATCH $N2/api/users/me '{"timezone":"Mars/Olympus_Mons","status_text":"should not stick"}' "$ut")
  c=$(call PATCH $N0/api/users/me '{"display_name":"","status_text":"should not stick"}' "$ut")
  e=$(call PATCH $N1/api/users/me "{\"status_text\":\"$(printf 'x%.0s' $(seq 1 1000))\"}" "$ut")
  b=$(call GET $N0/api/users/$uid "" "$vt"); s1=$(has "$HAS_PROFILE" "lunch")
  if [ "$a" = 400 ] && [ "$c" = 400 ] && [ "$e" = 400 ] && [ "$b" = 200 ] && [ "$s1" = yes ]; then
    ok C5.3 "PATCH me: invalid timezone, empty display_name, 1000-char status_text all 400; nothing in those requests is stored"
  else
    bad C5.3 "validation: timezone Mars/Olympus_Mons $a, empty display_name $c, 1000-character status_text $e (want 400 each); afterwards profile unchanged (timezone America/New_York, status_text lunch): $b $s1"
  fi

  # C5.4 user.updated: every open WebSocket on every node gets it within 3s, without channel_id / event_id
  nm="Renamed $RANDOM"
  if watch_ws "$vt" $W1 6 $TMPC.wsv; then p1=$WPID; else p1=""; fi
  if watch_ws "$ut" $W2 6 $TMPC.wsu; then p2=$WPID; else p2=""; fi
  a=$(call PATCH $N0/api/users/me "{\"display_name\":\"$nm\"}" "$ut")
  s1=no
  for _ in $(seq 1 30); do
    s1=$(python3 - "$uid" "$nm" $TMPC.wsv $TMPC.wsu <<'PY'
import json, sys
uid, nm, files = sys.argv[1], sys.argv[2], sys.argv[3:]
def good(f):
    try:
        for line in open(f):
            x = json.loads(line)
            if x.get("type") == "user.updated":
                u = x.get("user") or {}
                if str(u.get("id")) == uid and u.get("display_name") == nm and "channel_id" not in x and "event_id" not in x:
                    return True
    except Exception:
        pass
    return False
print("yes" if all(good(f) for f in files) else "no")
PY
)
    [ "$s1" = yes ] && break; sleep 0.1
  done
  [ -n "$p1" ] && kill "$p1" 2>/dev/null; [ -n "$p2" ] && kill "$p2" 2>/dev/null; wait $p1 $p2 2>/dev/null
  rm -f $TMPC.wsv $TMPC.wsu
  if [ -n "$p1" ] && [ -n "$p2" ] && [ "$a" = 200 ] && [ "$s1" = yes ]; then
    ok C5.4 "user.updated: unsubscribed WebSockets on 8001 and 8002 get it with the new user, no channel_id or event_id"
  else
    bad C5.4 "user.updated: WebSockets opened on 8001 '${p1:+yes}' and 8002 '${p2:+yes}'; PATCH on 8000 $a (want 200); both got {type: user.updated, user with the new display_name} with no channel_id/event_id within 3s: $s1"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_C_5; finish; }
