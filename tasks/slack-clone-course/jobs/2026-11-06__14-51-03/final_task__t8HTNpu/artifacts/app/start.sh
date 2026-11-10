#!/bin/sh
mkdir -p /var/lib/huddle
python3 - <<'PY'
import sys
sys.path.insert(0,'/app'); import app; app.init()
PY
while :; do
  for p in 8000 8001 8002; do
    if ! ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
      PORT=$p python3 /app/app.py >/tmp/huddle-$p.log 2>&1 &
    fi
  done
  if ! ss -ltn "sport = :6667" 2>/dev/null | grep -q LISTEN; then
    python3 -c 'import sys,threading;sys.path.insert(0,"/app");import app;app.init();threading.Thread(target=app.event_pump,daemon=True).start();app.irc_server()' >/tmp/huddle-irc.log 2>&1 &
  fi
  sleep 1
done
