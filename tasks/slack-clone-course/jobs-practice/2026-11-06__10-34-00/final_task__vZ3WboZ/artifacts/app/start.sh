#!/bin/sh
mkdir -p /var/lib/huddle/files
python3 -c "import sys;sys.path.insert(0,'/app');import server"
pids=""
for i in 0 1 2; do NODE_ID=$i python3 /app/server.py $((8000+i)) >>/var/lib/huddle/node$i.log 2>&1 & pids="$pids $!"; done
trap 'kill $pids 2>/dev/null; exit 0' TERM INT
wait
