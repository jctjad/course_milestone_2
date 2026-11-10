#!/bin/sh
mkdir -p /var/lib/huddle
if ! ss -ltn '( sport = :6379 )' 2>/dev/null|grep -q LISTEN; then redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /tmp --save '' >/tmp/huddle-redis.log 2>&1; fi
if ! ss -ltn '( sport = :6667 )' 2>/dev/null|grep -q LISTEN; then nohup python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; fi
for i in 0 1 2; do
 p=$((8000+i)); if ! ss -ltn "( sport = :$p )" 2>/dev/null|grep -q LISTEN; then NODE_ID=$i PORT=$p python3 /app/server.py >/tmp/huddle-$i.log 2>&1 & echo $! >/tmp/huddle-$i.pid; fi
done
for i in 0 1 2; do (while :; do redis-cli -h 127.0.0.1 -p 6379 subscribe huddle:events >/dev/null 2>&1 || sleep 1; done) & done
while :; do sleep 1; if ! ss -ltn '( sport = :6667 )' 2>/dev/null|grep -q LISTEN; then nohup python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; fi; for i in 0 1 2; do p=$((8000+i)); if ! ss -ltn "( sport = :$p )" 2>/dev/null|grep -q LISTEN; then NODE_ID=$i PORT=$p python3 /app/server.py >/tmp/huddle-$i.log 2>&1 & echo $! >/tmp/huddle-$i.pid; fi; done; if ! ss -ltn '( sport = :6379 )' 2>/dev/null|grep -q LISTEN; then redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /tmp --save '' >/tmp/huddle-redis.log 2>&1; fi; done
