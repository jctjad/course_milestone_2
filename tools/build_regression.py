#!/usr/bin/env python3
"""Build the A and B regression suite (checks/regression.sh, for Runs 5.1 and 5.2) from the A and B checks.

Kept outside milestone-2-specs/, so the agent never sees it. Rebuild after any change to an A or B check:
  python3 tools/build_regression.py            -> writes specs-master/regression/checks/regression.sh

Why generate instead of hand-copying: the suite must hold every A and B rule itself (the plain checks are not
in /spec in Runs 5.1 and 5.2), and generating it from the same check code keeps the two in step.

How the generated script works:
  - each A and B check file's rules_X_N() functions and helpers are pasted in, inside one function per
    section, so A's and B's helper libraries never overwrite each other (each section runs in a subshell)
  - ok() and bad() are replaced: passing rules print nothing, broken ones print "REGRESSION FAIL <rule> ..."
  - A1.8's fresh start (it restarts the cluster) runs alone first, then A.1-A.4, B.1-B.3 and B.4-B.6 run
    side by side, then A.0 runs alone (it kills port 8001)
  - everything stops by a time budget so the agent never loses the output to its 30 s command limit;
    rules not reached are reported as "not checked", so nothing is silently skipped
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
M = ROOT / "specs-master"
A, B = M / "component-A-staged", M / "component-B-staged"
OUT = M / "regression" / "checks" / "regression.sh"

STRIP = [re.compile(p) for p in (
    r"^#!/",                                              # shebang
    r"^set -u\s*$",                                        # set once at the top
    r'^\. "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)/lib\.sh"\s*$',   # sourcing lib.sh (pasted in instead)
    r'^\[ "\$\{CHECK_ALL:-\}" = 1 \] \|\|',                # the stand-alone runner line
)]


def body(path):
    """A check or lib file without its stand-alone lines, ready to paste inside a function."""
    out = []
    for line in path.read_text().splitlines():
        if any(p.search(line) for p in STRIP):
            continue
        if line.startswith("LIBDIR="):
            line = 'LIBDIR="$REG_TMP"   # ws.py is unpacked here'
        out.append(line)
    return "\n".join(out).strip("\n")


def rule_ids(folder, prefix):
    ids = []
    for md in sorted(folder.glob(f"{prefix}-*.md")):
        ids += re.findall(rf"^({prefix}\d\.\d+) \*\*", md.read_text(), re.M)
    return ids


def section(name, lib, checks, calls):
    parts = [f"reg_section_{name}() {{", "CHECK_ALL=1", body(lib),
             "# regression output: passing rules are only counted, broken ones are printed",
             'ok()  { echo "OK $1"; return 0; }',
             'bad() { echo "REGRESSION FAIL $1 $2"; return 0; }']
    parts += [f"# ---- {c.name} ----\n{body(c)}" for c in checks]
    # sections run side by side: give each its own response and WebSocket files (the checks use fixed
    # /tmp paths, which two sections would overwrite under each other)
    parts += [f'BODY="$REG_TMP/{name}.body.json"; WSF="$REG_TMP/{name}.ws"']
    parts += ["; ".join(calls), "}"]
    return "\n".join(parts)


a_ids, b_ids = rule_ids(A, "A"), rule_ids(B, "B")
a0_ids = [i for i in a_ids if i.startswith("A0.")]
ab_ids = [i for i in a_ids if not i.startswith("A0.")]
ws_py = (B / "checks" / "ws.py").read_text()
# the "status 000" explanation, printed once at the end if any section got no answer within 5 s
timeout_note = re.search(r'^TIMEOUT_NOTE=.*$', (A / "checks" / "lib.sh").read_text(), re.M).group(0)
assert "REG_WS_PY" not in ws_py

script = f'''#!/usr/bin/env bash
# A and B regression suite: reruns every rule of Specs A and B ({a_ids[0]} to {a_ids[-1]}, {b_ids[0]} to {b_ids[-1]}: {len(a_ids) + len(b_ids)} rules)
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
{ws_py.rstrip()}
REG_WS_PY

# ---------- cluster start (same rules as the checks: start /app/start.sh only if no node port is open) ----------
reg_healthy() {{ local p; for p in 8000 8001 8002; do curl -fs --max-time 1 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done; }}
reg_listening() {{ (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }}
reg_any_listening() {{ reg_listening 8000 || reg_listening 8001 || reg_listening 8002; }}
reg_boot() {{
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
}}
# reg_kill_tree <pid> -> stops a section and everything it started (by PID, never by name)
reg_kill_tree() {{
  local c
  for c in $(ps -A -o pid=,ppid= 2>/dev/null | awk -v p="$1" '$2==p {{print $1}}'); do reg_kill_tree "$c"; done
  kill -9 "$1" 2>/dev/null
  return 0
}}
# reg_wait <seconds> <pid>... -> waits for the sections, stopping any still running after <seconds>
reg_wait() {{
  local until=$((SECONDS + $1)) p alive; shift
  while :; do
    alive=0; for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ "$alive" = 0 ] && return 0
    [ "$SECONDS" -ge "$until" ] && {{ for p in "$@"; do kill -0 "$p" 2>/dev/null && reg_kill_tree "$p"; done; return 0; }}
    sleep 0.2
  done
}}

# ---------- A1.8's fresh start (stops and restarts the cluster, so it runs alone, before the sections) ----------
{section("AF", A / "checks" / "lib.sh", [A / "checks" / "check_A_1.sh"], ["fresh_start"])}

# ---------- Component A (A.1 to A.4) ----------
{section("A", A / "checks" / "lib.sh", [A / "checks" / f"check_A_{n}.sh" for n in (1, 2, 3, 4)], ["rules_A_1", "rules_A_2", "rules_A_3", "rules_A_4"])}

# ---------- Component B, first half (B.1 to B.3) ----------
{section("B1", B / "checks" / "lib.sh", [B / "checks" / f"check_B_{n}.sh" for n in (1, 2, 3)], ["rules_B_1", "rules_B_2", "rules_B_3"])}

# ---------- Component B, second half (B.4 to B.6; B.5 waits on WebSocket events, the slowest part) ----------
{section("B2", B / "checks" / "lib.sh", [B / "checks" / f"check_B_{n}.sh" for n in (4, 5, 6)], ["rules_B_4", "rules_B_5", "rules_B_6"])}

# ---------- A.0 (baseline; kills port 8001, so it runs alone, last) ----------
{section("A0", A / "checks" / "lib.sh", [A / "checks" / "check_A_0.sh"], ["rules_A_0"])}

# ---------- run ----------
if ! reg_boot; then
  echo "REGRESSION FAIL cluster not answering /api/health on 8000, 8001 and 8002 within 12 s (see /tmp/start.log); no rule was checked"
  echo "REGRESSION: {len(a_ids) + len(b_ids)} broken"
  exit 1
fi
# A1.8 starts the cluster as a new machine would; the sections below then read the result from A18_FRESH
A18_FRESH=$( ( reg_section_AF ) 2>/dev/null | tail -n 1 )
[ -n "$A18_FRESH" ] || A18_FRESH="no (the fresh-start test stopped before it finished)"
if ! reg_healthy; then
  echo "REGRESSION FAIL A1.8 fresh start: $A18_FRESH"
  echo "REGRESSION FAIL cluster not answering /api/health on 8000, 8001 and 8002 after the fresh-start test; no other rule was checked"
  echo "REGRESSION: {len(a_ids) + len(b_ids)} broken"
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
reg_report() {{   # <section> <rule ids...>
  local s=$1 r line; shift
  for r in "$@"; do
    line=$(grep -m1 "^REGRESSION FAIL $r " "$REG_TMP/$s.out" 2>/dev/null)
    if [ -n "$line" ]; then echo "${{line:0:600}}"; REG_BROKEN=$((REG_BROKEN + 1))
    elif ! grep -q "^OK $r\\b" "$REG_TMP/$s.out" 2>/dev/null; then
      echo "REGRESSION FAIL $r not checked: the suite ran out of time or stopped before this rule$(head -c 160 "$REG_TMP/$s.err" 2>/dev/null | head -1 | sed 's/^/ (error: /;s/$/)/')"
      REG_BROKEN=$((REG_BROKEN + 1))
    fi
  done
}}
reg_report A0 {" ".join(a0_ids)}
reg_report A  {" ".join(ab_ids)}
reg_report B1 {" ".join(i for i in b_ids if i[1] in "123")}
reg_report B2 {" ".join(i for i in b_ids if i[1] in "456")}
{timeout_note}
if ls "$REG_TMP"/*.timeout >/dev/null 2>&1; then echo "$TIMEOUT_NOTE"; fi
echo "REGRESSION: $REG_BROKEN broken"
[ "$REG_BROKEN" -eq 0 ]
'''

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(script)
OUT.chmod(0o755)
print(f"wrote {OUT.relative_to(ROOT)}: {len(a_ids)} A rules ({len(a0_ids)} in A.0), {len(b_ids)} B rules, "
      f"{len(script.splitlines())} lines")
