#!/bin/sh
set -eu
mkdir -p /var/lib/huddle
python3 -c 'import sys;sys.path.insert(0,"/app");import app;app.init()'
pids=""
for n in 0 1 2; do NODE_ID=$n python3 /app/app.py >/tmp/huddle-$n.log 2>&1 & pids="$pids $!"; done
trap 'kill $pids 2>/dev/null || true' INT TERM EXIT
wait
