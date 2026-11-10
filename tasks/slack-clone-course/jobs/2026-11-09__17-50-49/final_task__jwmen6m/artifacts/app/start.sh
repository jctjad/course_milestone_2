#!/bin/sh
mkdir -p /var/lib/huddle
python3 -c 'import sys; sys.path.insert(0,"/app"); import server; server.init()'
run_forever() { while true; do "$@"; sleep 0.5; done; }
run_forever redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle >/tmp/huddle-redis.log 2>&1 &
run_forever python3 /app/server.py 8000 >/tmp/huddle-8000.log 2>&1 &
run_forever python3 /app/server.py 8001 >/tmp/huddle-8001.log 2>&1 &
run_forever python3 /app/server.py 8002 >/tmp/huddle-8002.log 2>&1 &
run_forever python3 /app/server.py irc >/tmp/huddle-irc.log 2>&1 &
wait
