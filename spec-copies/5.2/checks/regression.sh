#!/usr/bin/env bash
# A and B regression suite: reruns every rule of Specs A and B (A0.1 to A4.3, B1.1 to B6.4: 50 rules)
# against the running cluster and prints only what broke, then one total line, for example:
#   REGRESSION FAIL A2.3 <what was expected, what came back>
#   REGRESSION: 1 broken
# Usage: bash /spec/checks/regression.sh      Exit 0 only if nothing broke. Takes up to about 27 seconds.
# Run it as a single command on its own. It kills the process on port 8001 once (A0.4) and puts the
# cluster back. A rule reported as "not checked" was not reached in time: requests were too slow.
set -u
REG_START=$SECONDS
REG_BUDGET=27                       # whole suite, seconds (the agent's command limit is 30)
REG_TMP=$(mktemp -d /tmp/regression.XXXXXX)
trap 'rm -rf "$REG_TMP"' EXIT
cat > "$REG_TMP/ws.py" <<'REG_WS_PY'
#!/usr/bin/env python3
"""Minimal WebSocket client for the component-B checks (standard library only).

Usage:
  ws.py reject URL                              -> prints "rejected" or "accepted"
  ws.py head URL CHANNEL_ID                      -> prints head_event_id from "subscribed"
  ws.py listen URL CHANNEL_ID COUNT SECONDS OUT  -> subscribes, writes every frame to OUT as one JSON
                                                   line, exits after COUNT event frames or SECONDS;
                                                   skips events at or below head_event_id (they
                                                   happened before the subscribe)
  ws.py resume URL CHANNEL_ID SINCE SECONDS      -> sends resume, prints frames until resumed/resume.gap

Not run directly by the agent; the check scripts call it. Errors are printed as one line starting
with "ERROR" so the checks can report them.
"""
import base64, json, os, socket, struct, sys, time
from urllib.parse import urlparse


class Rejected(Exception):
    pass


class Conn:
    def __init__(self, url, timeout=3.0):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        path = u.path + ("?" + u.query if u.query else "")
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Rejected("closed during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101" not in status:
            raise Rejected(status)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout):
        """Returns the next text frame as a dict, or None on timeout. Control frames (ping, pong)
        never extend the wait: the deadline holds even if the server keeps sending pings."""
        end = time.time() + max(timeout, 0.01)
        try:
            while True:
                left = end - time.time()
                if left <= 0:
                    return None
                self.sock.settimeout(left)
                b0, b1 = self._read(2)
                op, n = b0 & 0x0F, b1 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                data = self._read(n)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if op == 0x8:
                    raise EOFError("server closed the connection")
                if op == 0x9:  # ping -> pong
                    self.sock.sendall(struct.pack("!BB", 0x8A, 0x80) + b"\0\0\0\0")
                    continue
                if op in (0x1, 0x0):
                    return json.loads(data.decode())
        except socket.timeout:
            return None


def main(argv):
    cmd, url = argv[1], argv[2]
    try:
        if cmd == "reject":
            try:
                Conn(url)
                print("accepted")
            except (Rejected, ConnectionError, EOFError, socket.timeout):
                print("rejected")
            return
        c = Conn(url)
        cid = json.loads(argv[3]) if argv[3].lstrip("-").isdigit() else argv[3]
        if cmd == "head":
            c.send({"type": "subscribe", "channel_id": cid})
            deadline = time.time() + 3
            while time.time() < deadline:
                f = c.recv(deadline - time.time())
                if f and f.get("type") == "subscribed":
                    print(f.get("head_event_id"))
                    return
            print("ERROR no subscribed frame within 3s")
        elif cmd == "listen":
            count, secs, out = int(argv[4]), float(argv[5]), argv[6]
            with open(out, "w") as fh:
                c.send({"type": "subscribe", "channel_id": cid})
                deadline, got, head = time.time() + secs, 0, None
                while time.time() < deadline and got < count:
                    f = c.recv(deadline - time.time())
                    if f is None:
                        continue
                    if f.get("type") == "subscribed" and head is None:
                        head = f.get("head_event_id")
                    elif isinstance(head, int) and isinstance(f.get("event_id"), int) and f["event_id"] <= head:
                        continue  # happened before the subscribe; already counted in head_event_id
                    fh.write(json.dumps(f) + "\n")
                    fh.flush()
                    if f.get("type") not in ("subscribed", "resumed", "resume.gap", "error"):
                        got += 1
        elif cmd == "resume":
            since, secs = int(argv[4]), float(argv[5])
            c.send({"type": "resume", "channel_id": cid, "since_event_id": since})
            deadline = time.time() + secs
            while time.time() < deadline:
                f = c.recv(deadline - time.time())
                if f is None:
                    continue
                print(json.dumps(f))
                if f.get("type") in ("resumed", "resume.gap"):
                    return
    except Exception as e:  # report, never hang
        print(f"ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(sys.argv)
REG_WS_PY

# ---------- cluster start (same rules as the checks: start /app/start.sh only if no node port is open) ----------
reg_healthy() { local p; for p in 8000 8001 8002; do curl -fs --max-time 1 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done; }
reg_listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
reg_any_listening() { reg_listening 8000 || reg_listening 8001 || reg_listening 8002; }
reg_boot() {
  reg_healthy && return 0
  if ! reg_any_listening; then
    sleep 1
    if ! reg_any_listening; then
      if command -v setsid >/dev/null 2>&1; then
        (cd /app && exec setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1) </dev/null >/dev/null 2>&1 &
      else
        (cd /app && exec nohup /app/start.sh </dev/null >/tmp/start.log 2>&1) </dev/null >/dev/null 2>&1 &
      fi
    fi
  fi
  # wait by the clock, not by attempts: a slow node makes each attempt take up to 3 s
  while [ $((SECONDS - REG_START)) -lt 12 ]; do reg_healthy && return 0; sleep 1; done
  return 1
}
# reg_kill_tree <pid> -> stops a section and everything it started (by PID, never by name)
reg_kill_tree() {
  local c
  for c in $(ps -A -o pid=,ppid= 2>/dev/null | awk -v p="$1" '$2==p {print $1}'); do reg_kill_tree "$c"; done
  kill -9 "$1" 2>/dev/null
  return 0
}
# reg_wait <seconds> <pid>... -> waits for the sections, stopping any still running after <seconds>
reg_wait() {
  local until=$((SECONDS + $1)) p alive; shift
  while :; do
    alive=0; for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ "$alive" = 0 ] && return 0
    [ "$SECONDS" -ge "$until" ] && { for p in "$@"; do kill -0 "$p" 2>/dev/null && reg_kill_tree "$p"; done; return 0; }
    sleep 0.2
  done
}

# ---------- A1.8's fresh start (stops and restarts the cluster, so it runs alone, before the sections) ----------
reg_section_AF() {
CHECK_ALL=1
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
# regression output: passing rules are only counted, broken ones are printed
ok()  { echo "OK $1"; return 0; }
bad() { echo "REGRESSION FAIL $1 $2"; return 0; }
# ---- check_A_1.sh ----
# Self-check for A.1 (foundations). Prints PASS/FAIL per rule in /spec/component-A-staged/A-1-foundations.md.
# Usage: bash /spec/component-A-staged/checks/check_A_1.sh      Exit 0 only if every rule passes.

rules_A_1_api() {
  local u="chk$(date +%s)$RANDOM" s s2 s3 s4 s5 s6 s7 t n dn np uid slug wsname gid gname gpriv a b c d e h r

  # A1.1 register: 201 + user + token(16+); display_name kept; no password in user;
  #     duplicate 409; bad username, short or missing password 400
  s=$(call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\",\"display_name\":\"Check User\"}")
  t=$(field token); n=$(field user.username); dn=$(field user.display_name)
  np=$(has 'not any("password" in k for k in d["user"]) and all(k in d["user"] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji"))')
  s2=$(call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\"}")
  s3=$(call POST $N0/api/auth/register "{\"username\":\"bad name $RANDOM\",\"password\":\"$PW\"}")
  s4=$(call POST $N0/api/auth/register "{\"username\":\"${u}b\",\"password\":\"short77\"}")
  s5=$(call POST $N0/api/auth/register "{\"username\":\"bad!name$RANDOM\",\"password\":\"$PW\"}")
  s6=$(call POST $N0/api/auth/register "{\"username\":\"${u}c\"}")
  if [ "$s" = 201 ] && [ "${#t}" -ge 16 ] && [ "$n" = "$u" ] && [ "$dn" = "Check User" ] && [ "$np" = yes ] \
     && [ "$s2" = 409 ] && [ "$s3" = 400 ] && [ "$s4" = 400 ] && [ "$s5" = 400 ] && [ "$s6" = 400 ]; then
    ok A1.1 "register: 201 with user and token; duplicate 409; bad username, short or missing password 400"
  else
    bad A1.1 "register: got $s (token length ${#t}, username '$n', display_name '$dn', all UserObj fields and no password field: $np), duplicate $s2, space $s3, short password $s4, special char $s5, no password $s6; want 201 (16+, '$u', 'Check User', yes), 409, 400, 400, 400, 400"
  fi
  if [ -z "$t" ]; then
    for r in A1.2 A1.3 A1.4 A1.5 A1.6 A1.7; do bad $r "setup: could not register a user, fix A1.1 first"; done; return
  fi

  # A1.2 login: right password 200 + user + token (on another node too); wrong password or unknown user 401
  s=$(call POST $N0/api/auth/login "{\"username\":\"$u\",\"password\":\"$PW\"}")
  a=$(field token); n=$(field user.username)
  s2=$(call POST $N2/api/auth/login "{\"username\":\"$u\",\"password\":\"$PW\"}")
  s3=$(call POST $N0/api/auth/login "{\"username\":\"$u\",\"password\":\"wrong-password-9\"}")
  s4=$(call POST $N1/api/auth/login "{\"username\":\"nobody$RANDOM$RANDOM\",\"password\":\"$PW\"}")
  if [ "$s" = 200 ] && [ -n "$a" ] && [ "$n" = "$u" ] && [ "$s2" = 200 ] && [ "$s3" = 401 ] && [ "$s4" = 401 ]; then
    ok A1.2 "login: 200 with user and token on any node; wrong password or unknown user 401"
  else
    bad A1.2 "login: got $s (token length ${#a}), on 8002 $s2, wrong password $s3, unknown user $s4; want 200, 200, 401, 401"
  fi

  # A1.3 me: 200 + same user (on every node); no token or made-up token 401
  s=$(call GET $N0/api/auth/me "" "$t"); n=$(field user.username); uid=$(field user.id)
  b=$(call GET $N1/api/auth/me "" "$t"); c=$(call GET $N2/api/auth/me "" "$t")
  s2=$(call GET $N0/api/auth/me); s3=$(call GET $N0/api/auth/me "" "not-a-real-token-000000")
  if [ "$s" = 200 ] && [ "$n" = "$u" ] && [ -n "$uid" ] && [ "$b" = 200 ] && [ "$c" = 200 ] && [ "$s2" = 401 ] && [ "$s3" = 401 ]; then
    ok A1.3 "me: 200 with the same user on all three nodes; missing or made-up token 401"
  else
    bad A1.3 "me: expected 200 on 8000/8001/8002 then 401/401, got $s/$b/$c then $s2/$s3 (username '$n', id '$uid')"
  fi

  # A1.4 workspace object: {id, slug, name, owner_id, join_mode}; join_mode defaults to "open";
  #     the same fields appear in the GET /api/workspaces list
  slug=$(new_slug); wsname="Check Workspace $RANDOM"
  s=$(call POST $N0/api/workspaces "{\"slug\":\"$slug\",\"name\":\"$wsname\"}" "$t")
  gid=$(field general_channel.id); gname=$(field general_channel.name); gpriv=$(field general_channel.is_private)
  if [ -n "$(field workspace.id)" ] && [ "$(field workspace.slug)" = "$slug" ] && [ "$(field workspace.name)" = "$wsname" ] \
     && [ "$(field workspace.owner_id)" = "$uid" ] && [ "$(field workspace.join_mode)" = "open" ]; then a=yes; else a=no; fi
  b=$(call GET $N1/api/workspaces "" "$t")
  h=$(has 'any(str(w["slug"])==a[0] and str(w["owner_id"])==a[1] and w["join_mode"]=="open" and w["name"]==a[2] and "id" in w for w in d["workspaces"])' "$slug" "$uid" "$wsname")
  if [ "$a" = yes ] && [ "$b" = 200 ] && [ "$h" = yes ]; then
    ok A1.4 "workspace object has id, slug, name, owner_id, and join_mode \"open\", in create and list"
  else
    bad A1.4 "workspace object: create response has all fields with join_mode open and owner_id '$uid': $a; list on 8001 got $b with the same fields: $h"
  fi

  # A1.5 workspace creation: 201 + public #general channel; duplicate slug 409; bad, short or long slug 400; no token 401
  s2=$(call POST $N0/api/workspaces "{\"slug\":\"$slug\",\"name\":\"Duplicate\"}" "$t")
  s3=$(call POST $N0/api/workspaces "{\"slug\":\"Bad_Slug_$RANDOM\",\"name\":\"Bad\"}" "$t")
  s4=$(call POST $N0/api/workspaces '{"slug":"a","name":"Too Short"}' "$t")
  s5=$(call POST $N0/api/workspaces "{\"slug\":\"$(printf 'a%.0s' $(seq 1 33))\",\"name\":\"Too Long\"}" "$t")
  s6=$(call POST $N0/api/workspaces "{\"slug\":\"$(new_slug)\",\"name\":\"No Token\"}")
  if [ "$s" = 201 ] && [ -n "$gid" ] && { [ "$gname" = general ] || [ "$gname" = "#general" ]; } && [ "$gpriv" = False ] \
     && [ "$s2" = 409 ] && [ "$s3" = 400 ] && [ "$s4" = 400 ] && [ "$s5" = 400 ] && [ "$s6" = 401 ]; then
    ok A1.5 "create workspace: 201 with a public #general; duplicate 409; bad, short, long slug 400; no token 401"
  else
    bad A1.5 "create workspace: got $s (channel '$gname', id '$gid', is_private '$gpriv'), duplicate $s2, bad slug $s3, 1-char slug $s4, 33-char slug $s5, no token $s6; want 201 (general, False), 409, 400, 400, 400, 401"
  fi
  if [ "$s" != 201 ] || [ -z "$gid" ]; then
    for r in A1.6 A1.7; do bad $r "setup: could not create a workspace with a #general channel, fix A1.5 first"; done; return
  fi

  # second user (not a member of the workspace yet), used by A1.6 and A1.7
  new_user || { for r in A1.6 A1.7; do bad $r "setup: could not register a second user, fix A1.1 first"; done; return; }

  # A1.6 retrieval: list shows only the caller's workspaces; members lists the owner with role "owner";
  #     no token 401; unknown slug 404
  a=$(call GET $N1/api/workspaces "" "$t"); s2=$(has 'any(str(w["slug"])==a[0] for w in d["workspaces"])' "$slug")
  b=$(call GET $N1/api/workspaces "" "$TOKEN"); s3=$(has 'not any(str(w["slug"])==a[0] for w in d["workspaces"])' "$slug")
  c=$(call GET $N2/api/workspaces/$slug/members "" "$t"); s4=$(has 'any(str(m["user_id"])==a[0] and m["role"]=="owner" for m in d["members"])' "$uid")
  d=$(call GET $N0/api/workspaces); e=$(call GET $N0/api/workspaces/$slug/members)
  s5=$(call GET $N0/api/workspaces/no-such-ws-$RANDOM/members "" "$t")
  if [ "$a" = 200 ] && [ "$s2" = yes ] && [ "$b" = 200 ] && [ "$s3" = yes ] && [ "$c" = 200 ] && [ "$s4" = yes ] \
     && [ "$d" = 401 ] && [ "$e" = 401 ] && [ "$s5" = 404 ]; then
    ok A1.6 "list workspaces shows only the caller's; members lists the creator as owner; no token 401; unknown slug 404"
  else
    bad A1.6 "retrieval: owner's list has it: $a/$s2, non-member's list leaves it out: $b/$s3, members has owner: $c/$s4 (want 200/yes each); no token on list $d and members $e (want 401); unknown slug $s5 (want 404)"
  fi

  # A1.7 join: non-member joins #general of an open workspace -> 200 and becomes a member;
  #     joining again is still 200 and does not add a second membership; unknown channel 404; no token 401
  s=$(call POST $N1/api/channels/$gid/join "" "$TOKEN")
  s4=$(call POST $N2/api/channels/$gid/join "" "$TOKEN")
  a=$(call GET $N2/api/workspaces/$slug/members "" "$t")
  s2=$(has 'sum(1 for m in d["members"] if str(m["user_id"])==a[0] and m["role"]=="member")==1 and sum(1 for m in d["members"] if str(m["user_id"])==a[0])==1' "$USER_ID")
  s3=$(call POST $N0/api/channels/999999999/join "" "$TOKEN")
  s5=$(call POST $N0/api/channels/$gid/join)
  if [ "$s" = 200 ] && [ "$s4" = 200 ] && [ "$a" = 200 ] && [ "$s2" = yes ] && [ "$s3" = 404 ] && [ "$s5" = 401 ]; then
    ok A1.7 "join #general on an open workspace: 200 and auto-added as member once; unknown channel 404; no token 401"
  else
    bad A1.7 "join: got $s, again $s4 (want 200 each); listed exactly once as member: $a/$s2; unknown channel $s3 (want 404); no token $s5 (want 401)"
  fi
}

# ---- A1.8 fresh start: start the cluster the way a new machine would (the grader starts /app/start.sh in a new
#      container that has only /app and the packages in requirements.txt) ----
A18_OLD=/var/lib/huddle.before-check
A18_OLDTMP=/tmp/huddle.before-check
# a18_start_pids -> PIDs of running start.sh copies (an argument named start.sh), never this check or its parents
a18_start_pids() {
  python3 - "$$" <<'PY'
import os, sys
anc, p = set(), int(sys.argv[1])
while p > 1:
    anc.add(p)
    try: p = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except Exception: break
for d in os.listdir("/proc"):
    if not d.isdigit() or int(d) in anc or int(d) == os.getpid(): continue
    try: argv = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
    except Exception: continue
    if any(os.path.basename(a.decode(errors="ignore")) == "start.sh" for a in argv): print(d)
PY
}
# a18_chains [show] -> PIDs of each process listening on 8000-8002 and of its parents up to (not including) PID 1,
#     so a supervisor that restarts the nodes is found whatever it is called; never this check or its parents.
#     With "show", prints one listener's chain as "pid (command) <- pid (command) ..." instead
a18_chains() {
  local ports p
  ports=$(for p in 8000 8001 8002; do pids_on_port $p; done)
  python3 - "${BASHPID:-$$}" "${1:-}" $ports <<'PY'
import os, sys
def ppid(p):
    try: return int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except Exception: return 0
def cmd(p):
    try: return open(f"/proc/{p}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore").strip()[:60]
    except Exception: return "?"
anc, p = {os.getpid()}, int(sys.argv[1])
while p > 1: anc.add(p); p = ppid(p)
out = []
for q in sys.argv[3:]:
    chain, p = [], int(q)
    while p > 1 and p not in anc and p not in chain: chain.append(p); p = ppid(p)
    if sys.argv[2] == "show":
        print(" <- ".join(f"{c} ({cmd(c)})" for c in chain)); break
    out += [c for c in chain if c not in out]
if sys.argv[2] != "show": print(" ".join(map(str, out)))
PY
}
# a18_stop -> stops start.sh and every process holding a node port, with the parents that would restart it,
#     until the three ports stay closed (up to about 8 s)
a18_stop() {
  local p
  for _ in $(seq 1 6); do
    for p in $(a18_start_pids) $(a18_chains); do kill -9 "$p" 2>/dev/null; done
    for _ in $(seq 1 5); do any_node_listening || break; sleep 0.2; done
    any_node_listening && continue
    sleep 1.2; any_node_listening || return 0
  done
  return 1
}
# a18_healthy -> like healthy, with a 1 s limit per node so the waits below stay short
a18_healthy() { local p; for p in 8000 8001 8002; do curl -fs --max-time 1 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done; }
# a18_move_aside -> moves /var/lib/huddle and every SQLite file under /tmp (with its -wal, -shm, -journal) aside
a18_move_aside() {
  rm -rf "$A18_OLD" "$A18_OLDTMP"
  if [ -e /var/lib/huddle ]; then mv /var/lib/huddle "$A18_OLD"; fi
  python3 - "$A18_OLDTMP" <<'PY'
import os, shutil, sys
dst = sys.argv[1]
for dp, dn, fn in os.walk("/tmp"):
    dn[:] = [x for x in dn if os.path.join(dp, x) != dst and dp[4:].count(os.sep) < 2]
    for f in fn:
        q = os.path.join(dp, f)
        try:
            if os.path.islink(q) or os.path.getsize(q) > 300000000: continue
            with open(q, "rb") as fh:
                if fh.read(16) != b"SQLite format 3\x00": continue
        except Exception: continue
        for x in (q, q + "-wal", q + "-shm", q + "-journal"):
            if os.path.exists(x):
                t = os.path.join(dst, os.path.relpath(x, "/tmp"))
                os.makedirs(os.path.dirname(t), exist_ok=True); shutil.move(x, t)
PY
}
# a18_restore -> puts the moved data back and starts the cluster on it again
a18_restore() {
  local f
  a18_stop
  if [ -e "$A18_OLD" ]; then rm -rf /var/lib/huddle; mv "$A18_OLD" /var/lib/huddle; fi
  if [ -d "$A18_OLDTMP" ]; then
    (cd "$A18_OLDTMP" && find . -type f) | while read -r f; do mkdir -p "/tmp/$(dirname "$f")"; mv -f "$A18_OLDTMP/$f" "/tmp/$f"; done
    rm -rf "$A18_OLDTMP"
  fi
  start_cluster
  for _ in $(seq 1 6); do a18_healthy && return 0; sleep 1; done
}
# fresh_start -> prints "yes" if the cluster comes up within 10 s from /app/start.sh with no data on disk;
#     otherwise prints why, puts the old data back and restarts the cluster on it
fresh_start() {
  local s log
  if ! a18_stop; then echo "no (could not stop the cluster: a node port kept coming back after its process and that process's parents were killed. Now listening: $(a18_chains show). Run one start.sh only, and do not start node processes outside it)"; return; fi
  a18_move_aside
  start_cluster
  s=$SECONDS
  while [ $((SECONDS - s)) -lt 10 ]; do a18_healthy && { echo yes; return; }; sleep 1; done
  log=$(tail -c 160 /tmp/start.log 2>/dev/null | tr '\n' ' ')
  a18_restore >/dev/null 2>&1
  echo "no (after a fresh start the three nodes did not all answer /api/health within 10 s. A new machine has only /app and the packages in requirements.txt, so start.sh must create every folder and file it needs, for example mkdir -p /var/lib/huddle before the nodes start. /tmp/start.log ends: '${log}'; also look at your nodes' own logs. Your old data is back and the cluster was restarted on it)"
}

# A1.8 fresh start + shared store: the cluster starts from /app/start.sh with no data on disk; then 30
#     registrations at once (10 through each node) all 201 and every token works on another node; the data is in a
#     SQLite file under /var/lib/huddle or /tmp (a new username shows up in it or its -wal)
# A18_FRESH: the regression suite runs fresh_start once before its sections run side by side and passes the result
rule_A1_8() {
  local dir i p t pids=() base="chk$(date +%s)$RANDOM" created reg_ok me_ok found fresh=${A18_FRESH:-}
  [ -n "$fresh" ] || fresh=$(fresh_start)
  if [ "$fresh" != yes ]; then bad A1.8 "fresh start: $fresh"; return; fi
  dir=$(mktemp -d /tmp/check_A18.XXXXXX)
  for i in $(seq 0 29); do
    p=$((8000 + i % 3))
    curl -s -o "$dir/r$i" -w '%{http_code}' --max-time 5 -X POST "http://127.0.0.1:$p/api/auth/register" \
      -H 'Content-Type: application/json' -d "{\"username\":\"${base}_$i\",\"password\":\"$PW\"}" >"$dir/s$i" 2>/dev/null &
    pids+=($!)
  done
  wait "${pids[@]}" 2>/dev/null; pids=()
  for i in $(seq 0 29); do
    p=$((8000 + (i + 1) % 3))
    t=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1]))["token"])
except Exception: pass' "$dir/r$i")
    curl -s -o "$dir/m$i" -w '%{http_code}' --max-time 5 -H "Authorization: Bearer $t" "http://127.0.0.1:$p/api/auth/me" >"$dir/ms$i" 2>/dev/null &
    pids+=($!)
  done
  wait "${pids[@]}" 2>/dev/null
  read -r created reg_ok me_ok < <(python3 -c 'import json,sys,os,collections
d,base=sys.argv[1],sys.argv[2]; st=collections.Counter(); ok=me=0
for i in range(30):
    s=open(os.path.join(d,"s%d"%i)).read().strip() or "000"; st[s]+=1; ok+=s=="201"
    try:
        u=json.load(open(os.path.join(d,"m%d"%i)))["user"]["username"]
        me+=open(os.path.join(d,"ms%d"%i)).read().strip()=="200" and u=="%s_%d"%(base,i)
    except Exception: pass
print(ok, ",".join("%sx%d"%(k,v) for k,v in sorted(st.items())), me)' "$dir" "$base")
  found=$(python3 -c 'import os,sys
name=sys.argv[1].encode(); dbs=hit=0
for root in ("/var/lib/huddle","/tmp"):
    for dp,dn,fn in os.walk(root):
        if dp[len(root):].count(os.sep)>=3: dn[:]=[]
        for f in fn:
            q=os.path.join(dp,f)
            try:
                if os.path.getsize(q)>300000000: continue
                with open(q,"rb") as fh:
                    if fh.read(16)!=b"SQLite format 3\x00": continue
            except Exception: continue
            dbs+=1
            for x in (q,q+"-wal"):
                try: hit+=name in open(x,"rb").read()
                except Exception: pass
print("yes" if hit else ("no (a SQLite file exists, but the new username is not in it or its -wal file)" if dbs else "no (no SQLite file found)"))' "${base}_29")
  rm -rf "$dir"
  if [ "$created" = 30 ] && [ "$me_ok" = 30 ] && [ "$found" = yes ]; then
    ok A1.8 "fresh start: all three nodes came up from /app/start.sh with no data on disk (your earlier data is in /var/lib/huddle.before-check); shared store: 30 registrations at once (10 per node) all 201 and every token works on another node; data in a SQLite file under /var/lib/huddle or /tmp"
  else
    bad A1.8 "fresh start: yes; shared store: $created of 30 registrations at once got 201 (statuses $reg_ok), $me_ok of 30 tokens worked for /api/auth/me on another node (want 30 and 30); new username found in a SQLite file under /var/lib/huddle or /tmp: $found (want yes)"
  fi
}

rules_A_1() { rules_A_1_api; rule_A1_8; }
BODY="$REG_TMP/AF.body.json"; WSF="$REG_TMP/AF.ws"
fresh_start
}

# ---------- Component A (A.1 to A.4) ----------
reg_section_A() {
CHECK_ALL=1
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
# regression output: passing rules are only counted, broken ones are printed
ok()  { echo "OK $1"; return 0; }
bad() { echo "REGRESSION FAIL $1 $2"; return 0; }
# ---- check_A_1.sh ----
# Self-check for A.1 (foundations). Prints PASS/FAIL per rule in /spec/component-A-staged/A-1-foundations.md.
# Usage: bash /spec/component-A-staged/checks/check_A_1.sh      Exit 0 only if every rule passes.

rules_A_1_api() {
  local u="chk$(date +%s)$RANDOM" s s2 s3 s4 s5 s6 s7 t n dn np uid slug wsname gid gname gpriv a b c d e h r

  # A1.1 register: 201 + user + token(16+); display_name kept; no password in user;
  #     duplicate 409; bad username, short or missing password 400
  s=$(call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\",\"display_name\":\"Check User\"}")
  t=$(field token); n=$(field user.username); dn=$(field user.display_name)
  np=$(has 'not any("password" in k for k in d["user"]) and all(k in d["user"] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji"))')
  s2=$(call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\"}")
  s3=$(call POST $N0/api/auth/register "{\"username\":\"bad name $RANDOM\",\"password\":\"$PW\"}")
  s4=$(call POST $N0/api/auth/register "{\"username\":\"${u}b\",\"password\":\"short77\"}")
  s5=$(call POST $N0/api/auth/register "{\"username\":\"bad!name$RANDOM\",\"password\":\"$PW\"}")
  s6=$(call POST $N0/api/auth/register "{\"username\":\"${u}c\"}")
  if [ "$s" = 201 ] && [ "${#t}" -ge 16 ] && [ "$n" = "$u" ] && [ "$dn" = "Check User" ] && [ "$np" = yes ] \
     && [ "$s2" = 409 ] && [ "$s3" = 400 ] && [ "$s4" = 400 ] && [ "$s5" = 400 ] && [ "$s6" = 400 ]; then
    ok A1.1 "register: 201 with user and token; duplicate 409; bad username, short or missing password 400"
  else
    bad A1.1 "register: got $s (token length ${#t}, username '$n', display_name '$dn', all UserObj fields and no password field: $np), duplicate $s2, space $s3, short password $s4, special char $s5, no password $s6; want 201 (16+, '$u', 'Check User', yes), 409, 400, 400, 400, 400"
  fi
  if [ -z "$t" ]; then
    for r in A1.2 A1.3 A1.4 A1.5 A1.6 A1.7; do bad $r "setup: could not register a user, fix A1.1 first"; done; return
  fi

  # A1.2 login: right password 200 + user + token (on another node too); wrong password or unknown user 401
  s=$(call POST $N0/api/auth/login "{\"username\":\"$u\",\"password\":\"$PW\"}")
  a=$(field token); n=$(field user.username)
  s2=$(call POST $N2/api/auth/login "{\"username\":\"$u\",\"password\":\"$PW\"}")
  s3=$(call POST $N0/api/auth/login "{\"username\":\"$u\",\"password\":\"wrong-password-9\"}")
  s4=$(call POST $N1/api/auth/login "{\"username\":\"nobody$RANDOM$RANDOM\",\"password\":\"$PW\"}")
  if [ "$s" = 200 ] && [ -n "$a" ] && [ "$n" = "$u" ] && [ "$s2" = 200 ] && [ "$s3" = 401 ] && [ "$s4" = 401 ]; then
    ok A1.2 "login: 200 with user and token on any node; wrong password or unknown user 401"
  else
    bad A1.2 "login: got $s (token length ${#a}), on 8002 $s2, wrong password $s3, unknown user $s4; want 200, 200, 401, 401"
  fi

  # A1.3 me: 200 + same user (on every node); no token or made-up token 401
  s=$(call GET $N0/api/auth/me "" "$t"); n=$(field user.username); uid=$(field user.id)
  b=$(call GET $N1/api/auth/me "" "$t"); c=$(call GET $N2/api/auth/me "" "$t")
  s2=$(call GET $N0/api/auth/me); s3=$(call GET $N0/api/auth/me "" "not-a-real-token-000000")
  if [ "$s" = 200 ] && [ "$n" = "$u" ] && [ -n "$uid" ] && [ "$b" = 200 ] && [ "$c" = 200 ] && [ "$s2" = 401 ] && [ "$s3" = 401 ]; then
    ok A1.3 "me: 200 with the same user on all three nodes; missing or made-up token 401"
  else
    bad A1.3 "me: expected 200 on 8000/8001/8002 then 401/401, got $s/$b/$c then $s2/$s3 (username '$n', id '$uid')"
  fi

  # A1.4 workspace object: {id, slug, name, owner_id, join_mode}; join_mode defaults to "open";
  #     the same fields appear in the GET /api/workspaces list
  slug=$(new_slug); wsname="Check Workspace $RANDOM"
  s=$(call POST $N0/api/workspaces "{\"slug\":\"$slug\",\"name\":\"$wsname\"}" "$t")
  gid=$(field general_channel.id); gname=$(field general_channel.name); gpriv=$(field general_channel.is_private)
  if [ -n "$(field workspace.id)" ] && [ "$(field workspace.slug)" = "$slug" ] && [ "$(field workspace.name)" = "$wsname" ] \
     && [ "$(field workspace.owner_id)" = "$uid" ] && [ "$(field workspace.join_mode)" = "open" ]; then a=yes; else a=no; fi
  b=$(call GET $N1/api/workspaces "" "$t")
  h=$(has 'any(str(w["slug"])==a[0] and str(w["owner_id"])==a[1] and w["join_mode"]=="open" and w["name"]==a[2] and "id" in w for w in d["workspaces"])' "$slug" "$uid" "$wsname")
  if [ "$a" = yes ] && [ "$b" = 200 ] && [ "$h" = yes ]; then
    ok A1.4 "workspace object has id, slug, name, owner_id, and join_mode \"open\", in create and list"
  else
    bad A1.4 "workspace object: create response has all fields with join_mode open and owner_id '$uid': $a; list on 8001 got $b with the same fields: $h"
  fi

  # A1.5 workspace creation: 201 + public #general channel; duplicate slug 409; bad, short or long slug 400; no token 401
  s2=$(call POST $N0/api/workspaces "{\"slug\":\"$slug\",\"name\":\"Duplicate\"}" "$t")
  s3=$(call POST $N0/api/workspaces "{\"slug\":\"Bad_Slug_$RANDOM\",\"name\":\"Bad\"}" "$t")
  s4=$(call POST $N0/api/workspaces '{"slug":"a","name":"Too Short"}' "$t")
  s5=$(call POST $N0/api/workspaces "{\"slug\":\"$(printf 'a%.0s' $(seq 1 33))\",\"name\":\"Too Long\"}" "$t")
  s6=$(call POST $N0/api/workspaces "{\"slug\":\"$(new_slug)\",\"name\":\"No Token\"}")
  if [ "$s" = 201 ] && [ -n "$gid" ] && { [ "$gname" = general ] || [ "$gname" = "#general" ]; } && [ "$gpriv" = False ] \
     && [ "$s2" = 409 ] && [ "$s3" = 400 ] && [ "$s4" = 400 ] && [ "$s5" = 400 ] && [ "$s6" = 401 ]; then
    ok A1.5 "create workspace: 201 with a public #general; duplicate 409; bad, short, long slug 400; no token 401"
  else
    bad A1.5 "create workspace: got $s (channel '$gname', id '$gid', is_private '$gpriv'), duplicate $s2, bad slug $s3, 1-char slug $s4, 33-char slug $s5, no token $s6; want 201 (general, False), 409, 400, 400, 400, 401"
  fi
  if [ "$s" != 201 ] || [ -z "$gid" ]; then
    for r in A1.6 A1.7; do bad $r "setup: could not create a workspace with a #general channel, fix A1.5 first"; done; return
  fi

  # second user (not a member of the workspace yet), used by A1.6 and A1.7
  new_user || { for r in A1.6 A1.7; do bad $r "setup: could not register a second user, fix A1.1 first"; done; return; }

  # A1.6 retrieval: list shows only the caller's workspaces; members lists the owner with role "owner";
  #     no token 401; unknown slug 404
  a=$(call GET $N1/api/workspaces "" "$t"); s2=$(has 'any(str(w["slug"])==a[0] for w in d["workspaces"])' "$slug")
  b=$(call GET $N1/api/workspaces "" "$TOKEN"); s3=$(has 'not any(str(w["slug"])==a[0] for w in d["workspaces"])' "$slug")
  c=$(call GET $N2/api/workspaces/$slug/members "" "$t"); s4=$(has 'any(str(m["user_id"])==a[0] and m["role"]=="owner" for m in d["members"])' "$uid")
  d=$(call GET $N0/api/workspaces); e=$(call GET $N0/api/workspaces/$slug/members)
  s5=$(call GET $N0/api/workspaces/no-such-ws-$RANDOM/members "" "$t")
  if [ "$a" = 200 ] && [ "$s2" = yes ] && [ "$b" = 200 ] && [ "$s3" = yes ] && [ "$c" = 200 ] && [ "$s4" = yes ] \
     && [ "$d" = 401 ] && [ "$e" = 401 ] && [ "$s5" = 404 ]; then
    ok A1.6 "list workspaces shows only the caller's; members lists the creator as owner; no token 401; unknown slug 404"
  else
    bad A1.6 "retrieval: owner's list has it: $a/$s2, non-member's list leaves it out: $b/$s3, members has owner: $c/$s4 (want 200/yes each); no token on list $d and members $e (want 401); unknown slug $s5 (want 404)"
  fi

  # A1.7 join: non-member joins #general of an open workspace -> 200 and becomes a member;
  #     joining again is still 200 and does not add a second membership; unknown channel 404; no token 401
  s=$(call POST $N1/api/channels/$gid/join "" "$TOKEN")
  s4=$(call POST $N2/api/channels/$gid/join "" "$TOKEN")
  a=$(call GET $N2/api/workspaces/$slug/members "" "$t")
  s2=$(has 'sum(1 for m in d["members"] if str(m["user_id"])==a[0] and m["role"]=="member")==1 and sum(1 for m in d["members"] if str(m["user_id"])==a[0])==1' "$USER_ID")
  s3=$(call POST $N0/api/channels/999999999/join "" "$TOKEN")
  s5=$(call POST $N0/api/channels/$gid/join)
  if [ "$s" = 200 ] && [ "$s4" = 200 ] && [ "$a" = 200 ] && [ "$s2" = yes ] && [ "$s3" = 404 ] && [ "$s5" = 401 ]; then
    ok A1.7 "join #general on an open workspace: 200 and auto-added as member once; unknown channel 404; no token 401"
  else
    bad A1.7 "join: got $s, again $s4 (want 200 each); listed exactly once as member: $a/$s2; unknown channel $s3 (want 404); no token $s5 (want 401)"
  fi
}

# ---- A1.8 fresh start: start the cluster the way a new machine would (the grader starts /app/start.sh in a new
#      container that has only /app and the packages in requirements.txt) ----
A18_OLD=/var/lib/huddle.before-check
A18_OLDTMP=/tmp/huddle.before-check
# a18_start_pids -> PIDs of running start.sh copies (an argument named start.sh), never this check or its parents
a18_start_pids() {
  python3 - "$$" <<'PY'
import os, sys
anc, p = set(), int(sys.argv[1])
while p > 1:
    anc.add(p)
    try: p = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except Exception: break
for d in os.listdir("/proc"):
    if not d.isdigit() or int(d) in anc or int(d) == os.getpid(): continue
    try: argv = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
    except Exception: continue
    if any(os.path.basename(a.decode(errors="ignore")) == "start.sh" for a in argv): print(d)
PY
}
# a18_chains [show] -> PIDs of each process listening on 8000-8002 and of its parents up to (not including) PID 1,
#     so a supervisor that restarts the nodes is found whatever it is called; never this check or its parents.
#     With "show", prints one listener's chain as "pid (command) <- pid (command) ..." instead
a18_chains() {
  local ports p
  ports=$(for p in 8000 8001 8002; do pids_on_port $p; done)
  python3 - "${BASHPID:-$$}" "${1:-}" $ports <<'PY'
import os, sys
def ppid(p):
    try: return int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except Exception: return 0
def cmd(p):
    try: return open(f"/proc/{p}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore").strip()[:60]
    except Exception: return "?"
anc, p = {os.getpid()}, int(sys.argv[1])
while p > 1: anc.add(p); p = ppid(p)
out = []
for q in sys.argv[3:]:
    chain, p = [], int(q)
    while p > 1 and p not in anc and p not in chain: chain.append(p); p = ppid(p)
    if sys.argv[2] == "show":
        print(" <- ".join(f"{c} ({cmd(c)})" for c in chain)); break
    out += [c for c in chain if c not in out]
if sys.argv[2] != "show": print(" ".join(map(str, out)))
PY
}
# a18_stop -> stops start.sh and every process holding a node port, with the parents that would restart it,
#     until the three ports stay closed (up to about 8 s)
a18_stop() {
  local p
  for _ in $(seq 1 6); do
    for p in $(a18_start_pids) $(a18_chains); do kill -9 "$p" 2>/dev/null; done
    for _ in $(seq 1 5); do any_node_listening || break; sleep 0.2; done
    any_node_listening && continue
    sleep 1.2; any_node_listening || return 0
  done
  return 1
}
# a18_healthy -> like healthy, with a 1 s limit per node so the waits below stay short
a18_healthy() { local p; for p in 8000 8001 8002; do curl -fs --max-time 1 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done; }
# a18_move_aside -> moves /var/lib/huddle and every SQLite file under /tmp (with its -wal, -shm, -journal) aside
a18_move_aside() {
  rm -rf "$A18_OLD" "$A18_OLDTMP"
  if [ -e /var/lib/huddle ]; then mv /var/lib/huddle "$A18_OLD"; fi
  python3 - "$A18_OLDTMP" <<'PY'
import os, shutil, sys
dst = sys.argv[1]
for dp, dn, fn in os.walk("/tmp"):
    dn[:] = [x for x in dn if os.path.join(dp, x) != dst and dp[4:].count(os.sep) < 2]
    for f in fn:
        q = os.path.join(dp, f)
        try:
            if os.path.islink(q) or os.path.getsize(q) > 300000000: continue
            with open(q, "rb") as fh:
                if fh.read(16) != b"SQLite format 3\x00": continue
        except Exception: continue
        for x in (q, q + "-wal", q + "-shm", q + "-journal"):
            if os.path.exists(x):
                t = os.path.join(dst, os.path.relpath(x, "/tmp"))
                os.makedirs(os.path.dirname(t), exist_ok=True); shutil.move(x, t)
PY
}
# a18_restore -> puts the moved data back and starts the cluster on it again
a18_restore() {
  local f
  a18_stop
  if [ -e "$A18_OLD" ]; then rm -rf /var/lib/huddle; mv "$A18_OLD" /var/lib/huddle; fi
  if [ -d "$A18_OLDTMP" ]; then
    (cd "$A18_OLDTMP" && find . -type f) | while read -r f; do mkdir -p "/tmp/$(dirname "$f")"; mv -f "$A18_OLDTMP/$f" "/tmp/$f"; done
    rm -rf "$A18_OLDTMP"
  fi
  start_cluster
  for _ in $(seq 1 6); do a18_healthy && return 0; sleep 1; done
}
# fresh_start -> prints "yes" if the cluster comes up within 10 s from /app/start.sh with no data on disk;
#     otherwise prints why, puts the old data back and restarts the cluster on it
fresh_start() {
  local s log
  if ! a18_stop; then echo "no (could not stop the cluster: a node port kept coming back after its process and that process's parents were killed. Now listening: $(a18_chains show). Run one start.sh only, and do not start node processes outside it)"; return; fi
  a18_move_aside
  start_cluster
  s=$SECONDS
  while [ $((SECONDS - s)) -lt 10 ]; do a18_healthy && { echo yes; return; }; sleep 1; done
  log=$(tail -c 160 /tmp/start.log 2>/dev/null | tr '\n' ' ')
  a18_restore >/dev/null 2>&1
  echo "no (after a fresh start the three nodes did not all answer /api/health within 10 s. A new machine has only /app and the packages in requirements.txt, so start.sh must create every folder and file it needs, for example mkdir -p /var/lib/huddle before the nodes start. /tmp/start.log ends: '${log}'; also look at your nodes' own logs. Your old data is back and the cluster was restarted on it)"
}

# A1.8 fresh start + shared store: the cluster starts from /app/start.sh with no data on disk; then 30
#     registrations at once (10 through each node) all 201 and every token works on another node; the data is in a
#     SQLite file under /var/lib/huddle or /tmp (a new username shows up in it or its -wal)
# A18_FRESH: the regression suite runs fresh_start once before its sections run side by side and passes the result
rule_A1_8() {
  local dir i p t pids=() base="chk$(date +%s)$RANDOM" created reg_ok me_ok found fresh=${A18_FRESH:-}
  [ -n "$fresh" ] || fresh=$(fresh_start)
  if [ "$fresh" != yes ]; then bad A1.8 "fresh start: $fresh"; return; fi
  dir=$(mktemp -d /tmp/check_A18.XXXXXX)
  for i in $(seq 0 29); do
    p=$((8000 + i % 3))
    curl -s -o "$dir/r$i" -w '%{http_code}' --max-time 5 -X POST "http://127.0.0.1:$p/api/auth/register" \
      -H 'Content-Type: application/json' -d "{\"username\":\"${base}_$i\",\"password\":\"$PW\"}" >"$dir/s$i" 2>/dev/null &
    pids+=($!)
  done
  wait "${pids[@]}" 2>/dev/null; pids=()
  for i in $(seq 0 29); do
    p=$((8000 + (i + 1) % 3))
    t=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1]))["token"])
except Exception: pass' "$dir/r$i")
    curl -s -o "$dir/m$i" -w '%{http_code}' --max-time 5 -H "Authorization: Bearer $t" "http://127.0.0.1:$p/api/auth/me" >"$dir/ms$i" 2>/dev/null &
    pids+=($!)
  done
  wait "${pids[@]}" 2>/dev/null
  read -r created reg_ok me_ok < <(python3 -c 'import json,sys,os,collections
d,base=sys.argv[1],sys.argv[2]; st=collections.Counter(); ok=me=0
for i in range(30):
    s=open(os.path.join(d,"s%d"%i)).read().strip() or "000"; st[s]+=1; ok+=s=="201"
    try:
        u=json.load(open(os.path.join(d,"m%d"%i)))["user"]["username"]
        me+=open(os.path.join(d,"ms%d"%i)).read().strip()=="200" and u=="%s_%d"%(base,i)
    except Exception: pass
print(ok, ",".join("%sx%d"%(k,v) for k,v in sorted(st.items())), me)' "$dir" "$base")
  found=$(python3 -c 'import os,sys
name=sys.argv[1].encode(); dbs=hit=0
for root in ("/var/lib/huddle","/tmp"):
    for dp,dn,fn in os.walk(root):
        if dp[len(root):].count(os.sep)>=3: dn[:]=[]
        for f in fn:
            q=os.path.join(dp,f)
            try:
                if os.path.getsize(q)>300000000: continue
                with open(q,"rb") as fh:
                    if fh.read(16)!=b"SQLite format 3\x00": continue
            except Exception: continue
            dbs+=1
            for x in (q,q+"-wal"):
                try: hit+=name in open(x,"rb").read()
                except Exception: pass
print("yes" if hit else ("no (a SQLite file exists, but the new username is not in it or its -wal file)" if dbs else "no (no SQLite file found)"))' "${base}_29")
  rm -rf "$dir"
  if [ "$created" = 30 ] && [ "$me_ok" = 30 ] && [ "$found" = yes ]; then
    ok A1.8 "fresh start: all three nodes came up from /app/start.sh with no data on disk (your earlier data is in /var/lib/huddle.before-check); shared store: 30 registrations at once (10 per node) all 201 and every token works on another node; data in a SQLite file under /var/lib/huddle or /tmp"
  else
    bad A1.8 "fresh start: yes; shared store: $created of 30 registrations at once got 201 (statuses $reg_ok), $me_ok of 30 tokens worked for /api/auth/me on another node (want 30 and 30); new username found in a SQLite file under /var/lib/huddle or /tmp: $found (want yes)"
  fi
}

rules_A_1() { rules_A_1_api; rule_A1_8; }
# ---- check_A_2.sh ----
# Self-check for A.2 (roles). Prints PASS/FAIL per rule in /spec/component-A-staged/A-2-roles.md.
# Usage: bash /spec/component-A-staged/checks/check_A_2.sh      Exit 0 only if every rule passes.

rules_A_2() {
  local all="A2.1 A2.2 A2.3 A2.4 A2.5" ot oid w1 g1 w2 g2 bt bid ct cid dt did et eid xt xid pub priv
  local s a b c d e f g h i j r1 r2 r3 mrole muid

  # setup from A.1: owner O with two workspaces; B is a member of both (joined via #general)
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN; oid=$USER_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w1=$SLUG; g1=$GEN_ID
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w2=$SLUG; g2=$GEN_ID
  new_member "$g1" && [ "$(call POST $N0/api/channels/$g2/join "" "$TOKEN")" = 200 ] \
    || { setup_fail "$all" "a new user could not join #general of an open workspace, fix A1.7 first"; return; }
  bt=$TOKEN; bid=$USER_ID

  # A2.1 per-workspace roles: B becomes admin in w1 but stays member in w2
  s=$(set_role "$ot" $w1 $bid admin); mrole=$(field member.role); muid=$(field member.user_id)
  r1=$(role_of "$ot" $w1 $bid $N1); r2=$(role_of "$ot" $w2 $bid $N2)
  if [ "$s" = 200 ] && [ "$r1" = admin ] && [ "$r2" = member ]; then
    ok A2.1 "the same user is admin in one workspace and member in another"
  else
    bad A2.1 "per-workspace roles: PATCH to admin got $s; expected admin in w1 and member in w2, got '$r1' and '$r2'"
  fi

  # A2.2 channel permissions: guest C, member D, admin B, outsider X
  new_member "$g1" || { setup_fail "A2.2 A2.3 A2.4 A2.5" "could not add a member to the workspace"; return; }
  ct=$TOKEN; cid=$USER_ID
  new_member "$g1" || { setup_fail "A2.2 A2.3 A2.4 A2.5" "could not add a member to the workspace"; return; }
  dt=$TOKEN; did=$USER_ID
  new_user || { setup_fail "A2.2 A2.3 A2.4 A2.5" "could not register a user"; return; }
  xt=$TOKEN; xid=$USER_ID
  if [ "$(set_role "$ot" $w1 $cid guest)" != 200 ]; then
    bad A2.2 "setup: owner could not PATCH a member to guest"
  else
    new_channel "$ot" $w1 false; pub=$CHAN_ID
    if ! is2xx "$ST" || [ -z "$pub" ]; then
      bad A2.2 "setup: owner could not create a public channel (got $ST, channel.id '$pub')"
    else
      a=$(call POST $N1/api/channels/$pub/join "" "$ct")
      new_channel "$ct" $w1 false $N1; b=$ST
      new_channel "$ct" $w1 true $N1; f=$ST
      new_channel "$dt" $w1 false $N2; c=$ST
      new_channel "$dt" $w1 true $N2; d=$ST
      new_channel "$xt" $w1 false $N0; g=$ST
      new_channel "$bt" $w1 true $N0; e=$ST; priv=$CHAN_ID
      # nobody gets into a private channel without an invite: not a member, admin, owner, guest or outsider;
      # the creator is already a member, so their own join is 200
      if [ -n "$priv" ]; then
        i=$(call POST $N2/api/channels/$priv/join "" "$bt")
        r3="$(call POST $N1/api/channels/$priv/join "" "$dt") $(call POST $N2/api/channels/$priv/join "" "$ot")"
        r3="$r3 $(call POST $N0/api/channels/$priv/join "" "$ct") $(call POST $N1/api/channels/$priv/join "" "$xt")"
        new_member "$g1" && [ "$(set_role "$ot" $w1 $USER_ID admin)" = 200 ] && r3="$r3 $(call POST $N2/api/channels/$priv/join "" "$TOKEN")"
        h=$(role_of "$ot" $w1 $xid)
      else r3="no private channel id"; h=""; i=""; fi
      if [ "$a" = 403 ] && [ "$b" = 403 ] && [ "$f" = 403 ] && [ "$c" = 201 ] && [ "$d" = 403 ] && [ "$g" = 403 ] && [ "$e" = 201 ] \
         && [ "$i" = 200 ] && [ "$r3" = "403 403 403 403 403" ] && [ -z "$h" ]; then
        ok A2.2 "guest cannot join or create channels; member creates public only; admin creates private; only the creator is in a private channel"
      else
        bad A2.2 "channel permissions: guest join $a, guest create public $b and private $f (want 403 each); member public $c (want 201), member private $d (want 403); outsider create $g (want 403); admin private $e (want 201); creator joins own private channel $i (want 200: creator is a member); private join by member/owner/guest/outsider/other admin '$r3' (want 403 each); outsider became '$h' (want not a member)"
      fi
    fi
  fi

  # A2.3 role changes: PATCH {role} returns {member:{user_id, role}}; owner's role cannot change;
  #     unknown role 400; unknown user or workspace 404; no token 401
  a=$(set_role "$bt" $w1 $oid member); b=$(set_role "$ot" $w1 $oid admin); r1=$(role_of "$ot" $w1 $oid)
  c=$(set_role "" $w1 $bid member)
  d=$(set_role "$ot" $w1 $did superuser); r2=$(role_of "$ot" $w1 $did)
  e=$(set_role "$ot" $w1 999999999 member)
  f=$(set_role "$ot" no-such-ws-$RANDOM $did member)
  if [ "$s" = 200 ] && [ "$mrole" = admin ] && [ "$muid" = "$bid" ] && [ "$a" = 403 ] && [ "$b" = 403 ] && [ "$r1" = owner ] \
     && [ "$c" = 401 ] && [ "$d" = 400 ] && [ "$r2" = member ] && [ "$e" = 404 ] && [ "$f" = 404 ]; then
    ok A2.3 "PATCH returns 200 with member; owner's role is 403; bad role 400; unknown user or workspace 404; no token 401"
  else
    bad A2.3 "role changes: PATCH got $s with member.role '$mrole', member.user_id '$muid'; admin->owner $a, owner->self $b (want 403 each, owner now '$r1'); no token $c (want 401); role superuser $d (want 400, role now '$r2'); unknown user $e, unknown workspace $f (want 404)"
  fi

  # A2.4 promoting/demoting: owner and admin can change member<->guest; owner can change admins;
  #     an admin cannot change another admin; role "owner" is 400; members and guests cannot PATCH
  a=$(set_role "$bt" $w1 $did guest); r1=$(role_of "$ot" $w1 $did); b=$(set_role "$bt" $w1 $did member)
  g=$(set_role "$ot" $w1 $did admin); h=$(set_role "$ot" $w1 $did member); r3=$(role_of "$ot" $w1 $did)
  new_member "$g1" && [ "$(set_role "$ot" $w1 $USER_ID admin)" = 200 ] \
    || { setup_fail "A2.4 A2.5" "could not add a second admin"; return; }
  et=$TOKEN; eid=$USER_ID
  c=$(set_role "$bt" $w1 $eid member); r2=$(role_of "$ot" $w1 $eid)
  d=$(set_role "$ot" $w1 $did owner)
  e=$(set_role "$dt" $w1 $cid member)
  f=$(set_role "$ct" $w1 $did guest)
  if [ "$a" = 200 ] && [ "$r1" = guest ] && [ "$b" = 200 ] && [ "$g" = 200 ] && [ "$h" = 200 ] && [ "$r3" = member ] \
     && [ "$c" = 403 ] && [ "$r2" = admin ] && [ "$d" = 400 ] && [ "$e" = 403 ] && [ "$f" = 403 ]; then
    ok A2.4 "admin demotes/promotes members; owner changes admins; admin cannot touch admin; role owner 400; members/guests 403"
  else
    bad A2.4 "promote/demote: admin member->guest $a ('$r1'), guest->member $b; owner member->admin $g, admin->member $h ('$r3') (want 200 each); admin edits admin $c (want 403, role now '$r2'); role owner $d (want 400); member PATCH $e, guest PATCH $f (want 403)"
  fi

  # A2.5 transfer ownership: owner-only; target must be admin; old owner becomes admin; owner_id updates
  a=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $eid)}" "$bt")
  i=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $bid)}" "$dt")
  j=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $bid)}")
  b=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $did)}" "$ot")
  c=$(call POST $N1/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $bid)}" "$ot")
  r1=$(role_of "$bt" $w1 $bid $N2); r2=$(role_of "$bt" $w1 $oid $N2)
  d=$(call GET $N0/api/workspaces "" "$bt"); h=$(has 'any(str(w["slug"])==a[0] and str(w["owner_id"])==a[1] for w in d["workspaces"])' "$w1" "$bid")
  e=$(call POST $N0/api/workspaces/$w1/transfer_ownership "{\"new_owner_id\":$(jv $eid)}" "$ot")
  if [ "$a" = 403 ] && [ "$i" = 403 ] && [ "$j" = 401 ] && [ "$b" = 400 ] && [ "$c" = 200 ] && [ "$r1" = owner ] && [ "$r2" = admin ] \
     && [ "$h" = yes ] && [ "$e" = 403 ]; then
    ok A2.5 "transfer_ownership: owner-only, target must be admin, old owner becomes admin, owner_id updates"
  else
    bad A2.5 "transfer: by admin $a, by member $i (want 403), no token $j (want 401), to member $b (want 400), to admin $c (want 200); new owner '$r1' (want owner), old owner '$r2' (want admin); owner_id updated: $h; old owner tries again $e (want 403)"
  fi
}
# ---- check_A_3.sh ----
# Self-check for A.3 (invitations). Prints PASS/FAIL per rule in /spec/component-A-staged/A-3-invitations.md.
# Usage: bash /spec/component-A-staged/checks/check_A_3.sh      Exit 0 only if every rule passes.

rules_A_3() {
  local all="A3.1 A3.2 A3.3 A3.4" ot w g mt adt gt ot2 xt xid nt nname yt yid zt c1 mu1 ct mut cn ce c2 cr ca
  local s1 s2 s3 s4 s5 s6 s7 s8 a b c d e f h i j k r

  # setup from A.1 and A.2: owner O with a workspace; M member, AD admin, G guest; X, N, OT outsiders
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "a new user could not join #general of an open workspace, fix A1.7 first"; return; }
  mt=$TOKEN
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] || { setup_fail "$all" "could not add an admin, fix A.2 first"; return; }
  adt=$TOKEN
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID guest)" = 200 ] || { setup_fail "$all" "could not add a guest, fix A.2 first"; return; }
  gt=$TOKEN
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot2=$TOKEN
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  xt=$TOKEN; xid=$USER_ID
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  nt=$TOKEN; nname=$USER

  # A3.1 create: admin/owner only; 201 with code; max_uses defaults to 1; targeted invites are single-use;
  #     member, guest and outsider 403; unknown workspace 404; no token 401
  new_invite "$ot" $w; s1=$ST; c1=$CODE; mu1=$MAX_USES
  new_invite "$ot" $w "{\"invited_user_id\":$(jv $xid),\"max_uses\":5}" $N1; s2=$ST; ct=$CODE; mut=$MAX_USES
  new_invite "$ot" $w "{\"invited_username\":\"$nname\"}" $N2; s3=$ST; cn=$CODE
  new_invite "$adt" $w '{}' $N1; s5=$ST; ca=$CODE
  new_invite "$mt" $w; s4=$ST
  new_invite "$gt" $w; s6=$ST
  new_invite "$ot2" $w; s7=$ST
  new_invite "$ot" no-such-ws-$RANDOM; s8=$ST
  a=$(call POST $N0/api/workspaces/$w/invitations '{}')
  if [ "$s1" = 201 ] && [ -n "$c1" ] && [ "$mu1" = 1 ] && [ "$s2" = 201 ] && [ -n "$ct" ] && [ "$mut" = 1 ] && [ "$s3" = 201 ] && [ -n "$cn" ] \
     && [ "$s5" = 201 ] && [ -n "$ca" ] && [ "$s4" = 403 ] && [ "$s6" = 403 ] && [ "$s7" = 403 ] && [ "$s8" = 404 ] && [ "$a" = 401 ]; then
    ok A3.1 "create invitation: owner and admin 201 with code; max_uses defaults to 1; targeted single-use; others 403"
  else
    bad A3.1 "create: plain $s1 (code '$c1', max_uses '$mu1'), by user id $s2 (max_uses '$mut'), by username $s3, by admin $s5 (want 201, max_uses 1); by member $s4, guest $s6, outsider $s7 (want 403); unknown workspace $s8 (want 404); no token $a (want 401)"
  fi
  if [ -z "$c1" ] || [ -z "$ct" ]; then setup_fail "A3.2 A3.3 A3.4" "could not create invitations, fix A3.1 first"; return; fi

  # A3.2 list: admin/owner see active invitations; member, guest, outsider 403; no token 401
  a=$(call GET $N1/api/workspaces/$w/invitations "" "$ot")
  h=$(has 'set(a) <= {str(i["code"]) for i in d["invitations"]}' "$c1" "$ct" "$cn" "$ca")
  e=$(call GET $N2/api/workspaces/$w/invitations "" "$adt")
  i=$(has 'a[0] in {str(i["code"]) for i in d["invitations"]}' "$c1")
  b=$(call GET $N1/api/workspaces/$w/invitations "" "$mt")
  c=$(call GET $N1/api/workspaces/$w/invitations "" "$gt")
  d=$(call GET $N1/api/workspaces/$w/invitations "" "$ot2")
  f=$(call GET $N1/api/workspaces/$w/invitations)
  if [ "$a" = 200 ] && [ "$h" = yes ] && [ "$e" = 200 ] && [ "$i" = yes ] && [ "$b" = 403 ] && [ "$c" = 403 ] && [ "$d" = 403 ] && [ "$f" = 401 ]; then
    ok A3.2 "owner and admin list active invitations; member, guest, outsider 403; no token 401"
  else
    bad A3.2 "list: owner $a (all codes listed: $h), admin $e (listed: $i) (want 200/yes); member $b, guest $c, outsider $d (want 403); no token $f (want 401)"
  fi

  # A3.3 accept: 200, consumes one use, caller becomes member; wrong user on a targeted invite 403
  #     (by id or by username); unknown/expired/exhausted 404; exhausted codes leave the list; no token 401
  new_user || { setup_fail "A3.3 A3.4" "could not register a user"; return; }; yt=$TOKEN; yid=$USER_ID
  new_user || { setup_fail "A3.3 A3.4" "could not register a user"; return; }; zt=$TOKEN
  k=$(call POST $N0/api/invitations/$c1/accept)
  a=$(call POST $N1/api/invitations/$c1/accept "" "$yt"); r=$(role_of "$ot" $w $yid $N2)
  b=$(call POST $N2/api/invitations/$c1/accept "" "$zt")
  c=$(call POST $N0/api/invitations/$ct/accept "" "$zt")
  d=$(call POST $N1/api/invitations/$ct/accept "" "$xt")
  j="$(call POST $N2/api/invitations/$cn/accept "" "$zt") $(call POST $N0/api/invitations/$cn/accept "" "$nt")"
  e=$(call POST $N0/api/invitations/no-such-code-$RANDOM$RANDOM/accept "" "$zt")
  new_invite "$ot" $w '{"expires_in_seconds":1}'; ce=$CODE
  new_invite "$ot" $w '{"max_uses":2}'; c2=$CODE
  sleep 2
  f=$(call POST $N0/api/invitations/$ce/accept "" "$zt")
  g=$(call POST $N0/api/invitations/$c2/accept "" "$zt")
  new_user; h=$(call POST $N1/api/invitations/$c2/accept "" "$TOKEN")
  new_user; i=$(call POST $N2/api/invitations/$c2/accept "" "$TOKEN")
  call GET $N0/api/workspaces/$w/invitations "" "$ot" >/dev/null
  s1=$(has 'not ({a[0], a[1], a[2]} & {str(i["code"]) for i in d["invitations"]})' "$c1" "$c2" "$ce")
  if [ "$k" = 401 ] && [ "$a" = 200 ] && [ "$r" = member ] && [ "$b" = 404 ] && [ "$c" = 403 ] && [ "$d" = 200 ] && [ "$j" = "403 200" ] \
     && [ "$e" = 404 ] && [ "$f" = 404 ] && [ "$g" = 200 ] && [ "$h" = 200 ] && [ "$i" = 404 ] && [ "$s1" = yes ]; then
    ok A3.3 "accept consumes a use; wrong user on targeted invite 403; unknown, expired, exhausted 404; used-up codes unlisted"
  else
    bad A3.3 "accept: no token $k (want 401), first use $a (role '$r'), reuse $b, wrong user by id $c, right user $d, by username wrong/right '$j', unknown $e, expired $f, max_uses 2: $g/$h/$i; used-up and expired codes gone from list: $s1; want 200 (member), 404, 403, 200, '403 200', 404, 404, 200/200/404, yes"
  fi

  # A3.4 delete: admin/owner only; 200 {"deleted":true}; revoked code is 404 on accept and no longer listed;
  #     unknown code 404; no token 401
  new_invite "$ot" $w; cr=$CODE
  a=$(call DELETE $N1/api/workspaces/$w/invitations/$cr "" "$mt")
  f=$(call DELETE $N1/api/workspaces/$w/invitations/$cr)
  b=$(call DELETE $N1/api/workspaces/$w/invitations/$cr "" "$ot"); e=$(field deleted)
  c=$(call POST $N2/api/invitations/$cr/accept "" "$zt")
  d=$(call GET $N0/api/workspaces/$w/invitations "" "$ot"); h=$(has 'not any(str(i["code"])==a[0] for i in d["invitations"])' "$cr")
  i=$(call DELETE $N2/api/workspaces/$w/invitations/$ca "" "$adt")
  j=$(call DELETE $N0/api/workspaces/$w/invitations/no-such-code-$RANDOM "" "$ot")
  if [ "$a" = 403 ] && [ "$f" = 401 ] && [ "$b" = 200 ] && [ "$e" = True ] && [ "$c" = 404 ] && [ "$h" = yes ] && [ "$i" = 200 ] && [ "$j" = 404 ]; then
    ok A3.4 "delete: owner and admin 200 {deleted:true}; member 403; revoked code 404 and unlisted; unknown code 404"
  else
    bad A3.4 "delete: by member $a (want 403), no token $f (want 401), by owner $b with deleted '$e' (want 200/True), accept revoked $c (want 404), gone from list: $h, by admin $i (want 200), unknown code $j (want 404)"
  fi
}
# ---- check_A_4.sh ----
# Self-check for A.4 (join mode). Prints PASS/FAIL per rule in /spec/component-A-staged/A-4-join_mode.md.
# Usage: bash /spec/component-A-staged/checks/check_A_4.sh      Exit 0 only if every rule passes.

rules_A_4() {
  local all="A4.1 A4.2 A4.3" ot w g at mt gt w2 g2 pub priv w3 g3 pub3 mt3 xt xid yid nm nm2 s a b c d e f h i j r1 r2 n1 j1 n2 j2

  # setup from A.1-A.2: owner O with a workspace; A admin, M member, G guest there
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] \
    || { setup_fail "$all" "could not add an admin, fix A1.7 and A.2 first"; return; }
  at=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A1.7 first"; return; }
  mt=$TOKEN
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID guest)" = 200 ] \
    || { setup_fail "$all" "could not add a guest, fix A.2 first"; return; }
  gt=$TOKEN
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  xt=$TOKEN

  # A4.1 PATCH workspace: admin/owner only; any subset of {name, join_mode}; 200 {workspace};
  #     unknown join_mode 400 (nothing changes); member, guest, outsider 403; unknown workspace 404; no token 401
  nm="Renamed $RANDOM"; nm2="Both $RANDOM"
  a=$(call PATCH $N0/api/workspaces/$w "{\"name\":\"$nm\"}" "$ot"); n1=$(field workspace.name); j1=$(field workspace.join_mode)
  b=$(call PATCH $N1/api/workspaces/$w '{"join_mode":"invite_only"}' "$at"); n2=$(field workspace.name); j2=$(field workspace.join_mode)
  c=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}' "$mt")
  e=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}' "$gt")
  f=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}' "$xt")
  s=$(call PATCH $N2/api/workspaces/$w '{"name":"Not Allowed"}')
  i=$(call PATCH $N0/api/workspaces/$w '{"join_mode":"closed"}' "$ot")
  j=$(call PATCH $N0/api/workspaces/no-such-ws-$RANDOM '{"name":"Nope"}' "$ot")
  d=$(call GET $N2/api/workspaces "" "$ot"); h=$(has 'any(str(x["slug"])==a[0] and x["join_mode"]=="invite_only" and x["name"]==a[1] for x in d["workspaces"])' "$w" "$nm")
  r1=$(call PATCH $N1/api/workspaces/$w "{\"name\":\"$nm2\",\"join_mode\":\"open\"}" "$ot")
  r2="$(field workspace.name)|$(field workspace.join_mode)"
  if [ "$a" = 200 ] && [ "$n1" = "$nm" ] && [ "$j1" = open ] && [ "$b" = 200 ] && [ "$j2" = invite_only ] && [ "$n2" = "$nm" ] \
     && [ "$c" = 403 ] && [ "$e" = 403 ] && [ "$f" = 403 ] && [ "$s" = 401 ] && [ "$i" = 400 ] && [ "$j" = 404 ] && [ "$h" = yes ] \
     && [ "$r1" = 200 ] && [ "$r2" = "$nm2|open" ]; then
    ok A4.1 "PATCH workspace: owner/admin change name and join_mode; bad join_mode 400; others 403; unknown 404; no token 401"
  else
    bad A4.1 "PATCH: owner name $a ('$n1', join_mode '$j1'), admin join_mode $b ('$j2', name '$n2'), both fields $r1 ('$r2') (want 200 each); member $c, guest $e, outsider $f (want 403); no token $s (want 401); join_mode closed $i (want 400, unchanged on 8002: $h); unknown workspace $j (want 404)"
  fi

  # A4.2 open workspace: any user joining a public channel gets 200 and becomes a member;
  #     a private channel is still 403 and does not make them a member
  new_ws "$ot" || { setup_fail "A4.2 A4.3" "could not create a workspace, fix A.1 first"; return; }
  w2=$SLUG; g2=$GEN_ID
  new_channel "$ot" $w2 false; pub=$CHAN_ID
  new_channel "$ot" $w2 true; priv=$CHAN_ID
  new_user; xid=$USER_ID; a=$(call POST $N1/api/channels/$g2/join "" "$TOKEN"); r1=$(role_of "$ot" $w2 $xid $N2)
  new_user; yid=$USER_ID; b=$(call POST $N2/api/channels/$pub/join "" "$TOKEN"); r2=$(role_of "$ot" $w2 $yid $N0)
  new_user; xid=$USER_ID; c=$(call POST $N0/api/channels/$priv/join "" "$TOKEN"); d=$(role_of "$ot" $w2 $xid $N1)
  if [ "$a" = 200 ] && [ "$r1" = member ] && [ -n "$pub" ] && [ "$b" = 200 ] && [ "$r2" = member ] && [ -n "$priv" ] && [ "$c" = 403 ] && [ -z "$d" ]; then
    ok A4.2 "open workspace: outsiders join public channels with 200 and become members; private channel 403"
  else
    bad A4.2 "open join: #general $a (role '$r1'), other public channel '$pub' $b (role '$r2') (want 200 and member each); private channel '$priv' $c (want 403, role '$d' should be empty)"
  fi

  # A4.3 invite_only workspace: outsiders get 403 on every public channel until they accept an invitation;
  #     existing members still join; switching back to open lets outsiders in again
  new_ws "$ot" || { setup_fail "A4.3" "could not create a workspace, fix A.1 first"; return; }
  w3=$SLUG; g3=$GEN_ID
  new_channel "$ot" $w3 false; pub3=$CHAN_ID
  new_member "$g3" || { setup_fail "A4.3" "could not add a member, fix A1.7 first"; return; }
  mt3=$TOKEN
  s=$(call PATCH $N0/api/workspaces/$w3 '{"join_mode":"invite_only"}' "$ot")
  [ "$s" = 200 ] || { bad A4.3 "setup: owner could not set join_mode to invite_only (got $s), fix A4.1 first"; return; }
  new_user; xid=$USER_ID; xt=$TOKEN
  a=$(call POST $N1/api/channels/$g3/join "" "$xt"); e=$(call POST $N2/api/channels/$pub3/join "" "$xt"); r1=$(role_of "$ot" $w3 $xid $N2)
  f=$(call POST $N0/api/channels/$pub3/join "" "$mt3")
  new_invite "$ot" $w3
  b=$(call POST $N2/api/invitations/$CODE/accept "" "$xt")
  c=$(call POST $N1/api/channels/$g3/join "" "$xt")
  s=$(call PATCH $N0/api/workspaces/$w3 '{"join_mode":"open"}' "$ot")
  new_user; d=$(call POST $N2/api/channels/$g3/join "" "$TOKEN")
  if [ "$a" = 403 ] && [ "$e" = 403 ] && [ -z "$r1" ] && [ "$f" = 200 ] && [ "$b" = 200 ] && [ "$c" = 200 ] && [ "$s" = 200 ] && [ "$d" = 200 ]; then
    ok A4.3 "invite_only: outsider 403 until invited, member still joins; back to open lets outsiders in"
  else
    bad A4.3 "invite_only: outsider join #general $a and other public channel $e (want 403, role '$r1' should be empty); existing member join $f (want 200); accept invite $b, join after $c (want 200); back to open $s, new outsider join $d (want 200)"
  fi
}
BODY="$REG_TMP/A.body.json"; WSF="$REG_TMP/A.ws"
rules_A_1; rules_A_2; rules_A_3; rules_A_4
}

# ---------- Component B, first half (B.1 to B.3) ----------
reg_section_B1() {
CHECK_ALL=1
{ set +xv; } 2>/dev/null
# Shared helpers for the component-B checks. Sourced by check_B_*.sh, not run directly.
# Self-contained on purpose (it does not source component A's lib.sh), so the B folder can be
# moved or combined with other layouts without breaking.
LIBDIR="$REG_TMP"   # ws.py is unpacked here
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
# regression output: passing rules are only counted, broken ones are printed
ok()  { echo "OK $1"; return 0; }
bad() { echo "REGRESSION FAIL $1 $2"; return 0; }
# ---- check_B_1.sh ----
# Self-check for B.1 (channels). Prints PASS/FAIL per rule in /spec/component-B-staged/B-1-channels.md.
# Usage: bash /spec/component-B-staged/checks/check_B_1.sh      Exit 0 only if every rule passes.

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
# ---- check_B_2.sh ----
# Self-check for B.2 (messages and history). Prints PASS/FAIL per rule in /spec/component-B-staged/B-2-messages.md.
# Usage: bash /spec/component-B-staged/checks/check_B_2.sh      Exit 0 only if every rule passes.

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
# ---- check_B_3.sh ----
# Self-check for B.3 (search and pins). Prints PASS/FAIL per rule in /spec/component-B-staged/B-3-search-pins.md.
# Usage: bash /spec/component-B-staged/checks/check_B_3.sh      Exit 0 only if every rule passes.

RIDS='",".join(str(m["id"]) for m in d["results"])'
PIDS='",".join(str(x["message"]["id"]) for x in d["pins"])'

rules_B_3() {
  local all="B3.1 B3.2 B3.3" ot mid mt at xt wid w g w2id g2 ch p tok s1 s2 s3 s4 m1 m2 pm cur ids s a b c e f h i j k

  # setup: owner O (workspace w), M member, AD admin with private channel p; X owns another workspace w2
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; wid=$WS_ID; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  mt=$TOKEN; mid=$USER_ID
  new_member "$g" && [ "$(set_role "$ot" $w $USER_ID admin)" = 200 ] || { setup_fail "$all" "could not add an admin, fix A.2 first"; return; }
  at=$TOKEN
  new_user && xt=$TOKEN && new_ws "$xt" || { setup_fail "$all" "could not create a second workspace"; return; }
  w2id=$WS_ID; g2=$GEN_ID
  new_channel "$ot" $w false; ch=$CHAN_ID
  new_channel "$at" $w true; p=$CHAN_ID
  [ -n "$ch" ] && [ -n "$p" ] && [ "$(join "$mt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join channels, fix B.1 first"; return; }

  # B3.1 search: newest first, case-insensitive, only readable channels, workspace filter, paging
  tok="zq$RANDOM$RANDOM"
  post "$ot" $ch "first $tok here" $N0; s1=$MSG_ID
  post "$mt" $ch "second $tok here" $N1; s2=$MSG_ID
  post "$at" $p "secret $tok here" $N2; s3=$MSG_ID
  post "$xt" $g2 "elsewhere $tok here" $N0; s4=$MSG_ID
  post "$ot" $ch "unrelated words" $N1
  a=$(call GET "$N0/api/search?q=$tok" "" "$mt"); ids=$(val "$RIDS")
  b=$(call GET "$N1/api/search?q=$(echo "$tok" | tr a-z A-Z)" "" "$mt"); i=$(val "$RIDS")
  c=$(call GET "$N2/api/search?q=$tok&workspace_id=$wid" "" "$at"); j=$(val "$RIDS")
  join "$xt" $g >/dev/null
  e=$(call GET "$N0/api/search?q=$tok&workspace_id=$w2id" "" "$xt"); k=$(val "$RIDS")
  f=$(call GET "$N1/api/search?q=$tok&limit=1" "" "$mt"); s=$(val "$RIDS"); cur=$(field next_cursor)
  h=$(call GET "$N2/api/search?q=$tok&limit=1&cursor=$(enc "$cur")" "" "$mt"); s="$s|$(val "$RIDS")"
  if [ "$a$b$c$e$f$h" = 200200200200200200 ] && [ "$ids" = "$s2,$s1" ] && [ "$i" = "$s2,$s1" ] && [ "$j" = "$s3,$s2,$s1" ] \
     && [ "$k" = "$s4" ] && [ "$s" = "$s2|$s1" ] && [ "$(call GET "$N0/api/search?q=$tok")" = 401 ]; then
    ok B3.1 "search: newest first, case-insensitive, only readable channels, workspace_id filter, paged; no token 401"
  else
    bad B3.1 "search: member got '$ids', uppercase query '$i' (want '$s2,$s1': not the private or other-workspace ones); private channel's member with workspace_id '$j' (want '$s3,$s2,$s1'); other workspace filter '$k' (want '$s4'); limit=1 pages '$s' (want '$s2|$s1'); statuses $a/$b/$c/$e/$f/$h; also no token should be 401"
  fi

  # B3.2 pins: pin returns {pin}, idempotent; list newest pin first, paged; unpin
  post "$ot" $ch "pin me one" $N0; m1=$MSG_ID
  post "$ot" $ch "pin me two" $N0; m2=$MSG_ID
  a=$(call POST $N1/api/messages/$m1/pin "" "$mt")
  i=$(has '(lambda x: str(x["message_id"])==a[0] and str(x["pinned_by"])==a[1] and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)", x["pinned_at"]) is not None)(d["pin"])' "$m1" "$mid")
  b=$(call POST $N2/api/messages/$m1/pin "" "$mt")
  sleep 1
  c=$(call POST $N0/api/messages/$m2/pin "" "$ot")
  e=$(call GET "$N1/api/channels/$ch/pins" "" "$mt"); ids=$(val "$PIDS"); j=$(has 'all(k in x for x in d["pins"] for k in ("message","pinned_by","pinned_at")) and d["next_cursor"] is None')
  call GET "$N2/api/channels/$ch/pins?limit=1" "" "$mt" >/dev/null; s=$(val "$PIDS"); cur=$(field next_cursor)
  call GET "$N0/api/channels/$ch/pins?limit=1&cursor=$(enc "$cur")" "" "$mt" >/dev/null; s="$s|$(val "$PIDS")"; k=$(field next_cursor)
  f=$(call DELETE $N1/api/messages/$m1/pin "" "$mt"); h=$(field unpinned)
  call GET "$N2/api/channels/$ch/pins" "" "$mt" >/dev/null; cur=$(val "$PIDS")
  if [ "$a" = 200 ] && [ "$i" = yes ] && [ "$b" = 200 ] && [ "$c" = 200 ] && [ "$e" = 200 ] && [ "$ids" = "$m2,$m1" ] && [ "$j" = yes ] \
     && [ "$s" = "$m2|$m1" ] && [ "$k" = null ] && [ "$f" = 200 ] && [ "$h" = true ] && [ "$cur" = "$m2" ]; then
    ok B3.2 "pins: 200 {pin}, idempotent; list newest pin first and paged; unpin 200 {unpinned:true} removes it"
  else
    bad B3.2 "pins: pin $a (fields right: $i), again $b, second pin $c (want 200); list $e gave '$ids' (want '$m2,$m1' once each; pin fields and null next_cursor: $j); limit=1 pages '$s' (want '$m2|$m1', last cursor '$k' want null); unpin $f with unpinned '$h' (want 200/true); list after '$cur' (want '$m2')"
  fi

  # B3.3 pin access: private non-member 403 on list and pin; unknown message 404; no token 401
  post "$at" $p "private pin" $N0; pm=$MSG_ID
  a=$(call GET "$N0/api/channels/$p/pins" "" "$mt")
  b=$(call POST $N1/api/messages/$pm/pin "" "$mt")
  c=$(call POST $N2/api/messages/999999999/pin "" "$mt")
  e=$(call GET "$N0/api/channels/$ch/pins")
  f=$(call GET "$N0/api/channels/$p/pins" "" "$at")
  if [ "$a" = 403 ] && [ "$b" = 403 ] && [ "$c" = 404 ] && [ "$e" = 401 ] && [ "$f" = 200 ]; then
    ok B3.3 "pin access: private non-member 403 on list and pin; unknown message 404; no token 401"
  else
    bad B3.3 "pin access: non-member list $a, pin $b (want 403); unknown message $c (want 404); no token $e (want 401); the private channel's member lists pins $f (want 200)"
  fi
}
BODY="$REG_TMP/B1.body.json"; WSF="$REG_TMP/B1.ws"
rules_B_1; rules_B_2; rules_B_3
}

# ---------- Component B, second half (B.4 to B.6; B.5 waits on WebSocket events, the slowest part) ----------
reg_section_B2() {
CHECK_ALL=1
{ set +xv; } 2>/dev/null
# Shared helpers for the component-B checks. Sourced by check_B_*.sh, not run directly.
# Self-contained on purpose (it does not source component A's lib.sh), so the B folder can be
# moved or combined with other layouts without breaking.
LIBDIR="$REG_TMP"   # ws.py is unpacked here
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
# regression output: passing rules are only counted, broken ones are printed
ok()  { echo "OK $1"; return 0; }
bad() { echo "REGRESSION FAIL $1 $2"; return 0; }
# ---- check_B_4.sh ----
# Self-check for B.4 (mentions). Prints PASS/FAIL per rule in /spec/component-B-staged/B-4-mentions.md.
# Usage: bash /spec/component-B-staged/checks/check_B_4.sh      Exit 0 only if every rule passes.

# mentions of the last posted message equal exactly the given ids (no duplicates, any order)
SAME='sorted(str(x) for x in d["message"]["mentions"])==sorted(a)'
GOT='",".join(sorted(str(x) for x in d["message"]["mentions"]))'

rules_B_4() {
  local all="B4.1 B4.2 B4.3" ot oid on g w ch bid bn cid cn yn mem a b c e f s1 s2 s3 s4 s5

  # setup: owner O posts; B is in the channel; C is in the workspace but not the channel; Y is outside
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN; oid=$USER_ID; on=$USER
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  bid=$USER_ID; bn=$USER; local bt=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  cid=$USER_ID; cn=$USER
  new_user || { setup_fail "$all" "could not register a user"; return; }
  yn=$USER
  new_channel "$ot" $w false; ch=$CHAN_ID
  [ -n "$ch" ] && [ "$(join "$bt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join a channel, fix B.1 first"; return; }

  # B4.1 resolving: @username -> id; duplicates once; author never; workspace member outside the channel counts
  post "$ot" $ch "hi @$bn, and @$bn again" $N0; a=$ST; s1=$(has "$SAME" "$bid"); e=$(val "$GOT")
  post "$ot" $ch "(@$cn) and @$on too" $N1; b=$ST; s2=$(has "$SAME" "$cid"); f=$(val "$GOT")
  if [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 201 ] && [ "$s2" = yes ]; then
    ok B4.1 "mentions: @username resolves once; author never included; workspace member outside the channel included"
  else
    bad B4.1 "mentions: '@B, and @B again' gave [$e] (want [$bid], status $a); '(@C) and @author' gave [$f] (want [$cid]: C is in the workspace, the author is never mentioned, status $b)"
  fi

  # B4.2 not mentions: in-word, email-shaped, unknown user, user outside the workspace
  post "$ot" $ch "x@$bn and $bn@example.com and @nosuchuser$RANDOM and @$yn" $N2; a=$ST; s3=$(has 'd["message"]["mentions"]==[]'); e=$(val "$GOT")
  if [ "$a" = 201 ] && [ "$s3" = yes ]; then
    ok B4.2 "not mentions: in-word, email-shaped, unknown users and users outside the workspace add nothing"
  else
    bad B4.2 "non-mentions: 'x@B, B@example.com, @unknown, @outsider' gave [$e] (want [], status $a)"
  fi

  # B4.3 @channel: every channel member except the author, deduplicated with @username
  call GET $N0/api/channels/$ch/members "" "$ot" >/dev/null
  mem=$(val '" ".join(str(u["id"]) for u in d["members"] if str(u["id"])!=a[0])' "$oid")
  post "$ot" $ch "@channel please read" $N0; a=$ST; s4=$(has "$SAME" $mem); e=$(val "$GOT")
  post "$ot" $ch "@channel and @$bn and @$cn" $N1; b=$ST; s5=$(has "$SAME" $mem "$cid"); f=$(val "$GOT")
  if [ -n "$mem" ] && [ "$a" = 201 ] && [ "$s4" = yes ] && [ "$b" = 201 ] && [ "$s5" = yes ]; then
    ok B4.3 "@channel: every channel member except the author, deduplicated with @username"
  else
    bad B4.3 "@channel: gave [$e] (want channel members without the author: [$mem], status $a); '@channel @B @C' gave [$f] (want those plus $cid, once each, status $b)"
  fi
}
# ---- check_B_5.sh ----
# Self-check for B.5 (real-time events). Prints PASS/FAIL per rule in /spec/component-B-staged/B-5-events.md.
# Usage: bash /spec/component-B-staged/checks/check_B_5.sh      Exit 0 only if every rule passes.

WSF=/tmp/check_B_ws
# listen_all <token> <channel> <count> <seconds> -> one listener per node in the background; waits until
# each has its "subscribed" frame (up to 3s). Sets LPIDS.
listen_all() {
  local i; LPIDS=()
  for i in 0 1 2; do
    rm -f $WSF$i.jsonl
    ws listen "ws://127.0.0.1:800$i/api/ws?token=$1" "$2" "$3" "$4" $WSF$i.jsonl & LPIDS+=($!)
  done
  for _ in $(seq 1 30); do
    grep -q '"subscribed"' ${WSF}{0,1,2}.jsonl 2>/dev/null && [ "$(grep -l '"subscribed"' ${WSF}{0,1,2}.jsonl 2>/dev/null | wc -l)" -eq 3 ] && return
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

# SUMMARY_PY: reads ws.py frames from stdin, prints "type:event_id" per frame (head_event_id for resumed)
read -r -d '' SUMMARY_PY <<'PY'
import json, sys
fr = [json.loads(l) for l in sys.stdin if l.startswith("{")]
print(" ".join("%s:%s" % (f.get("type"), f.get("event_id", f.get("head_event_id"))) for f in fr))
PY

rules_B_5() {
  local all="B5.1 B5.2 B5.3 B5.4" ot mt jt g w ch h h1 h2 m1 s a b c e f k

  # setup: owner O with a channel ch that member M joined; J is a workspace member who will join and leave
  new_user || { setup_fail "$all" "could not register a user, fix A.1 first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix A.1 first"; return; }
  w=$SLUG; g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  mt=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not add a member, fix A.1 first"; return; }
  jt=$TOKEN
  new_channel "$ot" $w false; ch=$CHAN_ID
  [ -n "$ch" ] && [ "$(join "$mt" $ch)" = 200 ] || { setup_fail "$all" "could not create and join a channel, fix B.1 first"; return; }

  # B5.1 connect: bad token rejected on every node; subscribe -> subscribed with the same head everywhere
  a="$(ws reject "$W0/api/ws?token=bogus-token-000") $(ws reject "$W1/api/ws?token=bogus-token-000") $(ws reject "$W2/api/ws?token=bogus-token-000")"
  h="$(head_of "$ot" $ch $W0) $(head_of "$mt" $ch $W1) $(head_of "$ot" $ch $W2)"
  if [ "$a" = "rejected rejected rejected" ] && [[ "$h" =~ ^([0-9]+)\ \1\ \1$ ]]; then
    ok B5.1 "websocket: bad token rejected on every node; subscribe answers subscribed with the same head_event_id"
  else
    bad B5.1 "websocket: bad token on 8000/8001/8002 '$a' (want rejected each); head_event_id on 8000/8001/8002 '$h' (want the same integer three times)"
  fi

  # B5.2 live events: a post and a reply reach subscribers on all three nodes
  listen_all "$ot" $ch 2 6
  post "$mt" $ch "live one" $N1; m1=$MSG_ID; s=$ST
  post "$ot" $ch "live reply" $N2 "$m1"; s="$s $ST"
  wait "${LPIDS[@]}" 2>/dev/null
  a=$(verdict $ch "message.created,message.reply")
  if [ "$s" = "201 201" ] && [ "$a" = yes ]; then
    ok B5.2 "live events: message.created and message.reply reach subscribers on all three nodes with the next event_ids"
  else
    bad B5.2 "live events: posts '$s' (want '201 201'); each node should get message.created then message.reply with event_id head+1, head+2: $a"
  fi

  # B5.3 dense ordering: 8 real changes across nodes = 8 consecutive events; no-op join/leave emit nothing
  listen_all "$mt" $ch 8 10
  post "$ot" $ch "dense one" $N0; s=$ST
  post "$mt" $ch "dense two" $N1; s="$s $ST"
  post "$ot" $ch "dense reply" $N2 "$MSG_ID"; s="$s $ST"
  s="$s $(join "$jt" $ch $N0) $(join "$jt" $ch $N1)"
  s="$s $(call POST $N1/api/channels/$ch/leave "" "$jt") $(call POST $N2/api/channels/$ch/leave "" "$jt")"
  s="$s $(call PATCH $N2/api/channels/$ch '{"topic":"dense topic"}' "$ot")"
  s="$s $(call POST $N0/api/channels/$ch/archive "" "$ot") $(call POST $N1/api/channels/$ch/unarchive "" "$ot")"
  wait "${LPIDS[@]}" 2>/dev/null
  a=$(verdict $ch "message.created,message.created,message.reply,member.joined,member.left,channel.updated,channel.archived,channel.unarchived")
  h1=$(python3 -c 'import json,sys
for l in open(sys.argv[1]):
    f=json.loads(l)
    if f.get("type")=="subscribed": print(f.get("head_event_id")); break' $WSF"0.jsonl" 2>/dev/null)
  if [ "$s" = "201 201 201 200 200 200 200 200 200 200" ] && [ "$a" = yes ]; then
    ok B5.3 "dense ordering: 8 changes across nodes give 8 consecutive event_ids, same on every node; no-op join/leave emit nothing"
  else
    bad B5.3 "dense: request statuses '$s' (want 201 x3 then 200 x7); expected created, created, reply, joined, left, updated, archived, unarchived with consecutive ids on every node (repeat join/leave must add nothing): $a"
  fi

  # B5.4 resume: replay after since_event_id on another node, then resumed; from head: only resumed
  if [[ "$h1" =~ ^[0-9]+$ ]]; then
    k=$((h1 + 8))
    a=$(ws resume "$W2/api/ws?token=$mt" $ch $((h1 + 2)) 4 | python3 -c "$SUMMARY_PY")
    b=$(ws resume "$W1/api/ws?token=$ot" $ch $k 4 | python3 -c "$SUMMARY_PY")
    e="message.reply:$((h1+3)) member.joined:$((h1+4)) member.left:$((h1+5)) channel.updated:$((h1+6)) channel.archived:$((h1+7)) channel.unarchived:$((h1+8)) resumed:$k"
    if [ "$a" = "$e" ] && [ "$b" = "resumed:$k" ]; then
      ok B5.4 "resume: replays every event after since_event_id in order on another node, then resumed; from the head only resumed"
    else
      bad B5.4 "resume from $((h1+2)) on 8002 gave '$a' (want '$e'); resume from head $k on 8001 gave '$b' (want 'resumed:$k')"
    fi
  else
    bad B5.4 "setup: no head_event_id from B5.3's subscription, fix B5.1 to B5.3 first"
  fi
}
# ---- check_B_6.sh ----
# Self-check for B.6 (read state). Prints PASS/FAIL per rule in /spec/component-B-staged/B-6-read-state.md.
# Usage: bash /spec/component-B-staged/checks/check_B_6.sh      Exit 0 only if every rule passes.

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
BODY="$REG_TMP/B2.body.json"; WSF="$REG_TMP/B2.ws"
rules_B_4; rules_B_5; rules_B_6
}

# ---------- A.0 (baseline; kills port 8001, so it runs alone, last) ----------
reg_section_A0() {
CHECK_ALL=1
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
# regression output: passing rules are only counted, broken ones are printed
ok()  { echo "OK $1"; return 0; }
bad() { echo "REGRESSION FAIL $1 $2"; return 0; }
# ---- check_A_0.sh ----
# Self-check for A.0 (baseline cluster). Prints PASS/FAIL per rule in /spec/component-A-staged/A-0-baseline.md.
# Usage: bash /spec/component-A-staged/checks/check_A_0.sh      Exit 0 only if every rule passes.
# Run it on its own: A0.4 kills port 8001 and may restart the cluster (up to about 25 seconds).

# health_of <node-url> -> prints "status|node_id|type" for that node's /api/health, e.g. "200|ok|1|int"
health_of() {
  local s; s=$(call GET "$1/api/health")
  echo "$s|$(val 'd["status"]')|$(val 'd["node_id"]')|$(val 'type(d["node_id"]).__name__')"
}
# upgrade_code <port> -> HTTP status of a WebSocket upgrade with a made-up token (000 = closed, no answer)
upgrade_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 2 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    "http://127.0.0.1:$1/api/ws?token=not-a-real-token-000000" 2>/dev/null || true
}

rules_A_0() {
  local h0 h1 h2 w0 w1 w2 a b back

  # A0.1 health on 8000: 200 {"status":"ok","node_id":0}
  h0=$(health_of $N0)
  if [ "$h0" = "200|ok|0|int" ]; then
    ok A0.1 "health on 8000: 200 with status ok and node_id 0"
  else
    bad A0.1 "health on 8000: expected '200|ok|0|int' (status|status field|node_id|type), got '$h0'"
  fi

  # A0.2 each node reports its own id: 8001 -> 1, 8002 -> 2 (integers)
  h1=$(health_of $N1); h2=$(health_of $N2)
  if [ "$h1" = "200|ok|1|int" ] && [ "$h2" = "200|ok|2|int" ]; then
    ok A0.2 "health on 8001 and 8002: 200 with node_id 1 and 2"
  else
    bad A0.2 "node ids: expected 8001 '200|ok|1|int' and 8002 '200|ok|2|int', got '$h1' and '$h2'"
  fi

  # A0.3 a WebSocket upgrade with a bad token is never accepted (no 101) on any port
  w0=$(upgrade_code 8000); w1=$(upgrade_code 8001); w2=$(upgrade_code 8002)
  if [ "$w0" != 101 ] && [ "$w1" != 101 ] && [ "$w2" != 101 ]; then
    ok A0.3 "WebSocket with a bad token is rejected on 8000, 8001, 8002"
  else
    bad A0.3 "bad-token WebSocket: expected no 101 on any port, got $w0/$w1/$w2 on 8000/8001/8002"
  fi

  # A0.4 SIGKILL the process on 8001; 8000 and 8002 keep answering health with status ok
  if [ -z "$(pids_on_port 8001)" ]; then
    bad A0.4 "crash tolerance: expected a process listening on 8001 to kill, found none"; return
  fi
  kill_port 8001; sleep 1
  a=$(health_of $N0); b=$(health_of $N2)
  if [ "${a%|*|*}" = "200|ok" ] && [ "${b%|*|*}" = "200|ok" ]; then
    ok A0.4 "after SIGKILL on 8001, 8000 and 8002 still answer health with 200 and status ok"
  else
    bad A0.4 "after SIGKILL on 8001: expected 8000 and 8002 to answer '200|ok', got '${a%|*|*}' and '${b%|*|*}'"
  fi

  # put the cluster back: give 8001 up to 3s to come back on its own, else restart everything
  back=no
  for _ in 1 2 3; do curl -fs --max-time 1 "$N1/api/health" >/dev/null 2>&1 && { back=yes; break; }; sleep 1; done
  if [ "$back" = no ] && ! restart_cluster; then
    [ "$FEEDBACK" = verbose ] && echo "NOTE cluster did not come back after the restart (see /tmp/start.log); start it again before other checks"
  fi
  return 0
}
BODY="$REG_TMP/A0.body.json"; WSF="$REG_TMP/A0.ws"
rules_A_0
}

# ---------- run ----------
if ! reg_boot; then
  echo "REGRESSION FAIL cluster not answering /api/health on 8000, 8001 and 8002 within 12 s (see /tmp/start.log); no rule was checked"
  echo "REGRESSION: 50 broken"
  exit 1
fi
# A1.8 starts the cluster as a new machine would; the sections below then read the result from A18_FRESH
A18_FRESH=$( ( reg_section_AF ) 2>/dev/null | tail -n 1 )
[ -n "$A18_FRESH" ] || A18_FRESH="no (the fresh-start test stopped before it finished)"
if ! reg_healthy; then
  echo "REGRESSION FAIL A1.8 fresh start: $A18_FRESH"
  echo "REGRESSION FAIL cluster not answering /api/health on 8000, 8001 and 8002 after the fresh-start test; no other rule was checked"
  echo "REGRESSION: 50 broken"
  exit 1
fi
( reg_section_A )  > "$REG_TMP/A.out"  2> "$REG_TMP/A.err"  & REG_PA=$!
( reg_section_B1 ) > "$REG_TMP/B1.out" 2> "$REG_TMP/B1.err" & REG_PB1=$!
( reg_section_B2 ) > "$REG_TMP/B2.out" 2> "$REG_TMP/B2.err" & REG_PB2=$!
REG_LEFT=$((REG_BUDGET - (SECONDS - REG_START)))
reg_wait $((REG_LEFT > 15 ? REG_LEFT - 9 : (REG_LEFT > 6 ? REG_LEFT - 6 : 1))) $REG_PA $REG_PB1 $REG_PB2
REG_LEFT=$((REG_BUDGET - (SECONDS - REG_START)))
if [ "$REG_LEFT" -ge 3 ]; then
  ( reg_section_A0 ) > "$REG_TMP/A0.out" 2> "$REG_TMP/A0.err" & REG_P0=$!
  reg_wait "$REG_LEFT" $REG_P0
else
  : > "$REG_TMP/A0.out"
fi

# ---------- report: broken rules in rule order, then rules not reached ----------
REG_BROKEN=0
reg_report() {   # <section> <rule ids...>
  local s=$1 r line; shift
  for r in "$@"; do
    line=$(grep -m1 "^REGRESSION FAIL $r " "$REG_TMP/$s.out" 2>/dev/null)
    if [ -n "$line" ]; then echo "${line:0:600}"; REG_BROKEN=$((REG_BROKEN + 1))
    elif ! grep -q "^OK $r\b" "$REG_TMP/$s.out" 2>/dev/null; then
      echo "REGRESSION FAIL $r not checked: the suite ran out of time or stopped before this rule$(head -c 160 "$REG_TMP/$s.err" 2>/dev/null | head -1 | sed 's/^/ (error: /;s/$/)/')"
      REG_BROKEN=$((REG_BROKEN + 1))
    fi
  done
}
reg_report A0 A0.1 A0.2 A0.3 A0.4
reg_report A  A1.1 A1.2 A1.3 A1.4 A1.5 A1.6 A1.7 A1.8 A2.1 A2.2 A2.3 A2.4 A2.5 A3.1 A3.2 A3.3 A3.4 A4.1 A4.2 A4.3
reg_report B1 B1.1 B1.2 B1.3 B1.4 B1.5 B1.6 B2.1 B2.2 B2.3 B2.4 B2.5 B2.6 B3.1 B3.2 B3.3
reg_report B2 B4.1 B4.2 B4.3 B5.1 B5.2 B5.3 B5.4 B6.1 B6.2 B6.3 B6.4
TIMEOUT_NOTE="NOTE status 000 above means a request got no answer within 5 s. Usually a node is stuck: a SQLite connection or transaction left open after an error (roll back and close it in finally), a lock held too long, or a handler that never sends its response. Try that route by hand with curl -m 5."
if ls "$REG_TMP"/*.timeout >/dev/null 2>&1; then echo "$TIMEOUT_NOTE"; fi
echo "REGRESSION: $REG_BROKEN broken"
[ "$REG_BROKEN" -eq 0 ]
