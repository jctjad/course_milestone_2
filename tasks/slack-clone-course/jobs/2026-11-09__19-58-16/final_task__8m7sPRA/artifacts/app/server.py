import sys,os,re,socket,json,sqlite3,secrets,hashlib,hmac,base64,datetime,urllib.parse,threading,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.sqlite3'
NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
def db():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row; return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True); c=db()
 c.executescript('''PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT '',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id)); CREATE TABLE IF NOT EXISTS dms(user_low INTEGER,user_high INTEGER,channel_id INTEGER UNIQUE,PRIMARY KEY(user_low,user_high)); CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE); CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle)); CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT); CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,expires_at TEXT,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);'''); c.close()
def U(r):
 return None if not r else {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def W(r): return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def C(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def user_token(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def auth(h):
 m=re.match(r'Bearer\s+(.+)',h or ''); return user_token(m.group(1)) if m else None
def msg(r):
 c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rx=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
 files=[{'id':z['id'],'uploader_id':z['uploader_id'],'filename':z['filename'],'content_type':z['content_type'],'size':z['size'],'created_at':z['created_at']} for z in c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],))]
 mentions=[]
 for x in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',r['body'] or ''):
  z=c.execute('SELECT u.id FROM users u JOIN wm ON wm.user_id=u.id JOIN channels ch ON ch.workspace_id=wm.workspace_id WHERE u.username=? AND ch.id=?',(x,r['channel_id'])).fetchone()
  if z and z['id']!=r['author_id'] and z['id'] not in mentions: mentions.append(z['id'])
 
 # Expand group handles after ordinary username resolution.
 chrow=c.execute('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
 if chrow and chrow['workspace_id']:
  for handle in re.findall(r'(?<![\\w@])@([a-z0-9][a-z0-9_-]{0,31})',r['body'] or ''):
   gr=c.execute('SELECT user_ids FROM groups WHERE workspace_id=? AND handle=?',(chrow['workspace_id'],handle)).fetchone()
   if gr:
    for gid in json.loads(gr['user_ids'] or '[]'):
     if gid!=r['author_id'] and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(r['channel_id'],gid)).fetchone() and gid not in mentions: mentions.append(gid)
 if '@here' in (r['body'] or ''):
  import time as _time
  for z in c.execute('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(_time.time()-1.5,)):
   if z['user_id']!=r['author_id'] and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(r['channel_id'],z['user_id'])).fetchone() and z['user_id'] not in mentions: mentions.append(z['user_id'])
 if '@channel' in (r['body'] or ''):
  for z in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(r['channel_id'],)):
   if z['user_id']!=r['author_id'] and z['user_id'] not in mentions: mentions.append(z['user_id'])
 c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':U(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':int(r['reply_count']) if 'reply_count' in r.keys() else 0,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files,'reactions':rx,'mentions':mentions}
def jcur(pos): return base64.urlsafe_b64encode(str(pos).encode()).decode()
def pcur(x):
 try:return int(base64.urlsafe_b64decode(x.encode()).decode())
 except: raise ValueError()
def limit(q):
 try:n=int(q.get('limit',['50'])[0])
 except: raise ValueError()
 if n<1 or n>200:raise ValueError()
 return n
def page(a,n,cur,desc=True):
 start=0 if not cur else pcur(cur)
 if start<0 or start>len(a):raise ValueError()
 z=a[start:start+n]; return z,(jcur(start+n) if start+n<len(a) else None)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def out(self,code,obj=None,typ='application/json'):
  b=(json.dumps(obj,separators=(',',':')).encode() if obj is not None else b'')
  self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def reqpath(self):
  u=urllib.parse.urlsplit(self.path); return u.path,urllib.parse.parse_qs(u.query)
 def ws_send(self,obj):
  b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
  if n<126: h=bytes([129,n])
  elif n<65536:h=bytes([129,126])+struct.pack('>H',n)
  else:h=bytes([129,127])+struct.pack('>Q',n)
  self.connection.sendall(h+b)
 def ws_read(self):
  h=self.connection.recv(2)
  if not h:return None
  ln=h[1]&127
  if ln==126:ln=struct.unpack('>H',self.connection.recv(2))[0]
  elif ln==127:ln=struct.unpack('>Q',self.connection.recv(8))[0]
  mask=self.connection.recv(4) if h[1]&128 else b''
  b=self.connection.recv(ln); 
  if mask:b=bytes(x^mask[i%4] for i,x in enumerate(b))
  if h[0]&15==8:return None
  if h[0]&15==9:
   self.connection.sendall(bytes([138,len(b)])+b);return {}
  try:return json.loads(b)
  except:return {}
 def websocket_loop(self,uid,conn_id=None):
  self.connection.settimeout(.15); subs={}
  def gone():
   if conn_id:
    try:
     z=db();z.execute('DELETE FROM presence WHERE conn_id=?',(conn_id,));z.close()
    except: pass
  while True:
   try:
    try:
     x=self.ws_read()
    except socket.timeout:x={}
    if x is None:gone();return
    if x:
     typ=x.get('type'); ch=int(x.get('channel_id',0))
     c=db(); row=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
     allowed=row and (not row['is_private'] or c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone())
     if not allowed:c.close();continue
     if typ=='subscribe':
      head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n'];subs[ch]=head
      self.ws_send({'type':'subscribed','channel_id':ch,'head_event_id':head})
     elif typ=='resume':
      since=int(x.get('since_event_id',0)); rows=c.execute('SELECT event_id,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
      if rows:
       for r in rows:
        f=json.loads(r['payload']); f=self.event_frame(c,f); self.ws_send(f)
       head=rows[-1]['event_id']
      else: head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
      subs[ch]=head;self.ws_send({'type':'resumed','channel_id':ch,'head_event_id':head})
     c.close()
    gc=db(); gr=gc.execute('SELECT id,payload FROM global_events ORDER BY id').fetchall(); gc.close()
    gseen=getattr(self,'global_seen',0)
    for ge in gr:
     if ge['id']>gseen:self.ws_send(json.loads(ge['payload']))
    if gr:self.global_seen=gr[-1]['id']
    for ch,last in list(subs.items()):
     c=db();rows=c.execute('SELECT event_id,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last)).fetchall()
     for r in rows:
      f=self.event_frame(c,json.loads(r['payload']));self.ws_send(f);last=r['event_id']
     subs[ch]=last;c.close()
   except (BrokenPipeError,ConnectionResetError,OSError):gone();return
 def event_frame(self,c,f):
  if f.get('type')=='message.created' and f.get('message_id'):
   r=c.execute('SELECT m.*,(SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE m.id=?',(f['message_id'],)).fetchone()
   if r:f['message']=msg(r);f.pop('message_id',None);f['type']='message.reply' if r['parent_id'] else 'message.created'
  return f
 def do_GET(self):
  p,q=self.reqpath()
  if p=='/api/health':return self.out(200,{'status':'ok','node_id':NODE})
  if p=='/':
   b=open('/app/static/index.html','rb').read();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  if p=='/api/ws':
   tok=q.get('token',[''])[0]
   if tok.startswith('Bearer '): tok=tok[7:]
   who=user_token(tok)
   if not who:return self.out(401,{'error':'unauthorized'})
   key=self.headers.get('Sec-WebSocket-Key')
   if not key:return self.out(400,{'error':'websocket key required'})
   accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
   conn_id=secrets.token_urlsafe(16); pc=db();pc.execute('INSERT OR REPLACE INTO presence(conn_id,user_id,last_seen) VALUES(?,?,?)',(conn_id,who['id'],__import__('time').time()));self.global_seen=pc.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0];pc.close();self.websocket_loop(who['id'],conn_id)
   return
  u=auth(self.headers.get('Authorization'))
  if p=='/api/auth/me':
   return self.out(200,{'user':U(u)}) if u else self.out(401,{'error':'unauthorized'})
  if not u:return self.out(401,{'error':'unauthorized'})
  c=db()
  try:
   m=re.match(r'/api/files/(\d+)(/download)?$',p)
   if m:
    f=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone()
    if not f:return self.out(404,{'error':'not found'})
    if m.group(2):
     self.send_response(200);self.send_header('Content-Type',f['content_type'] or 'application/octet-stream');self.send_header('Content-Disposition','attachment; filename="%s"'%f['filename']);self.send_header('Content-Length',str(f['size']));self.end_headers();self.wfile.write(f['data']);return
    return self.out(200,{'file':{'id':f['id'],'uploader_id':f['uploader_id'],'filename':f['filename'],'content_type':f['content_type'],'size':f['size'],'created_at':f['created_at']}})
   m=re.match(r'/api/users/(\d+)$',p)
   if m:
    x=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone()
    if not x:return self.out(404,{'error':'not found'})
    return self.out(200,{'user':U(x)})
   if p=='/api/workspaces':
    rs=c.execute('SELECT * FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();return self.out(200,{'workspaces':[W(x) for x in rs]})
   m=re.match(r'/api/workspaces/([^/]+)(?:/(members|groups|invitations))?$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.out(404,{'error':'not found'})
    role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not role:return self.out(403,{'error':'forbidden'})
    if m.group(2)=='members':
     rs=c.execute('SELECT * FROM wm WHERE workspace_id=?',(w['id'],)).fetchall();return self.out(200,{'members':[{'user_id':x['user_id'],'role':x['role']} for x in rs]})
    if m.group(2)=='groups':
     rows=c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall()
     return self.out(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'] or '[]')} for x in rows]})
    if m.group(2)=='groups':
     rows=c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall()
     return self.out(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'] or '[]')} for x in rows]})
    if m.group(2)=='invitations':
     rr=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
     if not rr or rr['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
     import datetime as _dt
     rows=c.execute("SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)",(w['id'],now())).fetchall()
     return self.out(200,{'invitations':[{'code':x['code'],'workspace_id':x['workspace_id'],'max_uses':x['max_uses'],'uses':x['uses'],'expires_at':x['expires_at'],'invited_user_id':x['invited_user_id']} for x in rows]})
    chans=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (? OR is_archived=0)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
    chans=[x for x in chans if not x['is_private'] or c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone()]
    states=[]
    for x in chans:
     rr=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone(); last=rr['last_read_event_id'] if rr else 0; unread=mention=0
     for er in c.execute('SELECT payload FROM events WHERE channel_id=? AND event_id>?',(x['id'],last)):
      f=json.loads(er['payload'])
      if f.get('type') in ('message.created','message.reply') and f.get('message_id'):
       mr=c.execute('SELECT author_id,body FROM messages WHERE id=?',(f['message_id'],)).fetchone()
       if mr and mr['author_id']!=u['id']:
        unread+=1
        full=c.execute('SELECT m.*,(SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE m.id=?',(f['message_id'],)).fetchone()
        if u['id'] in msg(full)['mentions']: mention+=1
     states.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mention})
    return self.out(200,{'workspace':W(w),'channels':[C(x) for x in chans],'read_state':states})
   m=re.match(r'/api/channels/(\d+)(?:/(members|messages|pins|read))?$',p)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
    if not ch:return self.out(404,{'error':'not found'})
    member=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    if ch['is_private'] and not member:return self.out(403,{'error':'forbidden'})
    if m.group(2)=='members':
     rs=c.execute('SELECT u.* FROM cm x JOIN users u ON u.id=x.user_id WHERE x.channel_id=?',(ch['id'],)).fetchall();return self.out(200,{'members':[U(x) for x in rs]})
    if m.group(2)=='read':
     if ch['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): return self.out(403,{'error':'forbidden'})
     rr=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone(); last=rr['last_read_event_id'] if rr else 0
     evs=c.execute('SELECT payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch['id'],last)).fetchall(); unread=mention=0
     for er in evs:
      f=json.loads(er['payload'])
      if f.get('type') in ('message.created','message.reply') and f.get('message_id'):
       mr=c.execute('SELECT author_id,body FROM messages WHERE id=?',(f['message_id'],)).fetchone()
       if mr and mr['author_id']!=u['id']:
        unread+=1
        full=c.execute('SELECT m.*,(SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE m.id=?',(f['message_id'],)).fetchone()
        if u['id'] in msg(full)['mentions']: mention+=1
     return self.out(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mention}})
    if m.group(2)=='messages':
     n=limit(q); cur=q.get('cursor',[None])[0]
     if cur:
      try: anchor=pcur(cur)
      except: return self.out(400,{'error':'invalid pagination'})
      rs=c.execute('SELECT m.*, (SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE m.channel_id=? AND m.deleted=0 AND m.parent_id IS NULL AND m.id<? ORDER BY m.id DESC',(ch['id'],anchor)).fetchall()
     else: rs=c.execute('SELECT m.*, (SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE m.channel_id=? AND m.deleted=0 AND m.parent_id IS NULL ORDER BY m.id DESC',(ch['id'],)).fetchall()
     z=rs[:n]; nx=jcur(z[-1]['id']) if len(rs)>n and z else None;return self.out(200,{'messages':[msg(x) for x in z],'next_cursor':nx})
    if m.group(2)=='pins':
     n=limit(q);rs=c.execute('SELECT p.*,m.*,p.pinned_by pb,p.pinned_at pa FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(ch['id'],)).fetchall();z,nx=page(rs,n,q.get('cursor',[None])[0]);return self.out(200,{'pins':[{'message':msg(x),'pinned_by':x['pb'],'pinned_at':x['pa']} for x in z],'next_cursor':nx})
    if m.group(2)=='read':
     r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone(); return self.out(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':r['last_read_event_id'] if r else 0,'unread_count':0,'mention_count':0}})
   m=re.match(r'/api/messages/(\d+)/replies$',p)
   if m:
    par=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
    if not par:return self.out(404,{'error':'not found'})
    n=limit(q);rs=c.execute('SELECT m.*,(SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE parent_id=? AND deleted=0 ORDER BY id',(par['id'],)).fetchall();z,nx=page(rs,n,q.get('cursor',[None])[0]);return self.out(200,{'replies':[msg(x) for x in z],'next_cursor':nx})
   if p=='/api/search':
    n=limit(q);term='%'+q.get('q',[''])[0].lower()+'%'; ws=q.get('workspace_id',[None])[0]; sql='SELECT m.*,(SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE ? AND (ch.is_private=0 OR EXISTS (SELECT 1 FROM cm WHERE cm.channel_id=ch.id AND cm.user_id=?))'; args=[term,u['id']]
    if ws is not None: sql+=' AND ch.workspace_id=?';args.append(ws)
    sql+=' AND EXISTS (SELECT 1 FROM wm WHERE wm.workspace_id=ch.workspace_id AND wm.user_id=?) ORDER BY m.id DESC';args.append(u['id']);rs=c.execute(sql,args).fetchall();z,nx=page(rs,n,q.get('cursor',[None])[0]);return self.out(200,{'results':[msg(x) for x in z],'next_cursor':nx})
  except ValueError:return self.out(400,{'error':'invalid pagination'})
  finally:c.close()
 def do_POST(self):
  p,q=self.reqpath()
  if p=='/api/auth/register':
   try:b=self.body(); un=b['username']; pw=b['password']
   except:return self.out(400,{'error':'invalid'})
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8 or 'email' in b:return self.out(400,{'error':'invalid'})
   c=db()
   try:
    c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un))
    uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    return self.out(201,{'user':U(r),'token':t})
   except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
   finally:c.close()
  if p=='/api/auth/login':
   b=self.body();c=db();r=c.execute('SELECT * FROM users WHERE username=?',(b.get('username'),)).fetchone()
   if not r or not hmac.compare_digest(r['password'],hashlib.sha256(str(b.get('password','')).encode()).hexdigest()):c.close();return self.out(401,{'error':'unauthorized'})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.close();return self.out(200,{'user':U(r),'token':t})
  u=auth(self.headers.get('Authorization'))
  if not u:return self.out(401,{'error':'unauthorized'})
  if p=='/api/files':
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.out(400,{'error':'file required'})
   n=int(self.headers.get('Content-Length','0'))
   if n>10485760+1048576:return self.out(413,{'error':'too large'})
   raw=self.rfile.read(n); import re as _re
   bm=_re.search(r'boundary=([^;]+)',ct)
   if not bm:return self.out(400,{'error':'file required'})
   boundary=('--'+bm.group(1).strip('"')).encode(); parts=raw.split(boundary); found=None
   for part in parts:
    if b'filename=' not in part:continue
    head,data=part.split(b'\r\n\r\n',1);data=data.rsplit(b'\r\n',1)[0]; found=(head,data);break
   if not found:return self.out(400,{'error':'file required'})
   head,data=found; fn=_re.search(br'filename="([^"]*)"',head); tm=_re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I); filename=(fn.group(1) if fn else b'file').decode(errors='replace');ctype=(tm.group(1).decode() if tm else 'application/octet-stream').strip()
   if len(data)>10485760:return self.out(413,{'error':'too large'})
   c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],filename,ctype,len(data),now(),data));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close()
   return self.out(201,{'file':{'id':f['id'],'uploader_id':f['uploader_id'],'filename':f['filename'],'content_type':f['content_type'],'size':f['size'],'created_at':f['created_at']}})
  b=self.body();c=db()
  try:
   if p=='/api/dms':
    other=c.execute('SELECT id FROM users WHERE id=?',(b.get('user_id'),)).fetchone()
    if not other:return self.out(404,{'error':'not found'})
    if other['id']==u['id']:return self.out(400,{'error':'self dm'})
    lo,hi=sorted((u['id'],other['id']));x=c.execute('SELECT channel_id FROM dms WHERE user_low=? AND user_high=?',(lo,hi)).fetchone()
    if x:return self.out(200,{'channel_id':x['channel_id']})
    c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)',(f'dm-{lo}-{hi}',1));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO dms VALUES(?,?,?)',(lo,hi,cid));c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO cm VALUES(?,?)',(cid,other['id']));return self.out(200,{'channel_id':cid})
   m=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.out(404,{'error':'not found'})
    rr=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not rr or rr['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
    target=b.get('invited_user_id')
    if b.get('invited_username'):
     z=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone()
     if not z:return self.out(400,{'error':'unknown user'})
     target=z['id']
    mx=int(b.get('max_uses',1) or 1); mx=1 if target else max(1,mx)
    exp=None
    if b.get('expires_in_seconds') is not None: exp=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=int(b['expires_in_seconds']))).isoformat(timespec='milliseconds').replace('+00:00','Z')
    code=secrets.token_urlsafe(18)
    c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],mx,0,exp,target))
    return self.out(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'uses':0,'expires_at':exp,'invited_user_id':target}})
   m=re.match(r'/api/invitations/([^/]+)/accept$',p)
   if m:
    x=c.execute('SELECT * FROM invitations WHERE code=?',(m.group(1),)).fetchone()
    if not x or x['revoked'] or x['uses']>=x['max_uses'] or (x['expires_at'] and x['expires_at']<=now()):return self.out(404,{'error':'not found'})
    if x['invited_user_id'] is not None and x['invited_user_id']!=u['id']:return self.out(403,{'error':'forbidden'})
    c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(x['code'],))
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone(); role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()['role']
    return self.out(200,{'workspace':W(w),'member':{'user_id':u['id'],'role':role}})
   m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return self.out(404,{'error':'not found'})
    if not role:return self.out(403,{'error':'forbidden'})
    h=b.get('handle','')
    if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel') or not isinstance(b.get('name'),str):return self.out(400,{'error':'invalid handle'})
    try:c.execute('INSERT INTO groups(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],h,b['name'],json.dumps(b.get('user_ids') or [])))
    except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
    gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];x=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone()
    return self.out(201,{'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'] or '[]')}})
   if p=='/api/workspaces':
    if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return self.out(400,{'error':'invalid'})
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']))
    except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
    wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return self.out(201,{'workspace':W(w),'general_channel':C(ch)})
   m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return self.out(404,{'error':'not found'})
    if not r or (b.get('is_private',False) and r['role'] not in ('owner','admin')) or (r['role']=='guest'):return self.out(403,{'error':'forbidden'})
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',b.get('name','')) or len(b['name'])>32 or len(b.get('topic',''))>250:return self.out(400,{'error':'invalid'})
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')))
    except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
    cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));return self.out(201,{'channel':C(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   m=re.match(r'/api/channels/(\d+)/join$',p)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
    if not ch:return self.out(404,{'error':'not found'})
    already=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    if ch['is_private'] and not already:return self.out(403,{'error':'forbidden'})
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not r:
     if ch['is_private'] or w['join_mode']!='open':return self.out(403,{'error':'forbidden'})
     c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(w['id'],u['id'],'member'))
    if r and r['role']=='guest':return self.out(403,{'error':'forbidden'})
    existed=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(); c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));
    if not existed:
     ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n'];c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch['id'],ev,json.dumps({'type':'member.joined','event_id':ev,'channel_id':ch['id'],'user_id':u['id']})))
    return self.out(200,{'channel':C(ch)})
   m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    t=c.execute('SELECT * FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],m.group(2))).fetchone() if w else None
    a=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    role=b.get('role')
    if not w or not t:return self.out(404,{'error':'not found'})
    if role not in ('owner','admin','member','guest'):return self.out(400,{'error':'invalid role'})
    if role=='owner':return self.out(400,{'error':'invalid role'})
    if t['role']=='owner' or not a or a['role'] not in ('owner','admin') or (a['role']=='admin' and t['role']=='admin'):return self.out(403,{'error':'forbidden'})
    c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],t['user_id']))
    return self.out(200,{'member':{'user_id':t['user_id'],'role':role}})
   m=re.match(r'/api/channels/(\d+)/(leave|archive|unarchive)$',p)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
    if not ch:return self.out(404,{'error':'not found'})
    if m.group(2)=='leave':
     existed=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone();c.execute('DELETE FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id']));
     if existed:
      ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n'];c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch['id'],ev,json.dumps({'type':'member.left','event_id':ev,'channel_id':ch['id'],'user_id':u['id']})))
     return self.out(200,{'channel':C(ch)})
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); a=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not a or a['role']!='owner':return self.out(403,{'error':'forbidden'})
    val=1 if m.group(2)=='archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id'])); fresh=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n'];c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch['id'],ev,json.dumps({'type':'channel.archived' if val else 'channel.unarchived','event_id':ev,'channel_id':ch['id'],'channel':C(fresh)})));return self.out(200,{'channel':C(fresh)})
   m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); a=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None; t=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],b.get('new_owner_id'))).fetchone() if w else None
    if not w:return self.out(404,{'error':'not found'})
    if not a or a['role']!='owner':return self.out(403,{'error':'forbidden'})
    if not t or t['role']!='admin':return self.out(400,{'error':'target must be admin'})
    c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']));c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b['new_owner_id']));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(b['new_owner_id'],w['id']));return self.out(200,{'workspace':W(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   m=re.match(r'/api/channels/(\d+)/read$',p)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
    if not ch:return self.out(404,{'error':'not found'})
    if ch['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return self.out(403,{'error':'forbidden'})
    old=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone(); cur=old['last_read_event_id'] if old else 0; val=max(cur,int(b.get('last_read_event_id',0)))
    c.execute('INSERT INTO reads(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(u['id'],ch['id'],val))
    return self.out(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':val,'unread_count':0,'mention_count':0}})
   m=re.match(r'/api/messages/(\d+)/reactions$',p)
   if m:
    r=c.execute('SELECT m.*,ch.is_private,ch.id channel_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(m.group(1),)).fetchone()
    if not r:return self.out(404,{'error':'not found'})
    if r['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return self.out(403,{'error':'forbidden'})
    emoji=b.get('emoji')
    if not isinstance(emoji,str) or not emoji:return self.out(400,{'error':'invalid emoji'})
    before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji)).fetchone()
    c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(r['id'],u['id'],emoji))
    rx=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(r['id'],))]
    if not before:
     ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']
     c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(r['channel_id'],ev,json.dumps({'type':'reaction.added','event_id':ev,'channel_id':r['channel_id'],'message_id':r['id'],'emoji':emoji,'user_id':u['id'],'reactions':rx})))
    return self.out(200,{'reactions':rx})
   m=re.match(r'/api/channels/(\d+)/messages$',p)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
    if not ch:return self.out(404,{'error':'not found'})
    if not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return self.out(403,{'error':'forbidden'})
    body=b.get('body'); 
    if not isinstance(body,str) or not body.strip():return self.out(400,{'error':'empty'})
    if body.startswith('/') and not body.startswith('//'):
     parts=body[1:].split(None,1); cmd=parts[0].lower() if parts else ''; arg=parts[1].strip() if len(parts)>1 else ''
     if cmd=='me':
      if not arg:return self.out(400,{'error':'invalid command'})
      body=body
     elif cmd=='shrug':
      body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
     elif cmd=='topic':
      if not arg or len(arg)>250:return self.out(400,{'error':'invalid topic'})
      owner=c.execute("SELECT 1 FROM wm WHERE workspace_id=? AND user_id=? AND role='owner'",(ch['workspace_id'],u['id'])).fetchone()
      member=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
      if not member and not owner:return self.out(403,{'error':'forbidden'})
      c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id'])); fresh=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n'];c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch['id'],ev,json.dumps({'type':'channel.updated','event_id':ev,'channel_id':ch['id'],'channel':C(fresh)})));return self.out(201,{'message':None,'channel':C(fresh)})
     elif cmd=='invite':
      if not arg.startswith('@') or len(arg)<2:return self.out(400,{'error':'invalid invite'})
      target=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
      if not target:return self.out(404,{'error':'not found'})
      if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],target['id'])).fetchone():return self.out(403,{'error':'forbidden'})
      c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],target['id']));return self.out(201,{'message':None,'invited_user':U(target)})
     elif cmd in ('archive','unarchive'):
      owner=c.execute("SELECT 1 FROM wm WHERE workspace_id=? AND user_id=? AND role='owner'",(ch['workspace_id'],u['id'])).fetchone()
      if not owner:return self.out(403,{'error':'forbidden'})
      val=1 if cmd=='archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));fresh=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n'];c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch['id'],ev,json.dumps({'type':'channel.archived' if val else 'channel.unarchived','event_id':ev,'channel_id':ch['id'],'channel':C(fresh)})));return self.out(201,{'message':None,'channel':C(fresh)})
     else:return self.out(400,{'error':'unknown command'})
    if ch['is_archived']:return self.out(423,{'error':'archived'})
    fids=b.get('file_ids') or []; valid=[]
    for fid in fids:
     f=c.execute('SELECT id FROM files WHERE id=? AND uploader_id=?',(fid,u['id'])).fetchone()
     used=c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone()
     if not f or used:return self.out(400,{'error':'invalid file'})
     valid.append(fid)
    par=b.get('parent_id');at=now();c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,par,at));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']; kind='message.reply' if par else 'message.created'; c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch['id'],ev,json.dumps({'type':kind,'event_id':ev,'channel_id':ch['id'],'message_id':mid})));c.executemany('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',[(mid,x) for x in valid]);c.commit();r=c.execute('SELECT m.*,(SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE m.id=?',(mid,)).fetchone();return self.out(201,{'message':msg(r)})
   m=re.match(r'/api/messages/(\d+)/pin$',p)
   if m:
    r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(m.group(1),)).fetchone()
    if not r:return self.out(404,{'error':'not found'})
    if not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return self.out(403,{'error':'forbidden'})
    c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],now()));x=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone();return self.out(200,{'pin':{'message_id':x['message_id'],'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}})
  finally:c.close()
 def do_DELETE(self):
  p,q=self.reqpath();u=auth(self.headers.get('Authorization'))
  if not u:return self.out(401,{'error':'unauthorized'})
  c=db()
  try:
   m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if not w or not g:return self.out(200,{'deleted':True})
    rr=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not rr or (g['creator_id']!=u['id'] and rr['role']!='owner'):return self.out(403,{'error':'forbidden'})
    c.execute('DELETE FROM groups WHERE id=?',(g['id'],));return self.out(200,{'deleted':True})
   m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); x=c.execute('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(m.group(2),w['id'] if w else -1)).fetchone()
    if not w or not x:return self.out(404,{'error':'not found'})
    rr=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not rr or rr['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
    c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(x['code'],));return self.out(200,{'deleted':True})
   m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',p)
   if m:
    mid=int(m.group(1)); emoji=urllib.parse.unquote(m.group(2))
    r=c.execute('SELECT m.*,ch.is_private,ch.id channel_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(mid,)).fetchone()
    if not r:return self.out(404,{'error':'not found'})
    if r['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return self.out(403,{'error':'forbidden'})
    if not emoji:return self.out(400,{'error':'invalid emoji'})
    existed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
    rx=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(mid,))]
    if existed:
     ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']
     c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(r['channel_id'],ev,json.dumps({'type':'reaction.removed','event_id':ev,'channel_id':r['channel_id'],'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':rx})))
    return self.out(200,{'reactions':rx})
   m=re.match(r'/api/messages/(\d+)/pin$',p)
   if m:c.execute('DELETE FROM pins WHERE message_id=?',(m.group(1),));return self.out(200,{'unpinned':True})
   m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    t=c.execute('SELECT * FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],m.group(2))).fetchone() if w else None
    a=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    role=b.get('role')
    if not w or not t:return self.out(404,{'error':'not found'})
    if role not in ('owner','admin','member','guest'):return self.out(400,{'error':'invalid role'})
    if role=='owner':return self.out(400,{'error':'invalid role'})
    if t['role']=='owner' or not a or a['role'] not in ('owner','admin') or (a['role']=='admin' and t['role']=='admin'):return self.out(403,{'error':'forbidden'})
    c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],t['user_id']))
    return self.out(200,{'member':{'user_id':t['user_id'],'role':role}})
   m=re.match(r'/api/messages/(\d+)$',p)
   if m:
    r=c.execute('SELECT m.*,ch.workspace_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(m.group(1),)).fetchone()
    if not r:return self.out(404,{'error':'not found'})
    owner=c.execute("SELECT 1 FROM wm WHERE workspace_id=? AND user_id=? AND role='owner'",(r['workspace_id'],u['id'])).fetchone()
    if r['author_id']!=u['id'] and not owner:return self.out(403,{'error':'forbidden'})
    if not r['deleted']:
     c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],))
     ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']
     c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(r['channel_id'],ev,json.dumps({'type':'message.deleted','event_id':ev,'channel_id':r['channel_id'],'message_id':r['id']})))
    return self.out(200,{'deleted':True})
  finally:c.close()
 def do_PATCH(self):
  p,q=self.reqpath();u=auth(self.headers.get('Authorization'))
  if not u:return self.out(401,{'error':'unauthorized'})
  b=self.body();c=db()
  try:
   m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if not w or not g:return self.out(404,{'error':'not found'})
    rr=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not rr or (g['creator_id']!=u['id'] and rr['role']!='owner'):return self.out(403,{'error':'forbidden'})
    c.execute('UPDATE groups SET name=?,user_ids=? WHERE id=?',(b.get('name',g['name']),json.dumps(b.get('user_ids',json.loads(g['user_ids'] or '[]'))),g['id']))
    x=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone()
    return self.out(200,{'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])}})
   if p=='/api/users/me':
    import zoneinfo
    allowed={'display_name':(str,1,80),'timezone':(str,1,128),'avatar_url':(str,0,2048),'status_text':(str,0,100),'status_emoji':(str,0,64)}
    for k,v in b.items():
     if k not in allowed:continue
     typ,lo,hi=allowed[k]
     if not isinstance(v,typ) or len(v)<lo or len(v)>hi:return self.out(400,{'error':'invalid profile'})
     if k=='timezone':
      try:zoneinfo.ZoneInfo(v)
      except: return self.out(400,{'error':'invalid timezone'})
    fs=[k+'=?' for k in b if k in allowed]; vals=[b[k] for k in b if k in allowed]
    if fs:c.execute('UPDATE users SET '+','.join(fs)+' WHERE id=?',(*vals,u['id']))
    x=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); frame={'type':'user.updated','user':U(x)};c.execute('INSERT INTO global_events(payload) VALUES(?)',(json.dumps(frame),))
    return self.out(200,{'user':U(x)})
   m=re.match(r'/api/workspaces/([^/]+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); a=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return self.out(404,{'error':'not found'})
    if not a or a['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
    if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.out(400,{'error':'invalid'})
    fs=[];vs=[]
    for k in ('name','join_mode'):
     if k in b:fs.append(k+'=?');vs.append(b[k])
    if fs:c.execute('UPDATE workspaces SET '+','.join(fs)+' WHERE id=?',(*vs,w['id']))
    return self.out(200,{'workspace':W(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)

   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    t=c.execute('SELECT * FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],m.group(2))).fetchone() if w else None
    a=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    role=b.get('role')
    if not w or not t:return self.out(404,{'error':'not found'})
    if role not in ('owner','admin','member','guest'):return self.out(400,{'error':'invalid role'})
    if role=='owner':return self.out(400,{'error':'invalid role'})
    if t['role']=='owner' or not a or a['role'] not in ('owner','admin') or (a['role']=='admin' and t['role']=='admin'):return self.out(403,{'error':'forbidden'})
    c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],t['user_id']))
    return self.out(200,{'member':{'user_id':t['user_id'],'role':role}})
   m=re.match(r'/api/messages/(\d+)$',p)
   if m:
    r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
    if not r:return self.out(404,{'error':'not found'})
    if r['author_id']!=u['id']:return self.out(403,{'error':'forbidden'})
    if r['deleted']:return self.out(409,{'error':'deleted'})
    if not b.get('body','').strip():return self.out(400,{'error':'empty'})
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),r['id']))
    ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']
    c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(r['channel_id'],ev,json.dumps({'type':'message.updated','event_id':ev,'channel_id':r['channel_id'],'message_id':r['id']})))
    r=c.execute('SELECT m.*,(SELECT COUNT(*) FROM messages z WHERE z.parent_id=m.id AND z.deleted=0) reply_count FROM messages m WHERE m.id=?',(r['id'],)).fetchone();return self.out(200,{'message':msg(r)})
   m=re.match(r'/api/channels/(\d+)$',p)
   if m:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
    if not ch:return self.out(404,{'error':'not found'})
    if ch['is_archived']:return self.out(423,{'error':'archived'})
    if len(b.get('topic',''))>250:return self.out(400,{'error':'invalid'})
    wrole=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone(); cmem=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    if not cmem and not (wrole and wrole['role']=='owner'):return self.out(403,{'error':'forbidden'})
    c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),ch['id']));fresh=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n'];c.execute('INSERT INTO events(channel_id,event_id,payload) VALUES(?,?,?)',(ch['id'],ev,json.dumps({'type':'channel.updated','event_id':ev,'channel_id':ch['id'],'channel':C(fresh)})));return self.out(200,{'channel':C(fresh)})
  finally:c.close()
def run():
 init();ThreadingHTTPServer(('127.0.0.1',8000+NODE),H).serve_forever()
if __name__=='__main__':run()
