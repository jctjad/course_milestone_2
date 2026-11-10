#!/bin/sh
mkdir -p /var/lib/huddle/files
python3 - <<'PY'
import sqlite3,os
os.makedirs('/var/lib/huddle',exist_ok=True)
c=sqlite3.connect('/var/lib/huddle/huddle.db'); c.execute('PRAGMA journal_mode=WAL'); c.commit(); c.close()
PY
pids=""
trap 'kill $pids 2>/dev/null; exit 0' INT TERM EXIT
while :; do
 for i in 0 1 2; do
  port=$((8000+i))
  if ! (ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN); then
   NODE_ID=$i python3 /app/server.py >/var/lib/huddle/node$i.log 2>&1 & pids="$pids $!"
  fi
 done
 sleep 1
done
