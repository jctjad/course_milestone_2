#!/bin/sh
mkdir -p /app
[ -f /app/huddle.db ] || : > /app/huddle.db
while :; do
  for n in 0 1 2; do
    if ! (ss -ltn "sport = :$((8000+n))" 2>/dev/null | grep -q LISTEN); then NODE_ID=$n python3 /app/app.py >/tmp/huddle-$n.log 2>&1 & echo $! >/tmp/huddle-$n.pid; fi
  done
  if command -v redis-server >/dev/null 2>&1 && ! (ss -ltn "sport = :6379" 2>/dev/null | grep -q LISTEN); then redis-server --daemonize yes --save '' --appendonly no >/tmp/redis.log 2>&1 || true; fi
  if command -v redis-cli >/dev/null 2>&1 && (ss -ltn "sport = :6379" 2>/dev/null | grep -q LISTEN); then
    for n in 0 1 2; do
      if [ ! -s "/tmp/huddle-redis-$n.pid" ] || ! kill -0 "$(cat "/tmp/huddle-redis-$n.pid" 2>/dev/null)" 2>/dev/null; then
        redis-cli -h 127.0.0.1 -p 6379 subscribe huddle:events >/tmp/huddle-redis-$n.log 2>&1 &
        echo $! >/tmp/huddle-redis-$n.pid
      fi
    done
  fi
  sleep 1
done
