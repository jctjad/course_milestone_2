#!/usr/bin/env bash
# Self-check for A.1 (foundations). Prints PASS/FAIL per rule in /spec/component-A-staged/A-1-foundations.md.
# Usage: bash /spec/component-A-staged/checks/check_A_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

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

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_A_1; finish; }
