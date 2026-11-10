import sys,os,json,sqlite3,hashlib,secrets,re,time,threading,socket,base64,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
DB='/var/lib/huddle/huddle.db'; NODE=int(sys.argv[1])-8000; subs=[]; slock=threading.Lock(); stop=False
SCHEMA='''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));'''
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=2000'); return c
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+f'{int(time.time()*1000)%1000:03d}Z'
def setup():
 os.makedirs('/var/lib/huddle',exist_ok=True); c=db(); c.executescript(SCHEMA); c.commit(); c.close()
def userj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chan(r): return {k:(bool(r[k]) if k in ('is_private','is_dm','is_archived') else r[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def token_user(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def msgj(r):
 c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); rc=c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':rc,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def mentions(body,cid,author):
 c=db(); ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone(); ids=[]
 if ch:
  members={x[0] for x in c.execute('SELECT user_id FROM wm WHERE workspace_id=?',(ch[0],))}
  members &= {x[0] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(cid,))}
  if '@channel' in body: ids=[x for x in members if x!=author]
  for n in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
   x=c.execute('SELECT id FROM users WHERE username=?',(n,)).fetchone()
   if x and x[0] in members and x[0]!=author: ids.append(x[0])
 c.close(); return list(dict.fromkeys(ids))
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); x=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
 n=(x[0]+1 if x else 1)
 if x:c.execute('UPDATE counters SET n=? WHERE channel_id=?',(n,cid))
 else:c.execute('INSERT INTO counters VALUES(?,?)',(cid,n))
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload)));c.commit();c.close(); notify(cid); return n
def notify(cid):
 try:
  s=socket.create_connection(('127.0.0.1',6379),.2); s.settimeout(.2); s.sendall(b'*2\r\n$4\r\nPUBLISH\r\n$13\r\nhuddle:events\r\n'); s.sendall(f'${len(str(cid))}\r\n{cid}\r\n'.encode()); s.close()
 except: pass
def redis_listener():
 while True:
  try:
   s=socket.create_connection(('127.0.0.1',6379),.2); s.settimeout(.5)
   s.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
   while True:
    if not s.recv(4096): break
  except: time.sleep(.5)
  finally:
   try:s.close()
   except:pass
def pump():
 seen=0
 while True:
  try:
   c=db(); rows=c.execute('SELECT id,channel_id,event_id,kind,payload FROM events WHERE id>? ORDER BY id',(seen,)).fetchall();c.close()
   for r in rows:
    seen=max(seen,r['id']); frame=json.loads(r['payload']); frame.update(type=r['kind'],event_id=r['event_id'],channel_id=r['channel_id'])
    with slock:
     for w in list(subs):
      if w['cid']==r['channel_id']: 
       try:w['send'](frame)
       except:
        try:subs.remove(w)
        except:pass
  except: pass
  time.sleep(.03)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,status,obj):
  b=json.dumps(obj,separators=(',',':')).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  x=self.headers.get('Authorization','');return token_user(x[7:] if x.startswith('Bearer ') else None)
 def do_GET(self):
  p=urlparse(self.path); q=parse_qs(p.query); path=p.path
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/': return self.sendj(200,{'status':'ok'})
  u=self.auth()
  if path=='/api/auth/me': return self.sendj(200,{'user':userj(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  try:
   if path=='/api/workspaces':
    a=[dict(x) for x in c.execute('SELECT w.* FROM workspaces w JOIN wm m ON w.id=m.workspace_id WHERE m.user_id=?',(u['id'],))]
    return self.sendj(200,{'workspaces':a})
   m=re.match(r'/api/workspaces/([^/]+)$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m[1],)).fetchone()
    if not w:return self.sendj(404,{})
    if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
    cs=[chan(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))',(w['id'],u['id']))]
    return self.sendj(200,{'workspace':dict(w),'channels':cs})
   m=re.match(r'/api/channels/(\d+)/members$',path)
   if m:
    a=[userj(x) for x in c.execute('SELECT u.* FROM users u JOIN cm m ON u.id=m.user_id WHERE m.channel_id=?',(m[1],))];return self.sendj(200,{'members':a})
   m=re.match(r'/api/channels/(\d+)/messages$',path)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m[1],)).fetchone()
    if not ch:return self.sendj(404,{})
    if not canread(c,ch,u['id']):return self.sendj(403,{})
    a=[msgj(x) for x in c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(m[1],))];return self.sendj(200,{'messages':a,'next_cursor':None})
   return self.sendj(404,{})
  finally:
   if c:
    try:c.close()
    except:pass
 def do_PATCH(self):
  return self.do_POST()
 def do_POST(self):
  path=urlparse(self.path).path;u=self.auth()
  if path=='/api/auth/register':
   try:
    d=self.body(); n=d['username'];pw=d['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',n) or len(pw)<8:raise ValueError()
    c=db();c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(n,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name',n)));uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.sendj(201,{'user':userj(r),'token':t})
   except sqlite3.IntegrityError:
    try:c.rollback();c.close()
    except:pass
    return self.sendj(409,{})
   except:
    try:c.rollback();c.close()
    except:pass
    return self.sendj(400,{})
  if path=='/api/auth/login':
   c=None
   try:
    d=self.body();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d['username'],hashlib.sha256(d['password'].encode()).hexdigest())).fetchone()
    if not r:
     c.close(); return self.sendj(401,{})
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.sendj(200,{'user':userj(r),'token':t})
   except:
    try:
     if c:c.rollback();c.close()
    except:pass
    return self.sendj(401,{})
  if not u:return self.sendj(401,{})
  d=self.body();c=db()
  try:
   if path=='/api/workspaces':
    if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{})
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d['name'],u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(gid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();g=c.execute('SELECT * FROM channels WHERE id=?',(gid,)).fetchone();return self.sendj(201,{'workspace':dict(w),'general_channel':chan(g)})
    except sqlite3.IntegrityError:return self.sendj(409,{})
   m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m[1],)).fetchone();role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return self.sendj(404,{})
    if not role:return self.sendj(403,{})
    if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.sendj(400,{})
    if d.get('is_private') and role[0]!='owner':return self.sendj(403,{})
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    except sqlite3.IntegrityError:return self.sendj(409,{})
   m=re.match(r'/api/channels/(\d+)/join$',path)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m[1],)).fetchone()
    if not ch:return self.sendj(404,{})
    if ch['is_private']:return self.sendj(403,{})
    if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone():c.execute('INSERT INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
    c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();return self.sendj(200,{'channel':chan(ch)})
   m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m[1],)).fetchone();w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']:return self.sendj(403,{})
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(m[2]=='archive',m[1]));c.commit();return self.sendj(200,{'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(m[1],)).fetchone())})
   m=re.match(r'/api/channels/(\d+)/messages$',path)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m[1],)).fetchone()
    if not ch:return self.sendj(404,{})
    if ch['is_archived']:return self.sendj(423,{})
    if not canread(c,ch,u['id']):return self.sendj(403,{})
    body=d.get('body','')
    if not body.strip():return self.sendj(400,{})
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();j=msgj(r);emit(ch['id'],'message.created',{'message':j});return self.sendj(201,{'message':j})
   m=re.match(r'/api/channels/(\d+)$',path)
   if m and self.command=='PATCH':
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m[1],)).fetchone()
    if len(d.get('topic',''))>250:return self.sendj(400,{})
    if not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(m[1],u['id'])).fetchone():return self.sendj(403,{})
    c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),m[1]));c.commit();return self.sendj(200,{'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(m[1],)).fetchone())})
   return self.sendj(404,{})
  finally:
   if c:
    try:c.close()
    except:pass
def canread(c,ch,uid):
 return bool(c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],uid)).fetchone()) if ch['is_private'] else bool(c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],uid)).fetchone())
class WS:
 def __init__(self,conn):self.c=conn
 def send(self,x):
  b=json.dumps(x,separators=(',',':')).encode();h=bytes([0x81]);n=len(b)
  if n<126:h+=bytes([n])
  elif n<65536:h+=bytes([126])+struct.pack('>H',n)
  else:h+=bytes([127])+struct.pack('>Q',n)
  self.c.sendall(h+b)
def ws_loop(h,u):
 key=h.headers.get('Sec-WebSocket-Key');h.send_response(101);h.send_header('Upgrade','WebSocket');h.send_header('Connection','Upgrade');h.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());h.end_headers();w=WS(h.connection)
 while True:
  z=h.connection.recv(2)
  if not z:break
  n=z[1]&127
  if n==126:n=struct.unpack('>H',h.connection.recv(2))[0]
  elif n==127:n=struct.unpack('>Q',h.connection.recv(8))[0]
  mask=h.connection.recv(4);b=bytes(x^mask[i%4] for i,x in enumerate(h.connection.recv(n)));d=json.loads(b)
  if d['type']=='subscribe':
   cid=int(d['channel_id']);c=db();x=c.execute('SELECT COALESCE(n,0) FROM counters WHERE channel_id=?',(cid,)).fetchone();c.close();w.send({'type':'subscribed','channel_id':cid,'head_event_id':x[0] if x else 0});withlock={'cid':cid,'send':w.send}
   with slock:subs.append(withlock)
  elif d['type']=='resume':
   cid=int(d['channel_id']);c=db();rs=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(d.get('since_event_id',0)))).fetchall();head=c.execute('SELECT COALESCE(n,0) FROM counters WHERE channel_id=?',(cid,)).fetchone()[0];c.close()
   for r in rs:
    x=json.loads(r['payload']);x.update(type=r['kind'],event_id=r['event_id'],channel_id=cid);w.send(x)
   w.send({'type':'resumed','channel_id':cid,'head_event_id':head})
def do_ws(self):
 u=token_user(parse_qs(urlparse(self.path).query).get('token',[''])[0])
 if not u:return self.send_error(401)
 try:ws_loop(self,u)
 except:pass
H.do_GET_orig=H.do_GET
def get2(self):
 if self.path.startswith('/api/ws'):return do_ws(self)
 return H.do_GET_orig(self)
H.do_GET=get2
if __name__=='__main__':
 setup();threading.Thread(target=pump,daemon=True).start();threading.Thread(target=redis_listener,daemon=True).start();ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
