#!/usr/bin/env bash
{ set +xv; } 2>/dev/null
# Shared helpers for the component-A checks. Sourced by check_A_*.sh, not run directly.
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_A_body.json
rm -f "${BODY%.json}.timeout"
pass=0; fail=0
PW="correct-horse-1"

FEEDBACK=verbose
ok()  { pass=$((pass+1)); [ "$FEEDBACK" = verbose ] && echo "PASS $1 $2"; return 0; }
bad() { fail=$((fail+1)); [ "$FEEDBACK" = verbose ] && echo "FAIL $1 $2"; return 0; }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }

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
    for k in sys.argv[2].split("."): v=v[k]
    print(v)
except Exception: pass' "$BODY" "$1"
}
# has <python-expr> [args...]  -> prints "yes" if the expression is true for the last response body.
# Inside the expression, d is the parsed body and a is the list of extra args (as strings).
# Example: has 'any(str(w["slug"])==a[0] for w in d["workspaces"])' "$slug"
has() {
  python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print("yes" if eval(sys.argv[2], {"d": d, "a": sys.argv[3:]}) else "no")
except Exception: print("no")' "$BODY" "$@"
}
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
# new_user -> registers a fresh user on node 8000; sets USER, TOKEN and USER_ID, fails if register did not work
new_user() {
  USER="chk$(date +%s)$RANDOM"
  call POST $N0/api/auth/register "{\"username\":\"$USER\",\"password\":\"$PW\"}" >/dev/null
  TOKEN=$(field token); USER_ID=$(field user.id)
  [ -n "$TOKEN" ] && [ -n "$USER_ID" ]
}
# new_slug -> prints a fresh workspace slug that matches ^[a-z0-9-]{2,32}$
new_slug() { echo "chk-$(date +%s)-$RANDOM"; }

# val <python-expr> [args...]  -> like has, but prints the expression's value (or nothing on error)
val() {
  python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(eval(sys.argv[2], {"d": d, "a": sys.argv[3:]}))
except Exception: pass' "$BODY" "$@"
}
# jv <id>  -> prints an id as a JSON value: bare if it is all digits, quoted otherwise
jv() { if [[ "$1" =~ ^[0-9]+$ ]]; then echo "$1"; else echo "\"$1\""; fi; }
# is2xx <status>  -> true for any 2xx status
is2xx() { [[ "$1" =~ ^2[0-9][0-9]$ ]]; }
# setup_fail "<rules>" <message>  -> marks every listed rule FAIL because its setup did not work
setup_fail() { local r; for r in $1; do bad "$r" "setup: $2"; done; }

# new_ws <token>  -> creates a fresh workspace on node 8000; sets SLUG, WS_ID and GEN_ID (#general's id)
new_ws() {
  SLUG=$(new_slug)
  local s; s=$(call POST $N0/api/workspaces "{\"slug\":\"$SLUG\",\"name\":\"Check $SLUG\"}" "$1")
  WS_ID=$(field workspace.id); GEN_ID=$(field general_channel.id)
  [ "$s" = 201 ] && [ -n "$GEN_ID" ]
}
# new_member <channel-id>  -> registers a fresh user who joins that channel of an open workspace,
# which makes them a workspace member (A.1 rule 1.7); sets USER, TOKEN and USER_ID
new_member() { new_user && [ "$(call POST $N0/api/channels/$1/join "" "$TOKEN")" = 200 ]; }
# set_role <token> <slug> <user-id> <role> [node]  -> prints the status of the PATCH
set_role() { call PATCH ${5:-$N0}/api/workspaces/$2/members/$3 "{\"role\":\"$4\"}" "$1"; }
# role_of <token> <slug> <user-id> [node]  -> prints that user's role from the members list, or nothing
role_of() {
  call GET ${4:-$N0}/api/workspaces/$2/members "" "$1" >/dev/null
  val 'next((m["role"] for m in d["members"] if str(m["user_id"])==a[0]), "")' "$3"
}
# new_channel <token> <slug> <true|false> [node]  -> creates a channel; sets ST (status) and CHAN_ID.
# Call it directly, not inside $(...), or ST and CHAN_ID are lost.
new_channel() {
  ST=$(call POST ${4:-$N0}/api/workspaces/$2/channels "{\"name\":\"chk-$RANDOM$RANDOM\",\"is_private\":$3}" "$1")
  CHAN_ID=$(field channel.id)
}
# new_invite <token> <slug> [json-body] [node]  -> creates an invitation; sets ST (status), CODE and MAX_USES.
# Call it directly, not inside $(...), or the variables are lost.
new_invite() {
  local b='{}'; [ -n "${3:-}" ] && b=$3
  ST=$(call POST ${4:-$N0}/api/workspaces/$2/invitations "$b" "$1")
  CODE=$(field invitation.code); MAX_USES=$(field invitation.max_uses)
}
TIMEOUT_NOTE="NOTE status 000 above means a request got no answer within 5 s. Usually a node is stuck: a SQLite connection or transaction left open after an error (roll back and close it in finally), a lock held too long, or a handler that never sends its response. Try that route by hand with curl -m 5."
finish() {
  if [ -e "${BODY%.json}.timeout" ]; then rm -f "${BODY%.json}.timeout"; [ "$FEEDBACK" = verbose ] && echo "$TIMEOUT_NOTE"; fi
  echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
}
# pids_on_port <port>  -> prints the PIDs listening on that TCP port, one per line (never by process name)
pids_on_port() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | sort -u
  else
    python3 - "$1" <<'PY2'
import os,sys
port=int(sys.argv[1]); inodes=set()
for f in ("/proc/net/tcp","/proc/net/tcp6"):
    try:
        for line in open(f).readlines()[1:]:
            p=line.split()
            if p[3]=="0A" and int(p[1].rsplit(":",1)[1],16)==port: inodes.add(p[9])
    except Exception: pass
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            l=os.readlink(f"/proc/{pid}/fd/{fd}")
            if l.startswith("socket:[") and l[8:-1] in inodes: print(pid); break
    except Exception: pass
PY2
  fi
}
# kill_port <port>  -> SIGKILLs every process listening on that port (never this shell)
kill_port() { local p; for p in $(pids_on_port "$1"); do [ "$p" != "$$" ] && kill -9 "$p" 2>/dev/null; done; return 0; }
# restart_cluster -> stops the three nodes by port PID and starts /app/start.sh again (waits up to 15s)
restart_cluster() {
  kill_port 8000; kill_port 8001; kill_port 8002; sleep 1
  # a supervising start.sh brings the nodes back by itself; start another copy only if none comes back
  for _ in $(seq 1 10); do any_node_listening && break; sleep 0.3; done
  any_node_listening || start_cluster
  for _ in $(seq 1 15); do healthy && return 0; sleep 1; done
  return 1
}

