# Read this first: work order and rules for this run

This run builds **Component C (features built on messages)**: reactions, DMs, files, slash commands, user
groups with `@group-handle` and `@here`, and profiles. It is written as five staged specs that build on
each other, on top of a small set of basics. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`,
you MUST have seen **all 4 baseline rules, all 5 basics rules and all 24 C rules** pass. Do not submit
unless every one of them passes.

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
| `component-C-staged/C-1-reactions.md` | C.1: reactions and their events (C1.1 to C1.4) |
| `component-C-staged/C-2-dms-files.md` | C.2: DMs, file upload and download, attaching files (C2.1 to C2.5) |
| `component-C-staged/C-3-slash-commands.md` | C.3: `/me`, `/shrug`, `/topic`, `/invite`, `/archive`, `/unarchive` (C3.1 to C3.6) |
| `component-C-staged/C-4-groups-mentions.md` | C.4: user groups, `@group-handle`, `@here` (C4.1 to C4.5) |
| `component-C-staged/C-5-profiles.md` | C.5: profiles and `user.updated` (C5.1 to C5.4) |
| `component-C-staged/checks/check_C_1.sh` to `check_C_5.sh` | PASS or FAIL for each rule of one C spec |
| `component-C-staged/checks/check_C_all.sh` | Runs the five C checks and prints one total |
| `*/checks/lib.sh`, `*/checks/ws.py` | Helpers for the checks (`ws.py` is a small WebSocket client). Do not run or edit them |

Each spec has **Uses** (functions you must call, not rewrite), **Adds** (functions this spec introduces,
with the names to use), and **Rules** (what must be true over HTTP or WebSocket).

## Scope

Work only on the basics and the routes in the five C spec files. Do NOT build roles (beyond the owner),
invitations, search, pins, threads, read state, message edit/delete, IRC or the UI in this run.

Component C owns the reaction, DM, file, group and profile APIs and their events. A later run builds the
web UI on top of them; message edit and delete (and what happens to reactions on deleted messages) are not
part of C.

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
budget: basics 12 minutes, C.1 5, C.2 9, C.3 7, C.4 8, C.5 5, final checks 4.

**Read each file in full, with its own `cat` command** (for example `cat /spec/basics/BASICS.md`). Do not
print several files in one command: long output gets its middle cut off. Do not `grep` the specs for rule
numbers: rules continue on the following lines (`Also:` and more), and those lines are required too.

1. `cat /spec/basics/BASICS.md`, then build the basics until `check_basics.sh` (5 rules) and
   `check_baseline.sh` (4 rules) pass.
2. C.1 to C.5 in order. After each, run its check until every rule passes: `check_C_1.sh` (4 rules),
   `check_C_2.sh` (5), `check_C_3.sh` (6), `check_C_4.sh` (5), `check_C_5.sh` (4).
3. At the end, run `check_basics.sh`, `check_baseline.sh` and `check_C_all.sh`, each on its own. If a rule
   from an earlier step starts failing, fix it before doing anything else.

Do not start a spec while a rule from an earlier step is failing. Fix one failing rule at a time, lowest
number first.

## How to verify

Run each of these as a **single command on its own**:

```bash
bash /spec/basics/checks/check_basics.sh
bash /spec/basics/checks/check_baseline.sh             # kills port 8001; run it on its own
bash /spec/component-C-staged/checks/check_C_1.sh     # ... through check_C_5.sh
bash /spec/component-C-staged/checks/check_C_all.sh
```

Each ends with a summary line, for example `RESULT: 5 passed, 1 failed`.

- Each command is cut off at 30 seconds. Never chain two checks, or a cluster restart and a check, in one
  command: restart in one command, then run the check in the next. If `check_C_all.sh` is ever cut off, run
  the single checks one by one instead.
- A check starts `/app/start.sh` itself only if no node port is open. After you change code, restart the
  nodes before checking again: find the process that owns each port (`ss -ltnp`) and stop it by PID. If your
  `start.sh` restarts dead nodes on its own, it brings them back with the new code; otherwise start
  `/app/start.sh` again. Never run a second `start.sh` while one is still running: the two fight over the
  ports. Never stop processes by name (`pkill`, `killall`, `pgrep -f`): that can kill you.

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
- `check_C_all.sh` ends with `RESULT: 24 passed, 0 failed`.

If you changed code after a check last passed, run that check again before submitting. If a check seems
wrong, do not submit around it: re-read the rule, compare your response with what the FAIL line expects,
and keep working until it passes. Use the remaining time; do not submit early with any rule failing.

These rules are necessary, not sufficient. The full task in the grading contract still applies.
