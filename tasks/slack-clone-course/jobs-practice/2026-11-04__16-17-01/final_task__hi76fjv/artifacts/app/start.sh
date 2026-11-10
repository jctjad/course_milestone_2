#!/bin/sh
mkdir -p /var/lib/huddle
python3 /app/server.py 8000 >/tmp/huddle8000.log 2>&1 & echo $! >/tmp/huddle8000.pid
python3 /app/server.py 8001 >/tmp/huddle8001.log 2>&1 & echo $! >/tmp/huddle8001.pid
python3 /app/server.py 8002 >/tmp/huddle8002.log 2>&1 & echo $! >/tmp/huddle8002.pid
python3 /app/server.py --irc >/tmp/huddleirc.log 2>&1 & echo $! >/tmp/huddleirc.pid
trap 'kill $(cat /tmp/huddle8000.pid /tmp/huddle8001.pid /tmp/huddle8002.pid /tmp/huddleirc.pid) 2>/dev/null; exit' INT TERM EXIT
while :; do for p in 8000 8001 8002; do f=/tmp/huddle$p.pid; kill -0 $(cat $f) 2>/dev/null || (python3 /app/server.py $p >/tmp/huddle$p.log 2>&1 & echo $! >$f); done; kill -0 $(cat /tmp/huddleirc.pid) 2>/dev/null || (python3 /app/server.py --irc >/tmp/huddleirc.log 2>&1 & echo $! >/tmp/huddleirc.pid); sleep 1; done
