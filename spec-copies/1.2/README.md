# Read this first: work order and rules for this run

This run builds **Component D (cluster robustness)**: Redis fan-out, a killed HTTP node, a killed Redis, and
WebSocket speed under load. It is written as four staged specs that build on each other, on top of a small
set of basics. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen **all 4
baseline rules, all 5 basics rules and all 12 D rules** pass. Do not submit unless every one of them
passes.

The task description you were given is still the grading contract. The files in `/spec` do not change
it; they tell you what to work on now and how to prove it works. **Do not edit, move or delete anything in
`/spec`, and run the checks only as they are there: do not copy them, change their settings, or run changed
copies.**

## Files in /spec

| File | What it is |
|---|---|
| `README.md` | This file: scope, work order, how to verify, when you may submit |
| `basics/BASICS.md` | The basics: cluster, core routes, objects, events and function names. Build these first |
| `basics/checks/check_basics.sh` | PASS or FAIL for the basics (BASICS.1 to BASICS.5) |
| `basics/checks/check_baseline.sh` | PASS or FAIL for the four baseline conditions (M0.1 to M0.4) |
| `component-D-staged/D-1-redis.md` | D.1: Redis fan-out and a supervising `start.sh` (D1.1 to D1.3) |
| `component-D-staged/D-2-node-kill.md` | D.2: a killed HTTP node, resume, respawn (D2.1 to D2.5) |
| `component-D-staged/D-3-redis-kill.md` | D.3: a killed Redis (D3.1 to D3.3) |
| `component-D-staged/D-4-performance.md` | D.4: WebSocket speed under load (D4.1) |
| `component-D-staged/checks/check_D_1.sh` to `check_D_4.sh` | PASS or FAIL for each rule of one D spec |
| `*/checks/lib.sh`, `*/checks/ws.py`, `component-D-staged/checks/perf.py` | Helpers for the checks (`ws.py` is a small WebSocket client, `perf.py` the load test). Do not run or edit them |

Each spec has **Uses** (functions you must call, not rewrite), **Adds** (functions this spec introduces,
with the names to use), and **Rules** (what must be true over HTTP or WebSocket).

## Scope

Work only on the basics and the four D spec files. Do NOT build roles (beyond the owner), invitations,
search, pins, threads, read state, reactions, DMs, files, slash commands, groups, profiles, message
edit/delete, IRC or the UI in this run.

Component D owns `start.sh` supervision, Redis, cross-node event delivery and resume. IRC bridging during a
node outage belongs to the IRC gateway, which a later run builds.

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

## Order of work

You have 50 minutes: check the time left with `bash /app/timer.sh` at the start and after each step. Rough
budget: basics 12 minutes, D.1 8, D.2 9, D.3 7, D.4 6, final checks 5.

**Read each file in full, with its own `cat` command** (for example `cat /spec/basics/BASICS.md`). Do not
print several files in one command: long output gets its middle cut off. Do not `grep` the specs for rule
numbers: rules continue on the following lines (`Also:` and more), and those lines are required too.

1. `cat /spec/basics/BASICS.md`, then build the basics until `check_basics.sh` (5 rules) and
   `check_baseline.sh` (4 rules) pass.
2. D.1 to D.4 in order. After each, run its check until every rule passes: `check_D_1.sh` (3 rules),
   `check_D_2.sh` (5), `check_D_3.sh` (3), `check_D_4.sh` (1).
3. At the end, run `check_basics.sh`, `check_baseline.sh` and `check_D_1.sh` to `check_D_4.sh`, each on its
   own. If a rule from an earlier step starts failing, fix it before doing anything else.

Do not start a spec while a rule from an earlier step is failing. Fix one failing rule at a time, lowest
number first.

## How to verify

Run each of these as a **single command on its own**:

```bash
bash /spec/basics/checks/check_basics.sh
bash /spec/basics/checks/check_baseline.sh             # kills port 8001; run it on its own
bash /spec/component-D-staged/checks/check_D_1.sh     # ... through check_D_4.sh
```

Each ends with a summary line, for example `RESULT: 5 passed, 1 failed`.

- Each command is cut off at 30 seconds. Never chain two checks, or a cluster restart and a check, in one
  command: restart in one command, then run the check in the next.
- A check starts `/app/start.sh` itself only if no node port is open. After you change code, restart the
  nodes before checking again: find the process that owns each port (`ss -ltnp`) and stop it by PID. If your
  `start.sh` restarts dead nodes on its own, it brings them back with the new code; otherwise start
  `/app/start.sh` again. Never run a second `start.sh` while one is still running: the two fight over the
  ports. Never stop processes by name (`pkill`, `killall`, `pgrep -f`): that can kill you.
- `check_D_2.sh` kills the processes on ports `8001` and `8000`, and `check_D_3.sh` kills the one on port `6379`. Your
  `start.sh` must bring them back on its own; do not restart them by hand between checks.

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

- `check_basics.sh` ends with `RESULT: 5 passed, 0 failed`;
- `check_baseline.sh` ends with `RESULT: 4 passed, 0 failed`;
- `check_D_1.sh` to `check_D_4.sh` end with `RESULT: 3 passed, 0 failed`, `5 passed`, `3 passed` and
  `1 passed`.

If you changed code after a check last passed, run that check again before submitting. If a check seems
wrong, do not submit around it: re-read the rule, compare your response with what the FAIL line expects,
and keep working until it passes. Use the remaining time; do not submit early with any rule failing.

These rules are necessary, not sufficient. The full task in the grading contract still applies.
