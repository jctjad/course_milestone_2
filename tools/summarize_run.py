#!/usr/bin/env python3
"""Summarize one run (a harbor job folder) for the report.md runs table.

Reads only the job folder; changes nothing. Kept outside milestone-2-specs/ so the agent never sees it.

Usage (from the repo root):
  python3 tools/summarize_run.py tasks/slack-clone-course/jobs/<timestamp>
  python3 tools/summarize_run.py --latest
  python3 tools/summarize_run.py --latest --run 2.2 --component D --repeat 1   # also prints a table row

"Check results" are what the agent's own checks printed the last time it ran them before submitting,
per component. If a component's combined check (check_X_all.sh) ran after all of its single checks,
its total is used; otherwise the latest result of each single check is added up.
"""
import argparse
import json
import re
import sys
from pathlib import Path

JOBS = Path(__file__).resolve().parent.parent / "tasks" / "slack-clone-course" / "jobs"
# a check is "run" when bash/sh executes it (reading it with cat/grep/sed does not count)
CHECK_RE = re.compile(r"(?:\b(?:bash|sh)(?:\s+-\w+)*\s+|(?:^|[;&|(]\s*))/spec/(?:[^\s;&|'\"]*/)?checks/((?:check_[A-Za-z0-9_]+)|regression)\.sh", re.M)
RESULT_RE = re.compile(r"RESULT: (\d+) passed, (\d+) failed")
REGRESSION_RE = re.compile(r"REGRESSION: (\d+) broken")
RULE_RE = re.compile(r"^\s*(PASS|FAIL) (\S+)", re.M)
ORDER = ["baseline", "basics", "A", "B", "A and B", "C", "D", "E"]


def component_of(script):
    if script == "check_baseline":
        return "baseline"
    if script == "check_basics":
        return "basics"
    if script == "regression":
        return "A and B"
    m = re.match(r"check_([A-Za-z])_", script)
    return m.group(1).upper() if m else script


def summarize_trial(trial):
    steps = json.loads((trial / "agent" / "trajectory.json").read_text())["steps"]
    info = json.loads((trial / "agent" / "mini-swe-agent.trajectory.json").read_text()).get("info", {})
    runs = []  # one entry per check invocation: (step index, script, component, result, timed_out)
    rules = {}  # component -> {rule id: (step index, PASS/FAIL)}
    timeouts = truncated = 0
    submitted_step = None
    for idx, s in enumerate(steps):
        if s.get("source") != "agent":
            continue
        cmd = "\n".join(re.findall(r"```mswea_bash_command\n(.*?)\n```", s.get("message") or "", re.S))
        out = " ".join(r.get("content", "") for r in (s.get("observation") or {}).get("results", []))
        timed_out = "timed out after" in out
        timeouts += timed_out
        truncated += "output of your last command was too long" in out
        if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in cmd:
            submitted_step = idx
        scripts = CHECK_RE.findall(cmd)
        if not scripts:
            continue
        results = [("count", int(p), int(f)) for p, f in RESULT_RE.findall(out)]
        results += [("broken", int(b), None) for b in REGRESSION_RE.findall(out)]
        ran_any = bool(results) or timed_out or bool(RULE_RE.search(out))
        for i, script in enumerate(scripts):
            comp = component_of(script)
            res = results[i] if i < len(results) else None
            if not ran_any:   # nothing from any check: the file did not exist or an earlier && failed
                continue
            runs.append((idx, script, comp, res, timed_out))
        if len(set(component_of(x) for x in scripts)) == 1:
            comp = component_of(scripts[0])
            for status, rule in RULE_RE.findall(out):
                if rule == "0":   # "FAIL 0 cluster is not answering": a setup failure, not a rule
                    continue
                if submitted_step is None:
                    rules.setdefault(comp, {})[rule] = (idx, status)
    if submitted_step is not None:
        runs = [r for r in runs if r[0] < submitted_step]

    final = {}
    for comp in sorted({r[2] for r in runs}, key=lambda c: ORDER.index(c) if c in ORDER else 99):
        mine = [r for r in runs if r[2] == comp]
        latest = {}
        for r in mine:
            latest[r[1]] = r
        alls = [r for s, r in latest.items() if s.endswith("_all")]
        singles = [r for s, r in latest.items() if not s.endswith("_all")]
        if alls and all(alls[0][0] >= r[0] for r in singles):
            chosen = alls[:1]
        else:
            chosen = singles or alls
        missing = [r[1] for r in chosen if r[3] is None]
        latest_rules = rules.get(comp, {})
        if comp == "A and B":
            broken = sum(r[3][1] for r in chosen if r[3])
            text, failing = f"A and B: {broken} broken", broken > 0
        elif latest_rules:
            # verbose mode: the last PASS/FAIL seen for every rule is the most precise final state
            p = sum(1 for _, st in latest_rules.values() if st == "PASS")
            f = sum(1 for _, st in latest_rules.values() if st == "FAIL")
            text, failing, missing = f"{comp}: {p} passed, {f} failed", f > 0, []
        else:
            p = sum(r[3][1] for r in chosen if r[3])
            f = sum(r[3][2] for r in chosen if r[3])
            text, failing = f"{comp}: {p} passed, {f} failed", f > 0
        if missing:
            text += f" (no RESULT from {', '.join(missing)})"
        if comp != "A and B" and not latest_rules:
            text += " [score_only: from RESULT lines]"
        failed_rules = sorted(rule for rule, (_, st) in rules.get(comp, {}).items() if st == "FAIL")
        final[comp] = {"text": text, "failing": failing or bool(missing), "failed_rules": failed_rules,
                       "times": len(mine), "timed_out": sum(r[4] for r in mine)}

    res = json.loads((trial / "result.json").read_text())
    ae = res.get("agent_execution") or {}
    fmt = trial / "agent" / "format_errors.jsonl"
    return {
        "trial": trial.name,
        "cost": (info.get("model_stats") or {}).get("instance_cost"),
        "exit": info.get("exit_status"),
        "start": ae.get("started_at"), "end": ae.get("finished_at"),
        "exception": res.get("exception_info"),
        "checks_run": len(runs), "timeouts": timeouts, "truncated": truncated,
        "format_errors": len(fmt.read_text().splitlines()) if fmt.exists() else 0,
        "final": final,
    }


def minutes(a, b):
    from datetime import datetime
    try:
        return round((datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 60, 1)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", nargs="?", help="job folder, e.g. tasks/slack-clone-course/jobs/2026-11-03__11-10-06")
    ap.add_argument("--latest", action="store_true", help="use the newest folder in tasks/slack-clone-course/jobs")
    ap.add_argument("--run"); ap.add_argument("--component"); ap.add_argument("--repeat")
    a = ap.parse_args()
    if a.latest:
        jobs = sorted(p for p in JOBS.iterdir() if p.is_dir()) if JOBS.exists() else []
        if not jobs:
            sys.exit(f"no job folders in {JOBS}")
        job = jobs[-1]
    elif a.job:
        job = Path(a.job)
    else:
        ap.error("give a job folder or --latest")
    trials = sorted(p for p in job.iterdir() if (p / "agent" / "trajectory.json").exists())
    if not trials:
        sys.exit(f"no finished trial with agent/trajectory.json in {job}")
    for t in trials:
        s = summarize_trial(t)
        print(f"Job folder:   {job.parent.name}/{job.name}   (trial {s['trial']})")
        print(f"Cost:         ${s['cost']:.2f}" if s["cost"] is not None else "Cost:         unknown")
        print(f"Agent time:   {minutes(s['start'], s['end'])} min   exit: {s['exit']}   exception: {s['exception'] or 'none'}")
        print(f"Checks run:   {s['checks_run']} times before submitting")
        for comp, f in s["final"].items():
            extra = f"   failing at the end: {', '.join(f['failed_rules'])}" if f["failed_rules"] else ""
            to = f", {f['timed_out']} cut off at 30 s" if f["timed_out"] else ""
            print(f"  {f['text']:<40} (ran {f['times']}x{to}){extra}")
        failing = any(f["failing"] for f in s["final"].values())
        print(f"Submitted with failures: {'yes' if failing else 'no'}")
        problems = []
        if s["exit"] != "Submitted":
            problems.append(f"did not submit ({s['exit']})")
        if s["exception"]:
            problems.append("harness exception")
        if s["timeouts"]:
            problems.append(f"{s['timeouts']} commands cut off at 30 s")
        if s["truncated"]:
            problems.append(f"{s['truncated']} outputs truncated")
        if s["format_errors"]:
            problems.append(f"{s['format_errors']} format errors")
        print(f"Problems (automatic, add your own): {'; '.join(problems) or 'none found'}")
        if a.run:
            results = "; ".join(f["text"] for c, f in s["final"].items() if c not in ("baseline", "basics"))
            cost = f"${s['cost']:.2f}" if s["cost"] is not None else ""
            print("\nTable row (paste into report.md, then edit the Problems column):")
            print(f"| {a.run} | {a.component or ''} | {a.repeat or ''} | `{job.parent.name}/{job.name}` | {results} | {cost} "
                  f"| {s['checks_run']} | {'yes' if failing else 'no'} | {'; '.join(problems) or ''} |")
        print()


if __name__ == "__main__":
    main()
