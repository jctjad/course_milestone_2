#!/usr/bin/env python3
import sys,os,json,re,sqlite3,secrets,hashlib,base64,socket,threading,time,struct
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; PORT=int(sys.argv[1]); NODE=PORT-8000
clients=[]; clients_lock=threading.Lock(); stop=False
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER); CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id)); CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id)); CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));'''); c.commit(); c.close()
def userj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def channelj(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def user_from_token(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def get_user_by_username(n):
 c=db(); r=c.execute('SELECT * FROM users WHERE username=?',(n,)).fetchone(); c.close(); return r
def ws(slug):
 c=db(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); c.close(); return r
def ch(cid):
 c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return r
def member(cid,uid):
 c=db(); x=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.close(); return bool(x)
def role(wid,uid):
 c=db(); x=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone(); c.close(); return x['role'] if x else None
def msgj(r):
 c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
def mention_ids(body,channel,author):
 pats=set(re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body)); c=db(); out=[]
 for n in pats:
  if n=='channel':
   out += [x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(channel['id'],author))]
  else:
   x=c.execute('SELECT u.id FROM users u JOIN wm m ON u.id=m.user_id WHERE u.username=? AND m.workspace_id=?',(n,channel['workspace_id'])).fetchone()
   if x and x['id']!=author and member(channel['id'],x['id']): out.append(x['id'])
 c.close(); return list(dict.fromkeys(out))
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); x=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone()
 eid=(x['event_id']+1) if x else 1
 if x:c.execute('UPDATE counters SET event_id=? WHERE channel_id=?',(eid,cid))
 else:c.execute('INSERT INTO counters VALUES(?,?)',(cid,eid))
 frame={'type':kind,'event_id':eid,'channel_id':cid}; frame.update(payload)
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,eid,kind,json.dumps(frame))); c.commit(); c.close(); notify(); return frame
def notify():
 try:
  s=socket.create_connection(('127.0.0.1',6379),.15); s.sendall(b'*2\r\n$4\r\nPUBLISH\r\n$13\r\nhuddle:events\r\n'); s.sendall(b'$1\r\nx\r\n'); s.close()
 except: pass
def redis_listener():
 while True:
  try:
   s=socket.create_connection(('127.0.0.1',6379),1); s.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
   while s.recv(4096): pass
  except: time.sleep(.5)
def pump():
 last=0
 while True:
  try:
   c=db(); rows=c.execute('SELECT id,payload FROM events WHERE id>? ORDER BY id',(last,)).fetchall(); c.close()
   for r in rows:
    last=r['id']; f=json.loads(r['payload'])
    with clients_lock:
     for cl in list(clients):
      if f['channel_id'] in cl['subs'] and f.get('event_id',0)>cl['cursor'].get(f['channel_id'],0):
       try: sendframe(cl['sock'],f); cl['cursor'][f['channel_id']]=f.get('event_id',0)
       except: clients.remove(cl)
  except: pass
  time.sleep(.1)
def sendframe(s,obj):
 b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
 if n<126:h=bytes([0x81,n])
 elif n<65536:h=bytes([0x81,126])+struct.pack('!H',n)
 else:h=bytes([0x81,127])+struct.pack('!Q',n)
 s.sendall(h+b)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def out(self,status,obj=None,raw=None,ctype='application/json'):
  b=raw if raw is not None else json.dumps(obj,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.send_header('Connection','close'); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  x=self.headers.get('Authorization',''); return user_from_token(x[7:] if x.startswith('Bearer ') else '')
 def route(self,p):
  return p.strip('/').split('/')
 def do_GET(self):
  if self.path=='/api/health': return self.out(200,{'status':'ok','node_id':NODE})
  if self.path.startswith('/api/ws'): return self.websocket()
  u=self.auth()
  if not u:return self.out(401,{'error':'unauthorized'})
  q=self.path.split('?')[0]; z=self.route(q)
  if z==['api','auth','me']:return self.out(200,{'user':userj(u)})
  if len(z)==3 and z[1]=='workspaces':
   w=ws(z[2])
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   c=db(); rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0)',(w['id'],)).fetchall(); c.close()
   return self.out(200,{'workspace':dict(w),'channels':[channelj(x) for x in rows]})
  if len(z)==4 and z[1]=='channels' and z[3]=='members':
   x=ch(int(z[2])); 
   if not x:return self.out(404,{})
   c=db(); rows=c.execute('SELECT u.* FROM users u JOIN cm m ON u.id=m.user_id WHERE m.channel_id=?',(x['id'],)).fetchall(); c.close(); return self.out(200,{'members':[userj(r) for r in rows]})
  if len(z)==4 and z[1]=='channels' and z[3]=='messages':
   x=ch(int(z[2]))
   if not x or (x['is_private'] and not member(x['id'],u['id'])):return self.out(403,{})
   c=db(); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(x['id'],)).fetchall(); c.close(); return self.out(200,{'messages':[msgj(r) for r in rows],'next_cursor':None})
  return self.out(404,{})
 def do_POST(self):
  u=self.auth()
  if not u:return self.out(401,{})
  z=self.route(self.path.split('?')[0]); b=self.body()
  if z==['api','auth','register']: return self.out(401,{})
  if z==['api','workspaces']:
   slug=b.get('slug',''); name=b.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name:return self.out(400,{})
   c=db()
   try:
    c.execute('BEGIN'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,name,u['id'])); wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id'])); c.execute('INSERT INTO counters VALUES(?,0)',(cid,)); c.commit()
   except sqlite3.IntegrityError:c.rollback(); c.close(); return self.out(409,{})
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); g=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return self.out(201,{'workspace':dict(w),'general_channel':channelj(g)})
  if len(z)==4 and z[1]=='workspaces' and z[3]=='channels':
   w=ws(z[2]); name=b.get('name',''); private=bool(b.get('is_private',False))
   if not w:return self.out(404,{})
   if role(w['id'],u['id'])!='owner' or not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.out(400 if re.fullmatch(r'[a-z0-9-]{1,32}',name) else 400,{})
   c=db()
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id'])); c.execute('INSERT INTO counters VALUES(?,0)',(cid,)); c.commit()
   except sqlite3.IntegrityError:c.rollback();c.close();return self.out(409,{})
   r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.out(201,{'channel':channelj(r)})
  if len(z)==4 and z[1]=='channels' and z[3]=='join':
   x=ch(int(z[2]))
   if not x:return self.out(404,{})
   if x['is_private']:return self.out(403,{})
   c=db(); c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(x['workspace_id'],u['id'],'member')); c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(x['id'],u['id'])); c.commit();c.close();return self.out(200,{'channel':channelj(x)})
  if len(z)==4 and z[1]=='channels' and z[3] in ('archive','unarchive'):
   x=ch(int(z[2])); 
   if not x:return self.out(404,{})
   if role(x['workspace_id'],u['id'])!='owner':return self.out(403,{})
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(z[3]=='archive',x['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(x['id'],)).fetchone();c.close();return self.out(200,{'channel':channelj(r)})
  if len(z)==4 and z[1]=='channels' and z[3]=='messages':
   x=ch(int(z[2])); body=b.get('body','')
   if not x or not member(x['id'],u['id']):return self.out(403,{})
   if x['is_archived']:return self.out(423,{})
   if not isinstance(body,str) or not body.strip():return self.out(400,{})
   c=db(); t=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(x['id'],u['id'],body,b.get('parent_id'),t)); mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close(); m=msgj(r);m['mentions']=mention_ids(body,x,u['id']); emit(x['id'],'message.created',{'message':m});return self.out(201,{'message':m})
  return self.out(404,{})
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.out(401,{})
  z=self.route(self.path);b=self.body()
  if len(z)==3 and z[1]=='channels':
   x=ch(int(z[2])); 
   if not x:return self.out(404,{})
   if not member(x['id'],u['id']) and role(x['workspace_id'],u['id'])!='owner':return self.out(403,{})
   if len(b.get('topic',''))>250:return self.out(400,{})
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),x['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(x['id'],)).fetchone();c.close();return self.out(200,{'channel':channelj(r)})
  return self.out(404,{})
 def websocket(self):
  t=self.path.split('token=',1)[1].split('&')[0] if 'token=' in self.path else ''
  if not user_from_token(t): self.send_error(401); return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:self.send_error(400);return
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  c={'sock':self.connection,'subs':set(),'cursor':{}}; clients.append(c)
  try:
   while True:
    h=self.connection.recv(2)
    if not h:break
    ln=h[1]&127
    if ln==126:ln=struct.unpack('!H',self.connection.recv(2))[0]
    elif ln==127:ln=struct.unpack('!Q',self.connection.recv(8))[0]
    mask=self.connection.recv(4); data=self.connection.recv(ln); data=bytes(data[i]^mask[i%4] for i in range(ln))
    x=json.loads(data)
    if x.get('type')=='subscribe':
     cid=int(x['channel_id']);c['subs'].add(cid); q=db();r=q.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone();q.close(); head=r['event_id'] if r else 0; c['cursor'][cid]=head; sendframe(self.connection,{'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif x.get('type')=='resume':
     cid=int(x['channel_id']); since=int(x.get('since_event_id',0)); q=db()
     rows=q.execute('SELECT payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id ASC',(cid,since)).fetchall()
     r=q.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone(); q.close()
     c['subs'].add(cid)
     for row in rows:
      frame=json.loads(row['payload']); sendframe(self.connection,frame); c['cursor'][cid]=frame.get('event_id',c['cursor'].get(cid,0))
     c['cursor'][cid]=r['event_id'] if r else c['cursor'].get(cid,0); sendframe(self.connection,{'type':'resumed','channel_id':cid,'head_event_id':r['event_id'] if r else 0})
  except:pass
  if c in clients:clients.remove(c)
def register(path='/api/auth/register'):
 pass
_orig=H.do_POST
def post(self):
 if self.path.split('?')[0]=='/api/auth/register':
  b=self.body(); n=b.get('username','');p=b.get('password','')
  if not re.fullmatch(r'[A-Za-z0-9_]+',n) or len(p)<8:return self.out(400,{})
  c=db()
  try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(n,p,b.get('display_name') or n)); uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit()
  except sqlite3.IntegrityError:c.rollback();c.close();return self.out(409,{})
  r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.out(201,{'user':userj(r),'token':t})
 if self.path.split('?')[0]=='/api/auth/login':
  b=self.body();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
  if not r:c.close();return self.out(401,{})
  t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.out(200,{'user':userj(r),'token':t})
 return _orig(self)
H.do_POST=post
init();threading.Thread(target=redis_listener,daemon=True).start();threading.Thread(target=pump,daemon=True).start()
ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
