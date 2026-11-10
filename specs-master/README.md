# Read this first: work order and rules for this run

This run builds **two components**: **A (roles and invitations)** — auth, workspaces, joining, roles,
invitations, join mode — and **B (channels and message history)** — channels, history and paging, search
and pins, mentions, WebSocket events, read state. Each is written as staged specs that build on each
other, and B builds on A. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen
**all 4 A.0 baseline rules, all 19 other A rules and all 26 B rules** pass. Do not submit unless every one of them passes.

The task description you were given is still the grading contract. The files in `/spec` do not change
it; they tell you what to work on now and how to prove it works.

## Files in /spec

| File | What it is | Builds on |
|---|---|---|
| `README.md` | This file: scope, work order, how to verify, when you may submit | |
| `component-A-staged/A-0-baseline.md` | A.0: the baseline cluster: health, node ids, bad-token WebSocket, a killed node (A0.1 to A0.4) | nothing |
| `component-A-staged/A-1-foundations.md` | A.1: auth, workspaces, members, channel join (A1.1 to A1.7) | A.0 |
| `component-A-staged/A-2-roles.md` | A.2: roles, channel creation and permissions, ownership (A2.1 to A2.5) | A.1 |
| `component-A-staged/A-3-invitations.md` | A.3: invitations (A3.1 to A3.4) | A.1, A.2 |
| `component-A-staged/A-4-join_mode.md` | A.4: `open` vs `invite_only` workspaces (A4.1 to A4.3) | A.1 to A.3 |
| `component-B-staged/B-1-channels.md` | B.1: channel object, validation, workspace detail, topic, leave, members, archive (B1.1 to B1.6) | A.1, A.2 |
| `component-B-staged/B-2-messages.md` | B.2: posting, archived channels, history paging, threads (B2.1 to B2.6) | B.1 |
| `component-B-staged/B-3-search-pins.md` | B.3: search and pins (B3.1 to B3.3) | B.2 |
| `component-B-staged/B-4-mentions.md` | B.4: `@username` and `@channel` (B4.1 to B4.3) | B.2 |
| `component-B-staged/B-5-events.md` | B.5: WebSocket subscribe, live events, dense event ids, resume (B5.1 to B5.4) | B.1, B.2 |
| `component-B-staged/B-6-read-state.md` | B.6: read cursor, unread and mention counts (B6.1 to B6.4) | B.4, B.5 |
| `component-A-staged/checks/check_A_0.sh` to `check_A_4.sh` | PASS or FAIL for each rule of one A spec | |
| `component-A-staged/checks/check_A_all.sh` | Runs the A.1 to A.4 checks and prints one total (not A.0, which you run on its own) | |
| `component-B-staged/checks/check_B_1.sh` to `check_B_6.sh` | PASS or FAIL for each rule of one B spec | |
| `component-B-staged/checks/check_B_all.sh` | Runs the six B checks and prints one total | |
| `component-*-staged/checks/lib.sh`, `component-B-staged/checks/ws.py` | Helpers for the checks (`ws.py` is a small WebSocket client). Do not run or edit them | |

Each spec has **Uses** (functions from earlier specs that you must call, not rewrite), **Adds** (functions
this spec introduces, with the names to use), and **Rules** (what must be true over HTTP or WebSocket).

## Scope

Work only on the routes in the eleven spec files. Do NOT build files, reactions, message edit/delete, IRC
or the UI in this run.

- If `/app` is empty, first build the A.0 baseline cluster (`GET /api/health` on ports `8000`, `8001`
  and `8002`), then A.1. If `/app` already has code, improve it.
- All three nodes share one store. Data written through one port must be readable through the other two,
  and WebSocket events must reach subscribers on every port. The checks use different ports on purpose.
- Every authenticated route reads `Authorization: Bearer <token>` and answers `401` when it is missing or
  invalid. Responses use one top-level resource key (`user`, `workspace`, `workspaces`, `member`,
  `members`, `invitation`, `invitations`, `channel`, `channels`, `message`, `messages`, `replies`,
  `results`, `pin`, `pins`, `read_state`).
- Status codes, unless a rule says otherwise: `200` read/update, `201` create, `400` validation, `401`
  missing/invalid auth, `403` logged in but not allowed, `404` unknown id, `409` duplicate, `423` write to
  an archived channel. Timestamps are RFC 3339 UTC strings, e.g. `2026-05-16T08:07:34.123Z`.
- **Storage tip (earlier runs lost most of their time here).** Use one SQLite file for everything,
  including tokens and events, in WAL mode. Open a connection per request and always close it, even when
  the request fails (`try`/`finally`). Set the busy wait to 2 seconds or less (`timeout=2`, no larger
  `PRAGMA busy_timeout`). Keep three separate node processes; do not merge them into one, because
  killing port `8001` must leave `8000` and `8002` running.

## Order of work

Build A first, then B. Rough time budget: A.0 3 minutes, A.1 10, A.2 6, A.3 5, A.4 3, B.1 4, B.2 6, B.3 3,
B.4 2, B.5 5, B.6 3, final checks 3.

**Read each spec file in full, with its own `cat` command** (for example
`cat /spec/component-A-staged/A-2-roles.md`). Do not print several files in one command: long output
gets its middle cut off. Do not `grep` the specs for rule numbers: rules continue on the following lines
(`Also:` and more), and those lines are required too.

1. A.0 to A.4 in order. After each, run its check until every rule passes: `check_A_0.sh` (4 rules),
   `check_A_1.sh` (7),
   `check_A_2.sh` (5), `check_A_3.sh` (4), `check_A_4.sh` (3).
2. B.1 to B.6 in order, the same way: `check_B_1.sh` (6), `check_B_2.sh` (6), `check_B_3.sh` (3),
   `check_B_4.sh` (3), `check_B_5.sh` (4), `check_B_6.sh` (4).
3. After finishing A, and again at the end, run `check_A_0.sh`, `check_A_all.sh` and `check_B_all.sh`. If a rule from an
   earlier spec starts failing, undo your last change before doing anything else.

Do not start a spec while a rule from an earlier spec is failing. Fix one failing rule at a time, lowest
number first. B changes routes that A built (join adds channel members and emits events), so re-run the
A checks after B.1 and B.5.

## How to verify

Run each of these as a **single command on its own**:

```bash
bash /spec/component-A-staged/checks/check_A_0.sh     # baseline; run it on its own
bash /spec/component-A-staged/checks/check_A_1.sh     # ... through check_A_4.sh
bash /spec/component-A-staged/checks/check_A_all.sh
bash /spec/component-B-staged/checks/check_B_1.sh     # ... through check_B_6.sh
bash /spec/component-B-staged/checks/check_B_all.sh
```

Each prints one line per rule and a final summary:

```text
PASS A2.2 guest cannot join or create channels; member creates public only; ...
FAIL B2.4 history: pages 200/200/200 gave '9,8,7|6,5,4|4,3' (want '9,8,7|6,5,4|3', ...)
RESULT: 5 passed, 1 failed
```

- A FAIL line lists what each request returned and what it should have returned. Fix exactly that. A
  FAIL line that starts with `setup:` means an earlier rule is broken; fix that rule first.
- Never run `check_A_all.sh` and `check_B_all.sh` in one command, and never chain a cluster restart with
  a check: each command is cut off at 30 seconds. `check_A_all.sh` takes about 15 seconds and
  `check_B_all.sh` about 20. If one is ever cut off, run its single checks one by one instead.
- A check starts `/app/start.sh` itself if the cluster is not answering. After you change code, restart
  the cluster before checking again: find the process that owns each port and stop it by PID. Never stop
  processes by name.

## Baseline conditions (milestone 0)

The four baseline conditions are the A.0 rules (A0.1 to A0.4), tested by `check_A_0.sh`. They must
still hold when you submit, so run `check_A_0.sh` again at the end. It kills the process on port `8001`
and, if that node does not come back within 3 seconds, restarts the cluster with `/app/start.sh`, so run
it on its own and never chain it with another check.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- `check_A_0.sh` ends with `RESULT: 4 passed, 0 failed`;
- `check_A_all.sh` ends with `RESULT: 19 passed, 0 failed`;
- `check_B_all.sh` ends with `RESULT: 26 passed, 0 failed`;

These 49 rules are necessary, not sufficient. The full task in the grading contract still applies.
