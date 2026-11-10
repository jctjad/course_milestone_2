#!/usr/bin/env bash
mkdir -p /var/lib/huddle/files
while :; do
  for n in 0 1 2; do
    if [ -z "${pids[$n]+x}" ] || ! kill -0 "${pids[$n]}" 2>/dev/null; then
      python3 /app/server.py "$n" >/tmp/huddle-$n.log 2>&1 &
      pids[$n]=$!
    fi
  done
  sleep .3
done
