#!/bin/sh
set -eu
mkdir -p /app/data
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
python3 /app/server.py >/tmp/huddle-8000.log 2>&1 & echo $! >/tmp/huddle-8000.pid
PORT=8001 python3 /app/server.py >/tmp/huddle-8001.log 2>&1 & echo $! >/tmp/huddle-8001.pid
PORT=8002 python3 /app/server.py >/tmp/huddle-8002.log 2>&1 & echo $! >/tmp/huddle-8002.pid
wait
