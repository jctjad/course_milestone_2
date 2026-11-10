#!/bin/sh
mkdir -p /var/lib/huddle/files
python3 /app/server.py 8000 >/tmp/huddle-8000.log 2>&1 &
p0=$!
python3 /app/server.py 8001 >/tmp/huddle-8001.log 2>&1 &
p1=$!
python3 /app/server.py 8002 >/tmp/huddle-8002.log 2>&1 &
p2=$!
python3 /app/server.py --irc >/tmp/huddle-irc.log 2>&1 &
p3=$!
trap 'kill $p0 $p1 $p2 $p3 2>/dev/null; wait' INT TERM EXIT
while :; do
 kill -0 $p0 2>/dev/null || { python3 /app/server.py 8000 >/tmp/huddle-8000.log 2>&1 & p0=$!; }
 kill -0 $p1 2>/dev/null || { python3 /app/server.py 8001 >/tmp/huddle-8001.log 2>&1 & p1=$!; }
 kill -0 $p2 2>/dev/null || { python3 /app/server.py 8002 >/tmp/huddle-8002.log 2>&1 & p2=$!; }
 kill -0 $p3 2>/dev/null || { python3 /app/server.py --irc >/tmp/huddle-irc.log 2>&1 & p3=$!; }
 sleep 1
done
