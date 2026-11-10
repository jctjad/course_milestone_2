import sys,os,json,re,sqlite3,secrets,hashlib,base64,socket,threading,time
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; PORT=int(sys.argv[1]) if len(sys.argv)>1 else 8000; NODE=PORT-8000
subs={}; subs_lock=threading.Lock(); stop=threading.Event(); wake=threading.Event(); last_row=0
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None,check_same_thread=False); c.execute('PRAGMA busy_timeout=2000'); c.row_factory=sqlite3.Row; return c
def setup():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
'''); c.close()
def user_json(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'] or 'UTC','avatar_url':r['avatar_url'] or '','status_text':r['status_text'] or '','status_emoji':r['status_emoji'] or ''}
def channel_json(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def token_user(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def auth(h):
 x=h.get('Authorization','')
 return token_user(x[7:] if x.startswith('Bearer ') else None)
def getch(c,i): return c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone()
def member(c,ch,uid): return c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone() is not None
def canread(c,ch,uid):
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 return c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone() is not None and (not ch['is_private'] or member(c,ch['id'],uid))
def msgjson(c,r):
 u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_json(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':mentions(c,r['body'],r['channel_id'],r['author_id'])}
def mentions(c,body,ch,author):
 out=[]; names=re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body)
 cr=c.execute('SELECT workspace_id FROM channels WHERE id=?',(ch,)).fetchone()
 for n in names:
  if n=='channel':
   ids=[x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(ch,))]
  else:
   x=c.execute('SELECT id FROM users WHERE username=?',(n,)).fetchone(); ids=[x['id']] if x else []
  for i in ids:
   if i!=author and c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(cr['workspace_id'],i)).fetchone() and i not in out: out.append(i)
 return out
def emit(c,ch,kind,payload):
 for attempt in range(20):
  try:
   c.execute("BEGIN IMMEDIATE")
   row=c.execute("SELECT n FROM counters WHERE channel_id=?",(ch,)).fetchone()
   n=(row["n"]+1) if row else 1
   if row:
    c.execute("UPDATE counters SET n=? WHERE channel_id=?",(n,ch))
   else:
    c.execute("INSERT INTO counters(channel_id,n) VALUES(?,?)",(ch,n))
   c.execute("INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)",(ch,n,kind,json.dumps(payload)))
   c.commit()
   wake.set(); notify(ch)
   return {"type":kind,"event_id":n,"channel_id":ch,**payload}
  except (sqlite3.OperationalError,sqlite3.IntegrityError):
   try: c.rollback()
   except: pass
   time.sleep(0.01*(attempt+1))
 raise sqlite3.OperationalError("event allocation failed")

def notify(ch):
 try:
  s=socket.create_connection(('127.0.0.1',6379),.2); s.settimeout(.2); x=f'*2\r\n$7\r\nPUBLISH\r\n${len("huddle:events")}\r\nhuddle:events\r\n${len(str(ch))}\r\n{ch}\r\n'.encode(); s.sendall(x); s.close()
 except: pass
def pump():
 global last_row
 while True:
  try:
   c=db(); rows=c.execute('SELECT rowid AS event_row,* FROM events WHERE rowid>? ORDER BY rowid',(last_row,)).fetchall(); c.close()
   for r in rows:
    last_row=r['event_row']; frame={'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id'],**json.loads(r['payload'])}
    with subs_lock:
     ss=list(subs.get(r['channel_id'],[]))
    for ws in ss:
     try: ws.send(frame)
     except:
      with subs_lock:
       if ws in subs.get(r['channel_id'],[]): subs[r['channel_id']].remove(ws)
   wake.wait(.1); wake.clear()
  except Exception as e:
   import traceback; traceback.print_exc(); time.sleep(.1)
def redis_listener():
 while True:
  try:
   s=socket.create_connection(('127.0.0.1',6379),.2); s.settimeout(1); s.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
   while True:
    if not s.recv(4096): raise OSError()
    wake.set()
  except: time.sleep(.5)
class WS:
 def __init__(self,h): self.h=h; self.s=h.request
 def send(self,o):
  d=json.dumps(o,separators=(',',':')).encode(); n=len(d)
  if n<126: z=bytes([129,n])
  elif n<65536:z=bytes([129,126])+n.to_bytes(2,'big')
  else:z=bytes([129,127])+n.to_bytes(8,'big')
  self.s.sendall(z+d)
 def recv(self):
  a=self.s.recv(2)
  if not a:return None
  n=a[1]&127
  if n==126:n=int.from_bytes(self.s.recv(2),'big')
  if n==127:n=int.from_bytes(self.s.recv(8),'big')
  m=self.s.recv(4); d=self.s.recv(n); return json.loads(bytes(x^m[i%4] for i,x in enumerate(d)))
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x):pass
 def out(self,status,obj=None,raw=None,typ='application/json'):
  d=raw if raw is not None else json.dumps(obj,separators=(',',':')).encode()
  self.send_response(status); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(d))); self.end_headers(); self.wfile.write(d)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  if self.path=='/api/health': return self.out(200,{'status':'ok','node_id':NODE})
  if self.path.startswith('/api/ws'):
   u=auth(self.headers)
   if not u:
    q=self.path.split('?',1)[1] if '?' in self.path else ''
    tok=next((x[6:] for x in q.split('&') if x.startswith('token=')),None)
    u=token_user(tok)
   if not u:return self.send_error(401)
   key=self.headers.get('Sec-WebSocket-Key')
   if not key:return self.send_error(400)
   self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()); self.end_headers()
   ws=WS(self)
   try:
    while True:
     x=ws.recv()
     if not x:break
     ch=int(x.get('channel_id'))
     c=db(); cr=getch(c,ch)
     if x.get('type')=='subscribe':
      with subs_lock: subs.setdefault(ch,[]).append(ws)
      head=c.execute('SELECT COALESCE(max(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']; ws.send({'type':'subscribed','channel_id':ch,'head_event_id':head})
     elif x.get('type')=='resume':
      since=int(x.get('since_event_id',0)); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
      for r in rows: ws.send({'type':r['kind'],'event_id':r['event_id'],'channel_id':ch,**json.loads(r['payload'])})
      head=c.execute('SELECT COALESCE(max(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']; ws.send({'type':'resumed','channel_id':ch,'head_event_id':head})
     c.close()
   except: pass
   return
  u=auth(self.headers); c=db(); p=self.path.split('?')[0]; parts=p.split('/')
  if p=='/api/auth/me':
   if not u:return self.out(401,{'error':'unauthorized'})
   return self.out(200,{'user':user_json(u)})
  if len(parts)>=4 and parts[2]=='workspaces' and parts[3]:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[3],)).fetchone()
   if not w:return self.out(404,{'error':'not found'})
   if not u or not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.out(403,{'error':'forbidden'})
   chans=[channel_json(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)) if not x['is_private'] or member(c,x['id'],u['id'])]
   return self.out(200,{'workspace':dict(w),'channels':chans})
  if len(parts)>=5 and parts[2]=='channels' and parts[3].isdigit():
   ch=getch(c,int(parts[3]))
   if not ch:return self.out(404,{'error':'not found'})
   if parts[-1]=='members':
    if not u or not canread(c,ch,u['id']):return self.out(403,{'error':'forbidden'})
    return self.out(200,{'members':[user_json(c.execute('SELECT * FROM users WHERE id=?',(x['user_id'],)).fetchone()) for x in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(ch['id'],))]})
   if parts[-1]=='messages':
    if not u or not canread(c,ch,u['id']):return self.out(403,{'error':'forbidden'})
    return self.out(200,{'messages':[msgjson(c,x) for x in c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(ch['id'],))],'next_cursor':None})
  c.close(); return self.out(404,{'error':'not found'})
 def do_POST(self):
  p=self.path.split('?')[0]; parts=p.split('/'); data=self.body(); u=auth(self.headers); c=db()
  if p=='/api/auth/register':
   un=data.get('username',''); pw=data.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.out(400,{'error':'invalid'})
   try:
    c.execute('INSERT INTO users(username,password,display_name,timezone) VALUES(?,?,?,?)',(un,pw,data.get('display_name') or un,'UTC')); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); return self.out(201,{'user':user_json(c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()),'token':t})
   except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
  if p=='/api/auth/login':
   r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(data.get('username'),data.get('password'))).fetchone()
   if not r:return self.out(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();return self.out(200,{'user':user_json(r),'token':t})
  if not u:return self.out(401,{'error':'unauthorized'})
  if p=='/api/workspaces':
   slug=data.get('slug',''); 
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not data.get('name'):return self.out(400,{'error':'invalid'})
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,data['name'],u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); ch=c.execute('SELECT * FROM channels WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone();c.execute('INSERT INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();return self.out(201,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':channel_json(ch)})
   except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
  if len(parts)>=5 and parts[2]=='workspaces' and parts[4]=='channels':
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[3],)).fetchone()
   if not w:return self.out(404,{'error':'not found'})
   role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if not role:return self.out(403,{'error':'forbidden'})
   private=bool(data.get('is_private',False))
   if private and role['role']!='owner':return self.out(403,{'error':'forbidden'})
   name=data.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.out(400,{'error':'invalid'})
   if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():return self.out(409,{'error':'duplicate'})
   c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),data.get('topic',''))); ch=getch(c,c.execute('SELECT last_insert_rowid()').fetchone()[0]);c.execute('INSERT INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();return self.out(201,{'channel':channel_json(ch)})
  if len(parts)>=5 and parts[2]=='channels' and parts[3].isdigit():
   ch=getch(c,int(parts[3]))
   if not ch:return self.out(404,{'error':'not found'})
   if parts[-1]=='join':
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if ch['is_private']:return self.out(403,{'error':'private'})
    c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(w['id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();return self.out(200,{'channel':channel_json(ch)})
   if parts[-1]=='archive' or parts[-1]=='unarchive':
    w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']:return self.out(403,{'error':'forbidden'})
    val=parts[-1]=='archive';c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(val),ch['id']));c.commit();return self.out(200,{'channel':channel_json(getch(c,ch['id']))})
   if parts[-1]=='messages':
    if ch['is_archived']:return self.out(423,{'error':'archived'})
    if not canread(c,ch,u['id']):return self.out(403,{'error':'forbidden'})
    body=data.get('body','')
    if not isinstance(body,str) or not body.strip():return self.out(400,{'error':'empty'})
    c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],u['id'],body,now())); r=c.execute('SELECT * FROM messages WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone(); m=msgjson(c,r); emit(c,ch['id'],'message.created',{'message':m}); return self.out(201,{'message':m})
  c.close();return self.out(404,{'error':'not found'})
 def do_PATCH(self):
  u=auth(self.headers); c=db()
  if not u:return self.out(401,{'error':'unauthorized'})
  p=self.path.split('?')[0]; parts=p.split('/'); data=self.body()
  if len(parts)>=4 and parts[2]=='channels' and parts[3].isdigit():
   ch=getch(c,int(parts[3])); w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if not ch:return self.out(404,{'error':'not found'})
   if not member(c,ch['id'],u['id']) and w['owner_id']!=u['id']:return self.out(403,{'error':'forbidden'})
   if len(data.get('topic',''))>250:return self.out(400,{'error':'invalid'})
   c.execute('UPDATE channels SET topic=? WHERE id=?',(data.get('topic',''),ch['id']));c.commit();return self.out(200,{'channel':channel_json(getch(c,ch['id']))})
  return self.out(404,{'error':'not found'})

if __name__ == '__main__':
    setup()
    threading.Thread(target=pump,daemon=True).start()
    threading.Thread(target=redis_listener,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
