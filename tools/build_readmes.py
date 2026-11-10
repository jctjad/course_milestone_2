#!/usr/bin/env python3
"""Build the /spec/README.md for Runs 3, 4, 5.1, 5.2 and 5.3 from the frozen single-component READMEs.

Kept outside milestone-2-specs/, so the agent never sees it.
  python3 tools/build_readmes.py      -> writes specs-master/readmes/README-run-{3,4,5.1,5.2,5.3}.md

GUIDE section 4: the README may change between runs only where the run requires it (the files it lists, the
order of work, how A and B are checked); everything else stays word for word the same.

Size limit: mini-swe-agent shows only the first and last 5,000 characters of any command output over 10,000,
so an agent never saw the middle of the first, 12-15k combined READMEs. Each README built here must stay under
MAX_CHARS (the build stops otherwise): the file table lists one row per component, and the order of work
comes right after the opening paragraphs. So every shared
paragraph below is copied out of the frozen README-run-1.1-2.1-C.md (and the D and E ones) at build time,
and the build stops if one cannot be found. Only the run-specific parts are written here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R = ROOT / "specs-master" / "readmes"
C_MD = (R / "README-run-1.1-2.1-C.md").read_text()
D_MD = (R / "README-run-1.2-2.2-D.md").read_text()
E_MD = (R / "README-run-1.3-2.3-E.md").read_text()


def between(text, start, end, keep_end=False):
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j + (len(end) if keep_end else 0)].rstrip("\n")


def rows(text, prefix):
    out = [l for l in text.splitlines() if l.startswith(prefix)]
    assert out, prefix
    return out


# ---------- shared text, copied verbatim from the frozen READMEs ----------
TITLE = C_MD.splitlines()[0]
CONTRACT = between(C_MD, "The task description you were given", "changed\ncopies.**", keep_end=True)
USES = between(C_MD, "Each spec has **Uses**", "WebSocket).", keep_end=True)
API = between(C_MD, "## API conventions (every route)", "\n## Order of work")
TIMER = "You have 50 minutes: check the time left with `bash /app/timer.sh` at the start and after each step."
assert TIMER in C_MD
READ = between(C_MD, "**Read each file in full, with its own `cat` command**", "required too.", keep_end=True)
NO_START = between(C_MD, "Do not start a spec while a rule from an earlier step is failing.", "lowest\nnumber first.", keep_end=True)
SUMMARY = "Each ends with a summary line, for example `RESULT: 5 passed, 1 failed`."
assert SUMMARY in C_MD
CUTOFF = between(C_MD, "- Each command is cut off at 30 seconds.", "one by one instead.", keep_end=True)
RESTART = between(C_MD, "- A check starts `/app/start.sh` itself only if no node port is open.", "that can kill you.", keep_end=True)
D_KILLS = between(D_MD, "- `check_D_2.sh` kills the processes", "between checks.", keep_end=True)
E_BROWSER = between(E_MD, "- `check_E_3.sh` kills the process on port `8001`", "seconds each.", keep_end=True)
BASELINE_LIST = between(C_MD, "1. `GET /api/health` on `127.0.0.1:8000`", '`"status": "ok"`.', keep_end=True)
BASELINE_KILL = between(C_MD, "`check_baseline.sh` kills the process on port `8001` and, if that node does not come back within 3",
                        "`/app/start.sh`.", keep_end=True)
SUBMIT_HEAD = "You may submit only when all of these are true, from output you actually saw after your last code change:"
assert SUBMIT_HEAD in C_MD
SUBMIT_TAIL = between(C_MD, "If you changed code after a check last passed", "still applies.", keep_end=True)

# README + the agent's usual first listing of /spec and /app must stay under 10,000; runs with more files in
# /spec (5.x) list more, so they get less room (measured with setup_run.sh, 2026-11-09)
MAX_CHARS = {"3": 9200, "4": 9200, "5.1": 9050, "5.2": 9100, "5.3": 9100}

ORDER_FIRST = ("**Print this file on its own** (`cat /spec/README.md`, nothing else in that command): any output over "
               "10,000 characters gets its middle cut off.")

# ---------- file table: one row per component ----------
T_HEAD = ["| File | What it is |", "|---|---|"]
T_README = rows(C_MD, "| `README.md` |")
T_BASICS = rows(C_MD, "| `basics/BASICS.md` |") + rows(C_MD, "| `basics/checks/check_basics.sh` |") + rows(C_MD, "| `basics/checks/check_baseline.sh` |")
T_BASICS_CHK = rows(C_MD, "| `basics/checks/check_basics.sh` |") + rows(C_MD, "| `basics/checks/check_baseline.sh` |")
SPEC_NAMES = {X: sorted(p.name for p in (ROOT / "specs-master" / f"component-{X}-staged").glob(f"{X}-*.md")) for X in "ABCDE"}
SUBJECT = {"A": "auth, workspaces, roles, invitations, join mode (A.0 is the baseline cluster)",
           "B": "channels, messages, threads, search, pins, mentions, WebSocket events, read state",
           "D": "Redis fan-out, a killed node, a killed Redis, WebSocket speed under load",
           "C": "reactions, DMs and files, slash commands, groups and `@here`, profiles",
           "E": "message edit and delete, the IRC gateway, the web UI"}
ALL_CHECK = {"A": " and `check_A_all.sh` (A.1 to A.4 in one total; not A.0)", "B": " and `check_B_all.sh` (one total)",
             "C": " and `check_C_all.sh` (one total)", "D": "", "E": ""}
N_SPECS = {X: len(SPEC_NAMES[X]) for X in "ABCDE"}
FIRST = {"A": 0, "B": 1, "C": 1, "D": 1, "E": 1}


def t_row(X, specs=True):
    first, last = FIRST[X], FIRST[X] + N_SPECS[X] - 1
    chk = f"checks `component-{X}-staged/checks/check_{X}_{first}.sh` to `check_{X}_{last}.sh`{ALL_CHECK[X]}"
    if specs:
        return f"| `component-{X}-staged/` | Spec {X}: `{SPEC_NAMES[X][0]}` to `{SPEC_NAMES[X][-1]}`; {chk} |"
    return f"| `component-{X}-staged/checks/` | {chk[0].upper()}{chk[1:]} |"


T_HELPERS = "| `*/checks/lib.sh`, `ws.py`, `perf.py`, `irc.py`, `ui.py` | Helpers for the checks. Do not run or edit them |"
T_REG = "| `checks/regression.sh` | The A and B regression suite: all 50 A and B rules in one command. Prints what broke and `REGRESSION: N broken` |"

# ---------- run-specific text ----------
STORE = ("**Store and files.** All three nodes share one store: one SQLite file in WAL mode, a connection per "
         "request that is always closed (`try`/`finally`), a busy wait of 2 seconds or less, and the schema created "
         "once in `start.sh` before the nodes start (`CREATE TABLE IF NOT EXISTS`). `start.sh` must never delete "
         "or reset existing data. Keep every file that changes while the cluster runs (the database and its `-wal` "
         "and `-shm` files, uploads, pid files, logs) under `/var/lib/huddle/` or `/tmp`, never in `/app`.")


STORE_SHORT = ("**Store and files.** All three nodes share one SQLite file in WAL mode: a connection per request, "
               "always closed; the schema created in `start.sh` (`CREATE TABLE IF NOT EXISTS`); `start.sh` never deletes "
               "data. Files that change while the cluster runs go under `/var/lib/huddle/` (created by `start.sh`) or "
               "`/tmp`, never in `/app`.")


def budget_par(no_check_reading, specs=True):
    s = ("**Get every rule right.** Use the whole run: read each FAIL line, fix its cause "
         "(not just the one case the check tried), and rerun the check. Do not get stuck on one rule: if it still "
         "fails after several real attempts, go on to the next step and come back to it before the final checks "
         "(the one exception to the last paragraph of this section). Keep your context small: look at your code "
         "with `grep -n` and `sed -n 'A,Bp'`, not whole files")
    if no_check_reading:
        s += ", and do not print the check scripts: FAIL lines say what was expected and what came back"
    return s + "."


C1 = "C.1 (reactions; E.6 and the web UI use them): `check_C_1.sh`."
E_ALL = "E.1 to E.6: `check_E_1.sh` to `check_E_6.sh`."
AFTER_C5 = (" After C.5, rerun the D and E checks. Event forwarding (WebSocket, IRC, Redis) starts at the newest "
            "event, even after a restart, and replays old events only on `resume`.")
C_REST = "C.2 to C.5: `check_C_2.sh` to `check_C_5.sh`." + AFTER_C5
D_ALL = "D.1 to D.4: `check_D_1.sh` to `check_D_4.sh`."
STEP_RULE = "Build each spec in order and run its check until every rule passes before the next."

SUBMIT_DEC = ["- `check_D_1.sh` to `check_D_4.sh` end with `RESULT: 3 passed, 0 failed`, `5 passed`, `3 passed` and\n  `1 passed`;",
              "- `check_E_1.sh` to `check_E_6.sh` end with `RESULT: 4 passed, 0 failed`, `4 passed`, `5 passed`,\n  `4 passed`, `3 passed` and `3 passed`;",
              "- `check_C_all.sh` ends with `RESULT: 24 passed, 0 failed`."]

E6_NOTE_NO_B = ("E.6 needs threads and reactions: reactions are C.1, and threads are built exactly as E.6 describes "
                "(there is no Spec B in this run).")
E6_NOTE_B = "E.6 needs threads and reactions: reuse B2.6's threads and C.1's reactions."

BASICS_SHORT = """## The basics (build these first)

There is no spec file for the basics in this run. Build only this, then check it with `check_basics.sh`
(BASICS.1 to BASICS.5) and `check_baseline.sh` (M0.1 to M0.4):

- **Cluster:** `/app/start.sh` stays in the foreground and starts three separate node processes on
  `127.0.0.1:8000`, `:8001` and `:8002`. `GET /api/health` returns `200 {"status": "ok", "node_id": <0, 1 or 2>}`.
- {store}
- **Routes:** register, login and `me` (`/api/auth/...`); create a workspace (with a public `#general` owned by
  its creator) and read it by slug; create, join, list members of, set the topic of, archive and unarchive
  channels; post and list messages, with `@username` and `@channel` mentions; and a WebSocket at
  `/api/ws?token=<token>` that rejects a bad token, answers `subscribe` with `subscribed` and its
  `head_event_id`, and delivers `message.created` with the channel's next `event_id` on every node.
  The task description gives every field and status code."""


def wrap(text, width=110):
    """Wrap a paragraph like the frozen READMEs (they wrap near 110 columns), never inside `inline code`."""
    text = re.sub(r"`[^`]*`", lambda m: m.group(0).replace(" ", " "), text)
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    lines.append(cur)
    return "\n".join(lines).replace(" ", " ")


def wrap_item(text, first="", rest="   ", width=110):
    w = wrap(text, width - len(rest)).split("\n")
    return "\n".join([first + w[0]] + [rest + l for l in w[1:]])


def readme(intro, budget, budget_text, steps, final, scope, files, uses, verify_extra, baseline, submit,
           basics_short=False, read=True, summary=SUMMARY):
    out = [TITLE, "", wrap(ORDER_FIRST), "", wrap(intro), "", CONTRACT,
           "", "## Order of work", "", wrap(TIMER + " Rough budget: " + budget), "", wrap(budget_text)]
    if read:
        out += ["", READ]
    out += [""] + [wrap_item(st, f"{i}. ") for i, st in enumerate(steps + [final], 1)]
    out += ["", NO_START, "", "## Scope", "", scope]
    if basics_short:
        out += ["", BASICS_SHORT.replace("{store}", wrap_item(STORE_SHORT, rest="  "))]
    out += ["", API, "", "## Files in /spec", "", *T_HEAD, *files]
    if uses:
        out += ["", USES]
    out += ["", "## How to verify", "",
            wrap("Run each check as a **single command on its own**, for example "
                 "`bash /spec/component-D-staged/checks/check_D_1.sh`. " + summary),
            "", CUTOFF, RESTART,
            *[v if "\n" in v else wrap_item(v[2:], "- ", "  ") for v in verify_extra],
            "", "## Baseline conditions (milestone 0)", "", baseline, "",
            "## Before you submit", "", SUBMIT_HEAD, "", *submit, "", SUBMIT_TAIL, ""]
    return "\n".join(out)


BASELINE_3_4 = ("These must hold when you submit. `check_baseline.sh` tests them (M0.1 to M0.4):\n\n"
                f"{BASELINE_LIST}\n\n{BASELINE_KILL}")
assert ("These must hold when you submit. `check_baseline.sh` tests them (M0.1 to M0.4):") in C_MD
INTRO_TAIL = ("Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen **every rule listed in "
              "Before you submit** pass. Do not submit unless every one of them passes.")
OWNERS_SHORT = ("D owns `start.sh` supervision, Redis and cross-node delivery; C owns the reaction, DM, file, group "
                "and profile APIs; E owns message edit and delete, the IRC gateway and the web UI.")
FINAL_DCE = ("At the end, run every check above once more, each on its own (`check_C_all.sh` for all of C). If a rule "
             "from an earlier step starts failing, fix it before doing anything else.")

runs = {}

# ---------- Run 3: C, D and E together (specs + checks, verbose) ----------
runs["3"] = readme(
    intro=("This run builds **three components** on top of a small set of basics, as staged specs that build on "
           "each other: **D (cluster robustness)**, then **E (IRC, web UI and message edits)**, then **C (features "
           "built on messages)**, with C.1 (reactions) before E, which uses it. " + INTRO_TAIL),
    budget="basics 6 minutes, D 7, C.1 3, E 16, C.2 to C.5 12, final checks 4.",
    budget_text=budget_par(True),
    steps=["`cat /spec/basics/BASICS.md`, then build the basics until `check_basics.sh` and `check_baseline.sh` pass.",
           STEP_RULE + " " + D_ALL, C1, E_ALL, C_REST],
    final=FINAL_DCE,
    scope=wrap("Work only on the basics and the D, C and E spec files. Do NOT build roles (beyond the owner), "
               "invitations, search, pins or read state in this run. " + E6_NOTE_NO_B),
    files=[*T_README, *T_BASICS, t_row("D"), t_row("E"), t_row("C"), T_HELPERS], uses=True,
    verify_extra=[D_KILLS, E_BROWSER],
    baseline=BASELINE_3_4,
    submit=["- `check_basics.sh` ends with `RESULT: 5 passed, 0 failed` and `check_baseline.sh` with `4 passed`;", *SUBMIT_DEC])

# ---------- Run 4: C, D and E checks only (no spec files, verbose) ----------
runs["4"] = readme(
    intro=("This run builds **three components** on top of a small set of basics: **D (cluster "
           "robustness)**, then **E (IRC, web UI and message edits)**, then **C (features built on messages)**, with "
           "C's first check (reactions) before E, which uses it. There are **no spec files** in this run: the checks "
           "are the spec. " + INTRO_TAIL),
    budget="basics 6 minutes, D 7, C.1 3, E 16, C.2 to C.5 12, final checks 4.",
    budget_text=budget_par(False, specs=False),
    steps=["Build the basics below until `check_basics.sh` and `check_baseline.sh` pass.",
           "D: run `check_D_1.sh` to `check_D_4.sh` in order and make each pass before the next.",
           "C's reactions (E.6 and the web UI use them): `check_C_1.sh`.", "E, the same way: `check_E_1.sh` to `check_E_6.sh`.",
           "The rest of C, the same way: `check_C_2.sh` to `check_C_5.sh`." + AFTER_C5],
    final=FINAL_DCE,
    scope=wrap("Work only on the basics and the rules the D, C and E checks test. Do NOT build roles (beyond the owner), "
               "invitations, search, pins or read state in this run. The rule numbers in the checks' output (for "
               "example `C2.3`) belong to spec files that are not part of this run: the task description you were given "
               "has every route, field and status code. E.6's rules need threads and reactions: build reactions first, "
               "then threads for E.6."),
    files=[*T_README, *T_BASICS_CHK, t_row("D", False), t_row("E", False), t_row("C", False), T_HELPERS], uses=False,
    verify_extra=[D_KILLS, E_BROWSER],
    baseline=BASELINE_3_4,
    submit=["- `check_basics.sh` ends with `RESULT: 5 passed, 0 failed` and `check_baseline.sh` with `4 passed`;", *SUBMIT_DEC],
    basics_short=True, read=False)

# ---------- Runs 5.x: the whole app ----------
WHOLE = ("This run builds **the whole app** as staged specs: A (roles and invitations) and B (channels and message "
         "history) first, then D (cluster robustness), then E (IRC, web UI and message edits), then C (features built "
         "on messages), with C.1 (reactions) before E, which uses it. ")
BUDGET_5 = "A and B 14 minutes, D 5, C.1 2, E 15, C.2 to C.5 9, final checks 5."
AB_MAP = ("A0 baseline, A1 auth, workspaces and shared store, A2 roles, A3 invitations, A4 join mode, B1 "
          "channels, B2 messages and threads, B3 search and pins, B4 mentions, B5 events, B6 read state")
REG_AFTER = "Rerun `regression.sh` after D, E and C and fix what it reports: they can break A and B rules."
REG_BULLET = ("- `regression.sh` takes up to 27 seconds. It restarts the cluster (A1.8) and "
              "kills port `8001` (A0.4). `not checked`: a rule not reached in time.")
REG_SUMMARY = SUMMARY + " `regression.sh` ends with `REGRESSION: N broken`."
SUBMIT_REG = "- `regression.sh` ends with `REGRESSION: 0 broken`;"
BASELINE_REG_LIST = baseline_note = None
NAMES_SPEC = ("Where a spec's **Uses** table names a function from `A.1`, `B.2` and so on, it is the function of that "
              "name in Spec A or B.")
NAMES_NOSPEC = ("Where a spec's **Uses** table names a function from `A.1`, `B.2` and so on, there is no A or B spec "
                "file in this run: give your A and B code functions with exactly those names.")


def scope_5(basics_done, names, store):
    s = wrap("Build A, B, D, C and E, and nothing that is not in them. \"The basics\" that the D, C "
             f"and E specs mention are components A and B: they work when {basics_done}. {names} {E6_NOTE_B}")
    return s + ("\n\n" + wrap(STORE_SHORT) if store else "")


A_SPEC_BASELINE = lambda by: (f"These must hold when you submit: they are rules A0.1 to A0.4 in "
                              f"`component-A-staged/A-0-baseline.md`, and {by} tests them.")

runs["5.1"] = readme(
    intro=(WHOLE + "A and B have **no spec files** in this run, only a regression suite, `checks/regression.sh`, that "
           "tests every A and B rule. " + INTRO_TAIL),
    budget=BUDGET_5, budget_text=budget_par(True),
    steps=["Build A and B from the task description until `bash /spec/checks/regression.sh` ends with "
           "`REGRESSION: 0 broken`, in its rule order: " + AB_MAP + ".",
           STEP_RULE + " " + D_ALL + " " + REG_AFTER, C1, E_ALL, C_REST],
    final=FINAL_DCE.replace("every check above", "`regression.sh` and every check above"),
    scope=scope_5("`regression.sh` ends with `REGRESSION: 0 broken`", NAMES_NOSPEC, True),
    files=[*T_README, T_REG, t_row("D"), t_row("E"), t_row("C"), T_HELPERS], uses=True,
    verify_extra=[REG_BULLET, D_KILLS, E_BROWSER],
    baseline=wrap("These must hold when you submit, and `regression.sh` tests them (A0.1 to A0.4): health on all three "
                  "ports with `node_id` `0`, `1` and `2`; a bad-token WebSocket rejected; and `8000` and `8002` still "
                  "answering after the process on `8001` is killed."),
    submit=[SUBMIT_REG, *SUBMIT_DEC], summary=REG_SUMMARY)

runs["5.2"] = readme(
    intro=(WHOLE + "A and B have no checks of their own in this run: one regression suite, `checks/regression.sh`, "
           "tests every A and B rule. " + INTRO_TAIL),
    budget=BUDGET_5, budget_text=budget_par(True),
    steps=["A.0 to A.4, then B.1 to B.6, in order. After each, run `regression.sh` until every rule of that step and "
           "the steps before it passes (rules of steps you have not built yet fail until you build them).",
           STEP_RULE + " " + D_ALL + " " + REG_AFTER, C1, E_ALL, C_REST],
    final=FINAL_DCE.replace("every check above", "`regression.sh` and every check above"),
    scope=scope_5("`regression.sh` ends with `REGRESSION: 0 broken`", NAMES_SPEC, False),
    files=[*T_README, t_row("A").split("; checks")[0] + " |", t_row("B").split("; checks")[0] + " |", T_REG,
           t_row("D"), t_row("E"), t_row("C"), T_HELPERS], uses=True,
    verify_extra=[REG_BULLET, D_KILLS, E_BROWSER],
    baseline=A_SPEC_BASELINE("`regression.sh`"),
    submit=[SUBMIT_REG, *SUBMIT_DEC], summary=REG_SUMMARY)

runs["5.3"] = readme(
    intro=(WHOLE + "Each spec has its own check. " + INTRO_TAIL),
    budget=BUDGET_5, budget_text=budget_par(True),
    steps=[STEP_RULE + " A.0 to A.4: `check_A_0.sh` to `check_A_4.sh`.",
           "B.1 to B.6: `check_B_1.sh` to `check_B_6.sh`. B changes routes that A built, so run `check_A_all.sh` again "
           "after B.1 and B.5.",
           D_ALL + " After each of D, E and C, run `check_A_0.sh`, `check_A_all.sh` and `check_B_all.sh` again: they can "
           "break A and B rules that already passed.", C1, E_ALL, C_REST],
    final=FINAL_DCE,
    scope=scope_5("`check_A_0.sh`, `check_A_all.sh` and `check_B_all.sh` pass", NAMES_SPEC, False),
    files=[*T_README, t_row("A"), t_row("B"), t_row("D"), t_row("E"), t_row("C"), T_HELPERS], uses=True,
    verify_extra=["- `check_A_1.sh` and `check_A_all.sh` restart the cluster once with its data moved aside (A1.8).",
                  D_KILLS, E_BROWSER],
    baseline=A_SPEC_BASELINE("`check_A_0.sh`"),
    submit=["- `check_A_0.sh` ends with `RESULT: 4 passed, 0 failed`, `check_A_all.sh` with `20 passed` and\n"
            "  `check_B_all.sh` with `26 passed`;", *SUBMIT_DEC])

for run, text in runs.items():
    assert len(text) < MAX_CHARS[run], f"README-run-{run}.md is {len(text)} characters (limit {MAX_CHARS[run]})"
    p = R / f"README-run-{run}.md"
    p.write_text(text)
    print(f"wrote {p.relative_to(ROOT)} ({len(text)} characters, {len(text.splitlines())} lines)")
