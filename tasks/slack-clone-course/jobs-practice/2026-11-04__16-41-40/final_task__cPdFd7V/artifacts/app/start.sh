#!/bin/sh
mkdir -p /var/lib/huddle
python3 /app/server.py --init >/dev/null 2>&1 || true
python3 /app/server.py --irc >/tmp/huddle-irc.log 2>&1 &
IP=$!
trap 'kill $IP $P0 $P1 $P2 2>/dev/null; exit 0' INT TERM EXIT
while :; do
 for n in 0 1 2; do eval p=\${P$n}; if ! kill -0 "$p" 2>/dev/null; then PORT=$((8000+n)) NODE_ID=$n python3 /app/server.py >/tmp/huddle-$n.log 2>&1 & eval P$n=$!; fi; done
 sleep 1
done
