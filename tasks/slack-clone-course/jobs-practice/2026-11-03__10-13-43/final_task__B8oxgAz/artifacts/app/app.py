import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,time,threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
DB='/app/huddle.db'; NODE=int(os.environ.get('NODE_ID','0'))
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.execute('CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,created_at TEXT)'); c.commit(); c.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS memberships(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,event_seq INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS dms(id INTEGER PRIMARY KEY,user_a INTEGER,user_b INTEGER,channel_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(user_id INTEGER PRIMARY KEY,last_seen REAL);
CREATE TABLE IF NOT EXISTS presence_conn(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
'''); c.commit(); c.close()
def userjson(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chanjson(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def getuser(c,uid): return c.execute('select * from users where id=?',(uid,)).fetchone()
def auth(c,h):
 x=h.get('Authorization',''); t=x[7:] if x.startswith('Bearer ') else ''
 r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone()
 return r
def reactions(c,mid):
 out=[]
 for r in c.execute('select emoji,user_id from reactions where message_id=? order by rowid',(mid,)):
  z=next((x for x in out if x['emoji']==r['emoji']),None)
  if not z: z={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(z)
  z['user_ids'].append(r['user_id']);z['count']+=1
 return out
def filejson(r): return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def msgjson(c,r):
 fs=[filejson(x) for x in c.execute('select f.* from files f join attachments a on a.file_id=f.id where a.message_id=?',(r['id'],))]
 au=getuser(c,r['author_id'])
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userjson(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=?',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions(c,r['id']),'mentions':mentions(c,r['body'],r['channel_id'],r['author_id'])}
def members(c,cid): return [userjson(x) for x in c.execute('select u.* from users u join channel_members m on m.user_id=u.id where m.channel_id=?',(cid,))]
def canread(c,ch,uid):
 if ch['is_dm']: return c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],uid)).fetchone() is not None
 return c.execute('select 1 from memberships where workspace_id=? and user_id=?',(ch['workspace_id'],uid)).fetchone() is not None if not ch['is_private'] else c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],uid)).fetchone() is not None
def mentions(c,body,cid,author):
 ids=[]; c.execute('delete from presence_conn where last_seen<?',(time.time()-1.5,)); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
 if not ch:return ids
 channel_mem={x['user_id'] for x in c.execute('select user_id from channel_members where channel_id=?',(cid,))}
 mem=set(channel_mem)
 if not ch['is_dm']: mem|={x['user_id'] for x in c.execute('select user_id from memberships where workspace_id=?',(ch['workspace_id'],))}
 for tok in re.findall(r'(?<![\w@])@([A-Za-z0-9_-]+)',body):
  if tok=='here':
   ids += [x for x in channel_mem if x!=author and c.execute('select 1 from presence_conn where user_id=? and last_seen>?',(x,time.time()-1.5)).fetchone()]
  else:
   g=c.execute('select user_ids from groups_ where workspace_id=? and handle=?',(ch['workspace_id'],tok)).fetchone() if ch['workspace_id'] else None
   if g: ids += [x for x in json.loads(g[0]) if x in channel_mem and x!=author]
   else:
    u=c.execute('select id from users where username=?',(tok,)).fetchone()
    if u and u['id'] in mem and u['id']!=author: ids.append(u['id'])
 return list(dict.fromkeys(ids))
def emit(c,cid,kind,payload):
 c.execute('update channels set event_seq=event_seq+1 where id=?',(cid,)); n=c.execute('select event_seq from channels where id=?',(cid,)).fetchone()[0]
 p=dict(payload);p.update(type=kind,event_id=n,channel_id=cid)
 c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(cid,n,kind,json.dumps(p),now()));return p
def addmsg(c,ch,uid,body,parent=None,files=[]):
 if ch['is_archived']: raise ValueError('archived')
 if not body.strip() and not files: raise ValueError('empty')
 for fid in files:
  f=c.execute('select * from files where id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=uid or c.execute('select 1 from attachments where file_id=?',(fid,)).fetchone(): raise ValueError('file')
 r= c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],uid,body,parent,now()))
 mid=r.lastrowid
 for fid in files:c.execute('insert into attachments values(?,?)',(mid,fid))
 m=c.execute('select * from messages where id=?',(mid,)).fetchone()
 emit(c,ch['id'],'message.reply' if parent else 'message.created',{'message':msgjson(c,m)})
 return m
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,code,obj):
  b=json.dumps(obj,separators=(',',':')).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def sendraw(self,code,b,ct='application/octet-stream',extra={}):
  self.send_response(code);self.send_header('Content-Type',ct);self.send_header('Content-Length',str(len(b)))
  for k,v in extra.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return self.rfile.read(n)
 def json(self):
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def do_GET(self): self.route()
 def do_POST(self): self.route()
 def do_PATCH(self): self.route()
 def do_DELETE(self): self.route()
 def route(self):
  p=urlparse(self.path); path=p.path; c=db()
  try:
   if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
   if path=='/api/ws': return self.websocket(c)
   u=auth(c,self.headers)
   public=path in ('/api/auth/register','/api/auth/login')
   if not u and not public:return self.sendj(401,{'error':'unauthorized'})
   if path=='/api/auth/register':
    d=self.json()
    if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.sendj(400,{'error':'invalid'})
    try:c.execute('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username'],'UTC','','',''));uid=c.execute('select last_insert_rowid()').fetchone()[0]
    except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
    t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));c.commit();return self.sendj(201,{'user':userjson(getuser(c,uid)),'token':t})
   if path=='/api/auth/login':
    d=self.json();r=c.execute('select * from users where username=? and password=?',(d.get('username'),d.get('password'))).fetchone()
    if not r:return self.sendj(401,{'error':'invalid'})
    t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();return self.sendj(200,{'user':userjson(r),'token':t})
   if path=='/api/auth/me':return self.sendj(200,{'user':userjson(u)})
   if path.startswith('/api/users/'):
    if path.endswith('/me'): return self.profile(c,u)
    r=getuser(c,int(path.split('/')[-1])) if path.split('/')[-1].isdigit() else None
    return self.sendj(200,{'user':userjson(r)}) if r else self.sendj(404,{'error':'not found'})
   if path=='/api/workspaces' and self.command=='POST': return self.workspace(c,u)
   if path=='/api/workspaces' and self.command=='GET': return self.sendj(200,{'workspaces':[dict(x) for x in c.execute('select * from workspaces where owner_id=? or id in (select workspace_id from memberships where user_id=?)',(u['id'],u['id']))]})
   if path.startswith('/api/workspaces/'): return self.wsroute(c,u,path,p.query)
   if path.startswith('/api/channels/'): return self.chroute(c,u,path,p.query)
   if path.startswith('/api/messages/'): return self.reaction(c,u,path)
   if path=='/api/dms': return self.dm(c,u)
   if path=='/api/files': return self.upload(c,u)
   if path.startswith('/api/files/'): return self.file(c,u,path)
   return self.sendj(404,{'error':'not found'})
  except ValueError as e:self.sendj(423 if str(e)=='archived' else 400,{'error':str(e)})
  except Exception as e:self.sendj(500,{'error':str(e)})
  finally:c.close()
 def profile(self,c,u):
  if self.command=='GET':return self.sendj(200,{'user':userjson(u)})
  d=self.json(); limits={'display_name':(1,80),'timezone':(1,100),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
  for k,(a,b) in limits.items():
   if k in d and (not isinstance(d[k],str) or len(d[k])<a or len(d[k])>b or k=='timezone' and not validtz(d[k])):return self.sendj(400,{'error':'invalid'})
  sets=[k+'=?' for k in limits if k in d];vals=[d[k] for k in limits if k in d]
  if sets:
   c.execute('update users set '+','.join(sets)+' where id=?',vals+[u['id']])
   fresh=userjson(getuser(c,u['id']))
   c.execute('insert into global_events(payload,created_at) values(?,?)',(json.dumps({'type':'user.updated','user':fresh}),now()))
  c.commit();return self.sendj(200,{'user':userjson(getuser(c,u['id']))})
 def workspace(self,c,u):
  d=self.json()
  if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{})
  try:c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into memberships values(?,?,?)',(wid,u['id'],'owner'));c.execute("insert into channels(workspace_id,name,is_private) values(?,?,0)",(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into channel_members values(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':chanjson(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  except sqlite3.IntegrityError:return self.sendj(409,{})
 def wsroute(self,c,u,path,q):
  parts=path.split('/');slug=parts[3];w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
  if not w:return self.sendj(404,{})
  role=c.execute('select role from memberships where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
  if path.endswith('/channels') and self.command=='POST':
   d=self.json()
   if not role or role['role']!='owner' and d.get('is_private'):return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.sendj(400,{})
   try:
    c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')))
    cid=c.execute('select last_insert_rowid()').fetchone()[0]
    c.execute('insert into channel_members values(?,?)',(cid,u['id']))
    c.commit()
    return self.sendj(201,{'channel':chanjson(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  if len(parts)==4:
   if not role:return self.sendj(403,{})
   cs=[chanjson(x) for x in c.execute('select * from channels where workspace_id=? and is_archived=0 and (is_private=0 or id in(select channel_id from channel_members where user_id=?))',(w['id'],u['id']))]
   return self.sendj(200,{'workspace':dict(w),'channels':cs})
  if parts[-1]=='groups':
   if not role:return self.sendj(403,{})
   if self.command=='GET':return self.sendj(200,{'groups':[groupjson(x) for x in c.execute('select * from groups_ where workspace_id=?',(w['id'],))]})
   d=self.json();h=d.get('handle','')
   if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.sendj(400,{})
   try:c.execute('insert into groups_(workspace_id,creator_id,handle,name,user_ids) values(?,?,?,?,?)',(w['id'],u['id'],h,d.get('name',''),json.dumps(d.get('user_ids',[]))));c.commit();return self.sendj(201,{'group':groupjson(c.execute('select * from groups_ where id=last_insert_rowid()').fetchone())})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  if len(parts)>=6 and parts[4]=='groups':
   g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],parts[5])).fetchone()
   if self.command=='DELETE':
    if g and u['id'] not in (g['creator_id'],w['owner_id']):return self.sendj(403,{})
    if g:c.execute('delete from groups_ where id=?',(g['id'],));c.commit()
    return self.sendj(200,{'deleted':True})
   if not g:return self.sendj(404,{})
   if u['id'] not in (g['creator_id'],w['owner_id']):return self.sendj(403,{})
   d=self.json();c.execute('update groups_ set name=?,user_ids=? where id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['user_ids']))),g['id']));c.commit();return self.sendj(200,{'group':groupjson(c.execute('select * from groups_ where id=?',(g['id'],)).fetchone())})
 def chroute(self,c,u,path,q):
  cid=int(path.split('/')[3]);ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
  if not ch:return self.sendj(404,{})
  if path.endswith('/join'):
   if ch['is_private']:return self.sendj(403,{})
   c.execute('insert or ignore into channel_members values(?,?)',(cid,u['id'],))
   if ch['workspace_id']:c.execute('insert or ignore into memberships values(?,?,?)',(ch['workspace_id'],u['id'],'member'))
   c.commit();return self.sendj(200,{'channel':chanjson(ch)})
  if path.endswith('/members'):return self.sendj(200,{'members':members(c,cid)}) if canread(c,ch,u['id']) else self.sendj(403,{})
  if path.endswith('/messages') and self.command=='GET':
   if not canread(c,ch,u['id']):return self.sendj(403,{})
   return self.sendj(200,{'messages':[msgjson(c,x) for x in c.execute('select * from messages where channel_id=? and parent_id is null and 1 order by id desc',(cid,))],'next_cursor':None})
  if path.endswith('/messages') and self.command=='POST':
   if not canread(c,ch,u['id']):return self.sendj(403,{})
   d=self.json();body=d.get('body','')
   if body.startswith('/') and not body.startswith('//'):return self.slash(c,u,ch,body)
   m=addmsg(c,ch,u['id'],body,d.get('parent_id'),d.get('file_ids',[]));c.commit();return self.sendj(201,{'message':msgjson(c,m)})
  if self.command=='PATCH':
   if not canread(c,ch,u['id']):return self.sendj(403,{})
   d=self.json();c.execute('update channels set topic=? where id=?',(d.get('topic',''),cid));c.commit();return self.sendj(200,{'channel':chanjson(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  if path.endswith('/archive') or path.endswith('/unarchive'):
   w=c.execute('select owner_id from workspaces where id=?',(ch['workspace_id'],)).fetchone()
   if not w or w['owner_id']!=u['id']:return self.sendj(403,{})
   c.execute('update channels set is_archived=? where id=?',(0 if path.endswith('unarchive') else 1,cid));c.commit();return self.sendj(200,{'channel':chanjson(c.execute('select * from channels where id=?',(cid,)).fetchone())})
 def reaction(self,c,u,path):
  a=path.split('/');mid=int(a[3]);m=c.execute('select * from messages where id=?',(mid,)).fetchone()
  if not m:return self.sendj(404,{})
  ch=c.execute('select * from channels where id=?',(m['channel_id'],)).fetchone()
  if not canread(c,ch,u['id']):return self.sendj(403,{})
  emoji=unquote(a[-1]) if self.command=='DELETE' else self.json().get('emoji','')
  if not emoji:return self.sendj(400,{})
  before=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)).fetchone()
  changed=False
  if self.command=='POST' and not before:c.execute('insert into reactions values(?,?,?,?)',(mid,u['id'],emoji,now()));changed=True;kind='reaction.added'
  elif self.command=='DELETE' and before:c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji));changed=True;kind='reaction.removed'
  if changed:emit(c,ch['id'],kind,{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':reactions(c,mid)})
  c.commit();return self.sendj(200,{'reactions':reactions(c,mid)})
 def dm(self,c,u):
  d=self.json();v=getuser(c,d.get('user_id'))
  if not v or v['id']==u['id']:return self.sendj(400 if v else 404,{})
  a,b=sorted((u['id'],v['id']));x=c.execute('select channel_id from dms where user_a=? and user_b=?',(a,b)).fetchone()
  if x:return self.sendj(200,{'channel_id':x[0]})
  c.execute('insert into channels(name,is_private,is_dm) values(?,?,?)',(f'dm-{a}-{b}',1,1));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into channel_members values(?,?)',(cid,a));c.execute('insert into channel_members values(?,?)',(cid,b));c.execute('insert into dms(user_a,user_b,channel_id) values(?,?,?)',(a,b,cid));c.commit();return self.sendj(200,{'channel_id':cid})
 def upload(self,c,u):
  data=self.body();ct=self.headers.get('Content-Type','')
  if len(data)>10485760+100000:return self.sendj(413,{})
  m=re.search(b'name="file"; filename="([^"]*)".*?Content-Type:\s*([^\r\n]+)\r\n\r\n(.*)',data,re.S)
  if not m:return self.sendj(400,{})
  fn=m.group(1).decode(errors='replace');typ=m.group(2).decode().strip();blob=m.group(3)
  boundary_pos=blob.find(b'\r\n--')
  if boundary_pos >= 0: blob=blob[:boundary_pos]
  if len(blob)>10485760:return self.sendj(413,{})
  c.execute('insert into files(uploader_id,filename,content_type,size,data,created_at) values(?,?,?,?,?,?)',(u['id'],fn,typ,len(blob),blob,now()));c.commit();return self.sendj(201,{'file':filejson(c.execute('select * from files where id=last_insert_rowid()').fetchone())})
 def file(self,c,u,path):
  fid=int(path.split('/')[3]);f=c.execute('select * from files where id=?',(fid,)).fetchone()
  if not f:return self.sendj(404,{})
  if path.endswith('/download'):return self.sendraw(200,f['data'],f['content_type'],{'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
  return self.sendj(200,{'file':filejson(f)})
 def slash(self,c,u,ch,b):
  cmd,*rest=b[1:].split(' ',1);arg=rest[0].strip() if rest else ''
  if cmd=='me':
   if not arg:raise ValueError('empty')
   m=addmsg(c,ch,u['id'],b);c.commit();return self.sendj(201,{'message':msgjson(c,m)})
  if cmd=='shrug':
   m=addmsg(c,ch,u['id'],(arg+' ' if arg else '')+'¯\\_(ツ)_/¯');c.commit();return self.sendj(201,{'message':msgjson(c,m)})
  if cmd=='topic':
   owner=c.execute('select owner_id from workspaces where id=?',(ch['workspace_id'],)).fetchone()
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone() and (not owner or owner['owner_id']!=u['id']):return self.sendj(403,{})
   if not arg or len(arg)>250:raise ValueError('topic')
   c.execute('update channels set topic=? where id=?',(arg,ch['id']));c.commit();return self.sendj(201,{'message':None,'channel':chanjson(c.execute('select * from channels where id=?',(ch['id'],)).fetchone())})
  if cmd=='invite':
   if not re.fullmatch(r'@[A-Za-z0-9_]+',arg):raise ValueError('invite')
   v=c.execute('select * from users where username=?',(arg[1:],)).fetchone()
   if not v:return self.sendj(404,{})
   if not c.execute('select 1 from memberships where workspace_id=? and user_id=?',(ch['workspace_id'],v['id'])).fetchone():return self.sendj(403,{})
   c.execute('insert or ignore into channel_members values(?,?)',(ch['id'],v['id']));c.commit();return self.sendj(201,{'message':None,'invited_user':userjson(v)})
  if cmd in ('archive','unarchive'):
   return self.sendj(201,{'message':None,'channel':chanjson(c.execute('select * from channels where id=?',(ch['id'],)).fetchone())}) if False else self._archive_slash(c,u,ch,cmd)
  raise ValueError('unknown')
 def _archive_slash(self,c,u,ch,cmd):
  w=c.execute('select owner_id from workspaces where id=?',(ch['workspace_id'],)).fetchone()
  if not w or w['owner_id']!=u['id']:return self.sendj(403,{})
  c.execute('update channels set is_archived=? where id=?',(cmd=='archive',ch['id']));c.commit();return self.sendj(201,{'message':None,'channel':chanjson(c.execute('select * from channels where id=?',(ch['id'],)).fetchone())})
 def websocket(self,c):
  t=parse_qs(urlparse(self.path).query).get('token',[''])[0];r=c.execute('select user_id from tokens where token=?',(t,)).fetchone()
  if not r:self.send_error(401);return
  key=self.headers.get('Sec-WebSocket-Key','');acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers();self.connection.settimeout(0.1);uid=r[0];conn_id=secrets.token_urlsafe(18);c.execute('insert or replace into presence values(?,?)',(uid,time.time()));c.execute('insert into presence_conn values(?,?,?)',(conn_id,uid,time.time()));c.commit();subs=set();last=0;global_last=c.execute('select coalesce(max(id),0) from global_events').fetchone()[0]
  def out(x):
   b=json.dumps(x,separators=(',',':')).encode();h=bytes([0x81,len(b)]) if len(b)<126 else bytes([0x81,126])+struct.pack('>H',len(b));self.connection.sendall(h+b)
  cursors={}
  try:
   while True:
    received=False
    try:
     h=self.connection.recv(2)
     if not h:break
     op=h[0]&15; n=h[1]&127
     if n==126:n=struct.unpack('>H',self.connection.recv(2))[0]
     if n==127:n=struct.unpack('>Q',self.connection.recv(8))[0]
     mask=self.connection.recv(4) if h[1]&128 else None
     data=self.connection.recv(n)
     if mask:data=bytes(data[i]^mask[i%4] for i in range(n))
     if op==8:break
     if op==10:
      received=True
     elif op==1:
      d=json.loads(data);received=True
      if d.get('type')=='subscribe':
       cid=int(d['channel_id']);subs.add(cid);head=c.execute('select event_seq from channels where id=?',(cid,)).fetchone()[0];cursors[cid]=head;out({'type':'subscribed','channel_id':cid,'head_event_id':head})
    except socket.timeout:pass
    if received:
     c.execute('update presence set last_seen=? where user_id=?',(time.time(),uid));c.execute('update presence_conn set last_seen=? where conn_id=?',(time.time(),conn_id))
    for e in c.execute('select * from global_events where id>? order by id',(global_last,)).fetchall():
     global_last=e['id'];out(json.loads(e['payload']))
    for cid in list(subs):
     for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,cursors.get(cid,0))).fetchall():
      cursors[cid]=e['event_id'];out(json.loads(e['payload']))
    c.commit()
  except Exception:pass
  finally:c.execute('delete from presence_conn where conn_id=?',(conn_id,));c.execute('delete from presence where user_id=? and not exists(select 1 from presence_conn where user_id=?)',(uid,uid));c.commit()
def validtz(x):
 try:ZoneInfo(x);return True
 except:return False
def groupjson(r):return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'])}
if __name__=='__main__':
 import socket
 init();ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
