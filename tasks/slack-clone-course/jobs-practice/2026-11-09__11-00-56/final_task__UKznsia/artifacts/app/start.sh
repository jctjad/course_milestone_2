#!/bin/sh
set -eu
mkdir -p /tmp
[ -f /tmp/huddle_state.json ] || echo '{"users":{},"tokens":{},"workspaces":{},"channels":{},"members":{},"messages":{},"invitations":{},"pins":{},"events":{},"eseq":{},"reads":{},"seq":0}' >/tmp/huddle_state.json
nodes=""
subs0=""
subs1=""
subs2=""
start_redis() {
  if ! redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
    redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no --daemonize yes >/tmp/redis.log 2>&1 || true
    for _ in 1 2 3 4 5; do redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1 && break; sleep .2; done
  fi
}
start_subs() {
  count=$(redis-cli -h 127.0.0.1 -p 6379 pubsub numsub huddle:events 2>/dev/null | awk 'NR==2 {print $2}')
  case "$count" in 3|4|5|6|7|8|9) ;;
  *) for pid in "$subs0" "$subs1" "$subs2"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
     subs0=""; subs1=""; subs2="";;
  esac
  if [ -z "$subs0" ] || ! kill -0 "$subs0" 2>/dev/null; then
    (redis-cli -h 127.0.0.1 -p 6379 subscribe huddle:events >/dev/null 2>&1) &
    subs0=$!
  fi
  if [ -z "$subs1" ] || ! kill -0 "$subs1" 2>/dev/null; then
    (redis-cli -h 127.0.0.1 -p 6379 subscribe huddle:events >/dev/null 2>&1) &
    subs1=$!
  fi
  if [ -z "$subs2" ] || ! kill -0 "$subs2" 2>/dev/null; then
    (redis-cli -h 127.0.0.1 -p 6379 subscribe huddle:events >/dev/null 2>&1) &
    subs2=$!
  fi
}
start_redis
start_subs
for port in 8000 8001 8002; do
  (PORT=$port exec python3 /app/server.py) &
  nodes="$nodes $port:$!"
done
trap 'for x in $nodes; do kill "${x#*:}" 2>/dev/null || true; done; for x in "$subs0" "$subs1" "$subs2"; do [ -n "$x" ] && kill "$x" 2>/dev/null || true; done' TERM INT EXIT
while :; do
  start_redis
  start_subs
  for port in 8000 8001 8002; do
    found=0
    for x in $nodes; do [ "${x%%:*}" = "$port" ] && kill -0 "${x#*:}" 2>/dev/null && found=1; done
    if [ "$found" -eq 0 ]; then
      (PORT=$port exec python3 /app/server.py) &
      nodes="$nodes $port:$!"
    fi
  done
  sleep 1
done
