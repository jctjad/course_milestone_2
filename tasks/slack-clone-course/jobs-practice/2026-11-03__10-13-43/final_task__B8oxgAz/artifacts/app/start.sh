#!/bin/sh
set -u
python3 -c "import sys;sys.path.insert(0,'/app');import app;app.init()"
node_loop() {
  id="$1"
  port="$2"
  while :; do
    if [ "$id" = 0 ]; then
      python3 /app/app.py "$port"
    else
      NODE_ID="$id" python3 /app/app.py "$port"
    fi
    sleep 0.2
  done
}
node_loop 0 8000 &
P0=$!
node_loop 1 8001 &
P1=$!
node_loop 2 8002 &
P2=$!
trap 'kill "$P0" "$P1" "$P2" 2>/dev/null || true' INT TERM EXIT
wait
