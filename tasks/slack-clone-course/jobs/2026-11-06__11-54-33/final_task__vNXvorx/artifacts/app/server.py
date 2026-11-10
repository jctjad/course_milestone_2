import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,struct,urllib.parse,email.parser,email.policy
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
DB='/var/lib/huddle/huddle.db'; DATA='/var/lib/huddle'
os.makedirs(DATA,exist_ok=True)
def db():
 c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row; return c
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 c=db()
 c.executescript('''PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,first_id INTEGER,UNIQUE(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS mf(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,payload TEXT);
 '''); c.commit(); c.close()
def user_json(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chan_json(r): return {k:(bool(r[k]) if k in ('is_private','is_dm','is_archived') else r[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def auth(tok):
 if not tok:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close(); return r
def req_user(h):
 a=h.headers.get('Authorization','')
 if a.startswith('Bearer '): return auth(a[7:])
 q=urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
 return auth(q.get('token',[''])[0])
def getchan(i):
 c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone(); c.close(); return r
def role(w,u):
 c=db(); r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); c.close(); return r['role'] if r else None
def members(ch):
 c=db(); x=c.execute('SELECT u.* FROM users u JOIN cm m ON u.id=m.user_id WHERE m.channel_id=?',(ch,)).fetchall(); c.close(); return [user_json(r) for r in x]
def readable(ch,u):
 c=db(); x=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u)).fetchone(); c.close()
 if ch['is_dm']: return bool(x)
 return bool(role(ch['workspace_id'],u)) and (not ch['is_private'] or bool(x))
def cmember(ch,u):
 c=db();x=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch,u)).fetchone();c.close();return bool(x)
def mentions(body,ch,author):
 c=db(); out=[]; pats=re.findall(r'(?<!\w)@([A-Za-z0-9_-]+)',body)
 for p in pats:
  if p=='channel':
   out += [x['id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(ch['id'],author))]
  elif p=='here':
   ts=time.time()-1.5;out += [x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=? AND user_id IN (SELECT user_id FROM presence WHERE last_seen>?)',(ch['id'],author,ts))]
  else:
   g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],p)).fetchone() if ch['workspace_id'] else None
   if g: out += [int(x) for x in json.loads(g['user_ids']) if int(x)!=author and cmember(ch['id'],int(x))]
   else:
    u=c.execute('SELECT id FROM users WHERE username=?',(p,)).fetchone()
    if u and u['id']!=author and role(ch['workspace_id'],u['id']) and cmember(ch['id'],u['id']): out.append(u['id'])
 c.close(); return list(dict.fromkeys(out))
def reactions(mid):
 c=db(); rows=c.execute('SELECT emoji,user_id,first_id FROM reactions WHERE message_id=? ORDER BY first_id',(mid,)).fetchall();c.close();d={}
 for r in rows:d.setdefault(r['emoji'],[]).append(r['user_id'])
 return [{'emoji':e,'count':len(v),'user_ids':v} for e,v in d.items()]
def filej(r): return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def msgj(r):
 c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); fs=c.execute('SELECT f.* FROM files f JOIN mf ON f.id=mf.file_id WHERE mf.message_id=?',(r['id'],)).fetchall();c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_json(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':countrep(r['id']),'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[filej(x) for x in fs],'reactions':reactions(r['id']),'mentions':json.loads(r['mentions'])}
def countrep(i):
 c=db();n=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(i,)).fetchone()['n'];c.close();return n
def emit(ch,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone(); n=(n['n']+1 if n else 1); c.execute('INSERT OR REPLACE INTO seq VALUES(?,?)',(ch,n));c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(ch,n,kind,json.dumps(payload)));c.commit();c.close();return {'type':kind,'event_id':n,'channel_id':ch,**payload}
class WS:
 all=[]; lock=threading.Lock()
 def __init__(self,h,u):self.h=h;self.u=u;self.ch=set();self.last=0;self.cid=secrets.token_hex(8)
 def send(self,x):
  try:
   b=json.dumps(x).encode(); n=len(b); head=bytes([129])+ (bytes([n]) if n<126 else b'\x7e'+struct.pack('>H',n));self.h.wfile.write(head+b);self.h.wfile.flush()
  except: pass
 def run(self):
  with WS.lock:WS.all.append(self)
  c=db();c.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(self.cid,self.u,time.time()));c.commit();c.close()
  try:
   while True:
    h=self.h.rfile.read(2)
    if not h:break
    n=h[1]&127
    if n==126:n=struct.unpack('>H',self.h.rfile.read(2))[0]
    mask=self.h.rfile.read(4); data=bytearray(self.h.rfile.read(n))
    for i in range(n):data[i]^=mask[i%4]
    x=json.loads(data)
    if x.get('type')=='subscribe':
     self.ch.add(int(x['channel_id']));self.send({'type':'subscribed','channel_id':int(x['channel_id']),'head_event_id':head_event(int(x['channel_id']))})
    elif x.get('type')=='resume':
     ci=int(x['channel_id']); since=int(x.get('since_event_id',0));c=db();es=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ci,since)).fetchall();c.close()
     for e in es:self.send({'type':e['kind'],'event_id':e['event_id'],'channel_id':ci,**json.loads(e['payload'])})
     self.send({'type':'resumed','channel_id':ci,'head_event_id':head_event(ci)})
  finally:
   with WS.lock:
    if self in WS.all:WS.all.remove(self)
   c=db();c.execute('DELETE FROM presence WHERE conn_id=?',(self.cid,));c.commit();c.close()
def head_event(ch):
 c=db();r=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone();c.close();return r['n'] if r else 0
def broadcast(frame):
 with WS.lock:
  for w in list(WS.all):
   if frame.get('type')=='user.updated' or frame.get('channel_id') in w.ch:w.send(frame)
def poll(node):
 seen=0; seen_g=0
 while True:
  try:
   c=db(); es=c.execute('SELECT * FROM events WHERE id>? ORDER BY id',(seen,)).fetchall(); ge=c.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(seen_g,)).fetchall();c.close()
   for e in es: broadcast({'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**json.loads(e['payload'])})
   for e in ge:broadcast(json.loads(e['payload']))
   if es:seen=max(seen,max(e['id'] for e in es))
   if ge:seen_g=max(seen_g,max(e['id'] for e in ge))
  except:pass
  time.sleep(.1)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def out(self,code,obj,headers=None):
  b=obj if isinstance(obj,(bytes,bytearray)) else (json.dumps(obj).encode() if obj is not None else b'');self.send_response(code);self.send_header('Content-Type',(headers or {}).get('Content-Type','application/json'));self.send_header('Content-Length',str(len(b))); 
  for k,v in (headers or {}).items():
   if k.lower()!='content-type': self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return self.rfile.read(n)
 def json(self):
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/api/health':return self.out(200,{'status':'ok','node_id':PORT-8000})
  if path=='/api/ws':
   u=req_user(self)
   if not u:return self.close()
   key=self.headers.get('Sec-WebSocket-Key');accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers();return WS(self,u['id']).run()
  u=req_user(self)
  if path.startswith('/api/') and path not in ('/api/health',) and not u:return self.out(401,{'error':'unauthorized'})
  m=re.match(r'/api/users/(\d+)$',path)
  if m:
   c=db();r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone();c.close();return self.out(200,{'user':user_json(r)}) if r else self.out(404,{})
  if path=='/api/auth/me':return self.out(200,{'user':user_json(u)})
  if path=='/api/workspaces':
   c=db();rs=c.execute('SELECT w.* FROM workspaces w JOIN wm ON w.id=wm.workspace_id WHERE wm.user_id=?',(u['id'],)).fetchall();c.close();return self.out(200,{'workspaces':[dict(r) for r in rs]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); crows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)) if w else [];rows=crows.fetchall() if w else [];c.close()
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   return self.out(200,{'workspace':dict(w),'channels':[chan_json(x) for x in rows]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   ch=getchan(m.group(1))
   if not ch:return self.out(404,{})
   return self.out(200,{'members':members(ch['id'])}) if readable(ch,u['id']) else self.out(403,{})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=getchan(m.group(1))
   if not ch:return self.out(404,{})
   if not readable(ch,u['id']):return self.out(403,{})
   c=db();rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(ch['id'],)).fetchall();c.close();return self.out(200,{'messages':[msgj(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   c=db();r=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone();c.close()
   if not r:return self.out(404,{})
   if m.group(2):return self.out(200,r['data'],{'Content-Type':r['content_type'],'Content-Disposition':'attachment; filename="'+r['filename']+'"'} )
   return self.out(200,{'file':filej(r)})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall() if w else [];c.close()
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   return self.out(200,{'groups':[{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'])} for r in rs]})
  return self.out(404,{})
 def close(self): self.send_response(401);self.send_header('Content-Length','0');self.end_headers()
 def do_POST(self):
  path=urllib.parse.urlparse(self.path).path
  if path=='/api/auth/register':
   d=self.json();un=d.get('username','');pw=d.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.out(400,{})
   c=db()
   try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,d.get('display_name') or un));c.commit(); uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();return self.out(201,{'user':user_json(r),'token':t})
   except sqlite3.IntegrityError:return self.out(409,{})
   finally:c.close()
  d={} if path=='/api/files' else self.json()
  if path=='/api/auth/login':
   c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
   if not r:c.close();return self.out(401,{})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.out(200,{'user':user_json(r),'token':t})
  u=req_user(self)
  if not u:return self.out(401,{})
  if path=='/api/workspaces':
   sl=d.get('slug','');name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',sl):return self.out(400,{})
   c=db()
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(sl,name,u['id']));wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));gid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO cm VALUES(?,?)',(gid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();g=c.execute('SELECT * FROM channels WHERE id=?',(gid,)).fetchone();return self.out(201,{'workspace':dict(w),'general_channel':chan_json(g)})
   except sqlite3.IntegrityError:return self.out(409,{})
   finally:c.close()
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();c.close()
   if not w:return self.out(404,{})
   if role(w['id'],u['id'])!='owner':return self.out(403,{})
   n=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',n):return self.out(400,{})
   c=db()
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],n,int(bool(d.get('is_private'))),d.get('topic','')));i=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO cm VALUES(?,?)',(i,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone();return self.out(201,{'channel':chan_json(r)})
   except sqlite3.IntegrityError:return self.out(409,{})
   finally:c.close()
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=getchan(m.group(1))
   if not ch:return self.out(404,{})
   if ch['is_private']:return self.out(403,{})
   c=db();c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();c.close();return self.out(200,{'channel':chan_json(getchan(ch['id']))})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   ch=getchan(m.group(1))
   if not ch:return self.out(404,{})
   if role(ch['workspace_id'],u['id'])!='owner':return self.out(403,{})
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(m.group(2)=='archive',ch['id']));c.commit();c.close();return self.out(200,{'channel':chan_json(getchan(ch['id']))})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:return self.postmsg(int(m.group(1)),u,d)
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   mid=int(m.group(1));em=d.get('emoji')
   if not em:return self.out(400,{})
   c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close()
   if not r:return self.out(404,{})
   if not readable(getchan(r['channel_id']),u['id']):return self.out(403,{})
   c=db();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?,?)',(mid,u['id'],em,int(time.time()*1000000)));changed=c.total_changes;c.commit();c.close()
   if changed: emit(r['channel_id'],'reaction.added',{'message_id':mid,'emoji':em,'user_id':u['id'],'reactions':reactions(mid)})
   return self.out(200,{'reactions':reactions(mid)})
  if path=='/api/dms':
   other=d.get('user_id');c=db();x=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
   if not x or other==u['id']:c.close();return self.out(400 if other==u['id'] else 404,{})
   a,b=sorted((u['id'],other));name=f'dm-{a}-{b}';r=c.execute('SELECT id FROM channels WHERE name=? AND is_dm=1',(name,)).fetchone()
   if not r:c.execute('INSERT INTO channels(name,is_private,is_dm) VALUES(?,1,1)',(name,));i=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO cm VALUES(?,?)',(i,a));c.execute('INSERT INTO cm VALUES(?,?)',(i,b));c.commit()
   else:i=r['id']
   c.close();return self.out(200,{'channel_id':i})
  if path=='/api/files':
   n=int(self.headers.get('Content-Length','0')); raw=self.body()
   ct=self.headers.get('Content-Type','');mo=re.search(r'boundary=([^;]+)',ct)
   if not mo:return self.out(400,{})
   parts=raw.split(('--'+mo.group(1)).encode())
   target=None
   for part in parts:
    if b'name="file"' in part:
     z=part.split(b'\r\n\r\n',1)
     if len(z)==2:
      hh=z[0].decode('latin1');dat=z[1];dat=dat[:-2] if dat.endswith(b'\r\n') else dat;fn=re.search(r'filename="([^"]*)"',hh); typ=re.search(r'Content-Type:\s*([^\r\n]+)',hh,re.I);target=(fn.group(1) if fn else 'file',typ.group(1) if typ else 'application/octet-stream',dat)
   if not target:return self.out(400,{})
   fn,typ,data=target
   if len(data)>10485760:return self.out(413,{})
   c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data));i=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(i,)).fetchone();c.close();return self.out(201,{'file':filej(r)})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();c.close()
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   h=d.get('handle',''); 
   if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.out(400,{})
   c=db()
   try:c.execute('INSERT INTO groups_(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],h,d.get('name',''),json.dumps(d.get('user_ids',[]))));c.commit();r=c.execute('SELECT * FROM groups_ WHERE id=last_insert_rowid()').fetchone();return self.out(201,{'group':{'id':r['id'],'handle':h,'name':r['name'],'member_user_ids':d.get('user_ids',[])}})
   except sqlite3.IntegrityError:return self.out(409,{})
   finally:c.close()
  return self.out(404,{})
 def postmsg(self,i,u,d):
  ch=getchan(i)
  if not ch:return self.out(404,{})
  if not readable(ch,u['id']):return self.out(403,{})
  body=d.get('body','')
  if not isinstance(body,str) or not body.strip():return self.out(400,{})
  if ch['is_archived'] and body.strip()!='/unarchive':return self.out(423,{})
  if body.startswith('/') and not body.startswith('//'):
   parts=body.split(' ',1);cmd=parts[0];arg=parts[1].strip() if len(parts)>1 else ''
   if cmd=='/me':
    if not arg:return self.out(400,{})
   elif cmd=='/shrug':body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
   elif cmd=='/topic':
    if not arg or (len(arg)>250):return self.out(400,{})
    if not cmember(i,u['id']) and role(ch['workspace_id'],u['id'])!='owner':return self.out(403,{})
    c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,i));c.commit();c.close();return self.out(201,{'message':None,'channel':chan_json(getchan(i))})
   elif cmd=='/invite':
    if not cmember(i,u['id']) or not arg.startswith('@'):return self.out(400 if not arg.startswith('@') else 403,{})
    c=db();x=c.execute('SELECT * FROM users WHERE username=?',(arg[1:].strip(),)).fetchone();c.close()
    if not x:return self.out(404,{})
    if not role(ch['workspace_id'],x['id']):return self.out(403,{})
    c=db();c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(i,x['id']));c.commit();c.close();return self.out(201,{'message':None,'invited_user':user_json(x)})
   elif cmd in ('/archive','/unarchive'):
    if role(ch['workspace_id'],u['id'])!='owner':return self.out(403,{})
    c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(cmd=='/archive',i));c.commit();c.close();return self.out(201,{'message':None,'channel':chan_json(getchan(i))})
   else:return self.out(400,{})
  c=db(); ms=mentions(body,ch,u['id']); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(i,u['id'],body,d.get('parent_id'),now(),json.dumps(ms)));mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']
  for fid in d.get('file_ids',[]):
   f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=?',(fid,u['id'])).fetchone()
   if not f or c.execute('SELECT 1 FROM mf WHERE file_id=?',(fid,)).fetchone():c.rollback();c.close();return self.out(400,{})
   c.execute('INSERT INTO mf VALUES(?,?)',(mid,fid))
  c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close();j=msgj(r);emit(i,'message.created',{'message':j});return self.out(201,{'message':j})
 def do_PATCH(self):
  u=req_user(self)
  if not u:return self.out(401,{})
  p=urllib.parse.urlparse(self.path).path;d=self.json()
  if p=='/api/users/me':
   fields={k:v for k,v in d.items() if k in ('display_name','timezone','avatar_url','status_text','status_emoji')}
   if ('display_name' in fields and (not isinstance(fields['display_name'],str) or not 1<=len(fields['display_name'])<=80)) or ('timezone' in fields and (not isinstance(fields['timezone'],str) or not self.tz(fields['timezone']))):return self.out(400,{})
   lim={'avatar_url':2048,'status_text':100,'status_emoji':64}
   if any(not isinstance(v,str) or len(v)>lim[k] for k,v in fields.items() if k in lim):return self.out(400,{})
   c=db();c.execute('UPDATE users SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[u['id']]);c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.execute('INSERT INTO global_events(payload) VALUES(?)',(json.dumps({'type':'user.updated','user':user_json(r)}),));c.commit();c.close();return self.out(200,{'user':user_json(r)})
  m=re.match(r'/api/channels/(\d+)$',p)
  if m:
   ch=getchan(m.group(1))
   if not ch or not readable(ch,u['id']):return self.out(403 if ch else 404,{})
   if d.get('topic') is None or len(d.get('topic',''))>250:return self.out(400,{})
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id']));c.commit();c.close();return self.out(200,{'channel':chan_json(getchan(ch['id']))})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not w:return self.out(404,{})
   if not g:return self.out(404,{})
   if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.out(403,{})
   c.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['user_ids']))),g['id']));c.commit();r=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone();c.close();return self.out(200,{'group':{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'])}})
  return self.out(404,{})
 def tz(self,x):
  try:ZoneInfo(x);return True
  except:return False
 def do_DELETE(self):
  u=req_user(self)
  if not u:return self.out(401,{})
  p=urllib.parse.urlparse(self.path).path;m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',p)
  if m:
   mid=int(m.group(1));em=urllib.parse.unquote(m.group(2));c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close()
   if not r:return self.out(404,{})
   if not readable(getchan(r['channel_id']),u['id']):return self.out(403,{})
   c=db();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],em));x=c.total_changes;c.commit();c.close()
   if x:emit(r['channel_id'],'reaction.removed',{'message_id':mid,'emoji':em,'user_id':u['id'],'reactions':reactions(mid)})
   return self.out(200,{'reactions':reactions(mid)})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not w or not g:return self.out(200,{'deleted':True})
   if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.out(403,{})
   c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();c.close();return self.out(200,{'deleted':True})
  return self.out(404,{})
if __name__=='__main__':
 PORT=int(sys.argv[1]);init();threading.Thread(target=poll,args=(PORT,),daemon=True).start();ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
