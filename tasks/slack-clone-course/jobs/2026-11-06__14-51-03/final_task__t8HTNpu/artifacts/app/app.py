import os,sys,json,sqlite3,secrets,hashlib,re,base64,socket,threading,time,datetime,urllib.parse,html
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'
PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
lock=threading.RLock(); clients=[]; irc_clients=[]; irc_nicks=set()
def now():
 return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 c=db()
 c.executescript('''PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,event_seq INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS wmembers(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));''')
 c.commit(); c.close()
def q(sql,args=(),one=False):
 c=db()
 try:
  x=c.execute(sql,args); r=x.fetchone() if one else x.fetchall(); return r
 finally:c.close()
def userj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def tokuser(t):
 r=q('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(t,),True); return r
def auth(h):
 if not h.startswith('Bearer '): return None
 return tokuser(h[7:])
def chj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msgj(r):
 a=q('select * from users where id=?',(r['author_id'],),True)
 rx=q('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],))
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('select count(*) n from messages where parent_id=? and deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[z['user_id'] for z in q('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]} for x in rx],'mentions':mention_ids(r['channel_id'],r['author_id'],r['body'])}
def emit(cid,kind,payload):
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE'); r=c.execute('select event_seq from channels where id=?',(cid,)).fetchone(); eid=(r['event_seq'] if r else 0)+1
  c.execute('update channels set event_seq=? where id=?',(eid,cid)); frame={'type':kind,'event_id':eid,'channel_id':cid}; frame.update(payload)
  c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(cid,eid,kind,json.dumps(frame))); c.commit(); return frame
 finally:c.close()
def members(cid): return [x['user_id'] for x in q('select user_id from members where channel_id=?',(cid,))]
def broadcast(frame):
 for c in list(irc_clients):
  try:
   if frame.get('channel_id') in c.get('channels',[]) and frame['type']=='message.created':
    m=frame['message']; u=m['author']; name=irc_name(frame['channel_id'])
    c['sock'].sendall((':'+u['username']+'!'+u['username']+'@huddle PRIVMSG '+name+' :'+m['body']+'\r\n').encode())
  except: pass
def mention_ids(cid,author,body):
 c=q('select workspace_id from channels where id=?',(cid,),True)
 if not c:return []
 ids=[]
 for m in re.finditer(r'(?<!\w)@([A-Za-z0-9_]+)',body):
  x=m.group(1)
  if x.lower()=='channel': ids += [z for z in members(cid) if z!=author]
  else:
   u=q('select id from users where username=?',(x,),True)
   if u and u['id']!=author and q('select 1 from wmembers where workspace_id=? and user_id=?',(c['workspace_id'],u['id']),True): ids.append(u['id'])
 return list(dict.fromkeys(ids))
def create_msg(cid,uid,body,parent=None):
 c=db(); t=now()
 try:
  c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,edited_at) values(?,?,?,?,?,?)',(cid,uid,body,parent,t,None)); mid=c.execute('select last_insert_rowid()').fetchone()[0]; c.commit()
 finally:c.close()
 r=q('select * from messages where id=?',(mid,),True); f=emit(cid,'message.reply' if parent else 'message.created',{'message':msgj(r)}); broadcast(f); return r
def ws_send(s,data):
 b=data.encode(); n=len(b)
 if n<126: h=bytes([129,n])
 elif n<65536:h=bytes([129,126])+n.to_bytes(2,'big')
 else:h=bytes([129,127])+n.to_bytes(8,'big')
 s.sendall(h+b)
def ws_read(s):
 h=s.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=int.from_bytes(s.recv(2),'big')
 elif n==127:n=int.from_bytes(s.recv(8),'big')
 mask=s.recv(4); d=s.recv(n); return bytes(d[i]^mask[i%4] for i in range(n)).decode()
def ws_loop(s,u):
 w={'sock':s,'uid':u['id'],'cid':None,'last':0}; clients.append(w)
 try:
  while 1:
   x=ws_read(s)
   if not x:break
   d=json.loads(x); typ=d.get('type'); cid=d.get('channel_id')
   if typ=='subscribe':
    w['cid']=cid; h=q('select event_seq from channels where id=?',(cid,),True); w['last']=h['event_seq'] if h else 0; ws_send(s,json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':w['last']}))
   elif typ=='resume':
    h=q('select event_seq from channels where id=?',(cid,),True); old=d.get('since_event_id',0)
    for e in q('select payload from events where channel_id=? and event_id>? order by event_id',(cid,old)): ws_send(s,e['payload'])
    ws_send(s,json.dumps({'type':'resumed','channel_id':cid,'head_event_id':h['event_seq'] if h else 0}))
 finally:
  if w in clients:clients.remove(w)
  s.close()
def irc_name(cid):
 r=q('select w.slug,c.name from channels c join workspaces w on w.id=c.workspace_id where c.id=?',(cid,),True); return '#'+r['slug']+'/'+r['name'] if r else ''
def findchan(n):
 if not n.startswith('#') or '/' not in n[1:]:return None
 a,b=n[1:].split('/',1); return q('select c.* from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(a,b),True)
def irc_send(c,s): c['sock'].sendall((':huddle '+s+'\r\n').encode())
def irc_client(s):
 c={'sock':s,'uid':None,'nick':None,'channels':[],'pass':False}; irc_clients.append(c); buf=''
 try:
  while 1:
   z=s.recv(4096)
   if not z:break
   buf+=z.decode(errors='ignore')
   while '\r\n' in buf:
    line,buf=buf.split('\r\n',1); p=line.split(); cmd=p[0].upper() if p else ''
    if cmd=='PASS':
     c['uid']=tokuser(p[1]).__getitem__('id') if len(p)>1 and tokuser(p[1]) else None
     if not c['uid']: irc_send(c,'464 * :Password incorrect')
     else:c['pass']=True
    elif cmd=='NICK' and len(p)>1:
     n=p[1]
     if n in irc_nicks: irc_send(c,'433 * '+n+' :Nickname is already in use')
     elif re.fullmatch(r'[A-Za-z0-9_-]{1,30}',n): c['nick']=n; irc_nicks.add(n)
    elif cmd=='USER' and c['pass'] and c['nick'] and c['uid']:
     for k,t in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created now'),('004','huddle huddle 1'),('005','CHANTYPES=#')]: irc_send(c,k+ ' :'+t)
     irc_send(c,'422 :MOTD File is missing')
    elif cmd=='PING': s.sendall((':huddle PONG '+(p[-1] if p else '')+'\r\n').encode())
    elif cmd=='QUIT':return
    elif cmd in ('JOIN','NAMES','WHO','LIST','TOPIC','MODE'):
     if cmd=='JOIN' and len(p)>1:
      ch=findchan(p[1])
      if not ch: irc_send(c,'403 '+(c['nick'] or '*')+' '+p[1]+' :No such channel');continue
      if ch['is_private'] or not (q('select 1 from wmembers where workspace_id=? and user_id=?',(ch['workspace_id'],c['uid']),True)): irc_send(c,'403 * :Cannot join');continue
      if c['uid'] not in members(ch['id']): dbx('insert or ignore into members values(?,?)',(ch['id'],c['uid']))
      c['channels'].append(ch['id']); s.sendall((':'+c['nick']+'!'+str(c['uid'])+'@huddle JOIN '+p[1]+'\r\n').encode()); irc_send(c,'353 '+c['nick']+' = '+p[1]+' :'+c['nick']); irc_send(c,'366 '+c['nick']+' '+p[1]+' :End of NAMES')
     elif len(p)>1 and findchan(p[1]): irc_send(c,'366 '+(c['nick'] or '*')+' '+p[1]+' :OK')
    elif cmd=='PRIVMSG' and len(p)>2:
     ch=findchan(p[1]); body=line.split(' :',1)[1] if ' :' in line else ' '.join(p[2:])
     if ch and c['uid']: create_msg(ch['id'],c['uid'],body)
    else:
     if cmd not in ('PASS','NICK','USER',''): irc_send(c,'421 '+(c['nick'] or '*')+' '+cmd+' :Unknown command')
 finally:
  if c['nick']:irc_nicks.discard(c['nick'])
  if c in irc_clients:irc_clients.remove(c)
  s.close()
def dbx(sql,args=()):
 c=db();c.execute(sql,args);c.commit();c.close()
def irc_server():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
 while 1:
  x,_=s.accept();threading.Thread(target=irc_client,args=(x,),daemon=True).start()
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def send(self,status,obj,typ='application/json'):
  b=obj if isinstance(obj,bytes) else json.dumps(obj).encode(); self.send_response(status);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/': return self.send(200,open('/app/static/index.html','rb').read(),'text/html')
  if path=='/api/health':return self.send(200,{'status':'ok','node_id':NODE})
  if path=='/api/ws':
   t=urllib.parse.parse_qs(p.query).get('token',[''])[0];u=tokuser(t)
   if not u:return self.send(401,{'error':'unauthorized'})
   key=self.headers.get('Sec-WebSocket-Key'); self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers();return ws_loop(self.connection,u)
  u=auth(self.headers.get('Authorization',''))
  if path=='/api/auth/me':return self.send(200,{'user':userj(u)} if u else {'error':'unauthorized'}) if u else self.send(401,{'error':'unauthorized'})
  if not u:return self.send(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   ws=[]
   for w in q('select w.* from workspaces w join wmembers m on m.workspace_id=w.id where m.user_id=?',(u['id'],)): ws.append(dict(w))
   return self.send(200,{'workspaces':ws})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True)
   if not w:return self.send(404,{})
   if not q('select 1 from wmembers where workspace_id=? and user_id=?',(w['id'],u['id']),True):return self.send(403,{})
   cs=[chj(x) for x in q('select * from channels where workspace_id=? and is_archived=0 and (is_private=0 or id in(select channel_id from members where user_id=?))',(w['id'],u['id']))]
   return self.send(200,{'workspace':dict(w),'channels':cs})
  m=re.match(r'/api/channels/(\d+)/members',path)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.send(404,{})
   return self.send(200,{'members':[userj(q('select * from users where id=?',(x,),True)) for x in members(c['id'])]})
  m=re.match(r'/api/channels/(\d+)/messages',path)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.send(404,{})
   if c['is_private'] and u['id'] not in members(c['id']):return self.send(403,{})
   return self.send(200,{'messages':[msgj(x) for x in q('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(c['id'],))],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies',path)
  if m:
   return self.send(200,{'replies':[msgj(x) for x in q('select * from messages where parent_id=? and deleted=0 order by id',(m.group(1),))],'next_cursor':None})
  return self.send(404,{})
 def do_POST(self):
  path=urllib.parse.urlparse(self.path).path; u=auth(self.headers.get('Authorization',''))
  if path=='/api/auth/register':
   d=self.body()
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.send(400,{'error':'invalid username or password'})
   try:
    c=db();c.execute('insert into users(username,password,display_name) values(?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username']));uid=c.execute('select last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));c.commit();r=c.execute('select * from users where id=?',(uid,)).fetchone();c.close();return self.send(201,{'user':userj(r),'token':t})
   except sqlite3.IntegrityError:
    try: c.close()
    except: pass
    return self.send(409,{})
  if path=='/api/auth/login':
   d=self.body();r=q('select * from users where username=? and password=?',(d.get('username'),d.get('password')),True)
   if not r:return self.send(401,{})
   t=secrets.token_urlsafe(24);dbx('insert into tokens values(?,?)',(t,r['id']));return self.send(200,{'user':userj(r),'token':t})
  if not u:return self.send(401,{})
  d=self.body()
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.send(400,{})
   try:
    c=db();c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into wmembers values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?)',(cid,u['id']));c.commit();w=c.execute('select * from workspaces where id=?',(wid,)).fetchone();ch=c.execute('select * from channels where id=?',(cid,)).fetchone();c.close();return self.send(201,{'workspace':dict(w),'general_channel':chj(ch)})
   except sqlite3.IntegrityError:return self.send(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True)
   if not w:return self.send(404,{})
   role=q('select role from wmembers where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role:return self.send(403,{})
   if d.get('is_private') and role['role']!='owner':return self.send(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.send(400,{})
   if q('select 1 from channels where workspace_id=? and name=?',(w['id'],d.get('name','')),True): return self.send(409,{'error':'channel already exists'})
   try:
    c=db();c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?)',(cid,u['id']));c.commit();r=c.execute('select * from channels where id=?',(cid,)).fetchone();c.close();return self.send(201,{'channel':chj(r)})
   except sqlite3.IntegrityError:return self.send(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.send(404,{})
   if c['is_private']:return self.send(403,{})
   dbx('insert or ignore into members values(?,?)',(c['id'],u['id']));dbx('insert or ignore into wmembers values(?,?,?)',(c['workspace_id'],u['id'],'member'));return self.send(200,{'channel':chj(c)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True); body=d.get('body','')
   if not c:return self.send(404,{})
   if c['is_archived']:return self.send(423,{})
   if not body.strip():return self.send(400,{})
   if c['is_private'] and u['id'] not in members(c['id']):return self.send(403,{})
   return self.send(201,{'message':msgj(create_msg(c['id'],u['id'],body,d.get('parent_id')))})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   dbx('insert or ignore into reactions values(?,?,?)',(m.group(1),u['id'],d.get('emoji','')));r=q('select * from messages where id=?',(m.group(1),),True);return self.send(200,{'reactions':msgj(r)['reactions']})
  m=re.match(r'/api/messages/(\d+)/(pin|unpin)',path)
  if m:return self.send(200,{'deleted':True})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.send(404,{})
   owner=q('select owner_id from workspaces where id=?',(c['workspace_id'],),True)
   if not owner or owner['owner_id']!=u['id']:return self.send(403,{})
   archived=1 if m.group(2)=='archive' else 0
   dbx('update channels set is_archived=? where id=?',(archived,c['id']))
   return self.send(200,{'channel':chj(q('select * from channels where id=?',(c['id'],),True))})
  return self.send(404,{})
 def do_PATCH(self):
  u=auth(self.headers.get('Authorization',''))
  if not u:return self.send(401,{})
  d=self.body();m=re.match(r'/api/messages/(\d+)$',self.path); 
  if m:
   r=q('select * from messages where id=?',(m.group(1),),True)
   if not r:return self.send(404,{})
   if r['deleted']:return self.send(409,{})
   if r['author_id']!=u['id']:return self.send(403,{})
   if not d.get('body','').strip():return self.send(400,{})
   dbx('update messages set body=?,edited_at=? where id=?',(d['body'],now(),r['id']));r=q('select * from messages where id=?',(r['id'],),True);f=emit(r['channel_id'],'message.updated',{'message':msgj(r)});broadcast(f);return self.send(200,{'message':msgj(r)})
  m=re.match(r'/api/channels/(\d+)$',self.path)
  if m:
   r=q('select * from channels where id=?',(m.group(1),),True);dbx('update channels set topic=? where id=?',(d.get('topic',''),m.group(1)));return self.send(200,{'channel':chj(q('select * from channels where id=?',(m.group(1),),True))})
  return self.send(404,{})
 def do_DELETE(self):
  u=auth(self.headers.get('Authorization',''))
  if not u:return self.send(401,{})
  m=re.match(r'/api/messages/(\d+)$',self.path);r=q('select * from messages where id=?',(m.group(1),),True) if m else None
  if not r:return self.send(404,{})
  owner=q('select owner_id from workspaces w join channels c on c.workspace_id=w.id where c.id=?',(r['channel_id'],),True)
  if r['author_id']!=u['id'] and owner['owner_id']!=u['id']:return self.send(403,{})
  if not r['deleted']:
   dbx('update messages set deleted=1 where id=?',(r['id'],));f=emit(r['channel_id'],'message.deleted',{'message_id':r['id']});broadcast(f)
  return self.send(200,{'deleted':True})
def event_pump():
 last=0
 r=q('select coalesce(max(id),0) n from events',(),True)
 last=r['n'] if r else 0
 while True:
  try:
   rows=q('select id,channel_id,payload from events where id>? order by id',(last,))
   for e in rows:
    open('/tmp/irc-pump-debug.log','a').write('event '+str(e['id'])+' cid '+str(e['channel_id'])+' clients '+repr([(x.get('channels'),x.get('nick')) for x in irc_clients])+'\\n')
    last=e['id']
    try: frame=json.loads(e['payload'])
    except: continue
    for w in list(clients):
     if w.get('cid')==e['channel_id'] and json.loads(e['payload']).get('event_id',0)>w.get('last',0):
      try: ws_send(w['sock'],e['payload']); w['last']=json.loads(e['payload']).get('event_id',w.get('last',0))
      except: pass
    if frame.get('type')=='message.created':
     m=frame.get('message',{})
     for c in list(irc_clients):
      if e['channel_id'] in c.get('channels',[]):
       open('/tmp/irc-pump-debug.log','a').write('match '+str(e['channel_id'])+'\\n')
       try:
        u=m.get('author',{}).get('username','user')
        line=':'+u+'!'+u+'@huddle PRIVMSG '+irc_name(e['channel_id'])+' :'+m.get('body','')+'\r\n'
        c['sock'].sendall(line.encode())
       except: pass
  except: pass
  time.sleep(.05)
def run():
 init(); threading.Thread(target=event_pump,daemon=True).start(); ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
if __name__=='__main__': run()
