#!/bin/sh
mkdir -p /var/lib/huddle
exec python3 -u - <<'PY'
import subprocess,time,os
ps={}
subs={}
irc=None
redis=None
while True:
 if irc is None or irc.poll() is not None:
  irc=subprocess.Popen(['python3','/app/server.py','irc'],env=os.environ)
 if redis is None or redis.poll() is not None:
  redis=subprocess.Popen(['redis-server','--bind','127.0.0.1','--port','6379','--save','""','--appendonly','no'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for i,p in enumerate((8000,8001,8002)):
  x=ps.get(i)
  if x is None or x.poll() is not None:
   ps[i]=subprocess.Popen(['python3','/app/server.py',str(p)],env={**os.environ,'NODE_ID':str(i)})
  sub=subs.get(i)
  if sub is None or sub.poll() is not None:
   subs[i]=subprocess.Popen(['redis-cli','-h','127.0.0.1','-p','6379','SUBSCRIBE','huddle:events'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 time.sleep(1)
PY
