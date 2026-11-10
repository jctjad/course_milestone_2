#!/bin/sh
set -eu
mkdir -p /var/lib/huddle
python3 -c 'import sys;sys.path.insert(0,"/app");import app;app.init()'
run_forever(){ while :; do "$@" || true; sleep .5; done; }
run_forever redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no --dir /var/lib/huddle &
run_forever python3 /app/app.py 0 &
run_forever python3 /app/app.py 1 &
run_forever python3 /app/app.py 2 &
run_forever python3 -c 'import sys;sys.path.insert(0,"/app");import app;app.irc_server()' &
wait
