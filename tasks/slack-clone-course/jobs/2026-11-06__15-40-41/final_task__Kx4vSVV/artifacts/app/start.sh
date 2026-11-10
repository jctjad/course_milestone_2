#!/bin/sh
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sqlite3
c=sqlite3.connect('/var/lib/huddle/huddle.db'); c.execute('PRAGMA journal_mode=WAL'); c.commit(); c.close()
PY
p0= p1= p2= pi=
start_node() { eval "PORT=$1 python3 /app/app.py >>/tmp/huddle-$1.log 2>&1 & p$2=\$!"; }
start_irc() { python3 /app/irc.py >>/tmp/huddle-irc.log 2>&1 & pi=$!; }
start_node 8000 0; start_node 8001 1; start_node 8002 2; start_irc
trap 'kill "$p0" "$p1" "$p2" "$pi" 2>/dev/null; exit 0' TERM INT
while :; do
 sleep 1
 kill -0 "$p0" 2>/dev/null || start_node 8000 0
 kill -0 "$p1" 2>/dev/null || start_node 8001 1
 kill -0 "$p2" 2>/dev/null || start_node 8002 2
 kill -0 "$pi" 2>/dev/null || start_irc
done
