# Read this first: work order and rules for this run

**Print this file on its own** (`cat /spec/README.md`, nothing else in that command): any output over 10,000
characters gets its middle cut off.

This run builds **three components** on top of a small set of basics, as staged specs that build on each
other: **D (cluster robustness)**, then **E (IRC, web UI and message edits)**, then **C (features built on
messages)**, with C.1 (reactions) before E, which uses it. Before you run
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen **every rule listed in Before you submit**
pass. Do not submit unless every one of them passes.

The task description you were given is still the grading contract. The files in `/spec` do not change
it; they tell you what to work on now and how to prove it works. **Do not edit, move or delete anything in
`/spec`, and run the checks only as they are there: do not copy them, change their settings, or run changed
copies.**

## Order of work

You have 50 minutes: check the time left with `bash /app/timer.sh` at the start and after each step. Rough
budget: basics 6 minutes, D 7, C.1 3, E 16, C.2 to C.5 12, final checks 4.

**Get every rule right.** Use the whole run: read each FAIL line, fix its cause (not just the one case the
check tried), and rerun the check. Do not get stuck on one rule: if it still fails after several real
attempts, go on to the next step and come back to it before the final checks (the one exception to the last
paragraph of this section). Keep your context small: look at your code with `grep -n` and `sed -n 'A,Bp'`, not
whole files, and do not print the check scripts: FAIL lines say what was expected and what came back.

**Read each file in full, with its own `cat` command** (for example `cat /spec/basics/BASICS.md`). Do not
print several files in one command: long output gets its middle cut off. Do not `grep` the specs for rule
numbers: rules continue on the following lines (`Also:` and more), and those lines are required too.

1. `cat /spec/basics/BASICS.md`, then build the basics until `check_basics.sh` and `check_baseline.sh` pass.
2. Build each spec in order and run its check until every rule passes before the next. D.1 to D.4:
   `check_D_1.sh` to `check_D_4.sh`.
3. C.1 (reactions; E.6 and the web UI use them): `check_C_1.sh`.
4. E.1 to E.6: `check_E_1.sh` to `check_E_6.sh`.
5. C.2 to C.5: `check_C_2.sh` to `check_C_5.sh`. After C.5, rerun the D and E checks. Event forwarding
   (WebSocket, IRC, Redis) starts at the newest event, even after a restart, and replays old events only on
   `resume`.
6. At the end, run every check above once more, each on its own (`check_C_all.sh` for all of C). If a rule
   from an earlier step starts failing, fix it before doing anything else.

Do not start a spec while a rule from an earlier step is failing. Fix one failing rule at a time, lowest
number first.

## Scope

Work only on the basics and the D, C and E spec files. Do NOT build roles (beyond the owner), invitations,
search, pins or read state in this run. E.6 needs threads and reactions: reactions are C.1, and threads are
built exactly as E.6 describes (there is no Spec B in this run).

## API conventions (every route)

- Base URL `http://127.0.0.1:8000` (also `:8001`, `:8002`); all routes are under `/api/...` and return JSON.
- Every authenticated route reads `Authorization: Bearer <token>` and answers `401` when it is missing or
  invalid.
- Responses use one top-level resource key (`user`, `channel`, `message`, `file`, `group`, `groups`,
  `reactions`, `members`, ...).
- Timestamps are RFC 3339 UTC strings, e.g. `2026-05-16T08:07:34.123Z`. Ids are stable across the cluster.
- Status codes, unless a rule says otherwise: `200` read/update, `201` create, `204` delete with no body,
  `400` validation, `401` missing/invalid auth, `403` logged in but not allowed, `404` unknown id, `409`
  duplicate, `413` upload over the limit, `423` write to an archived channel.
- Data written through one port must be readable through the other two. The checks use different ports on
  purpose.
- Always read the whole request body (`Content-Length`) and always send `Content-Length` on responses.
  Every request must answer within 5 seconds.

## Files in /spec

| File | What it is |
|---|---|
| `README.md` | This file: scope, work order, how to verify, when you may submit |
| `basics/BASICS.md` | The basics: cluster, core routes, objects, events and function names. Build these first |
| `basics/checks/check_basics.sh` | PASS or FAIL for the basics (BASICS.1 to BASICS.5) |
| `basics/checks/check_baseline.sh` | PASS or FAIL for the four baseline conditions (M0.1 to M0.4) |
| `component-D-staged/` | Spec D: `D-1-redis.md` to `D-4-performance.md`; checks `component-D-staged/checks/check_D_1.sh` to `check_D_4.sh` |
| `component-E-staged/` | Spec E: `E-1-edit-delete.md` to `E-6-ui-threads-reactions.md`; checks `component-E-staged/checks/check_E_1.sh` to `check_E_6.sh` |
| `component-C-staged/` | Spec C: `C-1-reactions.md` to `C-5-profiles.md`; checks `component-C-staged/checks/check_C_1.sh` to `check_C_5.sh` and `check_C_all.sh` (one total) |
| `*/checks/lib.sh`, `ws.py`, `perf.py`, `irc.py`, `ui.py` | Helpers for the checks. Do not run or edit them |

Each spec has **Uses** (functions you must call, not rewrite), **Adds** (functions this spec introduces,
with the names to use), and **Rules** (what must be true over HTTP or WebSocket).

## How to verify

Run each check as a **single command on its own**, for example
`bash /spec/component-D-staged/checks/check_D_1.sh`. Each ends with a summary line, for example
`RESULT: 5 passed, 1 failed`.

- Each command is cut off at 30 seconds. Never chain two checks, or a cluster restart and a check, in one
  command: restart in one command, then run the check in the next. If `check_C_all.sh` is ever cut off, run
  the single checks one by one instead.
- A check starts `/app/start.sh` itself only if no node port is open. After you change code, restart the
  nodes before checking again: find the process that owns each port (`ss -ltnp`) and stop it by PID. If your
  `start.sh` restarts dead nodes on its own, it brings them back with the new code; otherwise start
  `/app/start.sh` again. Never run a second `start.sh` while one is still running: the two fight over the
  ports. Never stop processes by name (`pkill`, `killall`, `pgrep -f`): that can kill you.
- `check_D_2.sh` kills the processes on ports `8001` and `8000`, and `check_D_3.sh` kills the one on port `6379`. Your
  `start.sh` must bring them back on its own; do not restart them by hand between checks.
- `check_E_3.sh` kills the process on port `8001` and puts the cluster back afterwards. `check_E_4.sh` to
  `check_E_6.sh` open the page in a headless browser and take up to about 25 seconds each.

## Baseline conditions (milestone 0)

These must hold when you submit. `check_baseline.sh` tests them (M0.1 to M0.4):

1. `GET /api/health` on `127.0.0.1:8000` returns `200` with `{"status": "ok", "node_id": 0}`.
2. Port `8001` reports `node_id` `1` and port `8002` reports `node_id` `2`.
3. A WebSocket connection with an invalid token is rejected on every port.
4. If the process on port `8001` is killed with `SIGKILL`, ports `8000` and `8002` keep answering
   `GET /api/health` with `200` and `"status": "ok"`.

`check_baseline.sh` kills the process on port `8001` and, if that node does not come back within 3
seconds, restarts the cluster with `/app/start.sh`.

## Before you submit

You may submit only when all of these are true, from output you actually saw after your last code change:

- `check_basics.sh` ends with `RESULT: 5 passed, 0 failed` and `check_baseline.sh` with `4 passed`;
- `check_D_1.sh` to `check_D_4.sh` end with `RESULT: 3 passed, 0 failed`, `5 passed`, `3 passed` and
  `1 passed`;
- `check_E_1.sh` to `check_E_6.sh` end with `RESULT: 4 passed, 0 failed`, `4 passed`, `5 passed`,
  `4 passed`, `3 passed` and `3 passed`;
- `check_C_all.sh` ends with `RESULT: 24 passed, 0 failed`.

If you changed code after a check last passed, run that check again before submitting. If a check seems
wrong, do not submit around it: re-read the rule, compare your response with what the FAIL line expects,
and keep working until it passes. Use the remaining time; do not submit early with any rule failing.

These rules are necessary, not sufficient. The full task in the grading contract still applies.
