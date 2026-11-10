import socket,threading,sqlite3,re,time,json
DB='/var/lib/huddle/huddle.db'
clients=[]; lock=threading.Lock(); seen={}
def db():
 c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row; return c
def user(t):
 c=db(); r=c.execute('SELECT u.id,u.username FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def chan(name):
 m=re.fullmatch(r'#([^/]+)/([^/]+)',name or '')
 if not m:return None
 c=db(); r=c.execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON c.workspace_id=w.id WHERE w.slug=? AND c.name=?',(m[1],m[2])).fetchone(); c.close(); return r
def cname(ch): return '#'+ch['slug']+'/'+ch['name']
def send(s,line):
 try:s.sendall((line+'\r\n').encode())
 except:pass
def numeric(s,n,nick,text): send(s,':huddle %03d %s %s'%(int(n),nick or '*',text))
def pump():
 while True:
  try:
   c=db(); rows=c.execute("SELECT e.*,ch.name cname,w.slug FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.kind='message.created' ORDER BY e.id").fetchall()
   for e in rows:
    if e['id']<=seen.get(e['channel_id'],0): continue
    seen[e['channel_id']]=e['id']
    p=json.loads(e['payload']); m=p.get('message',{})
    if p.get('message_id'):
     r=c.execute('SELECT m.*,u.username FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(p['message_id'],)).fetchone()
     if r: body=r['body']; nick=r['username']
     else: body=p.get('body',''); nick='huddle'
    else:
     body=m.get('body',''); nick=m.get('author',{}).get('username','huddle')
    line=':'+nick+'!'+nick+'@localhost PRIVMSG #'+e['slug']+'/'+e['cname']+' :'+body
    with lock:
     for x in list(clients):
      if e['channel_id'] in x[4]: send(x[0],line)
   c.close()
  except Exception: pass
  time.sleep(.1)
def client(s):
 tok=None; nick=None; u=None; joined=[]
 def registered(): return bool(u and nick)
 try:
  f=s.makefile()
  for line in f:
   a=line.rstrip('\r\n'); sp=a.split(' ',2); cmd=sp[0].upper()
   if cmd=='PASS':
    tok=sp[1] if len(sp)>1 else ''; u=user(tok)
    if not u:numeric(s,464,nick,'Password incorrect')
   elif cmd=='NICK':
    nn=sp[1] if len(sp)>1 else ''
    with lock: collision=any(x[1]==nn for x in clients)
    if collision:numeric(s,433,nick,'Nickname in use')
    elif re.fullmatch(r'[A-Za-z0-9_-]{1,30}',nn): nick=nn
   elif cmd=='USER' and registered():
    for n,t in [(1,'Welcome'),(2,'Your host'),(3,'Created'),(4,'huddle'),(5,'CHATHOOD')]:numeric(s,n,nick,t)
    numeric(s,422,nick,'MOTD File is missing')
    with lock: clients.append((s,nick,u['id'],u['username'],joined))
   elif cmd=='PING': send(s,'PONG :'+(sp[-1].lstrip(':') if len(sp)>1 else ''))
   elif cmd=='JOIN' and len(sp)>1 and registered():
    ch=chan(sp[1])
    if not ch:numeric(s,403,nick,'No such channel')
    else:
     c=db(); c.execute('INSERT OR IGNORE INTO cm(channel_id,user_id) VALUES(?,?)',(ch['id'],u['id'])); c.commit(); c.close()
     if ch['id'] not in joined: joined.append(ch['id'])
     send(s,':'+nick+'!'+u['username']+'@localhost JOIN '+cname(ch))
     numeric(s,353,nick,'= '+cname(ch)+' :'+nick); numeric(s,366,nick,cname(ch)+' End of NAMES')
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE') and registered(): numeric(s,323,nick,'OK')
   elif cmd=='PRIVMSG' and len(sp)>2 and registered():
    ch=chan(sp[1]); body=sp[2].lstrip(':')
    if ch and ch['id'] in joined:
     c=db(); ts=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
     c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],u['id'],body,ts))
     mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     q=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch['id'],)).fetchone()[0]
     c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(ch['id'],q,'message.created',json.dumps({'message_id':mid,'body':body,'author_id':u['id']}))); c.commit(); c.close()
   elif cmd=='QUIT':break
   elif cmd: numeric(s,421,nick,'Unknown command')
 except Exception: pass
 finally:
  with lock:
   clients[:]=[x for x in clients if x[0] is not s]
  try:s.close()
  except:pass
def main():
 threading.Thread(target=pump,daemon=True).start()
 z=socket.socket(); z.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); z.bind(('0.0.0.0',6667)); z.listen()
 while True:
  s,_=z.accept(); threading.Thread(target=client,args=(s,),daemon=True).start()
main()
