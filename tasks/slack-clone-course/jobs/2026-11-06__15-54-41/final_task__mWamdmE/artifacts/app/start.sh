#!/bin/sh
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sqlite3
c=sqlite3.connect('/var/lib/huddle/huddle.db'); c.execute('PRAGMA journal_mode=WAL'); c.commit(); c.close()
PY
run_forever(){ while :; do "$@"; sleep .5; done; }
run_forever redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle >/tmp/huddle-redis.log 2>&1 &
run_forever python3 /app/server.py 8000 >/tmp/huddle-8000.log 2>&1 &
run_forever python3 /app/server.py 8001 >/tmp/huddle-8001.log 2>&1 &
run_forever python3 /app/server.py 8002 >/tmp/huddle-8002.log 2>&1 &
wait
