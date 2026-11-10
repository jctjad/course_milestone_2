#!/usr/bin/env bash
set -u
mkdir -p /var/lib/huddle
alive(){ [ -s "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }
while :; do
  if ! alive /tmp/huddle-redis.pid; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
    echo $! >/tmp/huddle-redis.pid
  fi
  for n in 0 1 2; do
    port=$((8000+n))
    if ! alive "/tmp/huddle-$n.pid"; then
      NODE_ID=$n PORT=$port nohup python3 /app/server.py >>"/tmp/huddle-$n.log" 2>&1 &
      echo $! >"/tmp/huddle-$n.pid"
    fi
    if ! alive "/tmp/huddle-redis-listener-$n.pid"; then
      nohup python3 /app/redis_listener.py >>"/tmp/huddle-redis-listener-$n.log" 2>&1 &
      echo $! >"/tmp/huddle-redis-listener-$n.pid"
    fi
  done
  sleep 1
done
