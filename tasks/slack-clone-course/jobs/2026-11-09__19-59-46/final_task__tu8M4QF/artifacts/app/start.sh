#!/bin/sh
set -eu
mkdir -p /var/lib/huddle
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
loop() { while :; do "$@" >/tmp/huddle-supervisor.log 2>&1 || true; sleep .5; done; }
loop redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle &
loop python3 /app/server.py 0 &
loop python3 /app/server.py 1 &
loop python3 /app/server.py 2 &
loop python3 /app/server.py irc &
wait
