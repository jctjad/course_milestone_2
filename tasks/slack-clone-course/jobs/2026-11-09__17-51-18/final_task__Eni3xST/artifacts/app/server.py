import sqlite3,json,os,sys,time,secrets,re,hashlib,base64,struct,threading,socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
DB='/var/lib/huddle/huddle.db'; os.makedirs('/var/lib/huddle',exist_ok=True)
lock=threading.RLock()
def db():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=2000'); c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db(); c.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,expires REAL);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,users TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);
CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
'''); c.commit(); c.close()

if os.environ.get("NODE_ID","0") != "0": time.sleep(2)
init()
clients={}; clients_lock=threading.Lock()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+f'{int(time.time()*1000)%1000:03d}Z'
def userobj(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def send_events(cid,kind,payload):
 c=db()
 c.execute('BEGIN IMMEDIATE')
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
 c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload)))
 c.commit(); c.close()
 return n
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def reply(self,status,obj=None,raw=None,typ='application/json'):
  if raw is not None: b=raw
  else: b=json.dumps(obj,separators=(',',':')).encode()
  self.send_response(status); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  x=self.headers.get('Authorization',''); t=x[7:] if x.startswith('Bearer ') else ''
  c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
 def path(self): return urlparse(self.path).path
 def json(self,status,x): self.reply(status,x)
 def workspace(self,slug): 
  c=db(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); c.close(); return r
 def channel(self,cid):
  c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return r
 def allowed(self,u,cid):
  c=db(); r=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(); c.close(); return bool(r)
 def chobj(self,r): return {**dict(r),'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'is_archived':bool(r['is_archived'])}
 def fileobj(self,r):
  return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
 def msgobj(self,r):
  c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
  rx=[]
  for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
   rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]})
  replies=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
  fs=[self.fileobj(z) for z in c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()]; c.close(); return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rx,'mentions':[]}
 def do_GET(self):
  p=urlparse(self.path).path; q=parse_qs(urlparse(self.path).query)
  if p=='/api/health': return self.json(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if p=='/': return self.reply(200,raw=HTML.encode(),typ='text/html')
  if p.startswith('/api/ws'): return self.websocket()
  u=self.auth()
  if p=='/api/auth/me':
   return self.json(200,{'user':userobj(u)} if u else {'error':'unauthorized'}) if u else self.json(401,{'error':'unauthorized'})
  if not u: return self.json(401,{'error':'unauthorized'})
  c=db()
  m=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not a or a['role'] not in ('owner','admin'):return self.json(403,{'error':'forbidden'})
   rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses AND (expires IS NULL OR expires>?)',(w['id'],time.time())).fetchall()
   return self.json(200,{'invitations':[dict(x) for x in rs]})
  m=re.match(r'/api/files/(\d+)(/download)?$',p)
  if m:
   z=c.execute('SELECT * FROM files WHERE id=?',(int(m.group(1)),)).fetchone()
   if not z:return self.json(404,{'error':'not found'})
   if m.group(2):
    self.send_response(200); self.send_header('Content-Type',z['content_type']); self.send_header('Content-Disposition','attachment; filename="'+z['filename']+'"'); self.send_header('Content-Length',str(z['size'])); self.end_headers(); self.wfile.write(z['data']); return
   return self.json(200,{'file':self.fileobj(z)})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not role:return self.json(403,{'error':'forbidden'})
   gs=[]
   for z in c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)):
    gs.append({'id':z['id'],'handle':z['handle'],'name':z['name'],'member_user_ids':json.loads(z['users'] or '[]')})
   return self.json(200,{'groups':gs})
  if p=='/api/workspaces':
   rs=c.execute('SELECT * FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); return self.json(200,{'workspaces':[dict(r) for r in rs]})
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.json(404,{'error':'not found'})
   actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if not actor:return self.json(403,{'error':'forbidden'})
   inc=q.get('include_archived',['0'])[0] in ('1','true')
   cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (? OR is_archived=0) AND (is_private=0 OR EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=channels.id AND cm.user_id=?))',(w['id'],int(inc),u['id'])).fetchall()
   states={}
   for x in cs:
    z=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone(); last=z['last_read_event_id'] if z else 0
    evs=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>?',(x['id'],last)).fetchall(); un=me=0
    for ev in evs:
     if ev['kind'] in ('message.created','message.reply'):
      mm=json.loads(ev['payload']).get('message',{})
      if mm.get('author_id')!=u['id']:un+=1
      if u['id'] in mm.get('mentions',[]):me+=1
    states[str(x['id'])]={'channel_id':x['id'],'last_read_event_id':last,'unread_count':un,'mention_count':me}
   return self.json(200,{'workspace':dict(w),'channels':[self.chobj(x) for x in cs],'read_state':list(states.values())})
  m=re.match(r'/api/workspaces/([^/]+)/members$',p)
  if m:
   w=c.execute('SELECT id FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.json(404,{'error':'not found'})
   if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.json(403,{'error':'forbidden'})
   rs=c.execute('SELECT m.user_id,m.role,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall(); return self.json(200,{'members':[dict(x) for x in rs]})
  m=re.match(r'/api/channels/(\d+)/read$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   if not self.allowed(u,cid):return self.json(403,{'error':'forbidden'})
   z=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); last=z['last_read_event_id'] if z else 0
   head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
   evs=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
   unread=mentions=0
   for ev in evs:
    if ev['kind'] in ('message.created','message.reply'):
     pl=json.loads(ev['payload']); mm=pl.get('message',{})
     if mm.get('author_id')!=u['id']: unread+=1
     if u['id'] in mm.get('mentions',[]): mentions+=1
   return self.json(200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   if not self.allowed(u,cid):return self.json(403,{'error':'forbidden'})
   try: lim=int(q.get('limit',['50'])[0])
   except: return self.json(400,{'error':'invalid limit'})
   if lim<1 or lim>200:return self.json(400,{'error':'invalid limit'})
   cur=q.get('cursor',[None])[0]
   if cur is not None:
    try: bound=int(cur)
    except: return self.json(400,{'error':'invalid cursor'})
    rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND id<? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?',(cid,bound,lim+1)).fetchall()
   else: rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?',(cid,lim+1)).fetchall()
   more=len(rs)>lim; rs=rs[:lim]; return self.json(200,{'messages':[self.msgobj(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
  m=re.match(r'/api/messages/(\d+)/replies$',p)
  if m:
   pid=int(m.group(1)); parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(pid,)).fetchone()
   if not parent:return self.json(404,{'error':'not found'})
   if not self.allowed(u,parent['channel_id']):return self.json(403,{'error':'forbidden'})
   try: lim=int(q.get('limit',['50'])[0])
   except: return self.json(400,{'error':'invalid limit'})
   if lim<1 or lim>200:return self.json(400,{'error':'invalid limit'})
   cur=q.get('cursor',[None])[0]
   if cur is not None:
    try: bound=int(cur)
    except: return self.json(400,{'error':'invalid cursor'})
    rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND id>? AND deleted=0 ORDER BY id LIMIT ?',(pid,bound,lim+1)).fetchall()
   else: rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?',(pid,lim+1)).fetchall()
   more=len(rs)>lim; rs=rs[:lim]; return self.json(200,{'replies':[self.msgobj(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
  m=re.match(r'/api/channels/(\d+)/pins$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   if not self.allowed(u,cid):return self.json(403,{'error':'forbidden'})
   try: lim=int(q.get('limit',['50'])[0])
   except: return self.json(400,{'error':'invalid limit'})
   if lim<1 or lim>200:return self.json(400,{'error':'invalid limit'})
   cur=q.get('cursor',[None])[0]
   if cur is not None:
    try: bound=int(cur)
    except: return self.json(400,{'error':'invalid cursor'})
    rs=c.execute('SELECT p.*,m.*,p.pinned_by pb,p.pinned_at pa FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND p.message_id<? ORDER BY p.pinned_at DESC LIMIT ?',(cid,bound,lim+1)).fetchall()
   else: rs=c.execute('SELECT p.*,m.*,p.pinned_by pb,p.pinned_at pa FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC LIMIT ?',(cid,lim+1)).fetchall()
   more=len(rs)>lim; rs=rs[:lim]
   return self.json(200,{'pins':[{'message':self.msgobj(x),'pinned_by':x['pb'],'pinned_at':x['pa']} for x in rs],'next_cursor':str(rs[-1]['message_id']) if more and rs else None})
  m=re.match(r'/api/channels/(\d+)/members$',p)
  if m:
   if not c.execute('SELECT 1 FROM channels WHERE id=?',(int(m.group(1)),)).fetchone():return self.json(404,{'error':'not found'})
   rs=c.execute('SELECT u.* FROM channel_members cm JOIN users u ON u.id=cm.user_id WHERE cm.channel_id=?',(int(m.group(1)),)).fetchall(); return self.json(200,{'members':[userobj(x) for x in rs]})
  if p.startswith('/api/search'):
   term=q.get('q',[''])[0].lower()
   try: lim=int(q.get('limit',['50'])[0])
   except: return self.json(400,{'error':'invalid limit'})
   if lim<1 or lim>200:return self.json(400,{'error':'invalid limit'})
   wsid=q.get('workspace_id',[None])[0]; cur=q.get('cursor',[None])[0]
   if cur is not None:
    try: bound=int(cur)
    except: return self.json(400,{'error':'invalid cursor'})
   else: bound=None
   sql='SELECT DISTINCT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE ? AND ((ch.is_private=0 AND EXISTS (SELECT 1 FROM members wm WHERE wm.workspace_id=ch.workspace_id AND wm.user_id=?)) OR (ch.is_private=1 AND EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=ch.id AND cm.user_id=?)))'
   args=['%'+term+'%',u['id'],u['id']]
   if wsid is not None: sql+=' AND ch.workspace_id=?'; args.append(int(wsid))
   if bound is not None: sql+=' AND m.id<?'; args.append(bound)
   sql+=' ORDER BY m.id DESC LIMIT ?'; args.append(lim+1)
   rs=c.execute(sql,args).fetchall(); more=len(rs)>lim; rs=rs[:lim]
   return self.json(200,{'results':[self.msgobj(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
  m=re.match(r'/api/users/(\d+)',p)
  if m:
   x=c.execute('SELECT * FROM users WHERE id=?',(int(m.group(1)),)).fetchone(); return self.json(200,{'user':userobj(x)}) if x else self.json(404,{'error':'not found'})
  c.close(); return self.json(404,{'error':'not found'})
 def do_POST(self):
  p=self.path; u=self.auth()
  if p=='/api/auth/register':
   try:
    b=self.body(); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: return self.json(400,{'error':'invalid'})
    c=db(); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); x=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return self.json(201,{'user':userobj(x),'token':t})
   except sqlite3.IntegrityError:return self.json(409,{'error':'duplicate'})
  if p=='/api/auth/login':
   b=self.body(); c=db(); x=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
   if not x:return self.json(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,x['id'])); c.commit(); c.close(); return self.json(200,{'user':userobj(x),'token':t})
  if not u:return self.json(401,{'error':'unauthorized'})
  c=db()
  if p=='/api/files':
   raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); ct=self.headers.get('Content-Type','')
   mpart=re.search(br'filename="([^"]*)"',raw)
   if not mpart:return self.json(400,{'error':'missing file'})
   filename=mpart.group(1).decode('utf-8','replace'); cm=re.search(br'Content-Type:\s*([^\r\n]+)',raw)
   content_type=(cm.group(1).decode() if cm else 'application/octet-stream')
   sep=re.search(br'\r?\n\r?\n',raw)
   if not sep:return self.json(400,{'error':'missing file'})
   data=raw[sep.end():]; end=data.rfind(b'\r\n--')
   if end>=0:data=data[:end]
   if len(data)>10485760:return self.json(413,{'error':'too large'})
   c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],filename,content_type,len(data),now(),data)); c.commit(); z=c.execute('SELECT * FROM files WHERE id=last_insert_rowid()').fetchone(); return self.json(201,{'file':self.fileobj(z)})
  if p=='/api/dms':
   b=self.body(); tid=b.get('user_id')
   if tid==u['id']:return self.json(400,{'error':'self'})
   target=c.execute('SELECT id FROM users WHERE id=?',(tid,)).fetchone()
   if not target:return self.json(404,{'error':'not found'})
   a,bid=sorted((u['id'],int(tid))); z=c.execute('SELECT id FROM channels WHERE is_dm=1 AND name=?',(f'dm-{a}-{bid}',)).fetchone()
   if z:return self.json(200,{'channel_id':z['id']})
   c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)',(f'dm-{a}-{bid}',1)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a)); c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,bid)); c.commit(); return self.json(200,{'channel_id':cid})
  b=self.body()
  m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.json(404,{'error':'not found'})
   actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if not actor or actor['role']!='owner':return self.json(403,{'error':'forbidden'})
   nid=b.get('new_owner_id'); target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],nid)).fetchone()
   if not target or target['role']!='admin':return self.json(400,{'error':'target'})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],nid))
   c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(nid,w['id'])); c.commit()
   return self.json(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  m=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not a or a['role'] not in ('owner','admin'):return self.json(403,{'error':'forbidden'})
   code=secrets.token_urlsafe(12); target=b.get('invited_user_id');
   if target is None and b.get('invited_username'):
    quser=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone(); target=quser['id'] if quser else None
   mx=1 if target is not None or b.get('invited_username') is not None else b.get('max_uses',1); exp=time.time()+b.get('expires_in_seconds',86400)
   c.execute('INSERT INTO invitations(code,workspace_id,invited_user_id,max_uses,expires) VALUES(?,?,?,?,?)',(code,w['id'],target,mx,exp)); c.commit()
   return self.json(201,{'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'max_uses':mx,'uses':0,'expires_at':exp}})
  m=re.match(r'/api/invitations/([^/]+)/accept$',p)
  if m:
   inv=c.execute('SELECT * FROM invitations WHERE code=?',(m.group(1),)).fetchone()
   if not inv or inv['uses']>=inv['max_uses'] or (inv['expires'] and inv['expires']<time.time()):return self.json(404,{'error':'not found'})
   if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return self.json(403,{'error':'forbidden'})
   c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member')); c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(m.group(1),)); c.commit(); return self.json(200,{'workspace_id':inv['workspace_id']})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not role:return self.json(403,{'error':'forbidden'})
   h=b.get('handle','')
   if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.json(400,{'error':'invalid handle'})
   try:c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,users) VALUES(?,?,?,?,?)',(w['id'],h,b.get('name',''),u['id'],json.dumps(b.get('user_ids',[])))); c.commit(); z=c.execute('SELECT * FROM groups_ WHERE id=last_insert_rowid()').fetchone(); return self.json(201,{'group':{'id':z['id'],'handle':z['handle'],'name':z['name'],'member_user_ids':json.loads(z['users'])}})
   except sqlite3.IntegrityError:return self.json(409,{'error':'duplicate'})
  if p=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')): return self.json(400,{'error':'invalid'})
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); return self.json(201,{'workspace':dict(w),'general_channel':self.chobj(ch)})
   except sqlite3.IntegrityError:return self.json(409,{'error':'duplicate'})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not role or role['role']=='guest' or (b.get('is_private') and role['role'] not in ('owner','admin')):return self.json(403,{'error':'forbidden'})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return self.json(400,{'error':'invalid'})
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit(); return self.json(201,{'channel':self.chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   except sqlite3.IntegrityError:return self.json(409,{'error':'duplicate'})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive|leave)$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if m.group(2)=='leave':
    was=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(); c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])); c.commit()
    if was: send_events(cid,'member.left',{'user_id':u['id']})
    return self.json(200,{'channel':self.chobj(ch)})
   if not a or a['role']!='owner':return self.json(403,{'error':'forbidden'})
   v=1 if m.group(2)=='archive' else 0; c.execute('UPDATE channels SET is_archived=? WHERE id=?',(v,cid)); c.commit(); z=self.chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()); send_events(cid,'channel.archived' if v else 'channel.unarchived',{'channel':z}); return self.json(200,{'channel':z})
  m=re.match(r'/api/channels/(\d+)/join$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   existing=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
   if not mem and w['join_mode']=='invite_only':return self.json(403,{'error':'forbidden'})
   if mem and mem['role']=='guest':return self.json(403,{'error':'forbidden'})
   if ch['is_private'] and not existing:return self.json(403,{'error':'forbidden'})
   if not mem:c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
   was=bool(existing); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit();
   if not was: send_events(cid,'member.joined',{'user_id':u['id']})
   return self.json(200,{'channel':self.chobj(ch)})
  m=re.match(r'/api/channels/(\d+)/read$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   if not self.allowed(u,cid):return self.json(403,{'error':'forbidden'})
   try:n=int(b.get('last_read_event_id',0))
   except:return self.json(400,{'error':'invalid'})
   old=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
   n=max(n,old['last_read_event_id'] if old else 0); c.execute('INSERT INTO read_states VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(u['id'],cid,n)); c.commit()
   return self.json(200,{'read_state':{'channel_id':cid,'last_read_event_id':n,'unread_count':0,'mention_count':0}})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   if ch['is_archived'] and not (isinstance(b.get('body'),str) and b.get('body').strip()=='/unarchive'):return self.json(423,{'error':'archived'})
   if not self.allowed(u,cid):return self.json(403,{'error':'forbidden'})
   body=b.get('body','')
   if not body:return self.json(400,{'error':'empty'})
   if body.startswith('/') and not body.startswith('//'):
    parts=body.split(None,1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
    if cmd=='/me':
     if not arg.strip():return self.json(400,{'error':'empty'})
    elif cmd=='/shrug':
     body=(arg.strip()+' ' if arg.strip() else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if not arg.strip():return self.json(400,{'error':'empty'})
     w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
     if not role or (not self.allowed(u,cid) and role['role']!='owner'):return self.json(403,{'error':'forbidden'})
     if len(arg)>250:return self.json(400,{'error':'invalid'})
     c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); c.commit(); z=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
     return self.json(201,{'message':None,'channel':self.chobj(z)})
    elif cmd=='/invite':
     if not arg.startswith('@') or len(arg)<=1:return self.json(400,{'error':'username'})
     target=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
     if not target:return self.json(404,{'error':'not found'})
     w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); wm=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target['id'])).fetchone(); caller=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
     if not caller:return self.json(403,{'error':'forbidden'})
     if not wm:return self.json(403,{'error':'forbidden'})
     c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,target['id'])); c.commit()
     return self.json(201,{'message':None,'invited_user':userobj(target)})
    elif cmd in ('/archive','/unarchive'):
     w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
     if not role or role['role']!='owner':return self.json(403,{'error':'forbidden'})
     v=1 if cmd=='/archive' else 0; c.execute('UPDATE channels SET is_archived=? WHERE id=?',(v,cid)); c.commit(); z=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
     return self.json(201,{'message':None,'channel':self.chobj(z)})
    else:return self.json(400,{'error':'unknown command'})
   fids=b.get('file_ids',[])
   if not isinstance(fids,list):return self.json(400,{'error':'file_ids'})
   for fid in fids:
    z=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not z or z['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():return self.json(400,{'error':'invalid file'})
   c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   for fid in fids:c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid))
   c.commit(); x=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
   ids=set(); cm=[int(z['user_id']) for z in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(cid,))]
   for z in c.execute('SELECT id,username FROM users'):
    if z['id']!=u['id'] and re.search(r'(?<![A-Za-z0-9_])@'+re.escape(z['username'])+r'(?![A-Za-z0-9_])',body) and (not ch['workspace_id'] or c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],z['id'])).fetchone()): ids.add(z['id'])
   if re.search(r'(?<![A-Za-z0-9_])@channel(?![A-Za-z0-9_])',body): ids.update(x for x in cm if x!=u['id'])
   for g in c.execute('SELECT users FROM groups_ WHERE workspace_id=?',(ch['workspace_id'],)) if ch['workspace_id'] else []:
    try:
     if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(g['handle'])+r'(?![A-Za-z0-9_])',body): ids.update(x for x in json.loads(g['users']) if x in cm and x!=u['id'])
    except: pass
   # Expand groups and online @here from shared presence.
   if ch['workspace_id']:
    toks=set(re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body))
    for g in c.execute('SELECT handle,users FROM groups_ WHERE workspace_id=?',(ch['workspace_id'],)):
     if g['handle'] in toks:
      for gid in json.loads(g['users'] or '[]'):
       if int(gid) in cm and int(gid)!=u['id']: ids.add(int(gid))
   if 'here' in set(re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body)):
    for row in c.execute('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(time.time()-2,)):
     if row['user_id'] in cm and row['user_id']!=u['id']: ids.add(row['user_id'])
   out=self.msgobj(x); out['mentions']=sorted(ids); send_events(cid,'message.reply' if b.get('parent_id') else 'message.created',{'message':out}); return self.json(201,{'message':out})
  m=re.match(r'/api/messages/(\d+)/pin$',p)
  if m:
   mid=int(m.group(1)); x=c.execute('SELECT m.*,ch.workspace_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
   if not x:return self.json(404,{'error':'not found'})
   if not self.allowed(u,x['channel_id']):return self.json(403,{'error':'forbidden'})
   ts=now(); c.execute('INSERT OR IGNORE INTO pins(message_id,pinned_by,pinned_at) VALUES(?,?,?)',(mid,u['id'],ts)); c.commit()
   z=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone(); return self.json(200,{'pin':{'message_id':mid,'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']}})
  m=re.match(r'/api/messages/(\d+)/reactions$',p)
  if m:
   mid=int(m.group(1)); emoji=b.get('emoji')
   x=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
   if not x:return self.json(404,{'error':'not found'})
   if not self.allowed(u,x['channel_id']):return self.json(403,{'error':'forbidden'})
   if not isinstance(emoji,str) or not emoji:return self.json(400,{'error':'invalid emoji'})
   before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
   c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji)); c.commit()
   rs=c.execute('SELECT emoji,COUNT(*) count,GROUP_CONCAT(user_id) user_ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall()
   out=[{'emoji':z['emoji'],'count':z['count'],'user_ids':[int(i) for i in z['user_ids'].split(',')]} for z in rs]
   if not before: send_events(x['channel_id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':out})
   return self.json(200,{'reactions':out})
  c.close(); return self.json(404,{'error':'not found'})
 def do_DELETE(self):
  u=self.auth()
  if not u:return self.json(401,{'error':'unauthorized'})
  p=self.path; c=db()
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); z=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not z:return self.json(200,{'deleted':True})
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']))
   role=role.fetchone()
   if not role or (role['role']!='owner' and z['creator_id']!=u['id']):return self.json(403,{'error':'forbidden'})
   c.execute('DELETE FROM groups_ WHERE id=?',(z['id'],)); c.commit(); return self.json(200,{'deleted':True})
  m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   inv=c.execute('SELECT * FROM invitations WHERE code=?',(m.group(2),)).fetchone()
   if not w or not inv:return self.json(404,{'error':'not found'})
   if not a or a['role'] not in ('owner','admin'):return self.json(403,{'error':'forbidden'})
   c.execute('DELETE FROM invitations WHERE code=?',(m.group(2),)); c.commit(); return self.json(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   x=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
   if not x:return self.json(404,{'error':'not found'})
   if x['author_id']!=u['id']:return self.json(403,{'error':'forbidden'})
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(x['id'],)); c.commit(); return self.json(200,{'deleted':True})
  return self.json(404,{'error':'not found'})
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.json(401,{'error':'unauthorized'})
  p=self.path; b=self.body(); c=db()
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); z=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not w or not z:return self.json(404,{'error':'not found'})
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if not role or (role['role']!='owner' and z['creator_id']!=u['id']):return self.json(403,{'error':'forbidden'})
   if 'name' in b:c.execute('UPDATE groups_ SET name=? WHERE id=?',(b['name'],z['id']))
   if 'user_ids' in b:c.execute('UPDATE groups_ SET users=? WHERE id=?',(json.dumps(b['user_ids']),z['id']))
   c.commit(); z=c.execute('SELECT * FROM groups_ WHERE id=?',(z['id'],)).fetchone(); return self.json(200,{'group':{'id':z['id'],'handle':z['handle'],'name':z['name'],'member_user_ids':json.loads(z['users'])}})
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not actor or actor['role'] not in ('owner','admin'):return self.json(403,{'error':'forbidden'})
   if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.json(400,{'error':'invalid'})
   c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(b.get('name'),b.get('join_mode'),w['id'])); c.commit()
   return self.json(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None; nid=b.get('new_owner_id')
   t=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],nid)).fetchone() if w else None
   if not w:return self.json(404,{'error':'not found'})
   if not a or a['role']!='owner':return self.json(403,{'error':'forbidden'})
   if not t or t['role']!='admin':return self.json(400,{'error':'target'})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id'])); c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],nid)); c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(nid,w['id'])); c.commit(); return self.json(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive|leave)$',p)
  if m:
   cid=int(m.group(1)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch else None; a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not ch:return self.json(404,{'error':'not found'})
   if m.group(2)=='leave':
    was=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(); c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])); c.commit()
    if was: send_events(cid,'member.left',{'user_id':u['id']})
    return self.json(200,{'channel':self.chobj(ch)})
   if not a or a['role']!='owner':return self.json(403,{'error':'forbidden'})
   v=1 if m.group(2)=='archive' else 0; c.execute('UPDATE channels SET is_archived=? WHERE id=?',(v,cid)); c.commit(); return self.json(200,{'channel':self.chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
  m=re.match(r'/api/channels/(\d+)$',p)
  if m:
   ch=c.execute('SELECT * FROM channels WHERE id=?',(int(m.group(1)),)).fetchone()
   if not ch:return self.json(404,{'error':'not found'})
   w=c.execute('SELECT workspace_id FROM channels WHERE id=?',(int(m.group(1)),)).fetchone(); owner=c.execute('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(w['workspace_id'],u['id'])).fetchone() if w else None
   if ch['is_archived']:return self.json(423,{'error':'archived'})
   if not self.allowed(u,int(m.group(1))) and not owner:return self.json(403,{'error':'forbidden'})
   if len(b.get('topic',''))>250:return self.json(400,{'error':'invalid'})
   c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),ch['id'])); c.commit(); z=self.chobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone()); send_events(ch['id'],'channel.updated',{'channel':z}); return self.json(200,{'channel':z})
  m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   target=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],int(m.group(2)))).fetchone() if w else None
   actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w or not target:return self.json(404,{'error':'not found'})
   role=b.get('role')
   if role not in ('owner','admin','member','guest'):return self.json(400,{'error':'invalid role'})
   if role=='owner':return self.json(400,{'error':'invalid role'})
   if target['role']=='owner' or int(m.group(2))==w['owner_id']:return self.json(403,{'error':'forbidden'})
   if not actor or actor['role'] not in ('owner','admin'):return self.json(403,{'error':'forbidden'})
   if actor['role']=='admin' and target['role']=='admin':return self.json(403,{'error':'forbidden'})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],int(m.group(2)))); c.commit()
   return self.json(200,{'member':{'user_id':int(m.group(2)),'role':role}})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   x=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
   if not x:return self.json(404,{'error':'not found'})
   if x['deleted']:return self.json(409,{'error':'deleted'})
   if not isinstance(b.get('body'),str) or not b.get('body').strip():return self.json(400,{'error':'empty'})
   if x['author_id']!=u['id']:return self.json(403,{'error':'forbidden'})
   c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),x['id'])); c.commit(); x=c.execute('SELECT * FROM messages WHERE id=?',(x['id'],)).fetchone(); out=self.msgobj(x); send_events(x['channel_id'],'message.updated',{'message':out}); return self.json(200,{'message':out})
  m=re.match(r'/api/users/me$',p)
  if m:
   validtz={'UTC','America/New_York','America/Los_Angeles','Europe/London','Asia/Tokyo'}
   if ('timezone' in b and b['timezone'] not in validtz) or any(k in b and (not isinstance(b[k],str) or len(b[k])>250) for k in ('display_name','avatar_url','status_text','status_emoji')) or ('display_name' in b and not b['display_name']):
    return self.json(400,{'error':'invalid profile'})
   fs=['display_name','timezone','avatar_url','status_text','status_emoji']; vals=[b[k] for k in fs if k in b]
   if vals:c.execute('UPDATE users SET '+','.join(k+'=?' for k in fs if k in b)+' WHERE id=?',vals+[u['id']]); c.commit()
   x=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); obj=userobj(x)
   c.execute('INSERT INTO profile_events(payload) VALUES(?)',(json.dumps({'type':'user.updated','user':obj}),)); c.commit()
   with clients_lock:
    for ws in list(clients.get('__global__',[])):
     try: ws({'type':'user.updated','user':obj})
     except: pass
   return self.json(200,{'user':obj})
  return self.json(404,{'error':'not found'})
 def do_DELETE(self):
  u=self.auth()
  if not u:return self.json(401,{'error':'unauthorized'})
  c=db()
  self.path=unquote(self.path)
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.json(404,{'error':'not found'})
   z=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone()
   if not z:return self.json(200,{'deleted':True})
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if not role or (role['role']!='owner' and z['creator_id']!=u['id']):return self.json(403,{'error':'forbidden'})
   c.execute('DELETE FROM groups_ WHERE id=?',(z['id'],)); c.commit(); return self.json(200,{'deleted':True})
  m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',self.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); inv=c.execute('SELECT * FROM invitations WHERE code=?',(m.group(2),)).fetchone()
   if not w or not inv:return self.json(404,{'error':'not found'})
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if not role or role['role'] not in ('owner','admin'):return self.json(403,{'error':'forbidden'})
   c.execute('DELETE FROM invitations WHERE code=?',(m.group(2),)); c.commit(); return self.json(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/([^/]+)$',self.path)
  if m:
   mid=int(m.group(1)); emoji=unquote(unquote(m.group(2))); x=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
   if not x:return self.json(404,{'error':'not found'})
   if not self.allowed(u,x['channel_id']):return self.json(403,{'error':'forbidden'})
   existed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
   c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); c.commit()
   rs=c.execute('SELECT emoji,COUNT(*) count,GROUP_CONCAT(user_id) user_ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall()
   out=[{'emoji':z['emoji'],'count':z['count'],'user_ids':[int(i) for i in z['user_ids'].split(',')]} for z in rs]
   if existed: send_events(x['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':out})
   return self.json(200,{'reactions':out})
  m=re.match(r'/api/messages/(\d+)/pin$',self.path)
  if m:
   x=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
   if not x:return self.json(404,{'error':'not found'})
   if not self.allowed(u,x['channel_id']):return self.json(403,{'error':'forbidden'})
   c.execute('DELETE FROM pins WHERE message_id=?',(x['id'],)); c.commit(); return self.json(200,{'unpinned':True})
  m=re.match(r'/api/messages/(\d+)$',self.path); c=db()
  if m:
   x=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
   if not x:return self.json(404,{'error':'not found'})
   if x['deleted']:return self.json(200,{'deleted':True})
   owner=c.execute('SELECT 1 FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=? AND w.owner_id=?',(x['channel_id'],u['id'])).fetchone()
   if x['author_id']!=u['id'] and not owner:return self.json(403,{'error':'forbidden'})
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(x['id'],)); c.commit(); send_events(x['channel_id'],'message.deleted',{'message_id':x['id']}); return self.json(200,{'deleted':True})
  return self.json(404,{'error':'not found'})
 def websocket(self):
  tok=parse_qs(urlparse(self.path).query).get('token',[''])[0]; c=db(); u=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone()
  if not u:return self.reply(401,{'error':'unauthorized'})
  conn_id=secrets.token_urlsafe(16); c.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(conn_id,u['user_id'],time.time())); c.commit()
  key=self.headers.get('Sec-WebSocket-Key'); raw=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',raw); self.end_headers()
  def out(o):
   d=json.dumps(o).encode(); n=len(d); h=bytes([129])+ (bytes([n]) if n<126 else bytes([126])+struct.pack('!H',n)); self.wfile.write(h+d); self.wfile.flush()
  with clients_lock: clients.setdefault('__global__',[]).append(out)
  def profile_follow():
   last=0
   while True:
    time.sleep(0.05)
    try:
     cc=db(); rows=cc.execute('SELECT * FROM profile_events WHERE id>? ORDER BY id',(last,)).fetchall(); cc.close()
     for ev in rows:
      out(json.loads(ev['payload'])); last=ev['id']
    except Exception:
     return
  threading.Thread(target=profile_follow,daemon=True).start()
  try:
   while True:
    h=self.rfile.read(2)
    if not h:break
    n=h[1]&127
    if n==126:n=struct.unpack('!H',self.rfile.read(2))[0]
    mask=self.rfile.read(4); d=self.rfile.read(n); d=bytes(x^mask[i%4] for i,x in enumerate(d)); z=json.loads(d)
    if z.get('type') in ('subscribe','resume'):
     cid=int(z['channel_id']); head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
     if z['type']=='resume':
      for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,z.get('since_event_id',0))): out({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
      out({'type':'resumed','channel_id':cid,'head_event_id':head})
     else: out({'type':'subscribed','channel_id':cid,'head_event_id':head})
     with clients_lock: clients.setdefault(cid,[]).append(out)
     def follow(channel, last):
      while True:
       time.sleep(0.03)
       try:
        cc=db(); rows=cc.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,last[0])).fetchall(); cc.close()
        for ev in rows:
         out({'type':ev['kind'],'event_id':ev['event_id'],'channel_id':channel,**json.loads(ev['payload'])})
         last[0]=ev['event_id']
       except Exception:
        return
     threading.Thread(target=follow,args=(cid,[head]),daemon=True).start()
  except: pass
HTML='<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;color:#fff;margin:3px}[data-button-role=primary]{background:#007a5a;color:#fff}[data-button-role=danger]{background:#e01e5a;color:#fff}[data-button-role=secondary]{background:#1264a3;color:#fff}.hidden{display:none!important}.shell{display:flex;min-height:100vh}.sidebar{width:240px;background:#3f0e40;color:#fff;padding:18px}.content{flex:1;padding:24px;position:relative}.content>[data-testid="thread-panel"]{position:absolute;left:calc(100% - 320px);top:90px;width:300px;background:#fff;border:1px solid #bbb;padding:16px;z-index:5}.msg{padding:12px;border-bottom:1px solid #ddd}.msg button,.content button:not([data-button-role]){background:#1264a3;color:#fff}.error{color:#b00020}</style></head><body><div id="app">\n<section id="auth-modal"><form data-testid="auth-form" data-mode="signup"><label>Username<input name="username" placeholder="Username"></label><label>Password<input name="password" type="password" placeholder="Password"></label><label class="signup-only">Display name<input name="display_name" placeholder="Display name"></label><button id="auth-submit" data-testid="auth-submit" data-button-role="primary">Sign up</button><button type="button" id="auth-toggle" data-testid="auth-toggle" data-button-role="secondary">Have an account? Log in</button><div id="auth-error" data-testid="auth-error" class="error hidden"></div></form></section>\n<section id="create-channel-modal" class="hidden"><form data-testid="create-channel-form"><label>Channel name<input name="name" placeholder="Channel name"></label><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button id="create-channel-submit" data-testid="create-channel-submit" data-button-role="primary">Create channel</button><button type="button" id="create-channel-cancel" data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button><div data-testid="create-channel-error" class="error"></div></form></section>\n<section id="channel-settings-modal" class="hidden"><h3 data-testid="channel-settings-title">Channel settings</h3><button data-testid="channel-settings-close" data-button-role="secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add member</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button></section>\n<section id="workspace-settings-modal" class="hidden"><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"><option>open</option><option>invite_only</option></select><button data-testid="workspace-general-submit" data-button-role="primary">Save workspace</button></section>\n<div id="workspace-create-area" class="hidden"><form data-testid="workspace-create-form"><label>Workspace slug<input name="slug" placeholder="workspace-slug"></label><label>Workspace name<input name="name" placeholder="Workspace name"></label><button data-testid="workspace-create-submit" data-button-role="primary">Create workspace</button></form><button data-testid="empty-state-create-workspace" data-button-role="primary">Create workspace</button></div>\n<div id="shell" class="shell hidden"><aside class="sidebar" data-testid="sidebar"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user"></div><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="logout-btn" data-button-role="danger">Logout</button><form data-testid="join-workspace-form"><input name="slug" placeholder="Workspace slug"><button data-testid="join-workspace-submit" data-button-role="primary">Join workspace</button><span data-testid="join-workspace-error"></span></form></aside><main class="content"><h2 data-testid="channel-title">#general</h2><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div id="message-list" data-testid="message-list"></div><input id="message-input" data-testid="message-input" placeholder="Write a message"><button id="send-btn" data-testid="send-btn" data-button-role="primary">Send</button><div data-testid="thread-panel" class="hidden"><input data-testid="thread-input" placeholder="Thread reply"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></div><button data-testid="open-thread-btn">Thread</button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create invitation</button></form><div data-testid="invitations-list"></div></main></div>\n<script>\nconst Q=s=>document.querySelector(s),T=()=>localStorage.getItem(\'huddle.token\')||\'\';\nasync function api(p,o={}){let h={\'Content-Type\':\'application/json\'};if(T())h.Authorization=\'Bearer \'+T();let r=await fetch(\'/api\'+p,{...o,headers:h}),d={};try{d=await r.json()}catch{};d.status=r.status;return d}\nlet mode=\'signup\',me,ws,chan;\nfunction authMode(){let f=Q(\'[data-testid=auth-form\');f.dataset.mode=mode;Q(\'.signup-only\').classList.toggle(\'hidden\',mode===\'login\');Q(\'#auth-submit\').textContent=mode===\'signup\'?\'Sign up\':\'Log in\';Q(\'#auth-toggle\').textContent=mode===\'signup\'?\'Have an account? Log in\':\'Need an account? Sign up\'}\nQ(\'#auth-toggle\').onclick=()=>{mode=mode===\'signup\'?\'login\':\'signup\';authMode()};\nQ(\'[data-testid=auth-form\').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),d=await api(mode===\'signup\'?\'/auth/register\':\'/auth/login\',{method:\'POST\',body:JSON.stringify({username:f.get(\'username\'),password:f.get(\'password\'),display_name:f.get(\'display_name\')||undefined})});if(d.token){localStorage.setItem(\'huddle.token\',d.token);await boot()}else{Q(\'#auth-error\').textContent=d.status===400?\'Password must be at least 8 characters\':\'Login failed\';Q(\'#auth-error\').classList.remove(\'hidden\')}};\nfunction addChannel(x){let e=document.createElement(\'div\');e.dataset.testid=\'channel-entry\';e.dataset.channelId=x.id;e.dataset.channelName=x.name;e.textContent=\'#\'+x.name;e.onclick=()=>loadChannel(x);Q(\'[data-testid=channel-list]\').append(e)}\nasync function loadChannel(x){chan=x;Q(\'[data-testid=channel-title]\').textContent=\'#\'+x.name;Q(\'[data-testid=channel-topic]\').textContent=x.topic||\'\';let d=await api(\'/channels/\'+x.id+\'/messages\'),l=Q(\'[data-testid=message-list]\');l.innerHTML=\'\';if(!(d.messages||[]).length){l.innerHTML=\'<div data-testid="channel-empty-state">No messages yet. Start the conversation.</div>\';return}(d.messages||[]).reverse().forEach(renderMessage)}\nfunction renderMessage(m){let e=document.createElement(\'div\');e.className=\'msg\';e.dataset.testid=\'message\';e.dataset.messageId=m.id;let chips=(m.reactions||[]).map(r=>\'<span data-testid="reaction-chip">\'+r.emoji+\' \'+r.count+\'</span>\').join(\'\');e.innerHTML=\'<span data-testid="message-avatar" style="display:inline-flex;width:28px;height:28px;background:#36c;color:#fff;align-items:center;justify-content:center;border-radius:50%">\'+(m.author.display_name||\'?\')[0]+\'</span> <b data-testid="message-author"></b> <small data-testid="message-time"></small> <span data-testid="message-body"></span> <span data-testid="message-edited" class="\'+(m.edited_at?\'\':\'hidden\')+\'">edited</span> <span data-testid="thread-reply-count">\'+(m.reply_count?m.reply_count+\' \'+(m.reply_count==1?\'reply\':\'replies\'):\'\')+\'</span> \'+chips+\' <button data-testid="message-edit-btn">Edit</button><button data-testid="message-delete-btn">Delete</button><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button" aria-label="Add reaction">😊</button>\';e.querySelector(\'[data-testid=message-author]\').textContent=m.author.display_name;e.querySelector(\'[data-testid=message-time]\').textContent=new Date(m.created_at).toLocaleTimeString();e.querySelector(\'[data-testid=message-body]\').textContent=m.body;e.querySelector(\'[data-testid=open-thread-btn]\').onclick=()=>{let p=Q(\'[data-testid=thread-panel]\');p.classList.remove(\'hidden\');Q(\'[data-testid=thread-send]\').onclick=async()=>{let i=Q(\'[data-testid=thread-input]\');if(i.value.trim()){await api(\'/channels/\'+chan.id+\'/messages\',{method:\'POST\',body:JSON.stringify({body:i.value,parent_id:m.id})});i.value=\'\';let rr=await api(\'/messages/\'+m.id+\'/replies\');let panel=Q(\'[data-testid=thread-panel]\');panel.querySelectorAll(\'[data-testid=message]\').forEach(z=>z.remove());(rr.replies||[]).forEach(r=>{let q=document.createElement(\'div\');q.dataset.testid=\'message\';q.dataset.messageId=r.id;q.innerHTML=\'<b data-testid="message-author"></b> <span data-testid="message-body"></span>\';q.querySelector(\'[data-testid=message-author]\').textContent=r.author.display_name;q.querySelector(\'[data-testid=message-body]\').textContent=r.body;panel.append(q)});loadChannel(chan)}}};e.querySelector(\'[data-testid=reaction-button]\').onclick=()=>{let p=document.createElement(\'div\');p.dataset.testid=\'emoji-picker\';[\'😀\',\'👍\',\'❤️\',\'🎉\',\'😂\',\'🚀\',\'🔥\',\'👏\'].forEach(x=>{let b=document.createElement(\'button\');b.dataset.testid=\'emoji-option\';b.textContent=x;b.onclick=async()=>{await api(\'/messages/\'+m.id+\'/reactions\',{method:\'POST\',body:JSON.stringify({emoji:x})});p.remove();loadChannel(chan)};p.append(b)});e.append(p)};e.querySelector(\'[data-testid=message-edit-btn]\').onclick=()=>{let i=document.createElement(\'input\');i.dataset.testid=\'message-edit-input\';i.value=m.body;e.querySelector(\'[data-testid=message-body]\').replaceWith(i);i.focus();i.onkeydown=async z=>{if(z.key===\'Enter\'){await api(\'/messages/\'+m.id,{method:\'PATCH\',body:JSON.stringify({body:i.value})});loadChannel(chan)}if(z.key===\'Escape\')loadChannel(chan)}};e.querySelector(\'[data-testid=message-delete-btn]\').onclick=async()=>{await api(\'/messages/\'+m.id,{method:\'DELETE\'});loadChannel(chan)};Q(\'[data-testid=message-list]\').append(e)}\nQ(\'[data-testid=close-thread]\').onclick=()=>Q(\'[data-testid=thread-panel]\').classList.add(\'hidden\');\nasync function boot(){let d=await api(\'/auth/me\');if(!d.user){localStorage.removeItem(\'huddle.token\');return}me=d.user;Q(\'#auth-modal\').classList.add(\'hidden\');Q(\'#shell\').classList.remove(\'hidden\');Q(\'[data-testid=current-user]\').textContent=me.display_name;let x=await api(\'/workspaces\');if(!x.workspaces||!x.workspaces.length){Q(\'#workspace-create-area\').classList.remove(\'hidden\');return}ws=x.workspaces[0];let w=await api(\'/workspaces/\'+ws.slug);Q(\'[data-testid=workspace-header]\').textContent=ws.name;Q(\'[data-testid=channel-list]\').innerHTML=\'\';(w.channels||[]).forEach(addChannel);if(w.channels&&w.channels[0])loadChannel(w.channels[0])}\nQ(\'[data-testid=send-btn]\').onclick=async()=>{let i=Q(\'#message-input\');if(i.value.trim()&&chan){await api(\'/channels/\'+chan.id+\'/messages\',{method:\'POST\',body:JSON.stringify({body:i.value})});i.value=\'\';loadChannel(chan)}};\nQ(\'[data-testid=new-channel-btn]\').onclick=()=>Q(\'#create-channel-modal\').classList.remove(\'hidden\');Q(\'[data-testid=create-channel-cancel]\').onclick=()=>Q(\'#create-channel-modal\').classList.add(\'hidden\');\nQ(\'[data-testid=create-channel-form]\').onsubmit=async e=>{e.preventDefault();let n=e.target.name.value.trim().toLowerCase().replace(/\\s+/g,\'-\'),d=await api(\'/workspaces/\'+ws.slug+\'/channels\',{method:\'POST\',body:JSON.stringify({name:n,is_private:Q(\'#create-channel-private\').checked})});if(d.channel){addChannel(d.channel);Q(\'#create-channel-modal\').classList.add(\'hidden\');loadChannel(d.channel)}else Q(\'[data-testid=create-channel-error]\').textContent=\'A channel with that name already exists\'};\nQ(\'[data-testid=workspace-create-form]\').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),d=await api(\'/workspaces\',{method:\'POST\',body:JSON.stringify({slug:f.get(\'slug\'),name:f.get(\'name\')})});if(d.workspace){ws=d.workspace;Q(\'#workspace-create-area\').classList.add(\'hidden\');let w=await api(\'/workspaces/\'+ws.slug);Q(\'[data-testid=workspace-header]\').textContent=ws.name;Q(\'[data-testid=channel-list]\').innerHTML=\'\';(w.channels||[]).forEach(addChannel);if(w.channels&&w.channels[0])loadChannel(w.channels[0])}};\nQ(\'[data-testid=empty-state-create-workspace]\').onclick=()=>Q(\'#workspace-create-area\').classList.remove(\'hidden\');Q(\'[data-testid=logout-btn]\').onclick=()=>{localStorage.removeItem(\'huddle.token\');location.reload()};authMode();if(T())boot();\n</script></body></html>\n'



irc_nicks=set(); irc_lock=threading.Lock(); irc_clients=[]; irc_clients_lock=threading.Lock()
def irc_find_channel(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1); c=db(); r=c.execute('SELECT ch.*,w.slug,w.name wname FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone(); c.close(); return r
def irc_name(ch): return '#'+ch['slug']+'/'+ch['name']
def irc_pump():
 last={}
 while True:
  time.sleep(.05)
  try:
   c=db(); rows=c.execute("SELECT e.*,ch.name cname,w.slug FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.kind='message.created' ORDER BY e.channel_id,e.event_id").fetchall(); c.close()
   with irc_clients_lock: cs=list(irc_clients)
   for e in rows:
    if e['event_id']<=last.get(e['channel_id'],0): continue
    msg=json.loads(e['payload']).get('message',{}); au=msg.get('author',{}).get('username','huddle'); target='#'+e['slug']+'/'+e['cname']; delivered=False
    for cl in cs:
     if cl.get('channel_id')==e['channel_id']:
      try: cl['put'](':'+au+'!'+au+'@host PRIVMSG '+target+' :'+msg.get('body','')); delivered=True
      except: pass
    if delivered: last[e['channel_id']]=e['event_id']
  except Exception:
   time.sleep(.05)
def irc_client(sock,addr):
 nick=None; user=None; passed=None; registered=False; closing=False
 def put(line):
  try:sock.sendall((line+'\r\n').encode())
  except:pass
 client={'put':put,'user_id':None,'channel_id':None}
 with irc_clients_lock: irc_clients.append(client)
 try:
  buf=b''
  while True:
   x=sock.recv(4096)
   if not x: break
   buf+=x
   while b'\r\n' in buf:
    raw,buf=buf.split(b'\r\n',1); line=raw.decode('utf-8','replace')
    if not line: continue
    parts=line.split(' ',1); cmd=parts[0].upper(); arg=parts[1] if len(parts)>1 else ''
    if cmd=='PASS':
     tok=arg.lstrip(':').strip(); c=db(); passed=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); c.close()
     if not passed: put(':huddle 464 * :Password incorrect')
     else:user=passed['user_id']; client['user_id']=user
    elif cmd=='NICK':
     n=arg.lstrip(':').strip()
     if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',n): put(':huddle 432 * :Erroneous nickname')
     else:
      with irc_lock:
       if n in irc_nicks: put(':huddle 433 * '+n+' :Nickname is already in use')
       else:
        if nick: irc_nicks.discard(n)
        nick=n; irc_nicks.add(n)
    elif cmd=='USER':
     userline=arg.split(':',1); user=(user if isinstance(user,int) else (userline[1] if len(userline)>1 else nick))
    elif cmd=='JOIN':
     target=arg.strip().split(',')[0]; ch=irc_find_channel(target)
     if not ch: put(':huddle 403 '+(nick or '*')+' '+target+' :No such channel'); continue
     c=db(); member=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],user)).fetchone()
     if not member and not ch['is_private']:
      c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],user)); c.commit()
     elif not member: put(':huddle 473 '+(nick or '*')+' '+target+' :Cannot join'); continue
     client['channel_id']=ch['id']; put(':'+nick+'!'+str(nick)+'@host JOIN '+target)
     names=' '.join(x['username'] for x in c.execute('SELECT u.username FROM channel_members cm JOIN users u ON u.id=cm.user_id WHERE cm.channel_id=?',(ch['id'],)))
     put(':huddle 353 '+nick+' = '+target+' :'+names); put(':huddle 366 '+nick+' '+target+' :End of NAMES list'); c.close()
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
     target=arg.strip().split()[0] if arg.strip() else ('' if cmd=='LIST' else '*'); ch=irc_find_channel(target) if target.startswith('#') else None
     if cmd=='LIST': put(':huddle 322 '+(nick or '*')+' '+target+' :channel'); put(':huddle 323 '+(nick or '*')+' :End of LIST')
     elif ch or client.get('channel_id'):
      target=target or '*'; put(':huddle 323 '+nick+' :OK')
     else: put(':huddle 403 '+nick+' '+target+' :No such channel')
    elif cmd=='PRIVMSG':
     bits=arg.split(' :',1); target=bits[0].strip(); text=bits[1] if len(bits)>1 else ''
     ch=irc_find_channel(target)
     if not ch or client.get('channel_id')!=ch['id']: put(':huddle 442 '+nick+' '+target+' :Not on channel')
     else:
      c=db(); c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],user,text,now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); x=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); out=H.msgobj(H.__new__(H),x); send_events(ch['id'],'message.created',{'message':out}); c.close()
    elif cmd=='PING':
     text=arg[1:] if arg.startswith(':') else arg; put(':huddle PONG huddle :'+text)
    elif cmd=='QUIT':
     closing=True
     try:sock.shutdown(socket.SHUT_RDWR)
     except:pass
     return
    elif cmd in ('PASS','NICK','USER'): pass
    elif cmd not in ('PONG','CAP'):
     put(':huddle 421 '+(nick or '*')+' '+cmd+' :Unknown command')
    if closing: return
    if not registered and isinstance(user,int) and nick and passed:
     registered=True
     for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 oi'),('005','CHANTYPES=# :are supported by this server')]:
      put(':huddle '+code+' '+nick+' :'+text)
     put(':huddle 422 '+nick+' :MOTD File is missing')
  if closing: return
 finally:
  with irc_clients_lock:
   if client in irc_clients: irc_clients.remove(client)
  if nick:
   with irc_lock: irc_nicks.discard(nick)
  try:sock.close()
  except:pass
def irc_server():
 srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen(100); threading.Thread(target=irc_pump,daemon=True).start()
 while True:
  sock,addr=srv.accept(); threading.Thread(target=irc_client,args=(sock,addr),daemon=True).start()

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='irc': irc_server()
 else:
  port=int(sys.argv[1]); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
