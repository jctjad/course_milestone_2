#!/bin/sh
mkdir -p /var/lib/huddle
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
python3 /app/irc.py &
I=$!
python3 -u /app/server.py &
P0=$!
PORT=8001 python3 -u /app/server.py &
P1=$!
PORT=8002 python3 -u /app/server.py &
P2=$!
trap 'kill $I $P0 $P1 $P2 2>/dev/null; wait' TERM INT EXIT
wait $P0
