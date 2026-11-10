#!/bin/sh
mkdir -p /var/lib/huddle/files
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
pids=""
trap 'kill $pids 2>/dev/null; exit 0' INT TERM EXIT
for i in 0 1 2; do python3 /app/server.py $((8000+i)) >/tmp/huddle-$i.log 2>&1 & pids="$pids $!"; done
while :; do
 for i in 0 1 2; do port=$((8000+i)); if ! (echo >/dev/tcp/127.0.0.1/$port) 2>/dev/null; then python3 /app/server.py "$port" >/tmp/huddle-$i.log 2>&1 & pids="$pids $!"; fi; done
 sleep 1
done
