# Milestone 2 report

- Name: Jacob Tjaden
- UNI: jt3722

All results are from my own checks.

Summary script that prints useful summary info about the run: `python3 tools/summarize_run.py --latest --run 2.2 --component D --repeat 1` (used to fill table).

## Runs

One row per repeat of each run (22 in total). "Run" is the run in the run plan in README Setup.

| Run | Component | Repeat | Job folder | Check results | Cost | Ran checks (times) | Submitted with failures? | Problems |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.1 | C | 1 | `jobs/2026-11-06__10-55-09` | C: 24 passed, 0 failed (basics 5/5, baseline 4/4) | $1.00 | 30 | no | Got verbose output by sourcing a check (6.4 of 8.7 min) |
| 1.1 | C | 2 | `jobs/2026-11-06__10-55-11` | C: 24 passed, 0 failed (basics 5/5, baseline 4/4) | $0.57 | 20 | no | Got verbose output by sourcing a check (3.7 of 5.5 min) |
| 1.2 | D | 1 | `jobs/2026-11-06__15-53-35` | D: 12 passed, 0 failed (basics 5/5, baseline 4/4) | $0.41 | 23 | no | Got verbose output by sourcing a check (2.5 of 4.7 min) |
| 1.2 | D | 2 | `jobs/2026-11-06__15-54-41` | D: 12 passed, 0 failed (basics 5/5, baseline 4/4) | $0.26 | 15 | no | Got verbose output by sourcing a check (2.2 of 2.9 min) |
| 1.3 | E | 1 | `jobs/2026-11-06__14-51-03` | E: 23 passed, 0 failed (basics 5/5, baseline 4/4) | $1.29 | 37 | no | Ran `ui.py` directly for verbose output (8.9 of 13.1 min) |
| 1.3 | E | 2 | `jobs/2026-11-06__14-51-30` | E: 9 passed, 14 failed (basics 4/5, baseline 4/4) | $1.89 | 54 | yes | Ran `ui.py` directly, broke its own server, fresh rerun: E 9/23, basics 1/5 |
| 2.1 | C | 1 | `jobs/2026-11-06__11-54-33` | C: 24 passed, 0 failed (basics 5/5, baseline 4/4) | $0.25 | 14 | no | 1 check cut off at 30 s |
| 2.1 | C | 2 | `jobs/2026-11-06__11-55-01` | C: 24 passed, 0 failed (basics 5/5, baseline 4/4) | $0.23 | 14 | no | None |
| 2.2 | D | 1 | `jobs/2026-11-06__16-05-24` | D: 12 passed, 0 failed (basics 5/5, baseline 4/4) | $0.21 | 12 | no | None |
| 2.2 | D | 2 | `jobs/2026-11-06__16-05-44` | D: 12 passed, 0 failed (basics 5/5, baseline 4/4) | $0.47 | 20 | no | D.4 race found and fixed from the verbose output |
| 2.3 | E | 1 | `jobs/2026-11-06__15-40-39` | E: 23 passed, 0 failed (basics 5/5, baseline 4/4) | $0.77 | 36 | no | 1 check cut off at 30 s |
| 2.3 | E | 2 | `jobs/2026-11-06__15-40-41` | E: 23 passed, 0 failed (basics 5/5, baseline 4/4) | $0.49 | 24 | no | None |
| 3 | C, D, E | 1 | `jobs/2026-11-09__16-58-16` | C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed (basics 5/5, baseline 4/4) | $3.81 | 77 | no | None |
| 3 | C, D, E | 2 | `jobs/2026-11-09__16-58-40` | C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed (basics 5/5, baseline 4/4) | $2.02 | 54 | no | None |
| 4 | C, D, E | 1 | `jobs/2026-11-09__17-48-13` | C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed (basics 5/5, baseline 4/4) | $2.80 | 80 | no | 1 D.4 check cut off at 30 s |
| 4 | C, D, E | 2 | `jobs/2026-11-09__17-48-50` | C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed (basics 5/5, baseline 4/4) | $3.24 | 73 | no | App hung on D.4 (5 checks cut off at 30 s) until fixed |
| 5.1 | A to E | 1 | `jobs/2026-11-09__17-50-49` | A and B: 0 broken; C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed | $6.25 | 107 | no | Left A/B at 30 broken, built C.1 to E.6, then came back |
| 5.1 | A to E | 2 | `jobs/2026-11-09__17-51-18` | A and B: 0 broken; C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed | $6.51 | 89 | no | A1.8 test couldn't stop the app's restart loop |
| 5.2 | A to E | 1 | `jobs/2026-11-09__19-58-16` | A and B: 0 broken; C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed | $4.13 | 77 | no | 7 C checks cut off at 30 s |
| 5.2 | A to E | 2 | `jobs/2026-11-09__19-58-34` | A and B: 0 broken; C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed | $6.95 | 90 | no | App hung on E.5 (10 checks cut off at 30 s); used 46 of 50 min |
| 5.3 | A to E | 1 | `jobs/2026-11-09__19-59-19` | A: 24 passed, 0 failed; B: 26 passed, 0 failed; C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed | $3.52 | 87 | no | None |
| 5.3 | A to E | 2 | `jobs/2026-11-09__19-59-46` | A: 24 passed, 0 failed; B: 26 passed, 0 failed; C: 24 passed, 0 failed; D: 12 passed, 0 failed; E: 23 passed, 0 failed | $6.81 | 128 | no | A C.4 edit deleted the POST handler; its rewrite dropped A/B routes (fixed after final A/B checks) |


## Experiments

### 1) Feedback

> Do you find that adding detailed output to the checks helps the agent?

#### Runs compared and why

The comparison is between Runs 1.1-1.3 (score-only) with 2.1-2.3 (verbose). Each pair builds the same component (C, D or E) from the same specs and checks. Each run runs twice. The only difference is the testing mode, so any gap in results in a 1.x run vs. 2.x run comes from how much the checks print.

#### Supporting results

The score-only runs generally passed all checks same as the verbose runs. However, the biggest difference is in agent time and cost. The agent generally took twice as long with twice the cost in score-only mode compared to verbose mode.

| Component | Score-only r1 | Score-only r2 | Verbose r1 | Verbose r2 |
| --- | --- | --- | --- | --- |
| C | 24/24, $1.00, 8.7 min | 24/24, $0.57, 5.5 min | 24/24, $0.25, 4.0 min | 24/24, $0.23, 3.3 min |
| D | 12/12, $0.41, 4.7 min | 12/12, $0.26, 2.9 min | 12/12, $0.21, 2.9 min | 12/12, $0.47, 4.6 min |
| E | 23/23, $1.29, 13.1 min | 9/23, $1.89, 21.1 min | 23/23, $0.77, 8.5 min | 23/23, $0.49, 5.9 min |

#### Answer

Yes, detailed output made the agent faster and cheaper. The comparison ended up being messy because the score-only agents eventually got the verbose output anyway by looking through ui.py, but it took awhile and likely would have performed even worse had it not found the verbose output at all.

#### Consistency across repeats

Component C --> consistent. Both verbose repeats were faster and cheaper than the score-only repeats.
Component E --> consistent (on cost and time), with verbose being faster and cheaper. However, one of the score-only runs ended up failing a number of tests.
Component D --> not consistent. Verbose repeat 2 cost more than either score-only repeat (it ran into a race condition).

#### Other evidence

All six score-only agents found a way to get detailed check output, even though the README forbids changing check settings:
- 1.1 and 1.2: they sourced a check and set FEEDBACK=verbose.
- 1.3: they ran the browser helper ui.py directly, which always prints details.

In 2.2 repeat 2, the D.4 failure line (posts_ok=76/90) led the agent to a race in which two nodes handed out the same event number, and it fixed that in two steps. In score-only mode it would have seen only RESULT: 0 passed, 1 failed, so verbose helped out here.

### 2) Tests as the spec

> Can the checks alone replace the written rules?

#### Runs compared and why

The comparison is between Run 3 with Run 4. Both build components C, D, E together in verbose mode with the same checks and README structure. The key difference is that Run 3 also gets the written spec files, while Run 4 only gets the checks. Differences between the runs stem from the written rules.

#### Supporting results

All four runs passed every check, at about the same average cost and time (about $3 and 22 min).

| Run | Repeat | C | D | E | Cost | Time |
| --- | --- | --- | --- | --- | --- | --- |
| 3 (specs + checks) | 1 | 24/24 | 12/12 | 23/23 | $3.81 | 29.1 min |
| 3 (specs + checks) | 2 | 24/24 | 12/12 | 23/23 | $2.02 | 14.1 min |
| 4 (checks only) | 1 | 24/24 | 12/12 | 23/23 | $2.80 | 19.7 min |
| 4 (checks only) | 2 | 24/24 | 12/12 | 23/23 | $3.24 | 23.6 min |

#### Answer

Yes, for these components. Without the written rules the agent passed the same checks at about the same cost and time.

#### Consistency across repeats

Consistent. Both repeats of both runs passed everything, and their costs overlap.

#### Other evidence

Without specs, the agent read the check scripts to learn the rules, even though the README says not to. The FAIL lines also say what was expected, so the checks worked as the spec.

### 3) Scale

> Do you find that a component passes fewer of its checks when the agent builds it together with
> other components, rather than on its own?

#### Runs compared and why

The comparison is between Runs 2.1–2.3 (each component built alone) and Run 3 (C, D and E built together). Both are in verbose mode and use the same specs, checks, and basics. Any gap in results comes from building components together or alone. Runs 5.1–5.3 (the whole app, A to E) add a larger scale to the same comparison.

#### Supporting results

Every component passed all its checks at every scale. Building together cost more than the alone runs added up.

| Build | Repeat | C | D | E | Cost | Time |
| --- | --- | --- | --- | --- | --- | --- |
| Alone (2.1 + 2.2 + 2.3) | 1 | 24/24 | 12/12 | 23/23 | $1.23 | 15.4 min |
| Alone (2.1 + 2.2 + 2.3) | 2 | 24/24 | 12/12 | 23/23 | $1.19 | 13.8 min |
| Together (Run 3) | 1 | 24/24 | 12/12 | 23/23 | $3.81 | 29.1 min |
| Together (Run 3) | 2 | 24/24 | 12/12 | 23/23 | $2.02 | 14.1 min |
| Whole app (5.1-5.3) | all 6 | 24/24 | 12/12 | 23/23 | $3.52-$6.95 | 21.5-46.0 min |

#### Answer

No. Each component passed the same checks together as alone. Scale cost more time and money, not more failed checks.

#### Consistency across repeats

Consistent for check results: all repeats passed everything. Cost varied more at scale.

#### Other evidence

As a note, I had my single-component runs capped at $3 whereas my combined runs have a $10 cap. The single-component runs never hit the cap so it shouldn't be an issue, but noting it here anyway.

### 4) Regression

> a) Do you find that building components C, D, and E breaks features from A and B that were
> already working?

> b) Does it make a difference whether the agent has Specs A and B or only their regression
> tests?

> c) Does it make a difference whether the A and B checks are given as a regression suite or as
> plain checks?

#### Runs compared and why

a) The comparison is between the A and B results partway through Runs 5.1–5.3 and at the end. In each run, the agent works on A and B first, so any A or B rule that breaks later was broken by building C, D and E.

b) The comparison is between Run 5.1 and Run 5.2. Both give the agent the A and B regression suite and Specs C, D and E. The key difference is that Run 5.2 also gets the written Specs A and B, while Run 5.1 gets only the regression tests. Differences between the runs come from having the written A and B rules.

c) The comparison is between Run 5.2 and Run 5.3. Both give the agent Specs A to E. The key difference is how the A and B checks are given: one regression suite in Run 5.2, and the plain per-spec checks in Run 5.3. Differences between the runs come from the form of the A and B checks.

#### Supporting results

All six runs ended with every A and B rule passing. "A/B broken before C" is how many A and B rules still failed when the agent started C.

| Run | Repeat | A/B at end | A/B broken before C | A/B broken by C, D, E | Cost | Time |
| --- | --- | --- | --- | --- | --- | --- |
| 5.1 (suite, no Specs A/B) | 1 | 0 broken | 30 | none | $6.25 | 32.4 min |
| 5.1 (suite, no Specs A/B) | 2 | 0 broken | 27 | 1 rule (fixed) | $6.51 | 33.5 min |
| 5.2 (suite + Specs A/B) | 1 | 0 broken | 0 | none | $4.13 | 28.5 min |
| 5.2 (suite + Specs A/B) | 2 | 0 broken | 0 | none | $6.95 | 46.0 min |
| 5.3 (plain checks + Specs A/B) | 1 | 50/50 | 0 | none | $3.52 | 21.5 min |
| 5.3 (plain checks + Specs A/B) | 2 | 50/50 | 0 | many routes (fixed) | $6.81 | 31.3 min |

#### Answer

a) Sometimes, but the agent caught it by rerunning the A and B checks and fixed it before submitting.

b) Yes in how the agent worked, not in the final result. With the specs it finished A and B first; without them it left A and B half done. Both ended with A and B fully passing.

c) No clear difference. Both forms ended with all A and B rules passing at similar cost.

#### Consistency across repeats

a) Mixed: breakage in 2 of 6 runs.
b) Consistent: both 5.1 repeats moved on early, both 5.2 repeats did not.
c) Consistent for results; cost varied as much within a run as between runs.

#### Other evidence

After 5.1 I fixed a bug in the A1.8 check (it could not stop an app that restarts its own nodes), so 5.2 and 5.3 used the fixed version. Rerun in a fresh container, the final 5.3 apps passed 109/109 (repeat 2) and 108/109 (repeat 1); repeat 1's one failure (E5.2) came from a timing issue in my check, not the app.
