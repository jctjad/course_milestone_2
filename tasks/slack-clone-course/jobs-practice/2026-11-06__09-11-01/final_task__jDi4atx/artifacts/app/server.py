import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,time,threading,email.parser,email.policy,fcntl
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
DB='/var/lib/huddle/huddle.db'; LOCK='/var/lib/huddle/huddle.lock'; FILEDIR='/var/lib/huddle/files'; os.makedirs(FILEDIR,exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def con():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=con(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT,attached INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT);
CREATE TABLE IF NOT EXISTS msgfiles(message_id INTEGER,file_id INTEGER,UNIQUE(message_id,file_id));
''');
 try: c.execute('ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT \'[]\'')
 except sqlite3.OperationalError: pass
 c.commit();c.close()
def row(sql,args=()):
 c=con()
 try:return c.execute(sql,args).fetchone()
 finally:c.close()
def user_to_json(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def channel_json(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'] or '','is_archived':bool(x['is_archived'])}
def user_from_token(t):
 r=row('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)); return r
def get_user_by_username(x): return row('SELECT * FROM users WHERE username=?',(x,))
def ws(slug): return row('SELECT * FROM workspaces WHERE slug=?',(slug,))
def ch(cid): return row('SELECT * FROM channels WHERE id=?',(cid,))
def member(cid,uid): return row('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)) is not None
def role(wid,uid):
 r=row('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(wid,uid)); return r['role'] if r else None
def canread(x,uid):
 if x['is_dm']: return member(x['id'],uid)
 if not x['is_private'] and x['workspace_id'] is not None and role(x['workspace_id'],uid): return True
 return member(x['id'],uid)
def reactions(mid):
 c=con(); rs=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall(); c.close(); out=[]
 for r in rs:
  z=next((x for x in out if x['emoji']==r['emoji']),None)
  if not z: z={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(z)
  z['user_ids'].append(r['user_id']);z['count']+=1
 return out
def msg(m):
 u=row('SELECT * FROM users WHERE id=?',(m['author_id'],)); fs=[]
 c=con(); q=c.execute('SELECT f.* FROM files f JOIN msgfiles mf ON mf.file_id=f.id WHERE mf.message_id=?',(m['id'],)).fetchall();c.close()
 for z in q: fs.append({k:z[k] for k in ('id','uploader_id','filename','content_type','size','created_at')})
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':user_to_json(u),'body':m['body'],'parent_id':m['parent_id'],'reply_count':(row('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],))['n']),'created_at':m['created_at'],'edited_at':m['edited_at'],'files':fs,'reactions':reactions(m['id']),'mentions':json.loads(m['mentions']) if 'mentions' in m.keys() else []}
def emit(cid,kind,payload):
 c=con(); c.execute('BEGIN IMMEDIATE'); h=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,h,kind,json.dumps(payload)));c.commit();c.close();return h
def online():
 c=con(); cutoff=time.time()-1.5; c.execute('DELETE FROM presence WHERE last_seen<?',(cutoff,)); r=[x['user_id'] for x in c.execute('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(cutoff,)).fetchall()]; c.commit(); c.close(); return r
def mention(body,cid,author):
 x=ch(cid); ids=set(); base=re.findall(r'(?<!\w)@([A-Za-z0-9_-]+)',body); cc=con(); mem=[r['user_id'] for r in cc.execute('SELECT user_id FROM cm WHERE channel_id=?',(cid,)).fetchall()]; cc.close()
 for n in base:
  if n=='channel': ids.update(mem);continue
  if n=='here': ids.update(i for i in mem if i in online());continue
  g=row('SELECT user_ids FROM groupsx WHERE workspace_id=? AND handle=?',(x['workspace_id'],n)) if x['workspace_id'] else None
  if g: ids.update(i for i in json.loads(g['user_ids']) if i in mem)
  else:
   u=get_user_by_username(n)
   if u and u['id'] in mem: ids.add(u['id'])
 ids.discard(author); return list(ids)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def auth(self):
  a=self.headers.get('Authorization',''); return user_from_token(a[7:] if a.startswith('Bearer ') else '')
 def send(self,code,obj,typ='application/json',extra={}):
  if getattr(self,'_flock',None):
   fcntl.flock(self._flock,fcntl.LOCK_UN); self._flock.close(); self._flock=None
  b=obj if isinstance(obj,bytes) else json.dumps(obj,separators=(',',':')).encode();self.send_response(code);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(b)));[self.send_header(k,v) for k,v in extra.items()];self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return self.rfile.read(n)
 def js(self):
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def wsframe(self,obj):
  b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
  if n<126:h=bytes([0x81,n])
  elif n<65536:h=bytes([0x81,126])+struct.pack('>H',n)
  else:h=bytes([0x81,127])+struct.pack('>Q',n)
  return h+b
 def websocket(self):
  qs=parse_qs(urlparse(self.path).query); t=qs.get('token',[''])[0]; u=user_from_token(t)
  if not u:return self.send_error(401);return
  key=self.headers.get('Sec-WebSocket-Key','')
  import hashlib as _h
  accept=base64.b64encode(_h.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers();self.connection.settimeout(.2)
  connid=secrets.token_hex(12)
  pc=con();pc.execute('INSERT INTO presence(conn_id,user_id,last_seen) VALUES(?,?,?)',(connid,u['id'],time.time()));pc.commit();pc.close()
  subs={}; gc=con(); last_global=gc.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0]; gc.close()
  try:
   while True:
    try:
     def rx(n):
      z=b''
      while len(z)<n:
       z+=self.connection.recv(n-len(z))
      return z
     h=rx(2)
     if not h:break
     ln=h[1]&127; masked=h[1]&128
     if ln==126:ln=struct.unpack('>H',rx(2))[0]
     elif ln==127:ln=struct.unpack('>Q',rx(8))[0]
     mask=rx(4) if masked else b''; data=rx(ln)
     if masked:data=bytes(data[i]^mask[i%4] for i in range(ln))
     if data:
      z=json.loads(data); typ=z.get('type'); cid=z.get('channel_id')
      if typ=='subscribe':
       h0=row('SELECT COALESCE(MAX(event_id),0) h FROM events WHERE channel_id=?',(cid,))['h'];subs[cid]=h0;self.wfile.write(self.wsframe({'type':'subscribed','channel_id':cid,'head_event_id':h0}));self.wfile.flush()
      elif typ=='resume':
       since=int(z.get('since_event_id',0));subs[cid]=since
       for ev in con().execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
        pl=json.loads(ev['payload']);pl.update(type=ev['kind'],event_id=ev['event_id'],channel_id=cid);self.wfile.write(self.wsframe(pl))
       self.wfile.write(self.wsframe({'type':'resumed','channel_id':cid,'head_event_id':row('SELECT COALESCE(MAX(event_id),0) h FROM events WHERE channel_id=?',(cid,))['h']}));self.wfile.flush()
    except TimeoutError:pass
    except OSError:break
    gc=con(); ge=gc.execute('SELECT id,payload FROM global_events WHERE id>? ORDER BY id',(last_global,)).fetchall(); gc.close()
    for ge_row in ge:
     self.wfile.write(self.wsframe(json.loads(ge_row['payload']))); last_global=ge_row['id']
    if ge:self.wfile.flush()
    for cid,last in list(subs.items()):
     c=con();es=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall();c.close()
     for ev in es:
      pl=json.loads(ev['payload']);pl.update(type=ev['kind'],event_id=ev['event_id'],channel_id=cid);self.wfile.write(self.wsframe(pl));last=ev['event_id']
     subs[cid]=last
     if es:self.wfile.flush()
  except Exception:pass
  finally:
   pc=con();pc.execute('DELETE FROM presence WHERE conn_id=?',(connid,));pc.commit();pc.close()
 def do_GET(self):
  if self.path.startswith('/api/ws') and self.headers.get('Upgrade','').lower()=='websocket':
   return self.websocket()
  p=urlparse(self.path); q=p.path.split('/'); u=self.auth()
  if p.path=='/api/health': return self.send(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if p.path=='/': return self.send(200,HTML,'text/html')
  if q[:3]==['','api','auth'] and len(q)>3 and q[3]=='me':
   return self.send(200,{'user':user_to_json(u)}) if u else self.send(401,{})
  if q[:3]==['','api','users'] and len(q)==4:
   if not u:return self.send(401,{})
   z=row('SELECT * FROM users WHERE id=?',(q[3],));return self.send(200,{'user':user_to_json(z)}) if z else self.send(404,{})
  if not u:return self.send(401,{})
  if q[2]=='workspaces' and len(q)==4:
   w=ws(q[3]); 
   if not w:return self.send(404,{})
   if not role(w['id'],u['id']):return self.send(403,{})
   c=con(); cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))',(w['id'],u['id'])).fetchall();c.close();return self.send(200,{'workspace':dict(w),'channels':[channel_json(x) for x in cs]})
  if q[2]=='channels' and len(q)>=5 and q[4]=='messages':
   x=ch(q[3])
   if not x:return self.send(404,{})
   if not canread(x,u['id']):return self.send(403,{})
   c=con(); ms=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(x['id'],)).fetchall();c.close();return self.send(200,{'messages':[msg(m) for m in ms],'next_cursor':None})
  if q[2]=='channels' and len(q)>=5 and q[4]=='members':
   c=con();rs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(q[3],)).fetchall();c.close();return self.send(200,{'members':[user_to_json(x) for x in rs]})
  if q[2]=='files':
   z=row('SELECT * FROM files WHERE id=?',(q[3],)); 
   if not z:return self.send(404,{})
   f={'id':z['id'],'uploader_id':z['uploader_id'],'filename':z['filename'],'content_type':z['content_type'],'size':z['size'],'created_at':z['created_at']}
   return self.send(200,{'file':f}) if len(q)==4 else self.send(200,z['data'],z['content_type'],{'Content-Disposition':f'attachment; filename="{z["filename"]}"'})
  if q[2]=='workspaces' and len(q)>=5 and q[4]=='groups':
   w=ws(q[3])
   if not w:return self.send(404,{})
   if not role(w['id'],u['id']):return self.send(403,{})
   c=con();rs=c.execute('SELECT * FROM groupsx WHERE workspace_id=?',(w['id'],)).fetchall();c.close()
   return self.send(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rs]})
  self.send(404,{})
 def do_POST(self):
  p=urlparse(self.path);q=p.path.split('/');u=self.auth()
  if q[:4]==['','api','auth','register']:
   d=self.js()
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.send(400,{})
   try:
    c=con();c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],hashlib.sha256(d['password'].encode()).hexdigest(),d.get('display_name') or d['username']));uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();z=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.send(201,{'user':user_to_json(z),'token':t})
   except sqlite3.IntegrityError:return self.send(409,{})
  if q[:4]==['','api','auth','login']:
   d=self.js();z=get_user_by_username(d.get('username',''))
   if not z or hashlib.sha256(d.get('password','').encode()).hexdigest()!=z['password']:return self.send(401,{})
   t=secrets.token_urlsafe(24);c=con();c.execute('INSERT INTO tokens VALUES(?,?)',(t,z['id']));c.commit();c.close();return self.send(200,{'user':user_to_json(z),'token':t})
  if not u:return self.send(401,{})
  if q[2]=='workspaces' and len(q)==3:
   d=self.js()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.send(400,{})
   try:
    c=con();c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d['name'],u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.send(201,{'workspace':dict(w),'general_channel':channel_json(x)})
   except sqlite3.IntegrityError:return self.send(409,{})
  if q[2]=='workspaces' and len(q)>=5 and q[4]=='channels':
   w=ws(q[3]); d=self.js()
   if not w:return self.send(404,{})
   if role(w['id'],u['id'])!='owner':return self.send(403,{})
   name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32:return self.send(400,{})
   try:
    c=con();c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,1 if d.get('is_private') else 0,d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.send(201,{'channel':channel_json(x)})
   except sqlite3.IntegrityError:return self.send(409,{})
  if q[2]=='channels' and len(q)>=4 and q[-1] in ('archive','unarchive'):
   x=ch(q[3])
   if not x:return self.send(404,{})
   if role(x['workspace_id'],u['id'])!='owner':return self.send(403,{})
   c=con();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if q[-1]=='archive' else 0,x['id']));c.commit();c.close();return self.send(200,{'channel':channel_json(ch(x['id']))})
  if q[2]=='workspaces' and len(q)>=5 and q[4]=='groups':
   w=ws(q[3]);d=self.js()
   if not w:return self.send(404,{})
   if not role(w['id'],u['id']):return self.send(403,{})
   h=d.get('handle','')
   if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel') or not isinstance(d.get('name'),str):return self.send(400,{})
   ids=d.get('user_ids',[])
   if not isinstance(ids,list):return self.send(400,{})
   try:
    c=con();c.execute('INSERT INTO groupsx(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],h,d['name'],json.dumps(ids)));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();c.close();return self.send(201,{'group':{'id':gid,'handle':h,'name':d['name'],'member_user_ids':ids}})
   except sqlite3.IntegrityError:return self.send(409,{})
  if q[2]=='dms':
   d=self.js();v=row('SELECT * FROM users WHERE id=?',(d.get('user_id'),))
   if not v or v['id']==u['id']:return self.send(400 if v else 404,{})
   a,b=sorted([u['id'],v['id']]);z=row('SELECT id FROM channels WHERE name=?',(f'dm-{a}-{b}',))
   if z: return self.send(200,{'channel_id':z['id']})
   c=con();c.execute('INSERT INTO channels(name,is_private,is_dm) VALUES(?,?,1)',(f'dm-{a}-{b}',1));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.executemany('INSERT INTO cm VALUES(?,?)',[(cid,a),(cid,b)]);c.commit();c.close();return self.send(200,{'channel_id':cid})
  if q[2]=='channels' and len(q)>=4 and q[-1]=='join':
   x=ch(q[3])
   if not x:return self.send(404,{})
   if x['is_private']:return self.send(403,{})
   c=con();c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(x['id'],u['id']));c.commit();c.close();return self.send(200,{'channel':channel_json(x)})
  if q[2]=='channels' and len(q)>=4 and q[-1]=='messages':
   x=ch(q[3]);d=self.js()
   if not x:return self.send(404,{})
   if not canread(x,u['id']):return self.send(403,{})
   if x['is_archived'] and d.get('body') != '/unarchive':return self.send(423,{})
   fids=d.get('file_ids',[])
   if not isinstance(fids,list):return self.send(400,{})
   for fid in fids:
    z=row('SELECT * FROM files WHERE id=?',(fid,))
    if not z or z['uploader_id']!=u['id'] or z['attached']:return self.send(400,{})
   b=d.get('body','')
   if not b.strip():return self.send(400,{})
   if b.startswith('/') and not b.startswith('//'):
    parts=b.split(' ',1);cmd=parts[0];arg=parts[1].strip() if len(parts)>1 else ''
    if cmd=='/me' and not arg:return self.send(400,{})
    if cmd=='/me': b='/me '+arg
    elif cmd=='/shrug': b=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if not arg or len(arg)>250:return self.send(400,{})
     if not member(x['id'],u['id']) and role(x['workspace_id'],u['id'])!='owner':return self.send(403,{})
     c=con();c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,x['id']));c.commit();c.close();return self.send(201,{'message':None,'channel':channel_json(ch(x['id']))})
    elif cmd=='/invite':
     if not arg.startswith('@'):return self.send(400,{})
     v=get_user_by_username(arg[1:]); 
     if not v:return self.send(404,{})
     if not role(x['workspace_id'],v['id']):return self.send(403,{})
     c=con();c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(x['id'],v['id']));c.commit();c.close();return self.send(201,{'message':None,'invited_user':user_to_json(v)})
    elif cmd in ('/archive','/unarchive'):
     if role(x['workspace_id'],u['id'])!='owner':return self.send(403,{})
     c=con();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(cmd=='/archive',x['id']));c.commit();c.close();return self.send(201,{'message':None,'channel':channel_json(ch(x['id']))})
    else:return self.send(400,{})
   c=con();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(x['id'],u['id'],b,d.get('parent_id'),now(),json.dumps(mention(b,x['id'],u['id']))));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.executemany('INSERT INTO msgfiles(message_id,file_id) VALUES(?,?)',[(mid,i) for i in fids]);c.executemany('UPDATE files SET attached=1 WHERE id=?',[(i,) for i in fids]);c.commit();c.close();mm=row('SELECT * FROM messages WHERE id=?',(mid,));e=emit(x['id'],'message.created',{'message':msg(mm)});return self.send(201,{'message':msg(mm)})
  if q[2]=='messages' and len(q)>=5 and q[4]=='reactions':
   m=row('SELECT * FROM messages WHERE id=?',(q[3],)); 
   if not m:return self.send(404,{})
   x=ch(m['channel_id']);d=self.js();em=d.get('emoji','')
   if not em:return self.send(400,{})
   if not canread(x,u['id']):return self.send(403,{})
   c=con();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?,?)',(m['id'],u['id'],em,now()));changed=c.total_changes;c.commit();c.close()
   if changed:emit(x['id'],'reaction.added',{'message_id':m['id'],'emoji':em,'user_id':u['id'],'reactions':reactions(m['id'])})
   return self.send(200,{'reactions':reactions(m['id'])})
  if q[2]=='files':
   ct=self.headers.get('Content-Type','');raw=self.body()
   if len(raw)>11000000:return self.send(413,{})
   if 'multipart/form-data' not in ct:return self.send(400,{})
   bnd=ct.split('boundary=')[-1].strip().encode();parts=raw.split(b'--'+bnd)
   found=None
   for part in parts:
    if b'name="file"' not in part:continue
    head,data=part.split(b'\r\n\r\n',1);data=data[:-2] if data.endswith(b'\r\n') else data;fn=re.search(br'filename="([^"]*)"',head);typ=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I);found=(fn.group(1).decode() if fn else 'file',typ.group(1).decode() if typ else 'application/octet-stream',data)
   if not found:return self.send(400,{})
   fn,typ,data=found
   if len(data)>10485760:return self.send(413,{})
   c=con();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,data,created_at) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),data,now()));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();z=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return self.send(201,{'file':{k:z[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  self.send(404,{})
 def do_DELETE(self):
  p=urlparse(self.path);q=p.path.split('/');u=self.auth()
  if not u:return self.send(401,{})
  if q[2]=='workspaces' and len(q)>=6 and q[4]=='groups':
   if getattr(self,'_flock',None): fcntl.flock(self._flock,fcntl.LOCK_UN); self._flock.close(); self._flock=None
   w=ws(q[3]);g=row('SELECT * FROM groupsx WHERE workspace_id=? AND handle=?',(w['id'],q[5]))
   if g and u['id']!=g['creator_id'] and role(w['id'],u['id'])!='owner':return self.send(403,{})
   if g:
    c=con();c.execute('DELETE FROM groupsx WHERE id=?',(g['id'],));c.commit();c.close()
   return self.send(200,{'deleted':True})
  if q[2]=='messages' and len(q)>=6 and q[4]=='reactions':
   m=row('SELECT * FROM messages WHERE id=?',(q[3],)); 
   if not m:return self.send(404,{})
   em=unquote(q[5]);
   if not em:return self.send(400,{})
   if not canread(ch(m['channel_id']),u['id']):return self.send(403,{})
   c=con();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],em));n=c.total_changes;c.commit();c.close()
   if n:emit(m['channel_id'],'reaction.removed',{'message_id':m['id'],'emoji':em,'user_id':u['id'],'reactions':reactions(m['id'])})
   return self.send(200,{'reactions':reactions(m['id'])})
  if q[2]=='workspaces' and len(q)>=6 and q[4]=='groups':
   c=con();c.execute('DELETE FROM groupsx WHERE workspace_id=? AND handle=?',(ws(q[3])['id'],q[5]));c.commit();c.close();return self.send(200,{'deleted':True})
  self.send(404,{})
 def do_PATCH(self):
  p=urlparse(self.path);q=p.path.split('/');u=self.auth()
  if not u:return self.send(401,{})
  d=self.js()
  if q[2]=='users' and q[3]=='me':
   allowed={'display_name':(1,80),'timezone':(1,100),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
   for k,v in d.items():
    if k in allowed and (not isinstance(v,str) or not allowed[k][0]<=len(v)<=allowed[k][1] or k=='timezone' and not self.tz(v)):return self.send(400,{})
   c=con();c.execute('UPDATE users SET '+','.join(k+'=?' for k in d if k in allowed)+' WHERE id=?',tuple(v for k,v in d.items() if k in allowed)+(u['id'],));c.commit();z=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.execute('INSERT INTO global_events(payload) VALUES(?)',(json.dumps({'type':'user.updated','user':user_to_json(z)}),));c.commit();c.close();return self.send(200,{'user':user_to_json(z)})
  if q[2]=='workspaces' and len(q)>=6 and q[4]=='groups':
   if getattr(self,'_flock',None): fcntl.flock(self._flock,fcntl.LOCK_UN); self._flock.close(); self._flock=None
   w=ws(q[3]);g=row('SELECT * FROM groupsx WHERE workspace_id=? AND handle=?',(w['id'],q[5]))
   if not g:return self.send(404,{})
   if u['id']!=g['creator_id'] and role(w['id'],u['id'])!='owner':return self.send(403,{})
   vals=[]; sets=[]
   if 'name' in d:
    if not isinstance(d['name'],str):return self.send(400,{})
    sets.append('name=?');vals.append(d['name'])
   if 'user_ids' in d:
    if not isinstance(d['user_ids'],list):return self.send(400,{})
    sets.append('user_ids=?');vals.append(json.dumps(d['user_ids']))
   c=sqlite3.connect(DB,timeout=0.5,isolation_level=None);c.row_factory=sqlite3.Row
   if sets:
    vals.append(g['id']);c.execute('UPDATE groupsx SET '+','.join(sets)+' WHERE id=?',vals)
   z=c.execute('SELECT * FROM groupsx WHERE id=?',(g['id'],)).fetchone();c.close()
   return self.send(200,{'group':{'id':z['id'],'handle':z['handle'],'name':z['name'],'member_user_ids':json.loads(z['user_ids'])}})
  if q[2]=='channels':
   x=ch(q[3]); 
   if not x:return self.send(404,{})
   if not member(x['id'],u['id']) and role(x['workspace_id'],u['id'])!='owner':return self.send(403,{})
   if 'topic' not in d or len(d['topic'])>250:return self.send(400,{})
   c=con();c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],x['id']));c.commit();c.close();return self.send(200,{'channel':channel_json(ch(x['id']))})
  self.send(404,{})
 def tz(self,x):
  try:ZoneInfo(x);return True
  except:return False
HTML=b'''<!doctype html><html><body><h1>Huddle</h1><div id="app">Huddle</div></body></html>'''
def main():
 port=int(sys.argv[1]);
 if not os.path.exists(DB): init()
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
if __name__=='__main__':main()
