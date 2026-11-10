import socket,threading,time,sys,re
sys.path.insert(0,'/app')
import server
clients={}; nicks=set(); lock=threading.RLock()
def send(c,x):
 try:c.sendall((x+'\r\n').encode())
 except: pass
def num(c,code,nick,text): send(c,':huddle '+code+' '+(nick or '*')+' '+text)
def find(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1); db=server.db(); r=db.execute('select ch.*,w.slug,w.name wsname,w.owner_id from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,cn)).fetchone(); db.close(); return r
def cname(ch): return '#'+ch['slug']+'/'+ch['name']
def register(c):
 u=c.get('user')
 if not u or not c.get('nick') or c.get('registered'): return
 c['registered']=True
 for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1'),('005','CHANTYPES=# :are supported by this server')]:num(c['sock'],code,c['nick'],text)
 num(c['sock'],'422',c['nick'],'MOTD File is missing')
def pump():
 seen={}
 while True:
  try:
   db=server.db(); rows=db.execute("select rowid,channel_id,event_id,kind,payload from events where kind='message.created' and rowid>? order by rowid",(seen.get('x',0),)).fetchall(); db.close()
   for r in rows:
    seen['x']=r['rowid']; p=server.json.loads(r['payload']); db=server.db(); a=db.execute('select username from users where id=?',(p['author_id'],)).fetchone(); db.close()
    if not a:continue
    ch=server.get if False else None
    db=server.db(); ch=db.execute('select c.*,w.slug from channels c join workspaces w on w.id=c.workspace_id where c.id=?',(r['channel_id'],)).fetchone(); db.close()
    if not ch:continue
    target=cname(ch)
    with lock:
     for c in list(clients.values()):
      if target in c['joined'] and r['event_id'] not in c.get('skip',set()):
       send(c['sock'],':'+a['username']+'!'+a['username']+'@huddle PRIVMSG '+target+' :'+p['body'])
  except Exception: pass
  time.sleep(.05)
def client(sock,c=None):
 if c is None: c={'sock':sock,'nick':None,'user':None,'registered':False,'joined':set(),'skip':set()}
 buf=b''
 try:
  while True:
   z=sock.recv(4096)
   if not z:break
   buf+=z
   while b'\r\n' in buf or b'\n' in buf:
    sep=b'\r\n' if b'\r\n' in buf else b'\n'; raw,buf=buf.split(sep,1); line=raw.decode(errors='replace'); ps=line.split(' ',1); cmd=ps[0].upper(); arg=ps[1] if len(ps)>1 else ''
    if cmd=='PASS':
     c['user']=server.usertok(arg.lstrip(':').strip())
     if not c['user']:num(sock,'464',c['nick'] or '*','Password incorrect')
    elif cmd=='NICK':
     nn=arg.strip()
     with lock:
      if nn in nicks and nn!=c['nick']:num(sock,'433',c['nick'] or '*',nn+' Nickname is already in use');continue
      if c['nick']:nicks.discard(c['nick'])
      c['nick']=nn;nicks.add(nn)
     register(c)
    elif cmd=='USER': register(c)
    elif cmd=='PING': send(sock,':huddle PONG huddle '+arg)
    elif cmd=='QUIT': return
    elif cmd=='JOIN':
     ch=find(arg.strip())
     if not ch:num(sock,'403',c['nick'] or '*',arg.strip()+' :No such channel');continue
     target=cname(ch); db=server.db(); db.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],c['user']['id'],'member'));db.execute('insert or ignore into cmembers values(?,?)',(ch['id'],c['user']['id']));db.commit(); members=db.execute('select username from users u join cmembers m on m.user_id=u.id where m.channel_id=?',(ch['id'],)).fetchall();db.close(); c['joined'].add(target); send(sock,':'+c['nick']+'!'+c['user']['username']+'@huddle JOIN '+target); num(sock,'353',c['nick'],'= '+target+' :'+' '.join(x['username'] for x in members));num(sock,'366',c['nick'],target+' :End of NAMES list')
    elif cmd=='PRIVMSG':
     q=arg.split(' ',1); target=q[0] if q else ''; body=q[1].lstrip(':') if len(q)>1 else ''
     ch=find(target)
     if ch and body:
      db=server.db(); db.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],c['user']['id'],body,None,server.now())); mid=db.execute('select last_insert_rowid()').fetchone()[0];db.commit(); r=db.execute('select * from messages where id=?',(mid,)).fetchone();db.close(); obj=server.msgjson(r); eid=server.emit(ch['id'],'message.created',obj); c['skip'].add(eid)
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
     if cmd=='NAMES' and arg.strip() not in c['joined']:num(sock,'403',c['nick'] or '*',arg+' :No such channel')
    else:num(sock,'421',c['nick'] or '*',cmd+' :Unknown command')
 finally:
  with lock:
   if c['nick']:nicks.discard(c['nick'])
   for k,v in list(clients.items()):
    if v is c:clients.pop(k,None)
  try:sock.close()
  except:pass
def main():
 threading.Thread(target=pump,daemon=True).start(); s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  z,_=s.accept(); k=id(z); 
  with lock: clients[k]={'sock':z,'nick':None,'user':None,'registered':False,'joined':set(),'skip':set()}
  threading.Thread(target=client,args=(z,clients[k]),daemon=True).start()
if __name__=='__main__':main()
