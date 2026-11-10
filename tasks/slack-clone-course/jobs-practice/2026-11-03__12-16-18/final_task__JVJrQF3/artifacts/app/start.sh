#!/bin/sh
set -u
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sqlite3,os
p='/var/lib/huddle/huddle.db'; c=sqlite3.connect(p); c.execute('PRAGMA journal_mode=WAL'); c.commit(); c.close()
PY
run(){ while :; do "$@"; sleep .5; done; }
run redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle &
run python3 /app/server.py 8000 &
run python3 /app/server.py 8001 &
run python3 /app/server.py 8002 &
wait
