import socket,threading,sqlite3,re,time,json
DB='/var/lib/huddle/huddle.sqlite3'
clients={}; watched={}; lock=threading.Lock()
def db():
 c=sqlite3.connect(DB,timeout=2);c.row_factory=sqlite3.Row;return c
def send(c,line):
 try:c.sendall((line+'\r\n').encode())
 except OSError:pass
def find(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,chname=name[1:].split('/',1);c=db();r=c.execute('SELECT ch.*,w.slug,w.name wname FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,chname)).fetchone();c.close();return r
def emit(c,ch,kind,extra):
 ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
 d={'type':kind,'event_id':ev,'channel_id':ch};d.update(extra);c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch,ev,json.dumps(d)))
def pump():
 while True:
  try:
   c=db()
   with lock:
    for ch in list(watched):
     if ch not in watched: continue
   for ch,last in list(watched.items()):
    for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last)):
     f=json.loads(e['payload']); watched[ch]=e['event_id']
     if f.get('type') not in ('message.created','message.reply'):continue
     m=c.execute('SELECT m.*,u.username FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(f.get('message_id'),)).fetchone()
     if not m:continue
     ws=c.execute('SELECT slug FROM workspaces w JOIN channels x ON x.workspace_id=w.id WHERE x.id=?',(ch,)).fetchone(); name='#'+ws['slug']+'/'+c.execute('SELECT name FROM channels WHERE id=?',(ch,)).fetchone()['name']
     with lock:
      for s,info in list(clients.items()):
       if ch in info['channels'] and not (info['uid']==m['author_id'] and info.get('last_irc')==m['id']):
        send(s,':%s!%s@huddle PRIVMSG %s :%s'%(m['username'],m['username'],name,m['body']))
   c.close()
  except Exception:pass
  time.sleep(.1)
def register(c,nick):
 for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle 1.0 oiwsz'),('005','CHANTYPES=# :are supported by this server'),('422',':MOTD File is missing')]:send(c,':huddle %s %s %s'%(code,nick,text))
def client(sock,addr):
 uid=None;nick=None;registered=False;channels=set(); token=None
 try:
  f=sock.makefile('r',encoding='utf8',errors='ignore')
  for raw in f:
   line=raw.rstrip('\r\n'); ps=line.split(' ',1);cmd=ps[0].upper();arg=ps[1] if len(ps)>1 else ''
   if cmd=='PASS':
    token=arg.strip();c=db();r=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();c.close()
    uid=r['user_id'] if r else None
    if not uid:send(sock,':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    x=arg.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',x):continue
    with lock:
     if any(v.get('nick')==x for v in clients.values()) and nick!=x:send(sock,':huddle 433 * %s :Nickname is already in use'%x);continue
     nick=x
     if uid and not registered:registered=True;register(sock,nick)
     clients[sock]={'uid':uid,'nick':nick,'channels':channels}
   elif cmd=='USER':
    if uid and nick and not registered:registered=True;register(sock,nick)
   elif cmd=='PING':send(sock,':huddle PONG huddle :'+(arg[1:] if arg.startswith(':') else arg))
   elif cmd=='JOIN':
    name=arg.split(',')[0].strip();ch=find(name)
    if not ch:send(sock,':huddle 403 %s %s :No such channel'%(nick or '*',name));continue
    c=db();r=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],uid)).fetchone()
    if not r:c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],uid));c.commit()
    channels.add(ch['id']);clients[sock]={'uid':uid,'nick':nick,'channels':channels};watched.setdefault(ch['id'],c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n'])
    send(sock,':%s!%s@huddle JOIN %s'%(nick,nick,name))
    names=','.join(x['username'] for x in c.execute('SELECT u.username FROM cm m JOIN users u ON u.id=m.user_id WHERE m.channel_id=?',(ch['id'],)))
    send(sock,':huddle 353 %s = %s :%s'%(nick,name,names));send(sock,':huddle 366 %s %s :End of NAMES list'%(nick,name));c.close()
   elif cmd=='PRIVMSG':
    if ' ' not in arg:continue
    target,text=arg.split(' ',1);text=text[1:] if text.startswith(':') else text;ch=find(target)
    if not ch:send(sock,':huddle 403 %s %s :No such channel'%(nick,target));continue
    c=db();c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],uid,text,time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime())));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];emit(c,ch['id'],'message.created',{'message_id':mid});c.commit();clients.get(sock,{} )['last_irc']=mid;c.close()
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):send(sock,':huddle 331 %s %s :No topic'% (nick,arg.split()[0] if arg else '*'))
   elif cmd=='QUIT':break
   elif cmd and cmd not in ('CAP','PONG'):send(sock,':huddle 421 %s %s :Unknown command'%(nick or '*',cmd))
 finally:
  with lock:clients.pop(sock,None)
  try:sock.close()
  except:pass
def run():
 threading.Thread(target=pump,daemon=True).start();s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  c,a=s.accept();threading.Thread(target=client,args=(c,a),daemon=True).start()
if __name__=='__main__':run()
