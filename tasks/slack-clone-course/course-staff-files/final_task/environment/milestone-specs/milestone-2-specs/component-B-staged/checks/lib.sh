#!/usr/bin/env bash
{ set +xv; } 2>/dev/null
# Shared helpers for the component-B checks. Sourced by check_B_*.sh, not run directly.
# Self-contained on purpose (it does not source component A's lib.sh), so the B folder can be
# moved or combined with other layouts without breaking.
LIBDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
W0=ws://127.0.0.1:8000; W1=ws://127.0.0.1:8001; W2=ws://127.0.0.1:8002
BODY=/tmp/check_B_body.json
rm -f "${BODY%.json}.timeout"
pass=0; fail=0
PW="correct-horse-1"

FEEDBACK=verbose
ok()  { pass=$((pass+1)); [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; return 0; }
bad() { fail=$((fail+1)); [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; return 0; }
setup_fail() { local r; for r in $1; do bad "$r" "setup: $2"; done; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 5 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  local s; s=$(curl "${args[@]}" 2>/dev/null) || true   # status 000 means no answer within 5s
  [ "${s:-000}" = 000 ] && : > "${BODY%.json}.timeout"   # finish() then explains what 000 means
  echo "${s:-000}"
}
# field <dotted.path>  -> prints that field of the last response body, or nothing
field() {
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[k] if not isinstance(v,list) else v[int(k)]
    print(json.dumps(v) if isinstance(v,(dict,list,bool)) or v is None else v)
except Exception: pass' "$BODY" "$1"
}
# has <python-expr> [args...]  -> "yes" if the expression is true for the last response body (d = body, a = args)
has() {
  python3 -c 'import json,re,sys
try:
    d=json.load(open(sys.argv[1]))
    print("yes" if eval(sys.argv[2], {"d": d, "a": sys.argv[3:], "re": re}) else "no")
except Exception: print("no")' "$BODY" "$@"
}
# val <python-expr> [args...]  -> prints the expression's value for the last response body
val() {
  python3 -c 'import json,re,sys
try:
    d=json.load(open(sys.argv[1]))
    print(eval(sys.argv[2], {"d": d, "a": sys.argv[3:], "re": re}))
except Exception: pass' "$BODY" "$@"
}
# enc <text> -> URL-encodes text for a query string (cursors are opaque and may contain + / =)
enc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"; }
jv() { if [[ "$1" =~ ^[0-9]+$ ]]; then echo "$1"; else echo "\"$1\""; fi; }

# start_cluster -> starts /app/start.sh fully detached: the outer redirect matters, because a background
# subshell that keeps this script's stdout open makes the agent's harness wait until its 30s timeout
start_cluster() {
  if command -v setsid >/dev/null 2>&1; then
    (cd /app && exec setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1) </dev/null >/dev/null 2>&1 &
  else
    (cd /app && exec nohup /app/start.sh </dev/null >/tmp/start.log 2>&1) </dev/null >/dev/null 2>&1 &
  fi
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}
# AIDEV-NOTE: total wait is capped at 20s so a whole check fits the agent's 30s command limit.
# listening <port> -> true if something accepts TCP connections on 127.0.0.1:<port>
listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
any_node_listening() { listening 8000 || listening 8001 || listening 8002; }
# AIDEV-NOTE: start /app/start.sh only when no node port is open at all. If any node is up, a start.sh is
# already running (maybe restarting a node), and a second copy would fight it for the ports.
boot() {
  if ! healthy; then
    if ! any_node_listening; then sleep 1; any_node_listening || start_cluster; fi
    for _ in $(seq 1 20); do healthy && break; sleep 1; done
  fi
  healthy || { [ "$FEEDBACK" = verbose ] && echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log; some ports may be open but not answering: restart the cluster yourself, one start.sh only)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }
}
TIMEOUT_NOTE="NOTE status 000 above means a request got no answer within 5 s. Usually a node is stuck: a SQLite connection or transaction left open after an error (roll back and close it in finally), a lock held too long, or a handler that never sends its response. Try that route by hand with curl -m 5."
finish() {
  if [ -e "${BODY%.json}.timeout" ]; then rm -f "${BODY%.json}.timeout"; [ "$FEEDBACK" = verbose ] && echo "$TIMEOUT_NOTE"; fi
  echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
}

# --- setup helpers (they rely on Component A routes; a failure means fix A first) ---
# new_user -> registers a fresh user on node 8000; sets USER, TOKEN and USER_ID
new_user() {
  USER="chk$(date +%s)$RANDOM"
  call POST $N0/api/auth/register "{\"username\":\"$USER\",\"password\":\"$PW\"}" >/dev/null
  TOKEN=$(field token); USER_ID=$(field user.id)
  [ -n "$TOKEN" ] && [ -n "$USER_ID" ]
}
new_slug() { echo "chk-$(date +%s)-$RANDOM"; }
# new_ws <token> -> creates a workspace; sets SLUG, WS_ID and GEN_ID (#general's id)
new_ws() {
  SLUG=$(new_slug)
  local s; s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"Check $SLUG\"}" "$1")
  WS_ID=$(field workspace.id); GEN_ID=$(field general_channel.id)
  [ "$s" = 201 ] && [ -n "$GEN_ID" ]
}
# join <token> <channel-id> [node] -> prints the status
join() { call POST ${3:-$N0}/api/channels/$2/join "" "$1"; }
# new_member <channel-id> -> fresh user who joins that channel (and so the open workspace); sets USER, TOKEN, USER_ID
new_member() { new_user && [ "$(join "$TOKEN" "$1")" = 200 ]; }
set_role() { call PATCH ${5:-$N0}/api/workspaces/$2/members/$3 "{\"role\":\"$4\"}" "$1"; }
# new_channel <token> <slug> <true|false> [node] [extra json fields]  -> sets ST, CHAN_ID and CHAN_NAME.
# Call it directly, not inside $(...), or the variables are lost.
new_channel() {
  CHAN_NAME="c-$(date +%s)-$RANDOM$RANDOM"
  ST=$(call POST ${4:-$N0}/api/workspaces/$2/channels "{\"name\":\"$CHAN_NAME\",\"is_private\":$3${5:+,$5}}" "$1")
  CHAN_ID=$(field channel.id)
}
# post <token> <channel-id> <text> [node] [parent-id] -> sets ST and MSG_ID. Text must not contain " or \.
post() {
  local p=""; [ -n "${5:-}" ] && p=",\"parent_id\":$(jv "$5")"
  ST=$(call POST ${4:-$N0}/api/channels/$2/messages "{\"body\":\"$3\"$p}" "$1")
  MSG_ID=$(field message.id)
}
# ws <args...> -> runs the WebSocket helper (see ws.py)
ws() { python3 "$LIBDIR/ws.py" "$@"; }
# head_of <token> <channel-id> [ws-base] -> prints the channel's head_event_id (via subscribe)
head_of() { ws head "${3:-$W0}/api/ws?token=$1" "$2"; }
