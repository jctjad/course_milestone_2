import sys,os,json,re,sqlite3,secrets,hashlib,base64,socket,struct,threading,time
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
subs={}; sublock=threading.RLock(); wake=threading.Event(); stop=False
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True); c=db(); c.executescript('''
 PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
 CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
 '''); c.commit(); c.close()
def user_json(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def usertok(t):
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def token(h):
 x=h or ''; return usertok(x[7:] if x.startswith('Bearer ') else '')
def chan(c): return {'id':c['id'],'workspace_id':c['workspace_id'],'name':c['name'],'is_private':bool(c['is_private']),'is_dm':bool(c['is_dm']),'topic':c['topic'] or '','is_archived':bool(c['is_archived'])}
def msg(r):
 c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_json(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def mentions(body,cid,aid):
 c=db(); ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone(); out=[]; pats=re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body)
 for p in pats:
  if p=='channel':
   out += [x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id<>?',(cid,aid))]
  else:
   x=c.execute('SELECT u.id FROM users u JOIN wm m ON m.user_id=u.id WHERE m.workspace_id=? AND u.username=? AND u.id<>?',(ch['workspace_id'],p,aid)).fetchone()
   if x: out.append(x['id'])
 c.close(); return list(dict.fromkeys(out))
def emit(cid,kind,payload):
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE'); r=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone()
  n=(r['event_id'] if r else 0)+1
  if r:c.execute('UPDATE counters SET event_id=? WHERE channel_id=?',(n,cid))
  else:c.execute('INSERT INTO counters VALUES(?,?)',(cid,n))
  c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload,separators=(',',':')))); c.commit()
 finally:c.close()
 notify(cid); return {'type':kind,'event_id':n,'channel_id':cid,**payload}
def notify(cid):
 try:
  s=socket.create_connection(('127.0.0.1',6379),.2); s.sendall(('*2\r\n$7\r\nPUBLISH\r\n$13\r\nhuddle:events\r\n$%d\r\n%d\r\n'%(len(str(cid)),cid)).encode()); s.close()
 except: pass
 wake.set()
def redis_listener():
 while True:
  s=None
  try:
   s=socket.create_connection(('127.0.0.1',6379),.2)
   s.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
   s.settimeout(.5)
   while True:
    try:
     data=s.recv(4096)
     if not data: raise OSError('redis closed')
     wake.set()
    except socket.timeout:
     continue
  except Exception:
   time.sleep(.25)
  finally:
   if s:
    try: s.close()
    except: pass
def wsframe(o):
 b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
 if n<126:return b'\x81'+bytes([n])+b
 if n<65536:return b'\x81\x7e'+struct.pack('!H',n)+b
 return b'\x81\x7f'+struct.pack('!Q',n)+b
def sendws(s,o):
 try:s.sendall(wsframe(o)); return True
 except:return False
def pump():
 c=db(); r=c.execute('SELECT COALESCE(MAX(id),0) AS n FROM events').fetchone(); last=r['n']; c.close()
 while True:
  wake.wait(.1); wake.clear(); c=db(); rows=c.execute('SELECT * FROM events WHERE id>? ORDER BY id',(last,)).fetchall(); c.close()
  for r in rows:
   last=r['id']; f={'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id'],**json.loads(r['payload'])}
   with sublock:
    for cid,arr in list(subs.items()):
     if cid==r['channel_id']:
      for s in arr[:]:
       if not sendws(s,f): arr.remove(s)
def setup_threads():
 threading.Thread(target=redis_listener,daemon=True).start(); threading.Thread(target=pump,daemon=True).start()
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x):pass
 def sendj(self,status,o):
  b=json.dumps(o,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  u=token(self.headers.get('Authorization')); 
  if not u:self.sendj(401,{'error':'unauthorized'})
  return u
 def route(self,method,path,q):
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/api/auth/register' and method=='POST':
   d=self.body(); un=d.get('username',''); pw=d.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.sendj(400,{'error':'invalid'})
   c=db()
   try:
    c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(un,pw,d.get('display_name') or un,'UTC','','','')); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();return self.sendj(201,{'user':user_json(u),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
   finally:c.close()
  if path=='/api/auth/login' and method=='POST':
   d=self.body();c=db();u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
   if not u:c.close();return self.sendj(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.commit();c.close();return self.sendj(200,{'user':user_json(u),'token':t})
  if path=='/api/auth/me':
   u=self.auth()
   return u and self.sendj(200,{'user':user_json(u)})
  u=self.auth()
  if not u:return
  c=db()
  try:
   if method=='POST' and path=='/api/workspaces':
    d=self.body(); slug=d.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return self.sendj(400,{'error':'slug'})
    try:
     c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,0,"")',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return self.sendj(201,{'workspace':{'id':wid,'slug':slug,'name':w['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':chan(ch)})
    except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
   m=re.match(r'^/api/workspaces/([^/]+)$',path)
   if m and method=='GET':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.sendj(404,{})
    if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
    cs=[chan(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))',(w['id'],u['id']))];return self.sendj(200,{'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':'open'},'channels':cs})
   m=re.match(r'^/api/workspaces/([^/]+)/channels$',path)
   if m and method=='POST':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();d=self.body(); name=d.get('name','')
    if not w:return self.sendj(404,{})
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32:return self.sendj(400,{})
    if d.get('is_private') and u['id']!=w['owner_id']:return self.sendj(403,{})
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit();return self.sendj(201,{'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    except sqlite3.IntegrityError:return self.sendj(409,{})
   m=re.match(r'^/api/channels/(\d+)(?:/(.*))?$',path); cid=int(m.group(1)) if m else None; tail=(m.group(2) or '') if m else ''
   ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone() if cid else None
   if m and not ch:return self.sendj(404,{})
   if cid and tail=='join' and method=='POST':
    if ch['is_private']:return self.sendj(403,{})
    c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,u['id']));c.commit();return self.sendj(200,{'channel':chan(ch)})
   if cid and tail=='members' and method=='GET':
    return self.sendj(200,{'members':[user_json(x) for x in c.execute('SELECT u.* FROM users u JOIN cm m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))]})
   if cid and tail in ('archive','unarchive') and method=='POST':
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']:return self.sendj(403,{})
    v=tail=='archive';c.execute('UPDATE channels SET is_archived=? WHERE id=?',(v,cid));c.commit();return self.sendj(200,{'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   if cid and tail=='' and method=='PATCH':
    d=self.body()
    if len(d.get('topic',''))>250:return self.sendj(400,{})
    if not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return self.sendj(403,{})
    c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),cid));c.commit();return self.sendj(200,{'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   if cid and tail=='messages' and method=='POST':
    d=self.body(); body=d.get('body','')
    if not isinstance(body,str) or not body.strip():return self.sendj(400,{})
    if ch['is_archived']:return self.sendj(423,{})
    if ch['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return self.sendj(403,{})
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); mo=msg(r); emit(cid,'message.created',{'message':mo});return self.sendj(201,{'message':mo})
   if cid and tail=='messages' and method=='GET':
    if ch['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return self.sendj(403,{})
    rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(cid,));return self.sendj(200,{'messages':[msg(x) for x in rs],'next_cursor':None})
   return self.sendj(404,{})
  finally:c.close()
 def do_GET(self):
  if self.path.startswith('/api/ws'):
   t=re.search(r'[?&]token=([^&]+)',self.path); u=usertok(t.group(1) if t else '')
   if not u:return self.send_error(401); 
   key=self.headers.get('Sec-WebSocket-Key')
   if not key:return self.send_error(400)
   self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers();s=self.connection
   try:
    while True:
     h=s.recv(2)
     if not h:break
     ln=h[1]&127
     if ln==126:ln=struct.unpack('!H',s.recv(2))[0]
     elif ln==127:ln=struct.unpack('!Q',s.recv(8))[0]
     mask=s.recv(4); data=bytes(x^mask[i%4] for i,x in enumerate(s.recv(ln))); d=json.loads(data)
     if d.get('type')=='subscribe':
      cid=int(d['channel_id']); c=db();head=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone();c.close()
      with sublock:subs.setdefault(cid,[]).append(s)
      sendws(s,{'type':'subscribed','channel_id':cid,'head_event_id':head['event_id'] if head else 0})
     elif d.get('type')=='resume':
      cid=int(d['channel_id']);c=db();rs=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(d.get('since_event_id',0)))).fetchall();head=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone()['event_id'];c.close()
      for r in rs:sendws(s,{'type':r['kind'],'event_id':r['event_id'],'channel_id':cid,**json.loads(r['payload'])})
      sendws(s,{'type':'resumed','channel_id':cid,'head_event_id':head})
   except: pass
   return
  p=self.path.split('?',1)[0];self.route('GET',p,{})
 def do_POST(self):self.route('POST',self.path.split('?',1)[0],{})
 def do_PATCH(self):self.route('PATCH',self.path.split('?',1)[0],{})
if __name__=='__main__':
 init();setup_threads();ThreadingHTTPServer(('127.0.0.1',8000+NODE),H).serve_forever()
