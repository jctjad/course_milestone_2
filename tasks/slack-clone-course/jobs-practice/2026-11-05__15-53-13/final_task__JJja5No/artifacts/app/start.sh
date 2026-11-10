#!/bin/sh
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sys
sys.path.insert(0,'/app')
import app
app.init()
PY
pids=""
for i in 0 1 2; do NODE_ID=$i python3 /app/app.py >>/tmp/huddle-$i.log 2>&1 & pids="$pids $!"; done
trap 'kill $pids 2>/dev/null; exit 0' INT TERM EXIT
wait
