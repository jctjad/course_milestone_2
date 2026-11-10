#!/bin/sh
mkdir -p /var/lib/huddle
python3 -c "import sys;sys.path.insert(0,'/app');import server;server.init()"
run_forever() { while :; do "$@" >/tmp/huddle-$1.log 2>&1; sleep .5; done; }
run_forever redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle &
run_forever python3 /app/server.py 8000 &
run_forever python3 /app/server.py 8001 &
run_forever python3 /app/server.py 8002 &
run_forever python3 /app/irc.py &
trap 'exit 0' INT TERM
wait
