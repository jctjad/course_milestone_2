#!/bin/sh
mkdir -p /var/lib/huddle /var/lib/huddle/files
python3 -c "import sys;sys.path.insert(0,'/app');import server;server.init()"
python3 /app/server.py 8000 &
echo $! > /tmp/huddle0.pid
python3 /app/server.py 8001 &
echo $! > /tmp/huddle1.pid
python3 /app/server.py 8002 &
echo $! > /tmp/huddle2.pid
python3 /app/server.py 8003 >/tmp/huddle-irc.log 2>&1 &
wait
