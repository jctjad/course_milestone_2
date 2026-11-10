#!/bin/sh
set -eu
mkdir -p /var/lib/huddle /tmp
python3 - <<'PY'
import sqlite3,os
os.makedirs('/var/lib/huddle',exist_ok=True)
c=sqlite3.connect('/var/lib/huddle/huddle.sqlite3')
c.execute('PRAGMA journal_mode=WAL')
c.close()
PY
redis_pid=
ircpid=
node_pids=
listener_pids=
cleanup() {
  for p in $node_pids $listener_pids ${redis_pid:-} ${ircpid:-}; do
    kill "$p" 2>/dev/null || true
  done
  exit 0
}
trap cleanup INT TERM EXIT
while :; do
  if ! ss -ltn 'sport = :6379' | grep -q LISTEN; then
    redis-server --bind 127.0.0.1 --port 6379 --dir /var/lib/huddle --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
    redis_pid=$!
  fi
  if ! ss -ltn 'sport = :6667' | grep -q LISTEN; then
    python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 &
    ircpid=$!
  fi
  for n in 0 1 2; do
    port=$((8000+n))
    if ! ss -ltn "sport = :$port" | grep -q LISTEN; then
      python3 /app/app.py "$n" >/tmp/huddle-$n.log 2>&1 &
      node_pids="$node_pids $!"
    fi
    lp=$(printf '%s\n' $listener_pids | sed -n "$((n+1))p" || true)
    if [ -z "$lp" ] || ! kill -0 "$lp" 2>/dev/null; then
      redis-cli -h 127.0.0.1 -p 6379 SUBSCRIBE huddle:events >/tmp/huddle-redis-listener-$n.log 2>&1 &
      listener_pids="$listener_pids $!"
    fi
  done
  sleep 1
done
