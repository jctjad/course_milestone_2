import socket,threading,sqlite3,json,urllib.request,urllib.parse,os,time
DB='/var/lib/huddle/huddle.db'; clients=[]; nicks=set(); lock=threading.RLock()
def db():
 c=sqlite3.connect(DB,timeout=5);c.row_factory=sqlite3.Row;return c
def q(sql,a=(),one=False):
 c=db();r=c.execute(sql,a);x=r.fetchone() if one else r.fetchall();c.close();return x
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def numeric(s,n,nick,text): send(s,':huddle %s %s %s'%(n,nick,text))
def chan(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cname=name[1:].split('/',1)
 return q('SELECT c.*,w.slug,w.name wsname FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cname),True)
def client_thread(s,addr):
 s.settimeout(20); buf=b''; token=None; uid=None; nick=None; joined={}; alive=True
 def line(x): send(s,x)
 try:
  while True:
   while b'\r\n' not in buf and b'\n' not in buf:
    z=s.recv(4096)
    if not z: return
    buf+=z
   sep=b'\r\n' if b'\r\n' in buf else b'\n'; raw,buf=buf.split(sep,1)
   a=raw.decode(errors='ignore').split(' ',2); cmd=a[0].upper()
   if cmd=='PASS':
    token=a[1] if len(a)>1 else ''
    r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token,),True)
    if not r:numeric(s,'464',nick or '*',':Password incorrect')
    else:uid=r['id']
   elif cmd=='NICK':
    nn=a[1] if len(a)>1 else ''
    with lock:
     if nn in nicks and nn!=nick:numeric(s,'433',nick or '*',nn+' :Nickname is already in use');continue
     if nick:nicks.discard(nick)
     nick=nn;nicks.add(nick)
   elif cmd=='USER':
    if not uid:
     numeric(s,'464',nick or '*',':Password incorrect');continue
    if nick:
     line(':huddle 001 %s :Welcome to Huddle'%nick);line(':huddle 002 %s :Your host is huddle'%nick);line(':huddle 003 %s :This server was created now'%nick);line(':huddle 004 %s huddle 1.0 io'%nick);line(':huddle 005 %s CHANTYPES=# :are supported by this server'%nick);numeric(s,'422',nick,':MOTD File is missing')
   elif cmd=='PING': line(':huddle PONG huddle '+(a[1] if len(a)>1 else ''))
   elif cmd=='QUIT': return
   elif cmd=='JOIN':
    name=a[1] if len(a)>1 else ''; c=chan(name)
    if not c:numeric(s,'403',nick or '*',name+' :No such channel');continue
    joined[c['id']]=name
    with lock:
     for z in clients:
      if z[0] is s:z[1]=nick;z[2]=joined
    line(':%s!%s@localhost JOIN %s'%(nick,nick,name))
    members=q('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(c['id'],))
    line(':huddle 353 %s = %s :%s'%(nick,name,' '.join(x['username'] for x in members)));numeric(s,'366',nick,name+' :End of NAMES list')
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
    if cmd=='NAMES' and len(a)>1 and chan(a[1]):
     c=chan(a[1]);ms=q('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(c['id'],));line(':huddle 353 %s = %s :%s'%(nick,a[1],' '.join(x['username'] for x in ms)));numeric(s,'366',nick,a[1]+' :End of NAMES list')
   elif cmd=='PRIVMSG' and len(a)>2:
    name=a[1]; body=a[2][1:] if a[2].startswith(':') else a[2]; c=chan(name)
    if c and c['id'] in joined:
     req=urllib.request.Request('http://127.0.0.1:8000/api/channels/%s/messages'%c['id'],data=json.dumps({'body':body}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token},method='POST')
     try:urllib.request.urlopen(req,timeout=4).read()
     except:pass
   else:
    numeric(s,'421',nick or '*',cmd+' :Unknown command')
 except:pass
 finally:
  with lock:
   if nick:nicks.discard(nick)
   for z in list(clients):
    if z[0] is s: clients.remove(z)
  try:s.close()
  except:pass
def poller():
 seen={}
 while True:
  try:
   rows=q('SELECT e.*,c.name cname,w.slug FROM events e JOIN channels c ON c.id=e.channel_id JOIN workspaces w ON w.id=c.workspace_id ORDER BY e.rowid DESC LIMIT 300')
   for e in reversed(rows):
    key=(e['channel_id'],e['event_id'])
    if key in seen:continue
    seen[key]=1
    if e['kind'] not in ('message.created','message.reply'):continue
    p=json.loads(e['payload']); m=p.get('message',{}); body=m.get('body',''); au=m.get('author',{}); name='#%s/%s'%(e['slug'],e['cname'])
    with lock:
     for s,n,j in list(clients):
      if e['channel_id'] in j:send(s,':%s!%s@localhost PRIVMSG %s :%s'%(au.get('username','user'),au.get('username','user'),name,body))
  except:pass
  time.sleep(.08)
def main():
 threading.Thread(target=poller,daemon=True).start()
 srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(('0.0.0.0',6667));srv.listen(100)
 while True:
  s,a=srv.accept(); item=[s,None,{}]
  with lock:clients.append(item)
  threading.Thread(target=client_thread,args=(s,a),daemon=True).start()
if __name__=='__main__':main()
