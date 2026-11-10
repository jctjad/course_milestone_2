#!/bin/sh
set -eu
rm -f /app/huddle.sqlite
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
python3 /app/server.py 8000 &
echo $! > /app/node8000.pid
python3 /app/server.py 8001 &
echo $! > /app/node8001.pid
python3 /app/server.py 8002 &
echo $! > /app/node8002.pid
wait
