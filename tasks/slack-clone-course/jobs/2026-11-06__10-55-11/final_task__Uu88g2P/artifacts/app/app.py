import os,sys,json,re,secrets,hashlib,base64,threading,time,struct,io,sqlite3
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
DB='/var/lib/huddle/huddle.db'; DATA='/var/lib/huddle'
os.makedirs(DATA,exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''
 PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
 CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);
 '''); c.commit(); c.close()
def U(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def user_token(t):
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t or '',)).fetchone(); c.close(); return r
def token(h):
 return user_token((h or '').split(' ',1)[1] if (h or '').startswith('Bearer ') else '')
def C(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def F(r): return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def reactions(mid):
 c=db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall(); c.close(); out=[]
 for e in dict.fromkeys(x['emoji'] for x in rows):
  ids=[x['user_id'] for x in rows if x['emoji']==e]; out.append({'emoji':e,'count':len(ids),'user_ids':ids})
 return out
def M(r):
 c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); fs=c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],)).fetchall(); replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':U(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[F(x) for x in fs],'reactions':reactions(r['id']),'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def members(cid):
 c=db(); return [x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(cid,)).fetchall()]
def mentions(body,cid,author):
 c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); mids=members(cid); out=[]
 for m in re.finditer(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  s=m.group(1); ids=[]
  if s=='channel': ids=mids
  elif s=='here':
   ids=[x['user_id'] for x in c.execute('SELECT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,)).fetchall()]
   ids=[i for i in ids if i in mids]
  elif ch and ch['workspace_id']:
   g=c.execute('SELECT user_ids FROM groupsx WHERE workspace_id=? AND handle=?',(ch['workspace_id'],s.lower())).fetchone()
   if g: ids=json.loads(g['user_ids'])
   else:
    x=c.execute('SELECT id FROM users WHERE username=?',(s,)).fetchone(); ids=[x['id']] if x else []
  for i in ids:
   if i!=author and i in mids and i not in out: out.append(i)
 c.close(); return out
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT coalesce(max(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload))); c.commit(); c.close(); return n
subs=[]; sublock=threading.Lock(); global_last=0
def frame(ws,obj):
 b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
 if n<126: h=bytes([0x81,n])
 elif n<65536:h=bytes([0x81,126])+struct.pack('>H',n)
 else:h=bytes([0x81,127])+struct.pack('>Q',n)
 try: ws.sendall(h+b)
 except: pass
def ws_loop(h,u):
 conn=h.connection; cid=''.join(secrets.choice('abcdef') for _ in range(12)); c=db(); c.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(cid,u['id'],time.time())); c.commit(); c.close()
 item={'sock':conn,'user':u['id'],'channels':set(),'last':0,'cid':cid}
 with sublock: subs.append(item)
 try:
  while True:
   hdr=conn.recv(2)
   if not hdr: break
   ln=hdr[1]&127; mask=conn.recv(4)
   if ln==126: ln=struct.unpack('>H',conn.recv(2))[0]
   elif ln==127: ln=struct.unpack('>Q',conn.recv(8))[0]
   data=conn.recv(ln); data=bytes(data[i]^mask[i%4] for i in range(ln))
   q=json.loads(data); typ=q.get('type'); ch=q.get('channel_id')
   if typ=='subscribe':
    item['channels'].add(int(ch)); c=db(); head=c.execute('SELECT coalesce(max(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']; c.close(); item['last']=max(item['last'],head); frame(conn,{'type':'subscribed','channel_id':ch,'head_event_id':head})
   elif typ=='resume':
    item['channels'].add(int(ch)); since=int(q.get('since_event_id',0)); c=db(); ev=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall(); head=c.execute('SELECT coalesce(max(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']; c.close()
    for e in ev: frame(conn,dict({'type':e['kind'],'event_id':e['event_id'],'channel_id':ch},**json.loads(e['payload'])))
    frame(conn,{'type':'resumed','channel_id':ch,'head_event_id':head})
 finally:
  with sublock:
   if item in subs: subs.remove(item)
  c=db(); c.execute('DELETE FROM presence WHERE conn_id=?',(cid,)); c.commit(); c.close()
def poller():
 global global_last
 while True:
  with sublock: ss=list(subs)
  c=db()
  ge=c.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(global_last,)).fetchall()
  for e in ge:
   try: obj=json.loads(e['payload'])
   except: obj={}
   for s in ss: frame(s['sock'],obj)
   global_last=max(global_last,e['id'])
  for s in ss:
   for ch in list(s['channels']):
    ev=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,s['last'])).fetchall()
    for e in ev:
     x={'type':e['kind'],'event_id':e['event_id'],'channel_id':ch}; x.update(json.loads(e['payload'])); frame(s['sock'],x); s['last']=max(s['last'],e['event_id'])
  c.close(); time.sleep(.1)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,obj,extra={}):
  b=json.dumps(obj,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); [self.send_header(k,v) for k,v in extra.items()]; self.end_headers(); self.wfile.write(b)
 def auth(self):
  u=token(self.headers.get('Authorization')); 
  if not u: self.sendj(401,{'error':'unauthorized'}); return None
  return u
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
 def getch(self,i):
  c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone(); c.close(); return r
 def allowed(self,ch,u,write=False):
  if not ch:return False
  if ch['is_dm']: return u['id'] in members(ch['id'])
  c=db(); wm=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone(); cm=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(); c.close()
  return bool(cm or (wm and not ch['is_private']))
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if path=='/api/ws':
   u= user_token(parse_qs(p.query).get('token',[''])[0])
   if not u:return self.send_error(401)
   key=self.headers.get('Sec-WebSocket-Key'); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers(); ws_loop(self,u); return
  u=self.auth()
  if not u:return
  if path=='/api/auth/me': return self.sendj(200,{'user':U(u)})
  if path.startswith('/api/users/'):
   c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(path.split('/')[-1],)).fetchone(); c.close(); return self.sendj(200,{'user':U(r)}) if r else self.sendj(404,{})
  u=self.auth()
  if not u:return
  if path=='/api/workspaces':
   c=db(); rs=c.execute('SELECT w.* FROM workspaces w JOIN wm ON wm.workspace_id=w.id WHERE wm.user_id=?',(u['id'],)).fetchall(); c.close(); return self.sendj(200,{'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rs]})
  m=re.match(r'/api/workspaces/([^/]+)(?:/(.*))?$',path)
  if m:
   c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:c.close();return self.sendj(404,{})
   if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): c.close();return self.sendj(403,{})
   tail=m.group(2)
   if tail=='groups':
    rs=c.execute('SELECT * FROM groupsx WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return self.sendj(200,{'groups':[{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'])} for r in rs]})
   rs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?)) AND is_archived=0',(w['id'],u['id'])).fetchall();c.close();return self.sendj(200,{'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[C(x) for x in rs]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   ch=self.getch(int(m.group(1))); 
   if not ch:return self.sendj(404,{})
   if not self.allowed(ch,u):return self.sendj(403,{})
   c=db(); rs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall();c.close();return self.sendj(200,{'members':[U(x) for x in rs]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=self.getch(int(m.group(1))); 
   if not ch:return self.sendj(404,{})
   if not self.allowed(ch,u):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(ch['id'],)).fetchall();c.close();return self.sendj(200,{'messages':[M(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   c=db();r=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone();c.close()
   if not r:return self.sendj(404,{})
   if m.group(2):
    self.send_response(200);self.send_header('Content-Type',r['content_type']);self.send_header('Content-Disposition',f'attachment; filename="{r["filename"]}"');self.send_header('Content-Length',str(r['size']));self.end_headers();self.wfile.write(r['data']);return
   return self.sendj(200,{'file':F(r)})
  return self.sendj(404,{})
 def do_POST(self):
  p=urlparse(self.path); path=p.path; raw=self.body()
  if path=='/api/auth/register':
   try:d=json.loads(raw)
   except:return self.sendj(400,{})
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.sendj(400,{})
   c=db()
   try:
    cur=c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(d['username'],hashlib.sha256(d['password'].encode()).hexdigest(),d.get('display_name') or d['username'],'UTC','','','')); uid=cur.lastrowid;t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.sendj(201,{'user':U(r),'token':t})
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
  if path=='/api/auth/login':
   try:d=json.loads(raw)
   except:return self.sendj(400,{})
   c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest())).fetchone()
   if not r:c.close();return self.sendj(401,{})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.sendj(200,{'user':U(r),'token':t})
  u=self.auth()
  if not u:return
  if path=='/api/workspaces':
   try:d=json.loads(raw)
   except:return self.sendj(400,{})
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{})
   c=db()
   try:
    cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id'])); wid=cur.lastrowid;c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,0)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'workspace':{'id':wid,'slug':w['slug'],'name':w['name'],'owner_id':u['id'],'join_mode':w['join_mode']},'general_channel':C(ch)})
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   try:d=json.loads(raw)
   except:return self.sendj(400,{})
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:c.close();return self.sendj(404,{})
   role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if not role or (d.get('is_private') and role['role']!='owner'):c.close();return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):c.close();return self.sendj(400,{})
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'channel':C(r)})
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=self.getch(int(m.group(1)))
   if not ch:return self.sendj(404,{})
   if ch['is_private']:return self.sendj(403,{})
   c=db();c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']))
   if ch['workspace_id']:c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
   c.commit();c.close();return self.sendj(200,{'channel':C(ch)})
  m=re.match(r'/api/channels/(\d+)/archive$',path)
  if m:return self.archive(int(m.group(1)),u,True)
  m=re.match(r'/api/channels/(\d+)/unarchive$',path)
  if m:return self.archive(int(m.group(1)),u,False)
  if path=='/api/dms':
   try:d=json.loads(raw); other=int(d['user_id'])
   except:return self.sendj(400,{})
   if other==u['id']:return self.sendj(400,{})
   c=db();x=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
   if not x:c.close();return self.sendj(404,{})
   a,b=sorted((u['id'],other));r=c.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,b)).fetchone()
   if r:cid=r['channel_id']
   else:
    c.execute('INSERT INTO channels(name,is_private,is_dm) VALUES(?,1,1)',(f'dm-{a}-{b}',));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid));c.execute('INSERT INTO cm VALUES(?,?),(?,?)',(cid,a,cid,b));c.commit()
   c.close();return self.sendj(200,{'channel_id':cid})
  if path=='/api/files':
   ct=self.headers.get('Content-Type',''); boundary=re.search(r'boundary=([^;]+)',ct)
   if not boundary:return self.sendj(400,{})
   if len(raw)>11000000:return self.sendj(413,{})
   parts=raw.split(('--'+boundary.group(1)).encode()); part=next((x for x in parts if b'filename=' in x),None)
   if not part:return self.sendj(400,{})
   head,data=part.split(b'\r\n\r\n',1);data=data[:-2] if data.endswith(b'\r\n') else data; fn=re.search(br'filename="([^"]*)"',head); typ=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
   if not fn:return self.sendj(400,{})
   if len(data)>10485760:return self.sendj(413,{})
   c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn.group(1).decode(),(typ.group(1).decode() if typ else 'application/octet-stream'),len(data),now(),data));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return self.sendj(201,{'file':F(r)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=self.getch(int(m.group(1)))
   if not ch:return self.sendj(404,{})
   if not self.allowed(ch,u):return self.sendj(403,{})
   try:d=json.loads(raw); body=d.get('body','')
   except:return self.sendj(400,{})
   if ch['is_archived'] and body.strip() != '/unarchive':return self.sendj(423,{})
   if not body.strip():return self.sendj(400,{})
   if body.startswith('/') and not body.startswith('//'):
    cmd=body.split(' ',1)[0]; arg=body[len(cmd):].strip()
    if cmd=='/me':
     if not arg:return self.sendj(400,{})
    elif cmd=='/shrug':body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if not arg or len(arg)>250 or not self.can_edit(ch,u):return self.sendj(403 if not self.can_edit(ch,u) else 400,{})
     self.sett_topic(ch,arg);return self.sendj(201,{'message':None,'channel':C(self.getch(ch['id']))})
    elif cmd=='/invite':
     if not arg.startswith('@'):return self.sendj(400,{})
     c=db();x=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone(); wm=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],x['id'] if x else -1)).fetchone()
     if not x:c.close();return self.sendj(404,{})
     if not wm:c.close();return self.sendj(403,{})
     c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],x['id']));c.commit();c.close();return self.sendj(201,{'message':None,'invited_user':U(x)})
    elif cmd in ('/archive','/unarchive'):
     return self.archive(ch['id'],u,cmd=='/archive',201)
    else:return self.sendj(400,{})
   c=db();files=d.get('file_ids',[])
   for fid in files:
    x=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=? AND attached=0',(fid,u['id'])).fetchone()
    if not x:c.close();return self.sendj(400,{})
   c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   for fid in files:c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid));c.execute('UPDATE files SET attached=1 WHERE id=?',(fid,))
   c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close(); mj=M(r); eid=emit(ch['id'],'message.created',{'message':mj});return self.sendj(201,{'message':mj})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   try:e=json.loads(raw).get('emoji','')
   except:e=''
   return self.react(int(m.group(1)),u,e,True)
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   try:d=json.loads(raw)
   except:c.close();return self.sendj(400,{})
   if not w or not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():c.close();return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',d.get('handle','')) or d['handle'] in ('here','channel'):c.close();return self.sendj(400,{})
   try:c.execute('INSERT INTO groupsx(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],d['handle'],d.get('name',''),json.dumps(d.get('user_ids',[]))));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();c.close();return self.sendj(201,{'group':{'id':gid,'handle':d['handle'],'name':d.get('name',''),'member_user_ids':d.get('user_ids',[])}})
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
  return self.sendj(404,{})
 def archive(self,cid,u,state,status=200):
  ch=self.getch(cid)
  if not ch:return self.sendj(404,{})
  c=db();r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone() if ch['workspace_id'] else None
  if not r or r['role']!='owner':c.close();return self.sendj(403,{})
  c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(state),cid));c.commit();c.close();ch=self.getch(cid)
  emit(cid,'channel.archived' if state else 'channel.unarchived',{'channel':C(ch)})
  return self.sendj(status,{'message':None,'channel':C(ch)} if status==201 else {'channel':C(ch)})
 def can_edit(self,ch,u):
  c=db();r=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone();own=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=? AND role="owner"',(ch['workspace_id'],u['id'])).fetchone();c.close();return bool(r or own)
 def sett_topic(self,ch,topic):
  c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));c.commit();c.close();emit(ch['id'],'channel.updated',{'channel':C(self.getch(ch['id']))})
 def react(self,mid,u,emoji,add):
  c=db();r=c.execute('SELECT m.*,c.* FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=? AND m.deleted=0',(mid,)).fetchone()
  if not r:c.close();return self.sendj(404,{})
  ch=self.getch(r['channel_id'])
  if not self.allowed(ch,u):c.close();return self.sendj(403,{})
  if not emoji:return c.close() or self.sendj(400,{})
  before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
  changed=False
  if add and not before:c.execute('INSERT INTO reactions VALUES(?,?,?,?)',(mid,u['id'],emoji,now()));changed=True
  if not add and before:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));changed=True
  c.commit();c.close();rx=reactions(mid)
  if changed:emit(ch['id'],'reaction.added' if add else 'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':rx})
  return self.sendj(200,{'reactions':rx})
 def do_PATCH(self):
  path=urlparse(self.path).path;u=self.auth()
  if not u:return
  try:d=json.loads(self.body())
  except:return self.sendj(400,{})
  if path=='/api/users/me':
   fields={}
   for k in ('display_name','timezone','avatar_url','status_text','status_emoji'):
    if k in d:
     v=d[k]
     if not isinstance(v,str) or (k=='display_name' and not 1<=len(v)<=80) or (k=='timezone' and (len(v)>100 or not self.validtz(v))) or (k=='avatar_url' and len(v)>2048) or (k=='status_text' and len(v)>100) or (k=='status_emoji' and len(v)>64):return self.sendj(400,{})
     fields[k]=v
   c=db()
   if fields:c.execute('UPDATE users SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',tuple(fields.values())+(u['id'],));c.commit()
   r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.close();self.broadcast({'type':'user.updated','user':U(r)});return self.sendj(200,{'user':U(r)})
  m=re.match(r'/api/channels/(\d+)$',path)
  if m:
   ch=self.getch(int(m.group(1)))
   if not ch:return self.sendj(404,{})
   if 'topic' not in d or not isinstance(d['topic'],str) or len(d['topic'])>250:return self.sendj(400,{})
   if not self.can_edit(ch,u):return self.sendj(403,{})
   self.sett_topic(ch,d['topic']);return self.sendj(200,{'channel':self.getch(ch['id']) and C(self.getch(ch['id']))})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groupsx WHERE workspace_id=? AND handle=?',(w['id'] if w else -1,m.group(2))).fetchone()
   if not g:c.close();return self.sendj(404,{})
   own=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=? AND role="owner"',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and not own:c.close();return self.sendj(403,{})
   vals={'name':d.get('name',g['name']),'user_ids':json.dumps(d.get('user_ids',json.loads(g['user_ids'])))}
   c.execute('UPDATE groupsx SET name=?,user_ids=? WHERE id=?',(vals['name'],vals['user_ids'],g['id']));c.commit();c.close();return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':vals['name'],'member_user_ids':json.loads(vals['user_ids'])}})
  return self.sendj(404,{})
 def validtz(self,x):
  try:ZoneInfo(x);return True
  except:return False
 def broadcast(self,obj):
  c=db(); c.execute('INSERT INTO global_events(payload) VALUES(?)',(json.dumps(obj),)); c.commit(); c.close()
 def do_DELETE(self):
  u=self.auth()
  if not u:return
  m=re.match(r'/api/messages/(\d+)/reactions/(.*)$',urlparse(self.path).path)
  if m:return self.react(int(m.group(1)),u,unquote(m.group(2)),False)
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',urlparse(self.path).path)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groupsx WHERE workspace_id=? AND handle=?',(w['id'] if w else -1,m.group(2))).fetchone()
   if not g:c.close();return self.sendj(200,{'deleted':True})
   own=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=? AND role="owner"',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and not own:c.close();return self.sendj(403,{})
   c.execute('DELETE FROM groupsx WHERE id=?',(g['id'],));c.commit();c.close();return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def do_OPTIONS(self): self.sendj(204,{})
init()
if __name__=='__main__':
 node=int(os.environ.get('NODE_ID','0'))
 threading.Thread(target=poller,daemon=True).start()
 ThreadingHTTPServer(('127.0.0.1',8000+node),H).serve_forever()
