#!/usr/bin/env bash
# Assemble milestone-2-specs/ (the agent's /spec) for one run, the same way every time.
# Kept outside milestone-2-specs/, so the agent never sees it.
#
# Usage (from anywhere):
#   tools/setup_run.sh <run>                run = 1.1 1.2 1.3 2.1 2.2 2.3 3 4 5.1 5.2 5.3 (a graded run)
#   tools/setup_run.sh <run> --practice     same folder, but no spec-copies/ entry and no log line
#   add --force to skip the "an agent run is still going" guard
# Runs 3 to 5.3 need A, B, the regression suite and their READMEs frozen (specs-master/FROZEN-AB.sha256)
# before a graded setup; until then use --practice.
#
# What it does:
#   1. checks the frozen checksums in specs-master/
#   2. refuses if an agent run is still going (its spec must not change under it)
#   3. rebuilds milestone-2-specs/ from specs-master/ with exactly the files the run plan lists for this run
#   4. sets the testing mode in the copied lib.sh files only: 1.x -> score_only, everything else -> verbose
#   5. checks every copied file matches specs-master (apart from that one FEEDBACK line), and that every file
#      the README names is there
#   6. graded runs: saves spec-copies/<run>/ the first time; for the second repeat, checks it is identical
#   7. graded runs: logs the setup time in spec-copies/setup-log.txt, to match job folders to runs later
# Testing only: SETUP_SPEC_DIR and SETUP_COPIES_DIR point it at other folders.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MASTER="$ROOT/specs-master"
TASK="$ROOT/tasks/slack-clone-course"
LIVE_SPEC="$TASK/course-staff-files/final_task/environment/milestone-specs/milestone-2-specs"
SPEC="${SETUP_SPEC_DIR:-$LIVE_SPEC}"
COPIES="${SETUP_COPIES_DIR:-$ROOT/spec-copies}"

USAGE="usage: tools/setup_run.sh <run> [--practice] [--force]   (run = 1.1 1.2 1.3 2.1 2.2 2.3 3 4 5.1 5.2 5.3)"
RUN="${1:?$USAGE}"; shift
PRACTICE=no; FORCE=no
for a in "$@"; do
  case "$a" in --practice) PRACTICE=yes ;; --force) FORCE=yes ;; *) echo "$USAGE" >&2; exit 1 ;; esac
done
die() { echo "setup_run: $*" >&2; exit 1; }

# PARTS: what goes into /spec, as <specs-master folder>:<all|md|checks>[:<name in /spec>]
#   all = specs and checks, md = spec files only, checks = checks only
MODE=verbose; FROZEN="FROZEN-C.sha256 FROZEN-D.sha256 FROZEN-E.sha256"; NEEDS_AB=no
case "$RUN" in
  1.1|2.1) README=README-run-1.1-2.1-C.md; PARTS="basics:all component-C-staged:all" ;;
  1.2|2.2) README=README-run-1.2-2.2-D.md; PARTS="basics:all component-D-staged:all" ;;
  1.3|2.3) README=README-run-1.3-2.3-E.md; PARTS="basics:all component-E-staged:all" ;;
  3)   README=README-run-3.md;   PARTS="basics:all component-C-staged:all component-D-staged:all component-E-staged:all"; NEEDS_AB=yes ;;
  4)   README=README-run-4.md;   PARTS="basics:checks component-C-staged:checks component-D-staged:checks component-E-staged:checks"; NEEDS_AB=yes ;;
  5.1) README=README-run-5.1.md; PARTS="regression:checks:. component-C-staged:all component-D-staged:all component-E-staged:all"; NEEDS_AB=yes ;;
  5.2) README=README-run-5.2.md; PARTS="component-A-staged:md component-B-staged:md regression:checks:. component-C-staged:all component-D-staged:all component-E-staged:all"; NEEDS_AB=yes ;;
  5.3) README=README-run-5.3.md; PARTS="component-A-staged:all component-B-staged:all component-C-staged:all component-D-staged:all component-E-staged:all"; NEEDS_AB=yes ;;
  *) die "unknown run '$RUN' ($USAGE)" ;;
esac
case "$RUN" in 1.*) MODE=score_only ;; esac
README_SRC="$MASTER/readmes/$README"
[ -f "$README_SRC" ] || die "missing $README_SRC"
if [ "$NEEDS_AB" = yes ]; then
  if [ -f "$MASTER/FROZEN-AB.sha256" ]; then FROZEN="$FROZEN FROZEN-AB.sha256"
  elif [ "$PRACTICE" = no ]; then die "A, B, the regression suite and the Run 3-5 READMEs are not frozen yet (no FROZEN-AB.sha256): use --practice"
  fi
fi

# 1. frozen files unchanged
( cd "$MASTER" && shasum -a 256 -c $FROZEN >/dev/null 2>&1 ) \
  || die "a frozen file changed: run 'cd specs-master && shasum -a 256 -c FROZEN-*.sha256' to see which"

# 2. no agent run in progress
if [ "$FORCE" = no ] && command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  live=$(docker ps --format '{{.Names}}' | grep -c 'final_task__' || true)
  [ "$live" -eq 0 ] || die "$live agent run container(s) still running; wait for them to finish (or pass --force)"
fi
docker info >/dev/null 2>&1 || echo "warning: Docker is not running; start Docker Desktop before the run"

# 3. rebuild the spec folder
[ -d "$SPEC" ] || die "missing $SPEC"
find "$SPEC" -mindepth 1 -maxdepth 1 ! -name .gitkeep -exec rm -rf {} +
cp "$README_SRC" "$SPEC/README.md"
for part in $PARTS; do
  src=$(echo "$part" | cut -d: -f1); what=$(echo "$part" | cut -d: -f2); dst=$(echo "$part" | cut -d: -f3)
  [ -n "$dst" ] || dst=$src
  [ -d "$MASTER/$src" ] || die "missing specs-master/$src"
  mkdir -p "$SPEC/$dst"
  case "$what" in
    all)    rsync -a --exclude __pycache__ --exclude .DS_Store --exclude '*.pyc' "$MASTER/$src/" "$SPEC/$dst/" ;;
    md)     rsync -a --include '*.md' --exclude '*' "$MASTER/$src/" "$SPEC/$dst/" ;;
    checks) rsync -a --exclude __pycache__ --exclude .DS_Store --exclude '*.pyc' "$MASTER/$src/checks" "$SPEC/$dst/" ;;
  esac
done

# 4. testing mode, in the copies only
while read -r lib; do
  [ "$(grep -c '^FEEDBACK=' "$lib")" -eq 1 ] || die "expected exactly one FEEDBACK= line in $lib"
  perl -pi -e "s/^FEEDBACK=.*/FEEDBACK=$MODE/" "$lib"
done < <(find "$SPEC" -name lib.sh)

# 5. every copied file matches specs-master (apart from FEEDBACK); every file the README names is there
norm() { sed 's/^FEEDBACK=.*/FEEDBACK=X/' "$1"; }
count_files() {  # <dir> <all|md|checks> -> number of files that part should hold
  if [ "$2" = md ]; then (cd "$1" && find . -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' ')
  else (cd "$1" && find . -type f ! -name .DS_Store ! -name .gitkeep ! -name '*.pyc' ! -path '*/__pycache__/*' ! -path ./README.md | wc -l | tr -d ' ')
  fi
}
for part in $PARTS; do
  src=$(echo "$part" | cut -d: -f1); what=$(echo "$part" | cut -d: -f2); dst=$(echo "$part" | cut -d: -f3)
  [ -n "$dst" ] || dst=$src
  sub=""; [ "$what" = checks ] && sub="/checks"      # a checks-only part is compared within its checks/ folder
  while read -r f; do
    cmp -s <(norm "$MASTER/$src$sub/$f") <(norm "$SPEC/$dst$sub/$f") || die "$dst$sub/$f differs from specs-master/$src$sub/$f"
  done < <(cd "$SPEC/$dst$sub" && find . -type f ! -name .gitkeep ! -path ./README.md | sed 's|^\./||')
  n_master=$(count_files "$MASTER/$src$sub" "$what"); n_copy=$(count_files "$SPEC/$dst$sub" "$what")
  [ "$n_master" -eq "$n_copy" ] || die "$dst$sub has $n_copy files, specs-master/$src$sub has $n_master"
done
cmp -s "$README_SRC" "$SPEC/README.md" || die "README.md differs from $README_SRC"
for f in $(grep -o -E '`(basics|checks|component-[A-E]-staged)/[^` *]*`' "$SPEC/README.md" | tr -d '`' | sort -u); do
  [ -e "$SPEC/$f" ] || die "README.md names $f but it is not in the spec folder"
done
case "$RUN" in 4) [ -z "$(find "$SPEC" -name '*.md' ! -path "$SPEC/README.md")" ] || die "Run 4 must have no spec files" ;; esac
if [ "$SPEC" = "$LIVE_SPEC" ]; then
  [ "$(readlink "$TASK/student-files/spec")" = "../course-staff-files/final_task/environment/milestone-specs/milestone-2-specs" ] \
    || die "student-files/spec must link to milestone-2-specs (see README step 4)"
fi
CONTENTS=$(cd "$SPEC" && find . -mindepth 1 -maxdepth 1 ! -name .gitkeep | sed 's|^\./||' | sort | tr '\n' ' ')

# 6 and 7. graded runs: spec copy for the submission (identical across repeats) and the log
if [ "$PRACTICE" = yes ]; then
  copy_note="practice: nothing saved to spec-copies/"
else
  mkdir -p "$COPIES"
  if [ -d "$COPIES/$RUN" ]; then
    diff -r -x .gitkeep "$COPIES/$RUN" "$SPEC" >/dev/null \
      || die "spec-copies/$RUN differs from what this run would get; the two repeats must be identical"
    copy_note="spec-copies/$RUN already saved and identical (this is a repeat)"
  else
    rsync -a --exclude .gitkeep "$SPEC/" "$COPIES/$RUN/"
    copy_note="saved spec-copies/$RUN"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S')  setup $RUN  ($README, $MODE)" >> "$COPIES/setup-log.txt"
fi

echo "Ready for Run $RUN$( [ "$PRACTICE" = yes ] && echo ' (PRACTICE)'): FEEDBACK=$MODE"
echo "  /spec = $CONTENTS  (frozen files verified; $copy_note)"
echo
echo "Start the run (two repeats of the same run may run at the same time, started ~15 s apart):"
echo "  cd \"$TASK/student-files\" && ./run_student_openai.sh 2"
echo "Do not run setup_run.sh again until every run of $RUN has finished."
