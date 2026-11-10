#!/bin/sh
set -u
mkdir -p /var/lib/huddle/files
python3 - <<'PY'
import sqlite3,os
os.makedirs('/var/lib/huddle',exist_ok=True)
c=sqlite3.connect('/var/lib/huddle/huddle.sqlite')
c.execute('pragma journal_mode=WAL'); c.commit(); c.close()
PY
run_forever() { while :; do "$@"; sleep 0.5; done; }
run_forever redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle &
run_forever python3 /app/server.py 0 &
run_forever python3 /app/server.py 1 &
run_forever python3 /app/server.py 2 &
run_forever python3 /app/irc.py &
while :; do sleep 3600; done
