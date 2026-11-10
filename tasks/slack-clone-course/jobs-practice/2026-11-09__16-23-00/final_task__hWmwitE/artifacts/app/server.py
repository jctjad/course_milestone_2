import sys, os, json, sqlite3, hashlib, secrets, re, time, threading, socket, struct, base64, urllib.parse, mimetypes
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

IRC_MODE=len(sys.argv)>1 and sys.argv[1]=='irc'
PORT=int(sys.argv[1]) if len(sys.argv)>1 and not IRC_MODE else 8000
NODE=PORT-8000
DB='/var/lib/huddle/huddle.db'
os.makedirs('/var/lib/huddle',exist_ok=True)
WS=set(); WS_USERS={}; WSL=threading.Lock(); EVENT_CURSOR=0
try:
 EVENT_CURSOR=q('SELECT COALESCE(MAX(id),0) n FROM events',())[0]['n'] if False else 0
except Exception: pass
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=30,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    return c
def init():
 c=db()
 c.executescript('''
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE); CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY, payload TEXT);
 '''); c.commit(); c.close()
init()
try:
 c0=sqlite3.connect(DB); EVENT_CURSOR=c0.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]; c0.close()
except Exception: EVENT_CURSOR=0
def q(sql,args=(),one=False):
 c=db(); r=c.execute(sql,args); out=r.fetchone() if one else r.fetchall(); c.close(); return out
def ex(sql,args=()):
 c=db()
 try:
  r=c.execute(sql,args); c.commit(); return r.lastrowid
 except Exception:
  c.rollback(); raise
 finally:
  c.close()
def userobj(r):
 if not r: return None
 d=dict(r)
 d.pop('password',None)
 return d
def auth(h):
 a=h.get('Authorization','')
 if not a.startswith('Bearer '): return None
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(a[7:],),True)
 return r
def wsobj(r): return dict(r)
def chanobj(r): 
 d=dict(r); d['is_private']=bool(d['is_private']); d['is_dm']=bool(d['is_dm']); d['is_archived']=bool(d['is_archived']); return d
def member(uid,wid):
 r=q('SELECT role FROM members WHERE user_id=? AND workspace_id=?',(uid,wid),True); return r['role'] if r else None
def accessible(uid,cid):
 r=q('SELECT c.workspace_id,c.is_private FROM channels c WHERE c.id=?',(cid,),True)
 return bool(r and q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid),True))
def msgobj(r):
 d=dict(r); d['parent_id']=d['parent_id']; d['edited_at']=d['edited_at']
 u=q('SELECT * FROM users WHERE id=?',(d['author_id'],),True); d['author']=dict(u) if u else None
 rr=q('SELECT emoji,COUNT(*) count,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(d['id'],))
 d['reactions']=[{'emoji':x['emoji'],'count':x['count'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rr]
 d['reply_count']=q('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(d['id'],),True)['n']; fs=q('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(d['id'],)); d['files']=[{k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')} for x in fs]; d['mentions']=[]
 return d
def notify_irc_local(cid,kind,payload):
 if kind!='message.created' or 'IRC_CLIENTS' not in globals(): return
 try:
  m=payload.get('message',{}); c=q('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,),True)
  if not c:return
  nick=str(m.get('author',{}).get('username','user')); line=':'+nick+'!'+nick+'@host PRIVMSG #'+c['slug']+'/'+c['name']+' :'+str(m.get('body',''))
  with IRC_LOCK: clients=list(IRC_CLIENTS.get(cid,set()))
  for sock,cn,uid in clients:
   if uid!=m.get('author_id'): irc_send(sock,line)
 except Exception: pass
def emit(cid,kind,payload):
 for attempt in range(12):
  c=None
  try:
   c=db()
   c.execute('BEGIN IMMEDIATE')
   n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
   c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload,separators=(',',':'))))
   c.commit(); c.close()
   notify_nodes(cid)
   notify_irc_local(cid,kind,payload)
   return n
  except sqlite3.IntegrityError:
   if c:
    try:c.rollback();c.close()
    except Exception:pass
   time.sleep(.005*(attempt+1))
  except sqlite3.OperationalError:
   if c:
    try:c.rollback();c.close()
    except Exception:pass
   time.sleep(.01*(attempt+1))
  except Exception:
   if c:
    try:c.rollback();c.close()
    except Exception:pass
   time.sleep(.01)
 raise RuntimeError('event commit failed')

def broadcast(cid,obj):
 raw=json.dumps(obj).encode()
 with WSL:
  targets=list(WS)
 for s,ch in targets:
  if ch==cid:
   try: sendws(s,raw)
   except: pass
def notify_nodes(channel_id):
 try:
  x=socket.create_connection(('127.0.0.1',6379),timeout=.15)
  msg=f'*3\r\n$7\r\nPUBLISH\r\n$13\r\nhuddle:events\r\n${len(str(channel_id))}\r\n{channel_id}\r\n'.encode()
  x.sendall(msg); x.close()
 except Exception: pass
def redis_listener():
 while True:
  s=None
  try:
   s=socket.create_connection(('127.0.0.1',6379),timeout=2)
   s.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
   s.settimeout(1)
   while True:
    try:
     if not s.recv(4096): raise OSError()
    except socket.timeout:
     continue
  except Exception:
   time.sleep(.5)
  finally:
   try:
    if s:s.close()
   except Exception: pass
threading.Thread(target=redis_listener,daemon=True).start()
GLOBAL_CURSOR=0
def broadcast_global(frame):
 raw=json.dumps(frame).encode()
 with WSL:
  targets=[x[0] for x in WS]
 for sock in targets:
  try: sendws(sock,raw)
  except Exception: pass
def global_pump():
 global GLOBAL_CURSOR
 try: GLOBAL_CURSOR=q('SELECT COALESCE(MAX(id),0) n FROM global_events',())[0]['n']
 except Exception: GLOBAL_CURSOR=0
 while True:
  try:
   rows=q('SELECT id,payload FROM global_events WHERE id>? ORDER BY id',(GLOBAL_CURSOR,))
   for r in rows:
    GLOBAL_CURSOR=r['id']; broadcast_global(json.loads(r['payload']))
  except Exception: pass
  time.sleep(.05)
threading.Thread(target=global_pump,daemon=True).start()
def event_pump():
 global EVENT_CURSOR
 while True:
  try:
   rows=q('SELECT id,channel_id,event_id,kind,payload FROM events WHERE id>? ORDER BY id',(EVENT_CURSOR,))
   for r in rows:
    EVENT_CURSOR=r['id']; obj={'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id']}; obj.update(json.loads(r['payload'])); broadcast(r['channel_id'],obj)
  except Exception: pass
  time.sleep(.05)
threading.Thread(target=event_pump,daemon=True).start()
def sendws(s,p):
 s.settimeout(0.25)
 b=p; n=len(b); head=bytes([0x81])
 if n<126: head+=bytes([n])
 elif n<65536: head+=bytes([126])+struct.pack('>H',n)
 else: head+=bytes([127])+struct.pack('>Q',n)
 s.sendall(head+b)
 s.setblocking(True)
def file_json(r):
 return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def group_json(r):
 return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'] or '[]')}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x): pass
 def send(self,code,obj=None,typ='application/json'):
  b=(json.dumps(obj).encode() if obj is not None else b'')
  self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def require(self):
  u=auth(self.headers)
  if not u:self.send(401,{'error':'unauthorized'}); return None
  return u
 def route(self):
  p=urllib.parse.urlparse(self.path); path=p.path; qs=urllib.parse.parse_qs(p.query); method=self.command
  if path=='/api/health': return self.send(200,{'status':'ok','node_id':NODE})
  if path=='/': return self.html()
  if path=='/api/auth/register' and method=='POST':
   b=self.body(); un=b.get('username',''); pw=b.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.send(400,{'error':'invalid'})
   try: uid=ex('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un))
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   t=secrets.token_urlsafe(24); ex('INSERT INTO tokens VALUES(?,?)',(t,uid)); return self.send(201,{'user':userobj(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':t})
  if path=='/api/auth/login' and method=='POST':
   b=self.body(); r=q('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest()),True)
   if not r:return self.send(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24); ex('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); return self.send(200,{'user':userobj(r),'token':t})
  if path.startswith('/api/ws'):
   return self.websocket()
  u=self.require()
  if not u:return
  uid=u['id']
  gm=re.fullmatch(r'/api/workspaces/([^/]+)/groups(?:/([^/]+))?',path)
  if gm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),),True)
   if not w:return self.send(404,{'error':'workspace'})
   role=member(uid,w['id'])
   if not role:return self.send(403,{'error':'member'})
   handle=gm.group(2)
   if method=='GET' and not handle:return self.send(200,{'groups':[group_json(x) for x in q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]})
   if method=='POST' and not handle:
    b=self.body(); h=b.get('handle','')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.send(400,{'error':'handle'})
    try: gid=ex('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,b.get('name',''),uid,json.dumps(b.get('user_ids',[]))))
    except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
    return self.send(201,{'group':group_json(q('SELECT * FROM groups_ WHERE id=?',(gid,),True))})
   g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
   if method=='DELETE':
    if not g:return self.send(200,{'deleted':True})
    if g['creator_id']!=uid and role!='owner':return self.send(403,{'error':'forbidden'})
    ex('DELETE FROM groups_ WHERE id=?',(g['id'],)); return self.send(200,{'deleted':True})
   if method=='PATCH':
    if not g:return self.send(404,{'error':'group'})
    if g['creator_id']!=uid and role!='owner':return self.send(403,{'error':'forbidden'})
    b=self.body(); vals=[]; args=[]
    if 'name' in b: vals.append('name=?');args.append(b['name'])
    if 'user_ids' in b: vals.append('user_ids=?');args.append(json.dumps(b['user_ids']))
    if vals: ex('UPDATE groups_ SET '+','.join(vals)+' WHERE id=?',tuple(args+[g['id']]))
    return self.send(200,{'group':group_json(q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True))})
  if path=='/api/files' and method=='POST':
   raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); ct=self.headers.get('Content-Type','')
   if len(raw)>10485760+100000:return self.send(413,{'error':'too large'})
   mm=re.search(r'boundary=([^;]+)',ct)
   if not mm or b'name="file"' not in raw:return self.send(400,{'error':'file'})
   boundary=('--'+mm.group(1).strip('"')).encode(); part=next((x for x in raw.split(boundary) if b'name="file"' in x),b'')
   hm=re.search(br'filename="([^"]*)"',part); cm=re.search(br'Content-Type:\s*([^\r\n]+)',part)
   if not hm:return self.send(400,{'error':'file'})
   data=part.split(b'\r\n\r\n',1)[1].rsplit(b'\r\n',1)[0]; fn=hm.group(1).decode(errors='replace'); typ=cm.group(1).decode().strip() if cm else 'application/octet-stream'
   if len(data)>10485760:return self.send(413,{'error':'too large'})
   fid=ex('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(uid,fn,typ,len(data),now(),data))
   return self.send(201,{'file':file_json(q('SELECT * FROM files WHERE id=?',(fid,),True))})
  fm=re.fullmatch(r'/api/files/(\d+)(/download)?',path)
  if fm:
   f=q('SELECT * FROM files WHERE id=?',(fm.group(1),),True)
   if not f:return self.send(404,{'error':'file'})
   if fm.group(2):
    b=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
   return self.send(200,{'file':file_json(f)})
  if path=='/api/auth/me': return self.send(200,{'user':userobj(u)})
  if path=='/api/users/me' and method=='PATCH':
   b=self.body(); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; vals={k:b[k] for k in b if k in allowed}
   limits={'display_name':(1,80),'timezone':(1,255),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
   for k,v in vals.items():
    if not isinstance(v,str) or not (limits[k][0]<=len(v)<=limits[k][1]): return self.send(400,{'error':k})
    if k=='timezone':
     try: ZoneInfo(v)
     except Exception:return self.send(400,{'error':'timezone'})
   if vals: ex('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(uid,))
   updated=userobj(q('SELECT * FROM users WHERE id=?',(uid,),True))
   gid=ex('INSERT INTO global_events(payload) VALUES(?)',(json.dumps({'type':'user.updated','user':updated},separators=(',',':')),))
   broadcast_global(json.loads(q('SELECT payload FROM global_events WHERE id=?',(gid,),True)['payload']))
   return self.send(200,{'user':updated})
  m=re.fullmatch(r'/api/users/(\d+)',path)
  if m:
   r=q('SELECT * FROM users WHERE id=?',(m.group(1),),True); return self.send(200,{'user':userobj(r)}) if r else self.send(404,{'error':'not found'})
  if path=='/api/workspaces' and method=='GET':
   rows=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(uid,)); return self.send(200,{'workspaces':[wsobj(x) for x in rows]})
  if path=='/api/workspaces' and method=='POST':
   b=self.body()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return self.send(400,{'error':'slug'})
   try: wid=ex('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',b['slug']),uid))
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   ex('INSERT INTO members VALUES(?,?,?)',(wid,uid,'owner')); cid=ex('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); ex('INSERT INTO channel_members VALUES(?,?)',(cid,uid))
   return self.send(201,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(wid,),True)),'general_channel':chanobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  m=re.fullmatch(r'/api/workspaces/([^/]+)',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   role=member(uid,w['id'])
   if method=='PATCH':
    if role not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
    b=self.body()
    if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'): return self.send(400,{'error':'join_mode'})
    vals={k:b[k] for k in ('name','join_mode') if k in b}; 
    if vals: ex('UPDATE workspaces SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(w['id'],))
    return self.send(200,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
   chans=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR is_archived=?) AND (is_private=0 OR EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=channels.id AND cm.user_id=?))',(w['id'],1 if qs.get('include_archived') else 0,uid))
   if not role:return self.send(403,{'error':'forbidden'})
   states=[]
   for ch in chans:
    rr=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(uid,ch['id']),True); last=rr['last_read_event_id'] if rr else 0
    head=q('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch['id'],),True)['n']
    states.append({'channel_id':ch['id'],'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':sum(1 for e in q('SELECT payload FROM events WHERE channel_id=? AND event_id>?',(ch['id'],last)) if ('@'+u['username']) in e['payload'] and u['username'] not in e['payload'].split('author_id')[0])})
   return self.send(200,{'workspace':wsobj(w),'channels':[chanobj(x) for x in chans],'read_state':states})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',path)
  if m and method=='PATCH':
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'workspace'})
   actor=member(uid,w['id']); target=int(m.group(2)); old=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True)
   if not old:return self.send(404,{'error':'user'})
   b=self.body(); role=b.get('role')
   if role not in ('owner','admin','member','guest') or role=='owner':return self.send(400,{'error':'role'})
   if actor not in ('owner','admin') or target==w['owner_id'] or (actor=='admin' and old['role']=='admin'):return self.send(403,{'error':'forbidden'})
   ex('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target))
   return self.send(200,{'member':{'user_id':target,'role':role}})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/members',path)
  if m:
   w=q('SELECT id FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   rows=q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)); return self.send(200,{'members':[dict(x) for x in rows]})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',path)
  if m and method=='POST':
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); role=member(uid,w['id']) if w else None
   if not w:return self.send(404,{'error':'not found'})
   b=self.body(); name=b.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(b.get('topic',''))>250:return self.send(400,{'error':'name'})
   if not role:return self.send(403,{'error':'member'})
   if role=='guest' or (b.get('is_private') and role not in ('owner','admin')):return self.send(403,{'error':'forbidden'})
   try: cid=ex('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')))
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   ex('INSERT INTO channel_members VALUES(?,?)',(cid,uid)); return self.send(201,{'channel':chanobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',path)
  if m and method=='POST':
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'workspace'})
   if uid!=w['owner_id']:return self.send(403,{'error':'forbidden'})
   target=self.body().get('new_owner_id'); tr=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True)
   if not tr or tr['role']!='admin':return self.send(400,{'error':'target'})
   ex('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],uid))
   ex('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target))
   ex('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']))
   return self.send(200,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'workspace'})
   role=member(uid,w['id'])
   if method=='POST':
    if role not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
    b=self.body(); code=secrets.token_urlsafe(12); target=b.get('invited_user_id')
    if b.get('invited_username'):
     z=q('SELECT id FROM users WHERE username=?',(b['invited_username'],),True); target=z['id'] if z else None
    expires=now() if b.get('expires_in_seconds')==0 else (datetime.fromtimestamp(time.time()+int(b['expires_in_seconds']),timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z') if b.get('expires_in_seconds') else None)
    maxuses=1 if target else int(b.get('max_uses',1))
    ex('INSERT INTO invitations(code,workspace_id,invited_user_id,expires_at,max_uses) VALUES(?,?,?,?,?)',(code,w['id'],target,expires,maxuses))
    return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':maxuses,'uses':0}})
   if method=='GET':
    if role not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
    return self.send(200,{'invitations':[dict(x) for x in q("SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)",(w['id'],now()))]})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations/([^/]+)',path)
  if m and method=='DELETE':
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); inv=q('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(m.group(2),w['id'] if w else -1),True)
   if not w:return self.send(404,{'error':'workspace'})
   if member(uid,w['id']) not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   if not inv:return self.send(404,{'error':'invite'})
   ex('DELETE FROM invitations WHERE code=?',(m.group(2),)); return self.send(200,{'deleted':True})
  m=re.fullmatch(r'/api/invitations/([^/]+)/accept',path)
  if m and method=='POST':
   inv=q('SELECT * FROM invitations WHERE code=?',(m.group(1),),True)
   if not inv or inv['uses']>=inv['max_uses'] or (inv['expires_at'] and inv['expires_at']<now()):return self.send(404,{'error':'invite'})
   if inv['invited_user_id'] and inv['invited_user_id']!=uid:return self.send(403,{'error':'invite'})
   ex('UPDATE invitations SET uses=uses+1 WHERE code=?',(m.group(1),))
   ex('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],uid,'member'))
   return self.send(200,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],),True))})
  m=re.fullmatch(r'/api/channels/(\d+)/(archive|unarchive)',path)
  if m and method=='POST':
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True); w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True) if c else None
   if not c:return self.send(404,{'error':'channel'})
   if uid!=w['owner_id']:return self.send(403,{'error':'forbidden'})
   val=1 if m.group(2)=='archive' else 0; ex('UPDATE channels SET is_archived=? WHERE id=?',(val,c['id']))
   emit(c['id'],'channel.archived' if val else 'channel.unarchived',{'channel':chanobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
   return self.send(200,{'channel':chanobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
  m=re.fullmatch(r'/api/channels/(\d+)/read',path)
  if m:
   cid=int(m.group(1)); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{'error':'channel'})
   if not accessible(uid,cid):return self.send(403,{'error':'forbidden'})
   if method=='POST':
    val=self.body().get('last_read_event_id',0); old=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(uid,cid),True)
    val=max(int(val),old['last_read_event_id'] if old else 0); ex('INSERT INTO reads(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(uid,cid,val))
   r=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(uid,cid),True); last=r['last_read_event_id'] if r else 0
   head=q('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,),True)['n']
   unread=q('SELECT COUNT(*) n FROM events e JOIN messages m ON m.id=json_extract(e.payload,\'$.message.id\') WHERE e.channel_id=? AND e.event_id>? AND m.author_id<>?',(cid,last,uid),True)['n']
   state={'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':sum(1 for e in q('SELECT payload FROM events WHERE channel_id=? AND event_id>?',(cid,last)) if ('@'+u['username']) in e['payload'] and u['username'] not in e['payload'].split('author_id')[0])}
   return self.send(200,{'read_state':state})
  m=re.fullmatch(r'/api/channels/(\d+)(?:/(.*))?',path)
  if m:
   cid=int(m.group(1)); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{'error':'channel'})
   if method=='POST' and m.group(2)=='join':
    w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True); role=member(uid,w['id'])
    if c['is_private']:
     if q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid),True): return self.send(200,{'channel':chanobj(c)})
     return self.send(403,{'error':'private'})
    if role=='guest':return self.send(403,{'error':'guest'})
    if not role:
     if w['join_mode']!='open':return self.send(403,{'error':'invite'})
     ex('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],uid,'member'))
    before=q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid),True)
    ex('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,uid))
    if not before: emit(cid,'member.joined',{'user_id':uid})
    return self.send(200,{'channel':chanobj(c)})
   wrole=member(uid,c['workspace_id'])
   if not accessible(uid,cid) and not (wrole in ('owner','admin','member','guest') and m.group(2) in ('leave',) or wrole=='owner' and m.group(2) in (None,'archive','unarchive')):
    return self.send(403,{'error':'forbidden'})
   sub=m.group(2)
   if method=='GET' and (not sub or sub=='messages'):
    try: lim=int(qs.get('limit',['50'])[0])
    except: return self.send(400,{'error':'pagination'})
    cur=qs.get('cursor',[None])[0]
    if lim<1 or lim>200 or (cur is not None and not cur.isdigit()):return self.send(400,{'error':'pagination'})
    sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL'
    args=[cid]
    if cur: sql+=' AND id<?'; args.append(int(cur))
    sql+=' ORDER BY id DESC LIMIT ?'; args.append(lim+1)
    rows=q(sql,tuple(args)); more=len(rows)>lim; rows=rows[:lim]
    return self.send(200,{'messages':[msgobj(x) for x in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
   if method=='POST' and sub=='messages':
    b=self.body(); body=b.get('body','')
    if not body:return self.send(400,{'error':'empty'})
    if body.startswith('/') and not body.startswith('//'):
     parts=body.split(None,1); cmd=parts[0][1:]; arg=parts[1] if len(parts)>1 else ''
     if cmd=='me':
      if not arg.strip():return self.send(400,{'error':'command'})
     elif cmd=='shrug':
      body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
     elif cmd=='topic':
      if not arg or len(arg)>250:return self.send(400,{'error':'topic'})
      if not q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid),True):return self.send(403,{'error':'forbidden'})
      ex('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); nc=chanobj(q('SELECT * FROM channels WHERE id=?',(cid,),True)); emit(cid,'channel.updated',{'channel':nc}); return self.send(201,{'message':None,'channel':nc})
     elif cmd in ('archive','unarchive'):
      w=q('SELECT owner_id FROM workspaces WHERE id=?',(c['workspace_id'],),True)
      if not w or uid!=w['owner_id']:return self.send(403,{'error':'forbidden'})
      val=1 if cmd=='archive' else 0; ex('UPDATE channels SET is_archived=? WHERE id=?',(val,cid)); nc=chanobj(q('SELECT * FROM channels WHERE id=?',(cid,),True)); emit(cid,'channel.archived' if val else 'channel.unarchived',{'channel':nc}); return self.send(201,{'message':None,'channel':nc})
     elif cmd=='invite':
      if not arg.startswith('@') or not arg[1:]:return self.send(400,{'error':'username'})
      w=q('SELECT workspace_id FROM channels WHERE id=?',(cid,),True); role=member(uid,w['workspace_id']) if w else None
      if role not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
      target=q('SELECT * FROM users WHERE username=?',(arg[1:].strip(),),True)
      if not target:return self.send(404,{'error':'user'})
      if not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['workspace_id'],target['id']),True):return self.send(403,{'error':'workspace member'})
      ex('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,target['id']))
      return self.send(201,{'message':None,'invited_user':userobj(target)})
     else:return self.send(400,{'error':'command'})
    if c['is_archived']:return self.send(423,{'error':'archived'})
    parent=b.get('parent_id'); file_ids=b.get('file_ids') or []
    for fid in file_ids:
     f=q('SELECT * FROM files WHERE id=?',(fid,),True)
     if not f or f['uploader_id']!=uid or q('SELECT 1 FROM message_files WHERE file_id=?',(fid,),True): return self.send(400,{'error':'file'})
    mid=ex('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,uid,body,parent,now()))
    for fid in file_ids: ex('INSERT INTO message_files VALUES(?,?)',(mid,fid))
    mo=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))
    names=re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body); mids=[]
    members=q('SELECT u.id,u.username FROM users u JOIN members wm ON wm.user_id=u.id WHERE wm.workspace_id=(SELECT workspace_id FROM channels WHERE id=?)',(cid,))
    by={x['username'].lower():x['id'] for x in members}
    for n in names:
     if n.lower()=='channel':
      cmembers=q('SELECT u.id FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,))
      mids += [x['id'] for x in cmembers if x['id']!=uid]
     elif n.lower()=='here':
      online={x['user_id'] for x in q('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,))}
      mids += [x['id'] for x in q('SELECT u.id FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)) if x['id']!=uid and x['id'] in online]
     else:
      gr=q('SELECT g.user_ids FROM groups_ g JOIN channels cc ON cc.workspace_id=g.workspace_id WHERE cc.id=? AND g.handle=?',(cid,n.lower()),True)
      if gr: mids += [int(x) for x in json.loads(gr['user_ids'] or '[]') if int(x)!=uid and q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,int(x)),True)]
      elif n.lower() in by and by[n.lower()]!=uid: mids.append(by[n.lower()])
    mo['mentions']=list(dict.fromkeys(mids))
    emit(cid,'message.reply' if parent else 'message.created',{'message':mo}); return self.send(201,{'message':mo})
   if method=='GET' and sub=='members':
    rows=q('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)); return self.send(200,{'members':[dict(x) for x in rows]})
   if method=='POST' and sub=='leave':
    before=q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid),True)
    ex('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid))
    if before: emit(cid,'member.left',{'user_id':uid})
    return self.send(200,{'channel':chanobj(c)})
   if method=='PATCH' and sub is None:
    b=self.body()
    if c['is_archived']:return self.send(423,{'error':'archived'})
    if len(b.get('topic',''))>250:return self.send(400,{'error':'topic'})
    ex('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),cid)); nc=chanobj(q('SELECT * FROM channels WHERE id=?',(cid,),True)); emit(cid,'channel.updated',{'channel':nc}); return self.send(200,{'channel':nc})
   if method=='GET' and sub=='pins':
    try: lim=int(qs.get('limit',['50'])[0])
    except: return self.send(400,{'error':'pagination'})
    cur=qs.get('cursor',[None])[0]
    if lim<1 or lim>200 or (cur is not None and not cur.isdigit()): return self.send(400,{'error':'pagination'})
    sql='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?'; args=[cid]
    if cur: sql+=' AND p.message_id<?'; args.append(int(cur))
    sql+=' ORDER BY p.message_id DESC LIMIT ?'; args.append(lim+1)
    rows=q(sql,tuple(args)); more=len(rows)>lim; rows=rows[:lim]
    return self.send(200,{'pins':[{'message':msgobj(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':str(rows[-1]['message_id']) if more and rows else None})
  m=re.fullmatch(r'/api/messages/(\d+)(?:/(.*))?',path)
  if m:
   mid=int(m.group(1)); msg=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not msg:return self.send(404,{'error':'message'})
   if method=='DELETE' and m.group(2)=='pin':
    ex('DELETE FROM pins WHERE message_id=?',(mid,)); return self.send(200,{'unpinned':True})
   if method=='DELETE' and m.group(2) and m.group(2).startswith('reactions/'):
    e=urllib.parse.unquote(m.group(2).split('/',1)[1])
    chx=q('SELECT is_private,workspace_id FROM channels WHERE id=?',(msg['channel_id'],),True)
    cm=bool(q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(msg['channel_id'],uid),True))
    own=bool(q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=?',(mid,uid),True))
    if chx['is_private'] and not cm and not own and not q('SELECT 1 FROM reactions WHERE message_id=?',(mid,),True):return self.send(403,{'error':'forbidden'})
    if q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,e),True):
     ex('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,e))
     mo=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))
     emit(msg['channel_id'],'reaction.removed',{'message_id':mid,'emoji':e,'user_id':uid,'reactions':mo['reactions']})
    return self.send(200,{'reactions':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))['reactions']})
   if not (m.group(2) and (m.group(2)=='pin' or m.group(2).startswith('reactions/'))) and not accessible(uid,msg['channel_id']) and member(uid,q('SELECT workspace_id FROM channels WHERE id=?',(msg['channel_id'],),True)['workspace_id']) not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   sub=m.group(2)
   if method=='PATCH':
    if msg['deleted']:return self.send(409,{'error':'deleted'})
    if msg['author_id']!=uid:return self.send(403,{'error':'author'})
    b=self.body()
    if not b.get('body'):return self.send(400,{'error':'empty'})
    ex('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),mid)); mo=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True)); emit(msg['channel_id'],'message.updated',{'message':mo}); return self.send(200,{'message':mo})
   if method=='DELETE':
    if msg['author_id']!=uid and member(uid,q('SELECT workspace_id FROM channels WHERE id=?',(msg['channel_id'],),True)['workspace_id'])!='owner':return self.send(403,{'error':'forbidden'})
    if not msg['deleted']:ex('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); emit(msg['channel_id'],'message.deleted',{'message_id':mid})
    return self.send(200,{'deleted':True})
   if method=='GET' and sub=='replies':
    try: lim=int(qs.get('limit',['50'])[0])
    except: return self.send(400,{'error':'pagination'})
    cur=qs.get('cursor',[None])[0]
    if lim<1 or lim>200 or (cur is not None and not cur.isdigit()): return self.send(400,{'error':'pagination'})
    sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0'; args=[mid]
    if cur: sql+=' AND id>?'; args.append(int(cur))
    sql+=' ORDER BY id LIMIT ?'; args.append(lim+1)
    rows=q(sql,tuple(args)); more=len(rows)>lim; rows=rows[:lim]
    return self.send(200,{'replies':[msgobj(x) for x in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
   if method=='POST' and sub=='pin':
    if not q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(msg['channel_id'],uid),True): return self.send(403,{'error':'forbidden'})
    pt=now(); ex('INSERT OR REPLACE INTO pins VALUES(?,?,?)',(mid,uid,pt)); return self.send(200,{'pin':{'message_id':mid,'pinned_by':uid,'pinned_at':pt}})
   if method=='DELETE' and sub=='pin':ex('DELETE FROM pins WHERE message_id=?',(mid,));return self.send(200,{'unpinned':True})
   if sub=='reactions' or (sub and sub.startswith('reactions/')):
    wrow=q('SELECT workspace_id,is_private FROM channels WHERE id=?',(msg['channel_id'],),True)
    ismember=bool(q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(msg['channel_id'],uid),True))
    if method=='POST' and not ismember:return self.send(403,{'error':'forbidden'})
    if method=='DELETE' and sub.startswith('reactions/'):
     e=urllib.parse.unquote(sub.split('/',1)[1])
     own=bool(q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=?',(mid,uid),True))
     if bool(wrow['is_private']) and not ismember and not own:return self.send(403,{'error':'forbidden'})
     if own:
      ex('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,e))
      mo=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))
      emit(msg['channel_id'],'reaction.removed',{'message_id':mid,'emoji':e,'user_id':uid,'reactions':mo['reactions']})
     return self.send(200,{'reactions':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))['reactions']})
    if method=='POST' and sub=='reactions':
     e=self.body().get('emoji')
     if not isinstance(e,str) or not e.strip():return self.send(400,{'error':'emoji'})
     before=q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,e),True)
     if not before:
      ex('INSERT INTO reactions VALUES(?,?,?)',(mid,uid,e)); mo=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))
      emit(msg['channel_id'],'reaction.added',{'message_id':mid,'emoji':e,'user_id':uid,'reactions':mo['reactions']})
     return self.send(200,{'reactions':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))['reactions']})

  if path=='/api/search':
   text=qs.get('q',[''])[0]
   try: lim=int(qs.get('limit',['50'])[0])
   except: return self.send(400,{'error':'pagination'})
   cur=qs.get('cursor',[None])[0]
   if lim<1 or lim>200 or (cur is not None and not cur.isdigit()): return self.send(400,{'error':'pagination'})
   sql='SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? AND ((c.is_private=0 AND EXISTS (SELECT 1 FROM members wm WHERE wm.workspace_id=c.workspace_id AND wm.user_id=?)) OR EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=m.channel_id AND cm.user_id=?))'; args=['%'+text+'%',uid,uid]
   if qs.get('workspace_id'):
    sql+=' AND c.workspace_id=?'; args.append(qs['workspace_id'][0])
   if cur: sql+=' AND m.id<?'; args.append(int(cur))
   sql+=' ORDER BY m.id DESC LIMIT ?'; args.append(lim+1)
   rows=q(sql,tuple(args)); more=len(rows)>lim; rows=rows[:lim]
   return self.send(200,{'results':[msgobj(x) for x in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
  if path=='/api/dms' and method=='POST':
   other=self.body().get('user_id')
   if not other or int(other)==uid:return self.send(400,{'error':'user'})
   if not q('SELECT id FROM users WHERE id=?',(int(other),),True):return self.send(404,{'error':'user'})
   lo,hi=sorted((uid,int(other))); name=f'dm-{lo}-{hi}'
   c=q('SELECT * FROM channels WHERE name=? AND is_dm=1',(name,),True)
   if not c:
    cid=ex('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,1,1)',(name,))
    ex('INSERT INTO channel_members VALUES(?,?)',(cid,uid)); ex('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,int(other)))
   else: cid=c['id']
   return self.send(200,{'channel_id':cid})

  if path=='/api/read': return self.send(404,{'error':'not found'})
  return self.send(404,{'error':'not found'})
 def do_GET(self): 
  try:self.route()
  except Exception as e:self.send(500,{'error':str(e)})
 def do_POST(self):
  try:self.route()
  except Exception as e:self.send(500,{'error':str(e)})
 def do_PATCH(self):
  try:self.route()
  except Exception as e:self.send(500,{'error':str(e)})
 def do_DELETE(self):
  try:self.route()
  except Exception as e:self.send(500,{'error':str(e)})
 def html(self):
  b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def websocket(self):
  if not auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]}): self.send(401,{}); return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send(400,{})
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  s=self.connection; cid=None
  wu=auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]})
  with WSL: WS.add((s,0)); WS_USERS[s]=wu['id'] if wu else 0
  conn_id=secrets.token_urlsafe(16); ex('INSERT OR REPLACE INTO presence(conn_id,user_id,last_seen) VALUES(?,?,?)',(conn_id,wu['id'] if wu else 0,time.time()))
  try:
   while True:
    h=s.recv(2)
    if not h:break
    ln=h[1]&127
    if ln==126:ln=struct.unpack('>H',s.recv(2))[0]
    elif ln==127:ln=struct.unpack('>Q',s.recv(8))[0]
    mask=s.recv(4); data=s.recv(ln); data=bytes(data[i]^mask[i%4] for i in range(ln)); x=json.loads(data)
    if x.get('type') in ('subscribe','resume'):
     cid=int(x['channel_id'])
     with WSL:
      WS.discard((s,0));WS.add((s,cid)); WS_USERS[s]=wu['id'] if wu else 0
     ev=q('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,),True)['n']
     if x.get('type')=='resume':
      since=int(x.get('since_event_id',0))
      for er in q('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since,)):
       eo={'type':er['kind'],'event_id':er['event_id'],'channel_id':cid}; eo.update(json.loads(er['payload'])); sendws(s,json.dumps(eo).encode())
     sendws(s,json.dumps({'type':'subscribed' if x['type']=='subscribe' else 'resumed','channel_id':cid,'head_event_id':ev}).encode())
  except:pass
  finally:
   ex('DELETE FROM presence WHERE conn_id=?',(conn_id,))
   with WSL:WS.discard((s,cid));WS.discard((s,0));WS_USERS.pop(s,None)
HTML='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
body{margin:0;font:14px Arial;color:#222}.app{display:flex;height:100vh}.side{width:250px;background:#3f0f40;color:#fff;padding:16px}.main{flex:1;padding:20px;position:relative;min-width:0}.main [data-testid=message-list]{width:100%}.main [data-testid=thread-panel]{position:absolute;right:0;top:0;width:320px;height:100%;background:#fff;border-left:1px solid #ddd;padding:16px;box-sizing:border-box;z-index:2}button{border:0;padding:8px 12px;border-radius:4px;color:#fff;background:#444}[data-button-role=primary]{background:#007a5a;color:#fff}[data-button-role=danger]{background:#e01e5a;color:#fff}[data-button-role=secondary]{background:#1264a3;color:#fff}
</style></head><body>
<div id="auth-modal"><form data-testid="auth-form" data-mode="signup"><label>Username <input name="username"></label><label>Password <input name="password" type="password"></label><label class="signup-only">Display name <input name="display_name"></label><button type="button" data-testid="auth-submit" data-button-role="primary">Sign up</button><button type="button" data-testid="auth-toggle">Have an account? Log in</button><div data-testid="auth-error" id="auth-error"></div></form></div>
<div id="workspace-settings-modal"><input placeholder="Workspace name" data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary">Save</button><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button></div>
<div id="create-channel-modal"><form data-testid="create-channel-form" style="display:none"><input name="name" placeholder="Channel name"><input data-testid="create-channel-private" type="checkbox"><button type="button" data-testid="create-channel-submit" data-button-role="primary">Create</button><button type="button" data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button><div data-testid="create-channel-error"></div></form></div>
<div id="channel-settings-modal"><h3 data-testid="channel-settings-title"></h3><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input placeholder="Member username" data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add</button></form><input placeholder="Channel topic" data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Topic</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button><button data-testid="channel-settings-close" data-button-role="secondary">Close</button></div>
<div class="app" style="display:none"><aside class="side" data-testid="sidebar"><h2 data-testid="workspace-header">Huddle</h2><span data-testid="current-user"></span><button data-testid="logout-btn" data-button-role="danger">Logout</button><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><form data-testid="workspace-create-form" style="display:none"><input name="slug" placeholder="Workspace slug"><input name="name" placeholder="Workspace name"><button type="button" data-testid="workspace-create-submit" data-button-role="primary">Create workspace</button></form><button data-testid="empty-state-create-workspace" data-button-role="primary">Create workspace</button><form data-testid="join-workspace-form"><button data-testid="join-workspace-submit" data-button-role="primary">Join</button></form><div data-testid="join-workspace-error"></div></aside>
<main class="main"><h2 data-testid="channel-title">Welcome</h2><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div data-testid="message-list"></div><label>Message <input placeholder="Message #general" data-testid="message-input"></label><button data-testid="send-btn" data-button-role="primary">Send</button><section data-testid="thread-panel" style="display:none"><div data-testid="thread-messages"></div><input placeholder="Thread reply" data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></section></main></div>
<div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create invite</button><div data-testid="invitations-list"></div></form>
<script>
const A=(p,o={})=>fetch('/api'+p,{headers:{'Content-Type':'application/json',Authorization:'Bearer '+(localStorage.getItem('huddle.token')||'')},...o}).then(r=>r.json());
let current=null;let workspaceSlug=null;
function login(){
 const f=document.querySelector('[data-testid=auth-form]'), sub=document.querySelector('[data-testid=auth-submit]'), tog=document.querySelector('[data-testid=auth-toggle]');
 tog.onclick=()=>{let up=f.dataset.mode!=='signup';f.dataset.mode=up?'signup':'login';sub.textContent=up?'Sign up':'Log in';tog.textContent=up?'Have an account? Log in':'Need an account? Sign up';document.querySelector('.signup-only').style.display=up?'block':'none'};
 sub.onclick=()=>{let b={username:f.querySelector('[name=username]').value,password:f.querySelector('[name=password]').value};if(f.dataset.mode==='signup'){b.display_name=f.querySelector('[name=display_name]').value;A('/auth/register',{method:'POST',body:JSON.stringify(b)}).then(done)}else A('/auth/login',{method:'POST',body:JSON.stringify(b)}).then(done)};
 function done(x){if(x.token){localStorage.setItem('huddle.token',x.token);app()}else document.getElementById('auth-error').textContent=x.error?'Password must be at least 8 characters or credentials are invalid':'Unable to sign in'}
}
function renderMessages(rows){let list=document.querySelector('[data-testid=message-list]');if(!(rows||[]).length){list.innerHTML='<div data-testid="channel-empty-state">No messages yet — start the conversation.</div>';return}list.innerHTML=(rows||[]).map(m=>'<div data-testid="message" data-message-id="'+m.id+'"><span data-testid="message-avatar" style="display:inline-flex;width:28px;height:28px;background:#1264a3;color:#fff;border-radius:50%;align-items:center;justify-content:center">'+(m.author.username||'?')[0].toUpperCase()+'</span><b data-testid="message-author">'+(m.author.display_name||m.author.username)+'</b><small data-testid="message-time">'+new Date(m.created_at).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})+'</small><div data-testid="message-body">'+m.body+'</div>'+(m.edited_at?'<span data-testid="message-edited">edited</span>':'')+(m.reply_count?'<span data-testid="thread-reply-count">'+m.reply_count+' '+(m.reply_count==1?'reply':'replies')+'</span>':'')+'<div class="toolbar"><button data-testid="open-thread-btn" aria-label="Open thread">Thread</button><button data-testid="reaction-button" aria-label="Add reaction">React</button><button data-testid="message-edit-btn">Edit</button><button data-testid="message-delete-btn">Delete</button></div>'+((m.reactions||[]).map(r=>'<span data-testid="reaction-chip">'+r.emoji+' '+r.count+'</span>').join(''))+'</div>').join('');document.querySelectorAll('[data-testid=message-edit-btn]').forEach(b=>b.onclick=()=>{let row=b.closest('[data-testid=message]'),inp=document.createElement('input');inp.dataset.testid='message-edit-input';inp.value=row.querySelector('[data-testid=message-body]').textContent;row.querySelector('[data-testid=message-body]').replaceWith(inp);inp.onkeydown=e=>{if(e.key==='Enter')A('/messages/'+row.dataset.messageId,{method:'PATCH',body:JSON.stringify({body:inp.value})}).then(()=>load(current));if(e.key==='Escape')load(current)}});document.querySelectorAll('[data-testid=message-delete-btn]').forEach(b=>b.onclick=()=>A('/messages/'+b.closest('[data-testid=message]').dataset.messageId,{method:'DELETE'}).then(()=>load(current)));wireExtras()}
function load(c){current=c;document.querySelector('[data-testid=channel-title]').textContent='#'+c.name;document.querySelector('[data-testid=channel-topic]').textContent=c.topic||'';A('/channels/'+c.id+'/messages').then(x=>renderMessages(x.messages))}
function wireExtras(){document.querySelectorAll('[data-testid=open-thread-btn]').forEach(b=>b.onclick=()=>{let row=b.closest('[data-testid=message]'),p=document.querySelector('[data-testid=thread-panel]');p.style.display='block';document.querySelector('[data-testid=message-list]').style.width='calc(100% - 340px)';p.dataset.parent=row.dataset.messageId;
function showReplies(){A('/messages/'+p.dataset.parent+'/replies').then(x=>p.querySelector('[data-testid=thread-messages]').innerHTML=(x.replies||[]).map(r=>'<div data-testid="message" data-message-id="'+r.id+'"><span data-testid="message-body">'+r.body+'</span></div>').join(''))}
showReplies();
p.querySelector('[data-testid=thread-send]').onclick=()=>{let i=p.querySelector('[data-testid=thread-input]');if(i.value.trim())A('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value,parent_id:Number(p.dataset.parent)})}).then(()=>{i.value='';showReplies();load(current)})}});document.querySelectorAll('[data-testid=reaction-button]').forEach(b=>b.onclick=()=>{let row=b.closest('[data-testid=message]'),pick=document.createElement('div');pick.dataset.testid='emoji-picker';pick.style.display='grid';pick.innerHTML=['👍','❤️','😂','🎉','🚀','👀','✅','🔥'].map(e=>'<button data-testid="emoji-option">'+e+'</button>').join('');row.append(pick);pick.querySelectorAll('[data-testid=emoji-option]').forEach(o=>o.onclick=()=>A('/messages/'+row.dataset.messageId+'/reactions',{method:'POST',body:JSON.stringify({emoji:o.textContent})}).then(()=>{pick.remove();load(current)}))});document.querySelector('[data-testid=close-thread]').onclick=()=>{document.querySelector('[data-testid=thread-panel]').style.display='none';document.querySelector('[data-testid=message-list]').style.width='100%'}}
function app(){document.querySelector('.app').style.display='flex';document.getElementById('auth-modal').style.display='none';document.querySelector('[data-testid=workspace-create-form]').style.display='none';A('/auth/me').then(me=>{document.querySelector('[data-testid=current-user]').textContent=me.user.display_name||me.user.username});A('/workspaces').then(x=>{if(!x.workspaces.length){document.querySelector('[data-testid=workspace-create-form]').style.display='block';return}let w=x.workspaces[0];workspaceSlug=w.slug;document.querySelector('[data-testid=workspace-header]').textContent=w.name;A('/workspaces/'+w.slug).then(d=>(d.channels||[]).forEach(c=>{let e=document.createElement('button');e.textContent='#'+c.name;e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.onclick=()=>load(c);document.querySelector('[data-testid=channel-list]').append(e)}))})}
document.querySelector('[data-testid=auth-toggle]').click();document.querySelector('[data-testid=auth-toggle]').click();
document.querySelector('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};
document.querySelector('[data-testid=new-channel-btn]').onclick=()=>document.querySelector('[data-testid=create-channel-form]').style.display='block';
document.querySelector('[data-testid=send-btn]').onclick=()=>{let i=document.querySelector('[data-testid=message-input]');if(i.value.trim()&&current)A('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value})}).then(()=>{i.value='';load(current)})};
document.querySelector('[data-testid=workspace-create-submit]').onclick=()=>{let f=document.querySelector('[data-testid=workspace-create-form');let b={slug:f.querySelector('[name=slug]').value,name:f.querySelector('[name=name]').value};A('/workspaces',{method:'POST',body:JSON.stringify(b)}).then(()=>app())};
document.querySelector('[data-testid=create-channel-submit]').onclick=()=>{let f=document.querySelector('[data-testid=create-channel-form'),n=f.querySelector('[name=name]'),name=n.value.toLowerCase().trim().replace(/\s+/g,'-');A('/workspaces/'+workspaceSlug+'/channels',{method:'POST',body:JSON.stringify({name})}).then(x=>{if(x.channel){document.querySelector('[data-testid=create-channel-form]').style.display='none';let e=document.createElement('button');e.textContent='#'+x.channel.name;e.dataset.testid='channel-entry';e.dataset.channelId=x.channel.id;e.dataset.channelName=x.channel.name;e.onclick=()=>load(x.channel);document.querySelector('[data-testid=channel-list]').append(e);load(x.channel)}else document.querySelector('[data-testid=create-channel-error]').textContent='A channel with that name already exists'})};
login();if(localStorage.getItem('huddle.token'))app();
</script></body></html>'''


IRC_NICKS=set(); IRC_CLIENTS={}; IRC_SEEN={}; IRC_LOCK=threading.Lock(); IRC_EVENT_CURSOR=0
def irc_send(c,line):
 try: c.sendall((line+'\r\n').encode()); return True
 except Exception: return False
def irc_find(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,chname=name[1:].split('/',1)
 w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
 return q('SELECT c.*,w.slug,w.name wname FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,chname),True) if w else None
def irc_channame(c): return '#'+c['slug']+'/'+c['name']
def irc_broadcast_events():
 global IRC_EVENT_CURSOR
 while True:
  try:
   rows=q('SELECT id,channel_id,kind,payload FROM events WHERE id>? ORDER BY id',(IRC_EVENT_CURSOR,))
   for r in rows:
    IRC_EVENT_CURSOR=r['id']
    if r['kind']!='message.created': continue
    x=json.loads(r['payload']); m=x.get('message',{}); c=q('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(r['channel_id'],),True)
    if not c: continue
    line=':'+str(m.get('author',{}).get('username','user'))+'!'+str(m.get('author',{}).get('username','user'))+'@host PRIVMSG '+irc_channame(c)+' :'+str(m.get('body',''))
    with IRC_LOCK:
     clients=list(IRC_CLIENTS.get(r['channel_id'],set()))
    for sock,nick,uid in clients:
     if r['id'] <= IRC_SEEN.get(sock,0): continue
     IRC_SEEN[sock]=r['id']
     if uid!=m.get('author_id'): irc_send(sock,line)
  except Exception: pass
  time.sleep(.03)
def irc_client(c,addr):
 token=None; nick=None; uid=None; registered=False; joined={}; buf=b''
 try:
  c.settimeout(300)
  while True:
   data=c.recv(4096)
   if not data:break
   buf+=data
   while b'\r\n' in buf or b'\n' in buf:
    if b'\r\n' in buf: line,buf=buf.split(b'\r\n',1)
    else: line,buf=buf.split(b'\n',1)
    line=line.decode(errors='replace').strip()
    if not line:continue
    z=line.split(' ',1); cmd=z[0].upper(); arg=z[1] if len(z)>1 else ''
    if cmd=='PASS':
     token=arg.lstrip(':').strip()
     if not q('SELECT 1 FROM tokens WHERE token=?',(token,),True):
      irc_send(c,':huddle 464 * :Password incorrect'); token=None
    elif cmd=='NICK':
     nn=arg.lstrip(':').strip()
     if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',nn): irc_send(c,':huddle 432 * '+nn+' :Erroneous nickname'); continue
     with IRC_LOCK: collision=nn.lower() in {x.lower() for x in IRC_NICKS} and (nick is None or nn.lower()!=nick.lower())
     if collision: irc_send(c,':huddle 433 '+(nick or '*')+' '+nn+' :Nickname is already in use'); continue
     if nick:
      with IRC_LOCK: IRC_NICKS.discard(nick)
     nick=nn
     with IRC_LOCK: IRC_NICKS.add(nick)
    elif cmd=='USER':
     u=q('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token,),True) if token else None
     if u and nick and not registered:
      uid=u['id']; registered=True
      for code,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle 1.0 oiwsz biklmnop'),('005','CHANTYPES=# PREFIX=(ov)@+ :are supported')]:irc_send(c,':huddle '+code+' '+nick+' '+text)
      irc_send(c,':huddle 422 '+nick+' :MOTD File is missing')
    elif cmd=='PING': irc_send(c,':huddle PONG huddle :'+arg.lstrip(':').strip())
    elif cmd=='QUIT':
     try:c.shutdown(socket.SHUT_RDWR)
     except:pass
     return
    elif cmd=='JOIN':
     for name in arg.split(','):
      cc=irc_find(name.strip())
      if not cc or cc['is_private'] or not registered: irc_send(c,':huddle 403 '+(nick or '*')+' '+name.strip()+' :No such channel'); continue
      ex('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cc['id'],uid)); joined[cc['id']]=cc
      head=q('SELECT COALESCE(MAX(id),0) n FROM events',(),True)['n']
      IRC_SEEN[c]=head
      with IRC_LOCK: IRC_CLIENTS.setdefault(cc['id'],set()).add((c,nick,uid))
      irc_send(c,':'+nick+'!'+str(uid)+'@host JOIN '+irc_channame(cc))
      names=q('SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cc['id'],))
      irc_send(c,':huddle 353 '+nick+' = '+irc_channame(cc)+' :'+ ' '.join(x['username'] for x in names))
      irc_send(c,':huddle 366 '+nick+' '+irc_channame(cc)+' :End of NAMES list')
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
     name=arg.split()[0] if arg else (irc_channame(next(iter(joined.values()))) if joined else '')
     cc=irc_find(name) if name else None
     if cmd=='NAMES' and cc: 
      names=q('SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cc['id'],)); irc_send(c,':huddle 353 '+nick+' = '+irc_channame(cc)+' :'+ ' '.join(x['username'] for x in names)); irc_send(c,':huddle 366 '+nick+' '+irc_channame(cc)+' :End of NAMES list')
     elif cmd=='TOPIC' and cc: irc_send(c,':huddle 332 '+nick+' '+irc_channame(cc)+' :'+(cc['topic'] or ''))
     elif cmd=='LIST': irc_send(c,':huddle 322 '+nick+' '+(irc_channame(cc) if cc else '*')+' :channel'); irc_send(c,':huddle 323 '+nick+' :End of LIST')
    elif cmd=='PRIVMSG':
     parts=arg.split(' ',1); target=parts[0] if parts else ''; body=parts[1].lstrip(':') if len(parts)>1 else ''
     cc=irc_find(target)
     if cc and target in [irc_channame(x) for x in joined.values()] and body:
      mid=ex('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(cc['id'],uid,body,now()))
      emit(cc['id'],'message.created',{'message':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))})
    elif cmd not in ('PONG','NOTICE','CAP'): irc_send(c,':huddle 421 '+(nick or '*')+' '+cmd+' :Unknown command')
 finally:
  if nick:
   with IRC_LOCK:
    IRC_NICKS.discard(nick)
    for cid in list(joined):
     IRC_CLIENTS.get(cid,set()).difference_update({x for x in IRC_CLIENTS.get(cid,set()) if x[0] is c})
     IRC_SEEN.pop(c,None)
  try:c.close()
  except:pass
threading.Thread(target=irc_broadcast_events,daemon=True).start()
def irc_server():
 srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen(100)
 while True:
  c,a=srv.accept(); threading.Thread(target=irc_client,args=(c,a),daemon=True).start()

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='irc': irc_server()
 else: ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
