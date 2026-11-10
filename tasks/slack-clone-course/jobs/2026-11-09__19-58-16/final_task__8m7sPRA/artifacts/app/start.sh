#!/bin/sh
set -eu
mkdir -p /var/lib/huddle
python3 -c "import sys;sys.path.insert(0,'/app');import server;server.init()"
pids=""
trap 'kill $pids 2>/dev/null || true; exit 0' INT TERM EXIT
while :; do
 if ! (ss -ltn 2>/dev/null | grep -q ':6667 '); then
  python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & pids="$pids $!"
 fi
 if ! (ss -ltn 2>/dev/null | grep -q ':6379 '); then
  redis-server --bind 127.0.0.1 --port 6379 --dir /var/lib/huddle --dbfilename redis.rdb --daemonize yes >/tmp/huddle-redis.log 2>&1 || true
  sleep 1
 fi
 for n in 0 1 2; do
  port=$((8000+n))
  if ! (ss -ltn 2>/dev/null | grep -q ":$port "); then
   python3 /app/server.py "$n" >/tmp/huddle-$n.log 2>&1 & pids="$pids $!"
  fi
  if ! (redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1); then
   break
  fi
  if ! (redis-cli -h 127.0.0.1 -p 6379 client list 2>/dev/null | grep -q "name=huddle-node-$n"); then
   redis-cli -h 127.0.0.1 -p 6379 --csv --user default --raw CLIENT SETNAME "huddle-node-$n" >/dev/null 2>&1 || true
   (redis-cli -h 127.0.0.1 -p 6379 SUBSCRIBE huddle:events >/tmp/huddle-redis-sub-$n.log 2>&1) & pids="$pids $!"
  fi
 done
 sleep 1
done
