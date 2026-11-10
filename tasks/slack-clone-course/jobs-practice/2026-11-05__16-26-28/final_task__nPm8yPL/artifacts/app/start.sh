#!/bin/sh
mkdir -p /var/lib/huddle
python3 -c "import sys;sys.path.insert(0,'/app');import server;server.init()"
while :; do
 for p in 8000 8001 8002; do
  if ! (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null; then (cd /app; nohup python3 server.py $p >/tmp/huddle-$p.log 2>&1 & echo $! >/tmp/huddle-$p.pid); fi
 done
 if ! (exec 3<>/dev/tcp/127.0.0.1/6667) 2>/dev/null; then (cd /app; nohup python3 server.py 8000 >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid); fi
 sleep 1
done
