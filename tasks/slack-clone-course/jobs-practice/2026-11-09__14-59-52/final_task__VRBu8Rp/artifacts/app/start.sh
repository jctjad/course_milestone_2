#!/bin/sh
set -eu
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sys
sys.path.insert(0,'/app')
import server
server.init()
PY
run_forever() { while :; do "$@" || true; sleep 0.5; done; }
run_forever redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle &
pids="$!"
run_forever python3 /app/server.py --irc &
pids="$!"
for n in 0 1 2; do
  run_forever python3 /app/server.py --node-id "$n" &
  pids="$pids $!"
done
trap 'kill $pids 2>/dev/null || true; wait' INT TERM EXIT
wait
