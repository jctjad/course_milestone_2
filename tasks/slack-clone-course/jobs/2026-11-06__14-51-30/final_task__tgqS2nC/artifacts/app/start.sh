#!/bin/sh
mkdir -p /var/lib/huddle
python3 -c "import sys;sys.path.insert(0,'/app');import server;server.init()"
python3 /app/server.py >/tmp/huddle-8000.log 2>&1 & echo $! >/tmp/huddle0.pid
PORT=8001 python3 /app/server.py >/tmp/huddle-8001.log 2>&1 & echo $! >/tmp/huddle1.pid
PORT=8002 python3 /app/server.py >/tmp/huddle-8002.log 2>&1 & echo $! >/tmp/huddle2.pid
IRC=1 python3 /app/server.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddleirc.pid
while :; do sleep 1; for n in 0 1 2; do p=$(cat /tmp/huddle$n.pid 2>/dev/null); kill -0 "$p" 2>/dev/null || { PORT=$((8000+n)) python3 /app/server.py >/tmp/huddle-$((8000+n)).log 2>&1 & echo $! >/tmp/huddle$n.pid; }; done; p=$(cat /tmp/huddleirc.pid 2>/dev/null); kill -0 "$p" 2>/dev/null || { IRC=1 python3 /app/server.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddleirc.pid; }; done
