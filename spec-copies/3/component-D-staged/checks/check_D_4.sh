#!/usr/bin/env bash
# Self-check for D.4 (WebSocket speed under load). Prints PASS/FAIL per rule in /spec/component-D-staged/D-4-performance.md.
# Usage: bash /spec/component-D-staged/checks/check_D_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# the grader's limits (300 ms median, 800 ms p95, 1% missing), with a load that is probably heavier than the
# grader's: 3 writers x 30 posts at 10 posts/s each and 2 subscribers per node (540 deliveries, about 5 s)
POSTS=30; RATE=10; SUBS=2; MAX_MEDIAN=300; MAX_P95=800; MAX_MISSING=1

rules_D_4() {
  local ot g out pok med p95 miss
  new_user || { setup_fail "D4.1" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "D4.1" "could not create a workspace, fix the basics first"; return; }
  g=$GEN_ID

  # D4.1 median / p95 delivery latency and missing deliveries under concurrent writes on all three nodes
  out=$(python3 "$LIBDIR/perf.py" "$ot" "$g" $POSTS $RATE $SUBS)
  pok=$(sed -n 's/.*posts_ok=\([0-9]*\)\/\([0-9]*\).*/\1 \2/p' <<<"$out")
  med=$(sed -n 's/.*median_ms=\(-*[0-9]*\).*/\1/p' <<<"$out"); p95=$(sed -n 's/.*p95_ms=\(-*[0-9]*\).*/\1/p' <<<"$out")
  miss=$(sed -n 's/.*missing_pct=\([0-9.]*\).*/\1/p' <<<"$out")
  if [ -n "$pok" ] && [ "${pok% *}" = "${pok#* }" ] && [ "${med:--1}" -ge 0 ] && [ "$med" -le $MAX_MEDIAN ] && [ "$p95" -le $MAX_P95 ] \
     && awk -v m="$miss" -v x=$MAX_MISSING 'BEGIN{exit !(m<=x)}'; then
    ok D4.1 "under load: median ${med} ms, p95 ${p95} ms, missing ${miss}%, every post 201 ($out)"
  else
    bad D4.1 "under load (3 writers x $POSTS posts at $RATE/s, $SUBS subscribers per node): want every post 201, median <= ${MAX_MEDIAN} ms, p95 <= ${MAX_P95} ms, missing <= ${MAX_MISSING}%; got: ${out:-no output}"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_D_4; finish; }
