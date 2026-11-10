#!/usr/bin/env bash
{ set +xv; } 2>/dev/null
# Shared helpers for the component-D checks. Sourced by check_D_*.sh, not run directly.
# Self-contained on purpose (it does not source the A or B lib.sh), so the D folder can be
# moved or combined with other layouts without breaking.
LIBDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
W0=ws://127.0.0.1:8000; W1=ws://127.0.0.1:8001; W2=ws://127.0.0.1:8002
BODY=/tmp/check_D_body.json
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
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 5s
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
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }

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

# --- component C helpers ---
TMPC=/tmp/check_D
# upload <token> <path> <filename> <content-type> [node] -> multipart upload; sets ST and FILE_ID
upload() {
  ST=$(curl -s -o "$BODY" -w '%{http_code}' --max-time 10 -X POST "${5:-$N0}/api/files" \
    -H "Authorization: Bearer $1" -F "file=@$2;filename=$3;type=$4" 2>/dev/null || true)
  FILE_ID=$(field file.id)
}
# dm <token> <user-id> [node] -> POST /api/dms; sets ST and DM_ID
dm() { ST=$(call POST ${3:-$N0}/api/dms "{\"user_id\":$(jv "$2")}" "$1"); DM_ID=$(field channel_id); }
# react <token> <message-id> <emoji> [node] -> prints the status of POST .../reactions
react() { call POST ${4:-$N0}/api/messages/$2/reactions "{\"emoji\":\"$3\"}" "$1"; }
# unreact <token> <message-id> <emoji> [node] -> prints the status of DELETE .../reactions/{emoji}
unreact() { call DELETE ${4:-$N0}/api/messages/$2/reactions/$(enc "$3") "" "$1"; }
# post_json <token> <channel-id> <json-body> [node] -> sets ST and MSG_ID (for bodies with file_ids etc.)
post_json() { ST=$(call POST ${4:-$N0}/api/channels/$2/messages "$3" "$1"); MSG_ID=$(field message.id); }
# ids_json <id>... -> prints a JSON list of ids, e.g. [3,7] or ["a","b"]
ids_json() { local out="" i; for i in "$@"; do out="$out${out:+,}$(jv "$i")"; done; echo "[$out]"; }
# watch_ws <token> <ws-base> <seconds> <out-file> -> keeps a WebSocket open in the background, writing
# every frame to out-file; waits (up to 3s) until it is open. Sets WPID.
watch_ws() {
  rm -f "$4"
  python3 "$LIBDIR/ws.py" watch "$2/api/ws?token=$1" "$3" "$4" & WPID=$!   # python itself, so kill closes the socket
  for _ in $(seq 1 30); do grep -q '"_ws": "open"' "$4" 2>/dev/null && return 0; sleep 0.1; done
  return 1
}

# --- WebSocket event helpers (same as check_B_5.sh) ---
WSF=/tmp/check_D_ws
# listen_all <token> <channel> <count> <seconds> -> one listener per node in the background; waits until
# each has its "subscribed" frame (up to 3s). Sets LPIDS.
listen_all() {
  local i; LPIDS=()
  for i in 0 1 2; do
    rm -f $WSF$i.jsonl
    python3 "$LIBDIR/ws.py" listen "ws://127.0.0.1:800$i/api/ws?token=$1" "$2" "$3" "$4" $WSF$i.jsonl & LPIDS+=($!)
  done
  for _ in $(seq 1 30); do
    [ "$(grep -l '"subscribed"' ${WSF}{0,1,2}.jsonl 2>/dev/null | wc -l)" -eq 3 ] && return
    sleep 0.1
  done
}
# verdict <channel> <kinds,comma,separated> -> "yes" if every node saw subscribed(head) then exactly those
# kinds with event_id head+1, head+2, ... and the right channel_id; otherwise a short description
verdict() {
  python3 - "$1" "$2" ${WSF}{0,1,2}.jsonl <<'PY'
import json, sys
cid, kinds, files = sys.argv[1], sys.argv[2].split(","), sys.argv[3:]
seen, problems = [], []
for n, f in enumerate(files):
    try:
        frames = [json.loads(l) for l in open(f) if l.strip()]
    except Exception:
        frames = []
    sub = [x for x in frames if x.get("type") == "subscribed"]
    ev = [x for x in frames if x.get("type") not in ("subscribed", "resumed", "resume.gap", "error")]
    if not sub:
        problems.append(f"node {n}: no subscribed frame"); continue
    h = sub[0].get("head_event_id")
    got = [(x.get("type"), x.get("event_id"), str(x.get("channel_id"))) for x in ev]
    want = [(k, h + i + 1, cid) for i, k in enumerate(kinds)] if isinstance(h, int) else None
    seen.append([(t, e) for t, e, _ in got])
    if got != want:
        problems.append(f"node {n}: head {h}, got {[(t, e) for t, e, _ in got]}")
if not problems and any(s != seen[0] for s in seen):
    problems.append("nodes delivered different sequences")
print("yes" if not problems else "; ".join(problems)[:600])
PY
}

# --- port helpers (same as component A lib.sh) ---
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

# --- component D helpers ---
# a killed node or Redis must be back within this many seconds (D2.4, D2.5, D3.3; the grader allows 60 s,
# but a check must finish in 30 s)
RESPAWN_S=5
# redis_ping -> prints PONG if 127.0.0.1:6379 answers PING, otherwise a short reason
redis_ping() {
  python3 -c 'import socket
try:
    s=socket.create_connection(("127.0.0.1",6379),timeout=1); s.sendall(b"*1\r\n$4\r\nPING\r\n")
    print("PONG" if s.recv(64).startswith(b"+PONG") else "no PONG")
except Exception as e: print("unreachable")'
}
# redis_numsub -> prints the subscriber count of huddle:events (PUBSUB NUMSUB), or -1 if Redis is unreachable
redis_numsub() {
  python3 -c 'import socket,re
try:
    s=socket.create_connection(("127.0.0.1",6379),timeout=1)
    s.sendall(b"*3\r\n$6\r\nPUBSUB\r\n$6\r\nNUMSUB\r\n$13\r\nhuddle:events\r\n")
    m=re.search(rb":(\d+)\r\n", s.recv(256)); print(int(m.group(1)) if m else -1)
except Exception: print(-1)'
}
numsub_ok() { [ "$(redis_numsub)" -ge 3 ]; }
# port_owner <port> -> prints the process name listening on that port (from ss), or nothing
port_owner() { ss -ltnp "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2; }
# wait_health <node-url> <seconds> -> true once /api/health answers 200 (checks every 0.2 s)
wait_health() {
  local i; for i in $(seq 1 $(( $2 * 5 ))); do curl -fs --max-time 1 "$1/api/health" >/dev/null 2>&1 && return 0; sleep 0.2; done; return 1
}
# wait_redis <seconds> -> true once Redis answers PING (checks every 0.2 s)
wait_redis() { local i; for i in $(seq 1 $(( $1 * 5 ))); do [ "$(redis_ping)" = PONG ] && return 0; sleep 0.2; done; return 1; }
# listen_ports <token> <channel> <count> <seconds> <port>... -> one listener per given port in the background,
# writing frames to $WSF<port>.jsonl; waits (up to 3 s) for every "subscribed". Sets LPIDS.
listen_ports() {
  local t=$1 ch=$2 n=$3 secs=$4 p; shift 4; LPIDS=(); LPORTS=("$@")
  for p in "$@"; do
    rm -f $WSF$p.jsonl
    python3 "$LIBDIR/ws.py" listen "ws://127.0.0.1:$p/api/ws?token=$t" "$ch" "$n" "$secs" $WSF$p.jsonl & LPIDS+=($!)
  done
  for _ in $(seq 1 30); do
    local got=0; for p in "$@"; do grep -q '"subscribed"' $WSF$p.jsonl 2>/dev/null && got=$((got+1)); done
    [ "$got" -eq $# ] && return; sleep 0.1
  done
}
# verdict_ports <channel> <kinds,comma,separated> [prefix] -> like verdict, for the ports given to listen_ports;
# with "prefix", only the first len(kinds) events are compared (later events are ignored)
verdict_ports() {
  local files=() p; for p in "${LPORTS[@]}"; do files+=($WSF$p.jsonl); done
  python3 - "$1" "$2" "${3:-all}" "${files[@]}" <<'PY'
import json, sys
cid, kinds, mode, files = sys.argv[1], sys.argv[2].split(","), sys.argv[3], sys.argv[4:]
seen, problems = [], []
for f in files:
    port = f.rsplit("ws", 1)[-1].split(".")[0]
    try:
        frames = [json.loads(l) for l in open(f) if l.strip()]
    except Exception:
        frames = []
    sub = [x for x in frames if x.get("type") == "subscribed"]
    ev = [x for x in frames if x.get("type") not in ("subscribed", "resumed", "resume.gap", "error")]
    if not sub:
        problems.append(f"port {port}: no subscribed frame"); continue
    h = sub[0].get("head_event_id")
    got = [(x.get("type"), x.get("event_id"), str(x.get("channel_id"))) for x in ev]
    want = [(k, h + i + 1, cid) for i, k in enumerate(kinds)] if isinstance(h, int) else None
    if mode == "prefix":
        got = got[:len(kinds)]
    seen.append([(t, e) for t, e, _ in got])
    if got != want:
        problems.append(f"port {port}: head {h}, got {[(t, e) for t, e, _ in got]}")
if not problems and any(s != seen[0] for s in seen):
    problems.append("ports delivered different sequences")
print("yes" if not problems else "; ".join(problems)[:600])
PY
}
# timed_post <token> <channel> <text> <node> -> prints "status:seconds" for one post (max 3 s)
timed_post() {
  local r; r=$(curl -s -o /dev/null -w '%{http_code}:%{time_total}' --max-time 3 -X POST "$4/api/channels/$2/messages" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $1" -d "{\"body\":\"$3\"}" 2>/dev/null || true)
  echo "${r:-000:3}"
}
# ms_since <start-ns> -> milliseconds since a $(date +%s%N) timestamp
ms_since() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }
# wait_until_ms <start-ns> <limit-s> <command...> -> runs the command every 0.2 s until it succeeds or
# limit-s seconds have passed since start-ns; true if it succeeded in time
wait_until_ms() {
  local t0=$1 lim=$(( $2 * 1000 )); shift 2
  while [ "$(ms_since $t0)" -le "$lim" ]; do "$@" && return 0; sleep 0.2; done; return 1
}
health_ok() { curl -fs --max-time 1 "$1/api/health" >/dev/null 2>&1; }
redis_ok() { [ "$(redis_ping)" = PONG ]; }
