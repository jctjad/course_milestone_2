#!/bin/sh
mkdir -p /var/lib/huddle/files
python3 -c 'import sys;sys.path.insert(0,"/app");import app;app.init()'
for i in 0 1 2; do NODE_ID=$i python3 /app/app.py >/tmp/huddle-$i.log 2>&1 & echo $! > /tmp/huddle-$i.pid; done
wait
