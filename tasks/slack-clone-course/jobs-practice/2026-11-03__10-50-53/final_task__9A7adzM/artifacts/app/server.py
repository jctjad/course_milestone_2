import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,time,threading,datetime,urllib.parse,email.parser,email.policy
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

DB='/app/huddle.sqlite'; PORT=int(sys.argv[1]) if len(sys.argv)>1 else 8000; NODE=PORT-8000
conns=[]; conlock=threading.Lock()
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS wm(workspace_id,user_id,role,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0, UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id,user_id,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT);
CREATE TABLE IF NOT EXISTS mf(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(channel_id,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS reactions(message_id,user_id,emoji TEXT,first_id INTEGER PRIMARY KEY AUTOINCREMENT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,data BLOB,created_at TEXT,attached INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
CREATE TABLE IF NOT EXISTS globals(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);
'''); c.close()
def userjson(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def tok(h): return (h.get('Authorization','').split(' ',1)[1] if h.get('Authorization','').startswith('Bearer ') else None)
def chan(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def getch(i):
 c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone(); c.close(); return r
def member(cid,uid):
 c=db(); x=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.close(); return bool(x)
def role(w,u):
 c=db(); x=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); c.close(); return x['role'] if x else None
def readable(ch,u):
 return bool(ch and ((not ch['is_private'] and (ch['workspace_id'] is None or role(ch['workspace_id'],u))) or member(ch['id'],u)))
def reactions(mid):
 c=db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY first_id',(mid,)).fetchall(); c.close()
 out=[]
 for r in rows:
  x=next((z for z in out if z['emoji']==r['emoji']),None)
  if not x: x={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(x)
  x['user_ids'].append(r['user_id']);x['count']+=1
 return out
def filej(r): return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')} if 'size' in r.keys() else {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':len(r['data']),'created_at':r['created_at']}
def msgj(r):
 c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); fs=[filej(x) for x in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,length(f.data) size,f.created_at FROM files f JOIN mf ON f.id=mf.file_id WHERE mf.message_id=?',(r['id'],)).fetchall()]; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userjson(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':0,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions(r['id']),'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def mentions(body,cid,author):
 c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); ids=[]
 for m in re.finditer(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  n=m.group(1)
  if n=='channel':
   ids += [x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(cid,author))]
  elif n=='here':
   ids += [x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(cid,author)) if online(x['user_id'])]
  else:
   g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],n)).fetchone() if ch['workspace_id'] else None
   if g: ids += [int(x) for x in json.loads(g['user_ids']) if x!=author and member(cid,int(x))]
   else:
    u=c.execute('SELECT id FROM users WHERE username=?',(n,)).fetchone()
    if u and u['id']!=author and ch['workspace_id'] and role(ch['workspace_id'],u['id']) and member(cid,u['id']): ids.append(u['id'])
 c.close(); return list(dict.fromkeys(ids))
def online(uid):
 c=db(); x=c.execute('SELECT 1 FROM presence WHERE user_id=? AND last_seen>?',(uid,time.time()-1.5)).fetchone();c.close();return bool(x)
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload))); c.commit();c.close();return n
def broadcast_global(x):
 c=db();c.execute('INSERT INTO globals(payload) VALUES(?)',(json.dumps(x),));c.commit();c.close()
def ws_send(s,x):
 b=json.dumps(x).encode(); n=len(b); h=bytes([0x81])+((bytes([n]) if n<126 else bytes([126])+struct.pack('>H',n))); 
 try:s.sendall(h+b)
 except:pass
def poll():
 last=0; gl=0
 while True:
  try:
   c=db(); es=c.execute('SELECT rowid,* FROM events WHERE rowid>? ORDER BY rowid',(last,)).fetchall(); gs=c.execute('SELECT * FROM globals WHERE id>? ORDER BY id',(gl,)).fetchall(); c.close()
   for e in es:
    last=e['rowid']; frame={'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**json.loads(e['payload'])}
    with conlock:
     for s,u,subs in conns[:]:
      if e['channel_id'] in subs and e['event_id'] > subs[e['channel_id']]: ws_send(s,frame); subs[e['channel_id']]=e['event_id']
   for g in gs:
    gl=g['id']
    with conlock:
     for s,u,subs in conns[:]: ws_send(s,json.loads(g['payload']))
  except: pass
  time.sleep(.1)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def send(self,status,obj=None,headers={}):
  b=(json.dumps(obj).encode() if obj is not None else b''); self.send_response(status)
  for k,v in headers.items(): self.send_header(k,v)
  self.send_header('Content-Length',str(len(b))); self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
 def jbody(self):
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def do_GET(self):
  p=urllib.parse.urlsplit(self.path); q=urllib.parse.parse_qs(p.query); path=p.path
  if path=='/api/health':return self.send(200,{'status':'ok','node_id':NODE})
  u=auth(tok(self.headers))
  if path.startswith('/api/ws'):
   if not u: u=auth(urllib.parse.parse_qs(p.query).get('token',[''])[0])
   return self.websocket(u)
  if not u:return self.send(401,{'error':'unauthorized'})
  c=db()
  try:
   if path=='/api/auth/me':return self.send(200,{'user':userjson(u)})
   m=re.match(r'/api/users/(\d+)$',path)
   if m:
    x=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone(); return self.send(200,{'user':userjson(x)}) if x else self.send(404,{})
   m=re.match(r'/api/workspaces/([^/]+)$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.send(404,{})
    if not role(w['id'],u['id']):return self.send(403,{})
    cs=[chan(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))',(w['id'],u['id']))]
    return self.send(200,{'workspace':dict(w),'channels':cs})
   m=re.match(r'/api/channels/(\d+)/members$',path)
   if m:
    if not readable(getch(m.group(1)),u['id']):return self.send(403,{})
    ids=[x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? ORDER BY user_id',(int(m.group(1)),)).fetchall()]; return self.send(200,{'members':[userjson(c.execute('SELECT * FROM users WHERE id=?',(i,)).fetchone()) for i in ids]})
   m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
   if m:
    ch=getch(m.group(1))
    if not ch:return self.send(404,{})
    if not ch['workspace_id'] or role(ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if m.group(2)=='archive' else 0,ch['id']));c.commit()
    return self.send(200,{'channel':chan(getch(ch['id']))})
   m=re.match(r'/api/channels/(\d+)/messages$',path)
   if m:
    ch=getch(m.group(1))
    if not readable(ch,u['id']):return self.send(403,{})
    rs=c.execute('SELECT * FROM messages WHERE channel_id=? ORDER BY id DESC',(ch['id'],)).fetchall()
    return self.send(200,{'messages':[msgj(x) for x in rs],'next_cursor':None})
   m=re.match(r'/api/files/(\d+)(/download)?$',path)
   if m:
    f=c.execute('SELECT *,length(data) size FROM files WHERE id=?',(m.group(1),)).fetchone()
    if not f:return self.send(404,{})
    if m.group(2): return self.send_raw(200,f['data'],{'Content-Type':f['content_type'],'Content-Disposition':'attachment; filename="'+f['filename']+'"'})
    return self.send(200,{'file':filej(f)})
   m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.send(404,{})
    if not role(w['id'],u['id']):return self.send(403,{})
    return self.send(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]})
  finally:c.close()
  self.send(404,{})
 def send_raw(self,s,b,h={}):
  self.send_response(s)
  for k,v in h.items():self.send_header(k,v)
  self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_POST(self):
  path=urllib.parse.urlsplit(self.path).path; u=auth(tok(self.headers)); d={} if path=='/api/files' else self.jbody()
  if path=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(d.get('username',''))) or len(str(d.get('password','')))<8:return self.send(400,{})
   c=db()
   try:
    c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(d['username'],hashlib.sha256(d['password'].encode()).hexdigest(),d.get('display_name',d['username']),'UTC','','','')); uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();return self.send(201,{'user':userjson(r),'token':t})
   except sqlite3.IntegrityError:return self.send(409,{})
   finally:c.close()
  if path=='/api/auth/login':
   c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(str(d.get('password','')).encode()).hexdigest())).fetchone()
   if not r:c.close();return self.send(401,{})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.send(200,{'user':userjson(r),'token':t})
  if not u:return self.send(401,{})
  c=db()
  try:
   if path=='/api/workspaces':
    if not re.fullmatch(r'[a-z0-9-]{2,32}',str(d.get('slug',''))):return self.send(400,{})
    try:
     c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,0)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return self.send(201,{'workspace':dict(w),'general_channel':chan(ch)})
    except sqlite3.IntegrityError:return self.send(409,{})
   m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.send(404,{})
    if role(w['id'],u['id'])!='owner' and d.get('is_private'):return self.send(403,{})
    if not role(w['id'],u['id']):return self.send(403,{})
    if not re.fullmatch(r'[a-z0-9-]{1,32}',str(d.get('name',''))):return self.send(400,{})
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();return self.send(201,{'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    except sqlite3.IntegrityError:return self.send(409,{})
   m=re.match(r'/api/channels/(\d+)/join$',path)
   if m:
    ch=getch(m.group(1))
    if not ch:return self.send(404,{})
    if ch['is_private']:return self.send(403,{})
    if ch['workspace_id'] and not role(ch['workspace_id'],u['id']):c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
    c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();return self.send(200,{'channel':chan(getch(ch['id']))})
   m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
   if m:
    ch=getch(m.group(1))
    if not ch:return self.send(404,{})
    if not ch['workspace_id'] or role(ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if m.group(2)=='archive' else 0,ch['id']));c.commit()
    return self.send(200,{'channel':chan(getch(ch['id']))})
   m=re.match(r'/api/channels/(\d+)/messages$',path)
   if m:
    ch=getch(m.group(1))
    if not readable(ch,u['id']):return self.send(403,{})
    body=str(d.get('body',''))
    if ch['is_archived'] and not body.startswith('/unarchive'):return self.send(423,{})
    if not body.strip():return self.send(400,{})
    if body.startswith('/') and not body.startswith('//'):
     parts=body.split(' ',1);cmd=parts[0];arg=parts[1].strip() if len(parts)>1 else ''
     if cmd=='/me':
      if not arg:return self.send(400,{})
     elif cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
     elif cmd=='/topic':
      if not arg or len(arg)>250 or (not member(ch['id'],u['id']) and role(ch['workspace_id'],u['id'])!='owner'):return self.send(403 if not member(ch['id'],u['id']) else 400,{})
      c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));c.commit();return self.send(201,{'message':None,'channel':chan(getch(ch['id']))})
     elif cmd=='/invite':
      if not member(ch['id'],u['id']) or not arg.startswith('@'):return self.send(400 if not arg.startswith('@') else 403,{})
      x=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
      if not x:return self.send(404,{})
      if not role(ch['workspace_id'],x['id']):return self.send(403,{})
      c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],x['id']));c.commit();return self.send(201,{'message':None,'invited_user':userjson(x)})
     elif cmd in ('/archive','/unarchive'):
      if role(ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
      c.execute('UPDATE channels SET is_archived=? WHERE id=?',(cmd=='/archive',ch['id']));c.commit();return self.send(201,{'message':None,'channel':chan(getch(ch['id']))})
     else:return self.send(400,{})
    fids=d.get('file_ids',[])
    if not isinstance(fids,list): return self.send(400,{})
    for fid in fids:
     f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
     if not f or f['uploader_id']!=u['id'] or f['attached']: return self.send(400,{})
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']
    for fid in fids: c.execute('INSERT INTO mf VALUES(?,?)',(mid,fid)); c.execute('UPDATE files SET attached=1 WHERE id=?',(fid,))
    c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();payload={'message':msgj(r)};eid=emit(ch['id'],'message.created',payload);payload['event_id']=eid;return self.send(201,{'message':msgj(r)})
   m=re.match(r'/api/messages/(\d+)/reactions$',path)
   if m:
    r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone(); em=d.get('emoji')
    if not r:return self.send(404,{})
    if not isinstance(em,str) or not em:return self.send(400,{})
    if not readable(getch(r['channel_id']),u['id']):return self.send(403,{})
    c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(r['id'],u['id'],em));changed=c.execute('SELECT changes() x').fetchone()['x'];c.commit()
    if changed:emit(r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':em,'user_id':u['id'],'reactions':reactions(r['id'])})
    return self.send(200,{'reactions':reactions(r['id'])})
   if path=='/api/dms':
    x=c.execute('SELECT id FROM users WHERE id=?',(d.get('user_id'),)).fetchone()
    if not x or x['id']==u['id']:return self.send(400 if x else 404,{})
    a,b=sorted((u['id'],x['id'])); q=c.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,b)).fetchone()
    if q:return self.send(200,{'channel_id':q['channel_id']})
    c.execute('INSERT INTO channels(name,is_private,is_dm) VALUES(?,?,1)',(f'dm-{a}-{b}',1));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid));c.execute('INSERT INTO cm VALUES(?,?),(?,?)',(cid,a,cid,b));c.commit();return self.send(200,{'channel_id':cid})
   if path=='/api/files':
    raw=self.body(); ct=self.headers.get('Content-Type',''); m=re.search('boundary=([^;]+)',ct)
    if not m:return self.send(400,{})
    fm=re.search(br'filename=(?:"([^"]*)"|([^;\r\n]+))',raw,re.I)
    if fm is None:return self.send(400,{})
    name=(fm.group(1) or fm.group(2)).decode(errors='replace')
    hs=raw.find(b'\r\n\r\n',fm.start())
    if hs<0:return self.send(400,{})
    ds=hs+4
    stop=raw.find(b'\r\n--',ds)
    data=raw[ds:stop if stop>=0 else len(raw)]
    if len(data)>10485760:return self.send(413,{})
    fct=re.search(br'Content-Type:\s*([^\r\n]+)',raw[:hs],re.I)
    typ=fct.group(1).decode(errors='replace').strip() if fct else 'application/octet-stream'
    c.execute('INSERT INTO files(uploader_id,filename,content_type,data,created_at) VALUES(?,?,?,?,?)',(u['id'],name,typ,data,now()));c.commit();f=c.execute('SELECT *,length(data) size FROM files WHERE id=?',(c.execute('SELECT last_insert_rowid() x').fetchone()['x'],)).fetchone();return self.send(201,{'file':filej(f)})
   m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.send(404,{})
    if not role(w['id'],u['id']):return self.send(403,{})
    h=str(d.get('handle',''))
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.send(400,{})
    try:c.execute('INSERT INTO groups_(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],h,d.get('name',''),json.dumps(d.get('user_ids',[]))));c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(c.execute('SELECT last_insert_rowid() x').fetchone()['x'],)).fetchone();return self.send(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
    except sqlite3.IntegrityError:return self.send(409,{})
  finally:c.close()
  self.send(404,{})
 def do_PATCH(self):
  u=auth(tok(self.headers)); p=urllib.parse.urlsplit(self.path).path; d=self.jbody()
  if not u:return self.send(401,{})
  c=db()
  try:
   if p=='/api/users/me':
    allowed={'display_name':(1,80),'timezone':(1,100),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
    for k,v in d.items():
     if k in allowed and (not isinstance(v,str) or not allowed[k][0]<=len(v)<=allowed[k][1] or k=='timezone' and (lambda:False)()): return self.send(400,{})
    if 'timezone' in d:
     try: ZoneInfo(d['timezone'])
     except: return self.send(400,{})
    fields={k:v for k,v in d.items() if k in allowed}; 
    if fields:
     c.execute('UPDATE users SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),u['id']));c.commit()
     r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();broadcast_global({'type':'user.updated','user':userjson(r)});return self.send(200,{'user':userjson(r)})
   m=re.match(r'/api/channels/(\d+)$',p)
   if m:
    ch=getch(m.group(1))
    if not ch:return self.send(404,{})
    if not member(ch['id'],u['id']) and role(ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
    if 'topic' not in d or not isinstance(d['topic'],str) or len(d['topic'])>250:return self.send(400,{})
    c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id']));c.commit();return self.send(200,{'channel':chan(getch(ch['id']))})
   m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if not g:return self.send(404,{})
    if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.send(403,{})
    c.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['user_ids']))),g['id']));c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone();return self.send(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
  finally:c.close()
  self.send(404,{})
 def do_DELETE(self):
  u=auth(tok(self.headers));p=urllib.parse.urlsplit(self.path).path
  if not u:return self.send(401,{})
  c=db()
  try:
   m=re.match(r'/api/messages/(\d+)/reactions/(.*)$',p)
   if m:
    r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
    if not r:return self.send(404,{})
    if not readable(getch(r['channel_id']),u['id']):return self.send(403,{})
    em=urllib.parse.unquote(m.group(2));c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],em));changed=c.execute('SELECT changes() x').fetchone()['x'];c.commit()
    if changed:emit(r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':em,'user_id':u['id'],'reactions':reactions(r['id'])})
    return self.send(200,{'reactions':reactions(r['id'])})
   m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if not g:return self.send(200,{'deleted':True})
    if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.send(403,{})
    c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();return self.send(200,{'deleted':True})
  finally:c.close()
  self.send(404,{})
 def websocket(self,u):
  if not u:return self.send_response(401) or self.end_headers()
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send(400,{})
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  s=self.connection; cid=secrets.token_hex(12);subs={}
  with conlock:conns.append((s,u['id'],subs))
  c=db();c.execute('INSERT INTO presence VALUES(?,?,?)',(cid,u['id'],time.time()));c.commit();c.close()
  try:
   while True:
    h=s.recv(2)
    if not h:break
    n=h[1]&127; mask=s.recv(4); data=s.recv(n); data=bytes(data[i]^mask[i%4] for i in range(n)); x=json.loads(data)
    if x.get('type')=='subscribe':
     c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(x['channel_id'],)).fetchone()['n'];c.close();subs[int(x['channel_id'])]=head;ws_send(s,{'type':'subscribed','channel_id':x['channel_id'],'head_event_id':head})
    c=db();c.execute('UPDATE presence SET last_seen=? WHERE conn_id=?',(time.time(),cid));c.commit();c.close()
  except:pass
  finally:
   with conlock:
    conns[:]=[x for x in conns if x[0] is not s]
   c=db();c.execute('DELETE FROM presence WHERE conn_id=?',(cid,));c.commit();c.close()
 def log_message(self,*a):pass

if __name__=='__main__':
 init();threading.Thread(target=poll,daemon=True).start()
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
