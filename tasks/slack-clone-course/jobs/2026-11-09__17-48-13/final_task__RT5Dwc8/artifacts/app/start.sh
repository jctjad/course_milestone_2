#!/bin/sh
mkdir -p /var/lib/huddle
[ -f /var/lib/huddle/huddle.db ] || true
while :; do
 for i in 0 1 2; do
  if ! python3 -c "import socket; s=socket.socket(); s.settimeout(.15); s.connect(('127.0.0.1',$((8000+i)))); s.close()" 2>/dev/null; then NODE_ID=$i python3 /app/server.py >/tmp/huddle-$i.log 2>&1 & echo $! >/tmp/huddle-$i.pid; fi
 done
 if ! python3 -c "import socket; s=socket.socket(); s.settimeout(.15); s.connect(('127.0.0.1',6667)); s.close()" 2>/dev/null; then python3 /app/irc_server.py >/tmp/huddle-irc.log 2>&1 & echo $! >/tmp/huddle-irc.pid; fi
 if ! python3 -c "import socket; s=socket.socket(); s.settimeout(.15); s.connect(('127.0.0.1',6379)); s.close()" 2>/dev/null; then redis-server --bind 127.0.0.1 --port 6379 --daemonize yes --pidfile /tmp/huddle-redis.pid --dir /var/lib/huddle --dbfilename redis.rdb >/tmp/huddle-redis.log 2>&1; fi
 for i in 0 1 2; do
  if [ ! -s /tmp/huddle-redis-sub-$i.pid ] || ! kill -0 $(cat /tmp/huddle-redis-sub-$i.pid) 2>/dev/null; then
   (while :; do redis-cli -h 127.0.0.1 -p 6379 SUBSCRIBE huddle:events >/tmp/huddle-redis-sub-$i.log 2>&1; sleep 1; done) &
   echo $! >/tmp/huddle-redis-sub-$i.pid
  fi
 done
 sleep 1
done
