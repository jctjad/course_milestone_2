import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,struct,urllib.parse,datetime,zoneinfo,mimetypes
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; FILEDIR='/var/lib/huddle/files'
NODE=int(os.environ.get('NODE_ID','0')); PORT=8000+NODE
conns={}; lock=threading.RLock()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row; return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True); os.makedirs(FILEDIR,exist_ok=True)
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER);
CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
CREATE TABLE IF NOT EXISTS groups_tbl(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
'''); c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'") if not any(x['name']=='mentions' for x in c.execute('pragma table_info(messages)').fetchall()) else None; c.commit(); c.close()
def q(sql,args=(),one=False):
 c=db()
 try:
  r=c.execute(sql,args); x=r.fetchone() if one else r.fetchall(); return x
 finally:c.close()
def ex(sql,args=(),ret=False):
 c=db()
 try:
  r=c.execute(sql,args); c.commit(); return r.lastrowid if ret else None
 finally:c.close()
def user(uid): return q('select * from users where id=?',(uid,),True)
def user_to_json(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def token_user(t):
 r=q('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,),True); return r
def ch(cid): return q('select * from channels where id=?',(cid,),True)
def ws(slug): return q('select * from workspaces where slug=?',(slug,),True)
def role(wid,uid):
 r=q('select role from members where workspace_id=? and user_id=?',(wid,uid),True); return r['role'] if r else None
def cjson(c): return {k:(bool(c[k]) if k in ('is_private','is_dm','is_archived') else c[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def members(cid): return [user(r['user_id']) for r in q('select user_id from cmembers where channel_id=?',(cid,))]
def addmem(cid,uid): ex('insert or ignore into cmembers values(?,?)',(cid,uid))
def canread(c,uid):
 if c['is_dm']: return bool(q('select 1 from cmembers where channel_id=? and user_id=?',(c['id'],uid),True))
 return bool(role(c['workspace_id'],uid)) and (not c['is_private'] or bool(q('select 1 from cmembers where channel_id=? and user_id=?',(c['id'],uid),True)))
def reactions(mid):
 out=[]
 for r in q('select emoji,user_id from reactions where message_id=? order by rowid',(mid,)):
  z=next((x for x in out if x['emoji']==r['emoji']),None)
  if not z: z={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(z)
  z['user_ids'].append(r['user_id']);z['count']+=1
 return out
def files_for(mid):
 return [filejson(q('select * from files where id=?',(r['file_id'],),True)) for r in q('select file_id from attachments where message_id=?',(mid,))]
def filejson(f): return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def mjson(m):
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':user_to_json(user(m['author_id'])),'body':m['body'],'parent_id':m['parent_id'],'reply_count':q('select count(*) n from messages where parent_id=? and deleted=0',(m['id'],),True)['n'],'created_at':m['created_at'],'edited_at':m['edited_at'],'files':files_for(m['id']),'reactions':reactions(m['id']),'mentions':json.loads(m['mentions']) if 'mentions' in m.keys() and m['mentions'] else []}
def online():
 return {r['user_id'] for r in q('select distinct user_id from presence where last_seen>?',(time.time()-1.5,))}
def mentions(body,c,author):
 ids=set(); wsid=c['workspace_id']
 pats=re.finditer(r'(?<!\w)@([A-Za-z0-9_-]+)',body)
 for m in pats:
  n=m.group(1)
  if n=='channel':
   ids.update(x['id'] for x in members(c['id']) if x['id']!=author)
  elif n=='here':
   ids.update(x['id'] for x in members(c['id']) if x['id']!=author and x['id'] in online())
  else:
   g=q('select user_ids from groups_tbl where workspace_id=? and handle=?',(wsid,n.lower()),True) if wsid else None
   if g:
    ids.update(int(x) for x in json.loads(g['user_ids']) if int(x)!=author and any(y['id']==int(x) for y in members(c['id'])))
   else:
    u=q('select * from users where username=?',(n,),True)
    if u and u['id']!=author and role(wsid,u['id']) and any(x['id']==u['id'] for x in members(c['id'])): ids.add(u['id'])
 return list(ids)
def emit(cid,kind,payload):
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE'); r=c.execute('select n from counters where channel_id=?',(cid,)).fetchone(); n=(r['n'] if r else 0)+1
  c.execute('insert or replace into counters values(?,?)',(cid,n)); frame={'type':kind,'event_id':n,'channel_id':cid,**payload}; c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(cid,n,kind,json.dumps(frame),now())); c.commit(); return frame
 finally:c.close()
def broadcast(frame):
 ex('insert into events(channel_id,event_id,kind,payload,created_at) values(NULL,NULL,?,?,?)',(frame['type'],json.dumps(frame),now()))
def ws_send(s,obj):
 try:
  b=json.dumps(obj).encode(); h=bytes([129])
  if len(b)<126:h+=bytes([len(b)])
  elif len(b)<65536:h+=bytes([126])+struct.pack('>H',len(b))
  else:h+=bytes([127])+struct.pack('>Q',len(b))
  s.sendall(h+b)
 except: pass
def poller():
 last=0
 while True:
  try:
   rows=q('select rowid as _event_rowid,* from events where rowid>? order by rowid',(last,))
   for r in rows:
    last=r['_event_rowid']; f=json.loads(r['payload'])
    with lock:
     for s in list(conns.values()):
      if f.get('type')=='user.updated' or f.get('channel_id') in s[1]: ws_send(s[0],f)
  except: pass
  time.sleep(.08)
def recvframe(s):
 h=s.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=struct.unpack('>H',s.recv(2))[0]
 elif n==127:n=struct.unpack('>Q',s.recv(8))[0]
 mask=s.recv(4); b=s.recv(n); return json.loads(bytes(b[i]^mask[i%4] for i in range(n))) if (h[0]&15)==1 else None
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x):pass
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
 def out(self,code,obj,headers=None):
  b=json.dumps(obj,separators=(',',':')).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def auth(self):
  a=self.headers.get('Authorization',''); return token_user(a[7:] if a.startswith('Bearer ') else '')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path; parts=path.split('/')
  if path=='/api/health':return self.out(200,{'status':'ok','node_id':NODE})
  if path.startswith('/api/ws'): return self.websocket()
  u=self.auth()
  if path.startswith('/api/auth/me'): return self.out(200,{'user':user_to_json(u)}) if u else self.out(401,{'error':'unauthorized'})
  if not u:return self.out(401,{'error':'unauthorized'})
  if len(parts)==4 and parts[2]=='users':
   x=user(parts[3]);return self.out(200,{'user':user_to_json(x)}) if x else self.out(404,{})
  if path=='/api/workspaces':
   return self.out(200,{'workspaces':[dict(x) for x in q('select * from workspaces where id in (select workspace_id from members where user_id=?)',(u['id'],))]})
  if len(parts)>=4 and parts[2]=='workspaces':
   w=ws(parts[3])
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   if len(parts)==4:
    cs=[cjson(x) for x in q('select * from channels where workspace_id=? and is_archived=0 and (is_private=0 or id in(select channel_id from cmembers where user_id=?))',(w['id'],u['id']))]
    return self.out(200,{'workspace':dict(w),'channels':cs})
   if parts[4]=='groups':
    gs=q('select * from groups_tbl where workspace_id=?',(w['id'],)); return self.out(200,{'groups':[{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])} for g in gs]})
  if len(parts)>=4 and parts[2]=='channels':
   c=ch(parts[3])
   if not c:return self.out(404,{})
   if parts[-1]=='members': return self.out(200,{'members':[user_to_json(x) for x in members(c['id'])]}) if canread(c,u['id']) else self.out(403,{})
   if parts[-1]=='messages':
    if not canread(c,u['id']):return self.out(403,{})
    ms=[mjson(x) for x in q('select * from messages where channel_id=? and deleted=0 order by id desc limit 200',(c['id'],))];return self.out(200,{'messages':ms,'next_cursor':None})
  if len(parts)>=4 and parts[2]=='files':
   f=q('select * from files where id=?',(parts[3],),True)
   if not f:return self.out(404,{})
   if parts[-1]=='download':
    b=f['data'];self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
   return self.out(200,{'file':filejson(f)})
  return self.out(404,{})
 def do_POST(self):
  p=urllib.parse.urlparse(self.path); path=p.path; parts=path.split('/')
  if path=='/api/auth/register':
   try:d=json.loads(self.body())
   except:return self.out(400,{})
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.out(400,{})
   if q('select 1 from users where username=?',(d['username'],),True):return self.out(409,{})
   uid=ex('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username'],'UTC','','',''),True); t=secrets.token_urlsafe(24);ex('insert into tokens values(?,?)',(t,uid));return self.out(201,{'user':user_to_json(user(uid)),'token':t})
  if path=='/api/auth/login':
   d=json.loads(self.body());x=q('select * from users where username=? and password=?',(d.get('username'),d.get('password')),True)
   if not x:return self.out(401,{})
   t=secrets.token_urlsafe(24);ex('insert into tokens values(?,?)',(t,x['id']));return self.out(200,{'user':user_to_json(x),'token':t})
  u=self.auth()
  if not u:return self.out(401,{})
  if path=='/api/workspaces':
   d=json.loads(self.body())
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.out(400,{})
   if ws(d['slug']):return self.out(409,{})
   wid=ex('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d.get('name',''),u['id']),True);ex('insert into members values(?,?,?)',(wid,u['id'],'owner'));cid=ex('insert into channels(workspace_id,name,is_private) values(?,?,0)',(wid,'general'),True);addmem(cid,u['id']);return self.out(201,{'workspace':dict(q('select * from workspaces where id=?',(wid,),True)),'general_channel':cjson(ch(cid))})
  if path=='/api/dms':
   d=json.loads(self.body()); v=user(d.get('user_id')); 
   if not v or v['id']==u['id']:return self.out(400 if v else 404,{})
   a,b=sorted((u['id'],v['id']));r=q('select channel_id from dms where a=? and b=?',(a,b),True)
   if r:return self.out(200,{'channel_id':r['channel_id']})
   cid=ex('insert into channels(name,is_private,is_dm) values(?,1,1)',(f'dm-{a}-{b}',),True);ex('insert into dms values(?,?,?)',(a,b,cid));addmem(cid,a);addmem(cid,b);return self.out(200,{'channel_id':cid})
  if path=='/api/files':
   raw=self.body();ct=self.headers.get('Content-Type','');m=re.search(r'boundary="?([^";]+)',ct)
   if not m or len(raw)>10485760+10000:return self.out(413 if len(raw)>10485760+10000 else 400,{})
   boundary=m.group(1).encode(); chunks=raw.split(b'--'+boundary); part=next((x for x in chunks if b'name="file"' in x),None)
   if not part:return self.out(400,{})
   head,data=part.split(b'\r\n\r\n',1);data=data[:-2] if data.endswith(b'\r\n') else data; disp=re.search(br'filename="([^"]*)"',head); typ=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I); fn=(disp.group(1).decode() if disp else 'file'); mime=(typ.group(1).decode() if typ else 'application/octet-stream')
   if len(data)>10485760:return self.out(413,{})
   fid=ex('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],fn,mime,len(data),now(),data),True);return self.out(201,{'file':filejson(q('select * from files where id=?',(fid,),True))})
  if len(parts)>=5 and parts[2]=='channels' and parts[4] in ('archive','unarchive'):
   c=ch(parts[3])
   if not c:return self.out(404,{})
   if not c['workspace_id'] or role(c['workspace_id'],u['id'])!='owner':return self.out(403,{})
   val=1 if parts[4]=='archive' else 0
   ex('update channels set is_archived=? where id=?',(val,c['id']))
   return self.out(200,{'channel':cjson(ch(c['id']))})
  if len(parts)>=5 and parts[2]=='workspaces' and parts[4]=='groups':
   w=ws(parts[3])
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   d=json.loads(self.body()); h=d.get('handle','')
   if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.out(400,{})
   if q('select 1 from groups_tbl where workspace_id=? and handle=?',(w['id'],h),True):return self.out(409,{})
   ids=d.get('user_ids',[])
   if not isinstance(ids,list):return self.out(400,{})
   gid=ex('insert into groups_tbl(workspace_id,creator_id,handle,name,user_ids) values(?,?,?,?,?)',(w['id'],u['id'],h,d.get('name',''),json.dumps(ids)),True)
   return self.out(201,{'group':{'id':gid,'handle':h,'name':d.get('name',''),'member_user_ids':ids}})
  if len(parts)>=5 and parts[2]=='channels' and parts[4]=='join':
   c=ch(parts[3]); 
   if not c:return self.out(404,{})
   if c['is_private']:return self.out(403,{})
   if c['workspace_id'] and not role(c['workspace_id'],u['id']):ex('insert into members values(?,?,?)',(c['workspace_id'],u['id'],'member'))
   addmem(c['id'],u['id']);return self.out(200,{'channel':cjson(ch(c['id']))})
  if len(parts)>=5 and parts[2]=='channels' and parts[4]=='messages':
   c=ch(parts[3])
   if not c:return self.out(404,{})
   if not canread(c,u['id']):return self.out(403,{})
   d=json.loads(self.body());body=d.get('body','')
   if c['is_archived'] and body.strip() != '/unarchive':return self.out(423,{})
   if not isinstance(body,str) or not body.strip():return self.out(400,{})
   if body.startswith('/') and not body.startswith('//'):
    a=body[1:].split(' ',1);cmd=a[0];arg=a[1].strip() if len(a)>1 else ''
    if cmd=='me':
     if not arg:return self.out(400,{})
    elif cmd=='shrug':body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='topic':
     if not arg or len(arg)>250:return self.out(400,{})
     if not (role(c['workspace_id'],u['id'])=='owner' or any(x['id']==u['id'] for x in members(c['id']))):return self.out(403,{})
     ex('update channels set topic=? where id=?',(arg,c['id']));return self.out(201,{'message':None,'channel':cjson(ch(c['id']))})
    elif cmd=='invite':
     if not arg.startswith('@'):return self.out(400,{})
     v=q('select * from users where username=?',(arg[1:],),True)
     if not v:return self.out(404,{})
     if not role(c['workspace_id'],v['id']):return self.out(403,{})
     if not any(x['id']==u['id'] for x in members(c['id'])):return self.out(403,{})
     addmem(c['id'],v['id']);return self.out(201,{'message':None,'invited_user':user_to_json(v)})
    elif cmd in ('archive','unarchive'):
     if role(c['workspace_id'],u['id'])!='owner':return self.out(403,{})
     ex('update channels set is_archived=? where id=?',(1 if cmd=='archive' else 0,c['id']));return self.out(201,{'message':None,'channel':cjson(ch(c['id']))})
    else:return self.out(400,{})
   fids=d.get('file_ids',[]); 
   for fid in fids:
    f=q('select * from files where id=?',(fid,),True)
    if not f or f['uploader_id']!=u['id'] or q('select 1 from attachments where file_id=?',(fid,),True):return self.out(400,{})
   mn=mentions(body,c,u['id']); mid=ex('insert into messages(channel_id,author_id,body,parent_id,created_at,mentions) values(?,?,?,?,?,?)',(c['id'],u['id'],body,d.get('parent_id'),now(),json.dumps(mn)),True); 
   for fid in fids:ex('insert into attachments values(?,?)',(mid,fid))
   mm=q('select * from messages where id=?',(mid,),True); frame=emit(c['id'],'message.created',{'message':mjson(mm)});return self.out(201,{'message':mjson(mm)})
  if len(parts)>=5 and parts[2]=='messages' and parts[4]=='reactions':
   m=q('select * from messages where id=?',(parts[3],),True)
   if not m:return self.out(404,{})
   c=ch(m['channel_id'])
   if not canread(c,u['id']):return self.out(403,{})
   d=json.loads(self.body());e=d.get('emoji','')
   if not e:return self.out(400,{})
   old=q('select 1 from reactions where message_id=? and user_id=? and emoji=?',(m['id'],u['id'],e),True)
   if not old:
    ex('insert into reactions values(?,?,?,?)',(m['id'],u['id'],e,now())); emit(c['id'],'reaction.added',{'message_id':m['id'],'emoji':e,'user_id':u['id'],'reactions':reactions(m['id'])})
   return self.out(200,{'reactions':reactions(m['id'])})
  if len(parts)>=5 and parts[2]=='workspaces' and parts[4]=='channels':
   w=ws(parts[3]);d=json.loads(self.body());name=d.get('name','')
   if not w:return self.out(404,{})
   if role(w['id'],u['id'])!='owner' and d.get('is_private'):return self.out(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.out(400,{})
   if q('select 1 from channels where workspace_id=? and name=?',(w['id'],name),True):return self.out(409,{})
   cid=ex('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')),True);addmem(cid,u['id']);return self.out(201,{'channel':cjson(ch(cid))})
  return self.out(404,{})
 def do_PATCH(self):
  p=urllib.parse.urlparse(self.path);parts=p.path.split('/');u=self.auth()
  if not u:return self.out(401,{})
  d=json.loads(self.body())
  if p.path=='/api/users/me':
   fields={k:v for k,v in d.items() if k in ('display_name','timezone','avatar_url','status_text','status_emoji')}
   if 'display_name' in fields and (not isinstance(fields['display_name'],str) or not 1<=len(fields['display_name'])<=80):return self.out(400,{})
   if 'timezone' in fields:
    try:zoneinfo.ZoneInfo(fields['timezone'])
    except:return self.out(400,{})
   limits={'avatar_url':2048,'status_text':100,'status_emoji':64}
   if any(not isinstance(v,str) or len(v)>limits[k] for k,v in fields.items() if k in limits):return self.out(400,{})
   if fields:
    ex('update users set '+','.join(k+'=?' for k in fields)+' where id=?',tuple(fields.values())+(u['id'],));broadcast({'type':'user.updated','user':user_to_json(user(u['id']))})
   return self.out(200,{'user':user_to_json(user(u['id']))})
  if len(parts)>=4 and parts[2]=='channels':
   c=ch(parts[3])
   if not c:return self.out(404,{})
   if not (role(c['workspace_id'],u['id']) or any(x['id']==u['id'] for x in members(c['id']))):return self.out(403,{})
   if 'topic' in d and (not isinstance(d['topic'],str) or len(d['topic'])>250):return self.out(400,{})
   ex('update channels set topic=? where id=?',(d.get('topic',c['topic']),c['id']));return self.out(200,{'channel':cjson(ch(c['id']))})
  if len(parts)>=5 and parts[2]=='workspaces' and parts[4]=='groups':
   w=ws(parts[3]);g=q('select * from groups_tbl where workspace_id=? and handle=?',(w['id'],parts[5]),True)
   if not g:return self.out(404,{})
   if u['id'] not in (g['creator_id'],w['owner_id']):return self.out(403,{})
   ex('update groups_tbl set name=?,user_ids=? where id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['user_ids']))),g['id']));g=q('select * from groups_tbl where id=?',(g['id'],),True);return self.out(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
  return self.out(404,{})
 def do_DELETE(self):
  p=urllib.parse.urlparse(self.path);parts=p.path.split('/');u=self.auth()
  if not u:return self.out(401,{})
  if len(parts)>=5 and parts[2]=='messages' and parts[4]=='reactions':
   m=q('select * from messages where id=?',(parts[3],),True)
   if not m:return self.out(404,{})
   c=ch(m['channel_id'])
   if not canread(c,u['id']):return self.out(403,{})
   e=urllib.parse.unquote(parts[5]) if len(parts)>5 else ''
   if not e:return self.out(400,{})
   if q('select 1 from reactions where message_id=? and user_id=? and emoji=?',(m['id'],u['id'],e),True):
    ex('delete from reactions where message_id=? and user_id=? and emoji=?',(m['id'],u['id'],e));emit(c['id'],'reaction.removed',{'message_id':m['id'],'emoji':e,'user_id':u['id'],'reactions':reactions(m['id'])})
   return self.out(200,{'reactions':reactions(m['id'])})
  if len(parts)>=5 and parts[2]=='workspaces' and parts[4]=='groups':
   w=ws(parts[3]);g=q('select * from groups_tbl where workspace_id=? and handle=?',(w['id'],parts[5]),True)
   if g and u['id'] not in (g['creator_id'],w['owner_id']):return self.out(403,{})
   if g:ex('delete from groups_tbl where id=?',(g['id'],))
   return self.out(200,{'deleted':True})
  return self.out(404,{})
 def websocket(self):
  t=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0];u=token_user(t)
  if not u:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key')
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  s=self.connection; cid=secrets.token_hex(8);ex('insert into presence values(?,?,?)',(cid,u['id'],time.time()))
  with lock:conns[cid]=(s,set())
  try:
   while True:
    x=recvframe(s)
    if not x:break
    if x.get('type') in ('subscribe','resume'):
     chx=x.get('channel_id');conns[cid][1].add(chx);head=q('select n from counters where channel_id=?',(chx,),True);n=head['n'] if head else 0
     if x['type']=='resume':
      since=x.get('since_event_id',0)
      for r in q('select payload from events where channel_id=? and event_id>? order by event_id',(chx,since)):ws_send(s,json.loads(r['payload']))
      ws_send(s,{'type':'resumed','channel_id':chx,'head_event_id':n})
     else:ws_send(s,{'type':'subscribed','channel_id':chx,'head_event_id':n})
    ex('update presence set last_seen=? where conn_id=?',(time.time(),cid))
  finally:
   with lock:conns.pop(cid,None)
   ex('delete from presence where conn_id=?',(cid,))
def run():
 init();threading.Thread(target=poller,daemon=True).start();ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
if __name__=='__main__':run()
