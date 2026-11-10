#!/bin/sh
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sys
sys.path.insert(0,'/app'); import server; server.setup()
PY
run(){ while :; do "$@"; sleep .5; done; }
run redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle >/tmp/huddle-redis.log 2>&1 &
run python3 /app/server.py 8000 >/tmp/huddle-8000.log 2>&1 &
run python3 /app/server.py 8001 >/tmp/huddle-8001.log 2>&1 &
run python3 /app/server.py 8002 >/tmp/huddle-8002.log 2>&1 &
wait
