#!/usr/bin/env bash
# Self-check for the four milestone 0 baseline conditions (M0.1 to M0.4) listed in /spec/README.md.
# Usage: bash /spec/basics/checks/check_baseline.sh      Exit 0 only if every rule passes.
# Run it on its own: M0.4 kills port 8001 and may restart the cluster (up to about 25 seconds).
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# health_of <node-url> -> prints "status|node_id|type" for that node's /api/health, e.g. "200|ok|1|int"
health_of() {
  local s; s=$(call GET "$1/api/health")
  echo "$s|$(val 'd["status"]')|$(val 'd["node_id"]')|$(val 'type(d["node_id"]).__name__')"
}
# upgrade_code <port> -> HTTP status of a WebSocket upgrade with a made-up token (000 = closed, no answer)
upgrade_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 2 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    "http://127.0.0.1:$1/api/ws?token=not-a-real-token-000000" 2>/dev/null || true
}

rules_baseline() {
  local h0 h1 h2 w0 w1 w2 a b back

  # M0.1 health on 8000: 200 {"status":"ok","node_id":0}
  h0=$(health_of $N0)
  if [ "$h0" = "200|ok|0|int" ]; then
    ok M0.1 "health on 8000: 200 with status ok and node_id 0"
  else
    bad M0.1 "health on 8000: expected '200|ok|0|int' (status|status field|node_id|type), got '$h0'"
  fi

  # M0.2 each node reports its own id: 8001 -> 1, 8002 -> 2 (integers)
  h1=$(health_of $N1); h2=$(health_of $N2)
  if [ "$h1" = "200|ok|1|int" ] && [ "$h2" = "200|ok|2|int" ]; then
    ok M0.2 "health on 8001 and 8002: 200 with node_id 1 and 2"
  else
    bad M0.2 "node ids: expected 8001 '200|ok|1|int' and 8002 '200|ok|2|int', got '$h1' and '$h2'"
  fi

  # M0.3 a WebSocket upgrade with a bad token is never accepted (no 101) on any port
  w0=$(upgrade_code 8000); w1=$(upgrade_code 8001); w2=$(upgrade_code 8002)
  if [ "$w0" != 101 ] && [ "$w1" != 101 ] && [ "$w2" != 101 ]; then
    ok M0.3 "WebSocket with a bad token is rejected on 8000, 8001, 8002"
  else
    bad M0.3 "bad-token WebSocket: expected no 101 on any port, got $w0/$w1/$w2 on 8000/8001/8002"
  fi

  # M0.4 SIGKILL the process on 8001; 8000 and 8002 keep answering health with status ok
  if [ -z "$(pids_on_port 8001)" ]; then
    bad M0.4 "crash tolerance: expected a process listening on 8001 to kill, found none"; return
  fi
  kill_port 8001; sleep 1
  a=$(health_of $N0); b=$(health_of $N2)
  if [ "${a%|*|*}" = "200|ok" ] && [ "${b%|*|*}" = "200|ok" ]; then
    ok M0.4 "after SIGKILL on 8001, 8000 and 8002 still answer health with 200 and status ok"
  else
    bad M0.4 "after SIGKILL on 8001: expected 8000 and 8002 to answer '200|ok', got '${a%|*|*}' and '${b%|*|*}'"
  fi

  # put the cluster back: give 8001 up to 3s to come back on its own, else restart everything
  back=no
  for _ in 1 2 3; do curl -fs --max-time 1 "$N1/api/health" >/dev/null 2>&1 && { back=yes; break; }; sleep 1; done
  if [ "$back" = no ] && ! restart_cluster; then
    [ "$FEEDBACK" = verbose ] && echo "NOTE cluster did not come back after the restart (see /tmp/start.log); start it again before other checks"
  fi
  return 0
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_baseline; finish; }
