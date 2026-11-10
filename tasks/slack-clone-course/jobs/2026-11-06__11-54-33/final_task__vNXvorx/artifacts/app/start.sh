#!/bin/bash
set -eu
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sys
sys.path.insert(0,'/app')
import server
server.init()
PY
pids=""
trap 'kill $pids 2>/dev/null || true; exit 0' INT TERM EXIT
while :; do
  python3 /app/server.py 8000 & p0=$!
  python3 /app/server.py 8001 & p1=$!
  python3 /app/server.py 8002 & p2=$!
  pids="$p0 $p1 $p2"
  wait -n $p0 $p1 $p2 || true
  kill $p0 $p1 $p2 2>/dev/null || true
  wait $p0 $p1 $p2 2>/dev/null || true
  sleep .2
done
