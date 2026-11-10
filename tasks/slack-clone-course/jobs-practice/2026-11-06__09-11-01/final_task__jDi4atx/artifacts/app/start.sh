#!/bin/sh
mkdir -p /var/lib/huddle/files
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
python3 /app/server.py 8000 >/tmp/huddle-8000.log 2>&1 & echo $! >/tmp/huddle8000.pid
python3 /app/server.py 8001 >/tmp/huddle-8001.log 2>&1 & echo $! >/tmp/huddle8001.pid
python3 /app/server.py 8002 >/tmp/huddle-8002.log 2>&1 & echo $! >/tmp/huddle8002.pid
wait
