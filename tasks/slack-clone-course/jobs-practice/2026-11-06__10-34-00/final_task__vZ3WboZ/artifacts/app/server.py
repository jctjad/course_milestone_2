import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,socket,threading,time,urllib.parse
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
DB='/var/lib/huddle/huddle.db'; FILEDIR='/var/lib/huddle/files'; NODE=int(os.environ.get('NODE_ID','0'))
os.makedirs(FILEDIR,exist_ok=True)
def conn():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row; return c
def now():
 return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 c=conn(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,mentions TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS mf(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY, payload TEXT);
''');
 try: c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'")
 except: pass
 c.close()
init()
def row(sql,p=()):
 c=conn()
 try:return c.execute(sql,p).fetchone()
 finally:c.close()
def rows(sql,p=()):
 c=conn()
 try:return c.execute(sql,p).fetchall()
 finally:c.close()
def user_json(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def user_by_id(i): return row('select * from users where id=?',(i,))
def token_user(t):
 r=row('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t or '',)); return r
def chan(i): return row('select * from channels where id=?',(i,))
def workspace(slug): return row('select * from workspaces where slug=?',(slug,))
def role(w,u):
 r=row('select role from wm where workspace_id=? and user_id=?',(w,u)); return r['role'] if r else None
def cmember(ch,u): return bool(row('select 1 from cm where channel_id=? and user_id=?',(ch,u)))
def chjson(x): return {k:(bool(x[k]) if k in ('is_private','is_dm','is_archived') else x[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def reactions(mid):
 out=[]
 for r in rows('select emoji,user_id from reactions where message_id=? order by rowid',(mid,)):
  z=next((x for x in out if x['emoji']==r['emoji']),None)
  if not z:z={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(z)
  z['user_ids'].append(r['user_id']);z['count']=len(z['user_ids'])
 return out
def files_for(mid):
 return [filejson(x) for x in rows('select f.* from files f join mf on f.id=mf.file_id where mf.message_id=?',(mid,))]
def msgjson(m):
 a=user_by_id(m['author_id']); return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':user_json(a),'body':m['body'],'parent_id':m['parent_id'],'reply_count':row('select count(*) n from messages where parent_id=?',(m['id'],))['n'],'created_at':m['created_at'],'edited_at':m['edited_at'],'files':files_for(m['id']),'reactions':reactions(m['id']),'mentions':json.loads(m['mentions']) if 'mentions' in m.keys() else []}
def mention(body,ch,author):
 ids=[]
 members=[x['user_id'] for x in rows('select user_id from cm where channel_id=?',(ch['id'],))]
 if re.search(r'(^|\W)@channel(?:\W|$)',body): ids += [i for i in members if i!=author]
 for hit in re.finditer(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  s=hit.group(1)
  if s in ('channel','here'): 
   if s=='here': ids += [i for i in online() if i in members and i!=author]
   continue
  g=row('select user_ids from groups_ where workspace_id=? and handle=?',(ch['workspace_id'],s.lower()))
  if g: ids += [i for i in json.loads(g['user_ids']) if i in members and i!=author]
  else:
   u=row('select id from users where username=?',(s,))
   if u and u['id'] in members and u['id']!=author:ids.append(u['id'])
 return list(dict.fromkeys(ids))
def online():
 return [x['user_id'] for x in rows('select distinct user_id from presence where last_seen>?',(time.time()-1.5,))]
def emit(ch,kind,payload):
 c=conn(); c.execute('begin immediate'); n=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(ch,)).fetchone()['n']; c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(ch,n,kind,json.dumps(payload)));c.commit();c.close();return n
def filejson(f): return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def valid_topic(x): return isinstance(x,str) and len(x)<=250
def create_msg(ch,u,body,parent=None,fileids=None):
 c=conn(); ts=now(); mentions=mention(body,chan(ch),u); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,edited_at,mentions) values(?,?,?,?,?,?,?)',(ch,u,body,parent,ts,None,json.dumps(mentions))); mid=c.execute('select last_insert_rowid()').fetchone()[0]
 if fileids:
  for fid in fileids:c.execute('insert into mf(message_id,file_id) values(?,?)',(mid,fid))
 c.commit();m=row('select * from messages where id=?',(mid,)); j=msgjson(m);emit(ch,'message.reply' if parent else 'message.created',{'message':j});c.close();return m
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def hdr(self,code,n=0,typ='application/json',extra={}):
  self.send_response(code);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(n));self.send_header('Connection','close')
  for k,v in extra.items():self.send_header(k,v)
  self.end_headers()
 def out(self,code,obj):
  b=json.dumps(obj,separators=(',',':')).encode();self.hdr(code,len(b));self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return self.rfile.read(n)
 def auth(self):
  x=self.headers.get('Authorization',''); return token_user(x[7:] if x.startswith('Bearer ') else '')
 def need(self):
  u=self.auth()
  if not u:self.out(401,{'error':'unauthorized'})
  return u
 def req_path(self): return urllib.parse.urlsplit(self.path).path
 def do_GET(self):
  p=self.req_path()
  if p=='/api/health':return self.out(200,{'status':'ok','node_id':NODE})
  if p=='/api/ws':return self.ws()
  u=self.need()
  if not u:return
  m=re.match(r'/api/auth/me$',p)
  if m:return self.out(200,{'user':user_json(u)})
  m=re.match(r'/api/users/(\d+)$',p)
  if m:
   x=user_by_id(m.group(1));return self.out(200,{'user':user_json(x)} if x else {'error':'not found'}) if x else self.out(404,{'error':'not found'})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=workspace(m.group(1))
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   gs=[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rows('select * from groups_ where workspace_id=?',(w['id'],))]
   return self.out(200,{'groups':gs})
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=workspace(m.group(1))
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   cs=[chjson(x) for x in rows('select * from channels where workspace_id=? and is_archived=0 and (is_private=0 or id in(select channel_id from cm where user_id=?))',(w['id'],u['id']))]
   return self.out(200,{'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':cs})
  m=re.match(r'/api/channels/(\d+)/members$',p)
  if m:
   x=chan(m.group(1))
   if not x:return self.out(404,{})
   if not cmember(x['id'],u['id']) and (x['is_private'] or x['is_dm']):return self.out(403,{})
   return self.out(200,{'members':[user_json(user_by_id(z['user_id'])) for z in rows('select user_id from cm where channel_id=?',(x['id'],))]})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   x=chan(m.group(1))
   if not x:return self.out(404,{})
   if not canread(x,u['id']):return self.out(403,{})
   return self.out(200,{'messages':[msgjson(z) for z in rows('select * from messages where channel_id=? order by id desc',(x['id'],))],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)(/download)?$',p)
  if m:
   f=row('select * from files where id=?',(m.group(1),))
   if not f:return self.out(404,{})
   if m.group(2):
    self.hdr(200,len(f['data']),f['content_type'],{'Content-Disposition':f'attachment; filename="{f["filename"]}"'});return self.wfile.write(f['data'])
   return self.out(200,{'file':filejson(f)})
  self.out(404,{})
 def do_PATCH(self):
  p=self.req_path(); u=self.auth()
  if not u:return self.out(401,{})
  if p=='/api/users/me':
   try:d=json.loads(self.body())
   except:return self.out(400,{})
   allowed={'display_name':(str,1,80),'timezone':(str,1,128),'status_text':(str,0,100),'status_emoji':(str,0,64),'avatar_url':(str,0,2048)}
   for k,(typ,lo,hi) in allowed.items():
    if k in d:
     if not isinstance(d[k],typ) or not lo<=len(d[k])<=hi:return self.out(400,{})
     if k=='timezone':
      try:ZoneInfo(d[k])
      except:return self.out(400,{})
   c=conn()
   fields=[k for k in allowed if k in d]
   if fields:
    c.execute('update users set '+','.join(k+'=?' for k in fields)+' where id=?',[d[k] for k in fields]+[u['id']])
    c.execute('insert into global_events(payload) values(?)',(json.dumps({'type':'user.updated','user':user_json(user_by_id(u['id']))}),))
   c.commit();c.close()
   return self.out(200,{'user':user_json(user_by_id(u['id']))})
  p=self.req_path(); u=self.auth()
  if not u:return self.out(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=workspace(m.group(1)); g=row('select * from groups_ where workspace_id=? and handle=?',(w['id'],m.group(2))) if w else None
   if not w or not g:return self.out(404,{})
   if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.out(403,{})
   try:d=json.loads(self.body())
   except:return self.out(400,{})
   name=d.get('name',g['name']); ids=d.get('user_ids',json.loads(g['user_ids']))
   if not isinstance(name,str) or not isinstance(ids,list):return self.out(400,{})
   c=conn();c.execute('update groups_ set name=?,user_ids=? where id=?',(name,json.dumps(ids),g['id']));c.commit();c.close()
   return self.out(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
  p=self.req_path(); u=self.auth()
  if not u:return self.out(401,{})
  m=re.match(r'/api/channels/(\d+)$',p)
  if not m:return self.out(404,{})
  x=chan(m.group(1))
  if not x:return self.out(404,{})
  if not canread(x,u['id']) and role(x['workspace_id'],u['id'])!='owner':return self.out(403,{})
  try:d=json.loads(self.body())
  except:return self.out(400,{})
  topic=d.get('topic')
  if not valid_topic(topic):return self.out(400,{})
  c=conn();c.execute('update channels set topic=? where id=?',(topic,x['id']));c.commit();c.close()
  return self.out(200,{'channel':chjson(chan(x['id']))})
 def do_DELETE(self):
  p=self.req_path(); u=self.auth()
  if not u:return self.out(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=workspace(m.group(1)); g=row('select * from groups_ where workspace_id=? and handle=?',(w['id'],m.group(2))) if w else None
   if not w:return self.out(404,{})
   if g and g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.out(403,{})
   if not g:return self.out(200,{'deleted':True})
   conn().execute('delete from groups_ where id=?',(g['id'],));return self.out(200,{'deleted':True})
  p=self.req_path(); u=self.auth()
  if not u:return self.out(401,{})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',p)
  if not m:return self.out(404,{})
  mm=row('select * from messages where id=?',(m.group(1),))
  if not mm:return self.out(404,{})
  x=chan(mm['channel_id'])
  if not canread(x,u['id']):return self.out(403,{})
  e=urllib.parse.unquote(m.group(2))
  if not e:return self.out(400,{})
  c=conn();c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mm['id'],u['id'],e));changed=c.total_changes;c.commit();c.close()
  if changed:emit(x['id'],'reaction.removed',{'message_id':mm['id'],'emoji':e,'user_id':u['id'],'reactions':reactions(mm['id'])})
  return self.out(200,{'reactions':reactions(mm['id'])})
 def do_POST(self):
  p=self.req_path(); u=self.auth()
  if p=='/api/auth/register':
   try:d=json.loads(self.body()); un=d['username'];pw=d['password']
   except:self.out(400,{});return
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.out(400,{})
   if row('select 1 from users where username=?',(un,)):return self.out(409,{})
   c=conn();c.execute('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(un,pw,d.get('display_name',un),'UTC','','',''));uid=c.execute('select last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));c.commit();c.close();return self.out(201,{'user':user_json(user_by_id(uid)),'token':t})
  if p=='/api/auth/login':
   try:d=json.loads(self.body());x=row('select * from users where username=? and password=?',(d['username'],d['password']))
   except:x=None
   if not x:return self.out(401,{})
   t=secrets.token_urlsafe(24);c=conn();c.execute('insert into tokens values(?,?)',(t,x['id']));c.commit();c.close();return self.out(200,{'user':user_json(x),'token':t})
  if p=='/api/files':
   if not u:return self.out(401,{})
   data=self.body();ct=self.headers.get('Content-Type','')
   if len(data)>10485760+100000:return self.out(413,{})
   mo=re.search('boundary=([^;]+)',ct)
   if not mo:return self.out(400,{})
   parts=data.split(b'--'+mo.group(1).strip().encode())
   part=next((z for z in parts if b'name="file"' in z),None)
   if not part:return self.out(400,{})
   head,dat=part.split(b'\r\n\r\n',1);dat=dat.rsplit(b'\r\n',1)[0]; fn=re.search(br'filename="([^"]*)"',head); typ=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
   if not fn:return self.out(400,{})
   if len(dat)>10485760:return self.out(413,{})
   c=conn();c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],fn.group(1).decode(),(typ.group(1).decode() if typ else 'application/octet-stream'),len(dat),now(),dat));fid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit();return self.out(201,{'file':filejson(row('select * from files where id=?',(fid,)))})
  if not u:return self.out(401,{})
  try:d=json.loads(self.body())
  except:d={}
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=workspace(m.group(1))
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   h=d.get('handle'); name=d.get('name',''); ids=d.get('user_ids',[])
   if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel') or not isinstance(name,str) or not isinstance(ids,list):return self.out(400,{})
   if row('select 1 from groups_ where workspace_id=? and handle=?',(w['id'],h)):return self.out(409,{})
   c=conn();c.execute('insert into groups_(workspace_id,creator_id,handle,name,user_ids) values(?,?,?,?,?)',(w['id'],u['id'],h,name,json.dumps(ids)));gid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit();c.close()
   return self.out(201,{'group':{'id':gid,'handle':h,'name':name,'member_user_ids':ids}})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   w=workspace(m.group(1))
   if not w:return self.out(404,{})
   if role(w['id'],u['id'])!='owner':return self.out(403,{})
   name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.out(400,{})
   if row('select 1 from channels where workspace_id=? and name=?',(w['id'],name)):return self.out(409,{})
   c=conn();c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();return self.out(201,{'channel':chjson(chan(cid))})
  if p=='/api/workspaces':
   wslug=d.get('slug','');name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',wslug):return self.out(400,{})
   if workspace(wslug):return self.out(409,{})
   c=conn();c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(wslug,name,u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into wm values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name,is_private) values(?,?,0)',(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();return self.out(201,{'workspace':dict(workspace(wslug)),'general_channel':chjson(chan(cid))})
  m=re.match(r'/api/dms$',p)
  if m:
   other=user_by_id(d.get('user_id'))
   if not other:return self.out(404,{})
   if other['id']==u['id']:return self.out(400,{})
   a,b=sorted((u['id'],other['id']));x=row('select id from channels where is_dm=1 and name=?',(f'dm-{a}-{b}',))
   if x:return self.out(200,{'channel_id':x['id']})
   c=conn();c.execute('insert into channels(name,is_private,is_dm) values(?,1,1)',(f'dm-{a}-{b}',));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,a));c.execute('insert into cm values(?,?)',(cid,b));c.commit();return self.out(200,{'channel_id':cid})
  m=re.match(r'/api/channels/(\d+)/join$',p)
  if m:
   x=chan(m.group(1))
   if not x:return self.out(404,{})
   if x['is_private']:return self.out(403,{})
   c=conn();c.execute('insert or ignore into wm values(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('insert or ignore into cm values(?,?)',(x['id'],u['id']));c.commit();return self.out(200,{'channel':chjson(x)})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   x=chan(m.group(1))
   if not x:return self.out(404,{})
   if not canread(x,u['id']):return self.out(403,{})
   body=d.get('body','')
   if x['is_archived'] and body != '/unarchive':return self.out(423,{})
   if not isinstance(body,str) or not body.strip():return self.out(400,{})
   if body.startswith('/') and not body.startswith('//'):
    z=slash(x,u,body)
    if z:return self.out(*z)
    return self.out(400,{})
   fs=d.get('file_ids',[])
   for fid in fs:
    f=row('select * from files where id=? and uploader_id=? and not exists(select 1 from mf where file_id=?)',(fid,u['id'],fid))
    if not f:return self.out(400,{})
   mm=create_msg(x['id'],u['id'],body,d.get('parent_id'),fs);return self.out(201,{'message':msgjson(mm)})
  m=re.match(r'/api/messages/(\d+)/reactions$',p)
  if m:
   mm=row('select * from messages where id=?',(m.group(1),))
   if not mm:return self.out(404,{})
   x=chan(mm['channel_id'])
   if not canread(x,u['id']):return self.out(403,{})
   e=d.get('emoji'); 
   if not isinstance(e,str) or not e:return self.out(400,{})
   c=conn();c.execute('insert or ignore into reactions values(?,?,?,?)',(mm['id'],u['id'],e,now()));changed=c.total_changes;c.commit()
   if changed:emit(x['id'],'reaction.added',{'message_id':mm['id'],'emoji':e,'user_id':u['id'],'reactions':reactions(mm['id'])})
   return self.out(200,{'reactions':reactions(mm['id'])})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',p)
  if m:
   mm=row('select * from messages where id=?',(m.group(1),))
   if not mm:return self.out(404,{})
   x=chan(mm['channel_id'])
   if not canread(x,u['id']):return self.out(403,{})
   e=urllib.parse.unquote(m.group(2))
   if not e:return self.out(400,{})
   c=conn();c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mm['id'],u['id'],e));ch=c.total_changes;c.commit()
   if ch:emit(x['id'],'reaction.removed',{'message_id':mm['id'],'emoji':e,'user_id':u['id'],'reactions':reactions(mm['id'])})
   return self.out(200,{'reactions':reactions(mm['id'])})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',p)
  if m:
   x=chan(m.group(1))
   if not x:return self.out(404,{})
   if role(x['workspace_id'],u['id'])!='owner':return self.out(403,{})
   v=m.group(2)=='archive';conn().execute('update channels set is_archived=? where id=?',(v,x['id']));return self.out(200,{'channel':chjson(chan(x['id']))})
  self.out(404,{})
def canread(x,u):
 return bool(x['is_dm'] and cmember(x['id'],u) or (not x['is_dm'] and (not x['is_private'] and role(x['workspace_id'],u) or cmember(x['id'],u))))
def slash(x,u,b):
 cmd,arg=(b[1:].split(' ',1)+[''])[:2] if ' ' in b[1:] else (b[1:],'')
 if cmd=='me':
  if not arg.strip():return None
  return 201,{'message':msgjson(create_msg(x['id'],u['id'],b))}
 if cmd=='shrug':
  return 201,{'message':msgjson(create_msg(x['id'],u['id'],(arg.strip()+' ' if arg.strip() else '')+'¯\\_(ツ)_/¯'))}
 if cmd=='topic':
  if not arg.strip() or not valid_topic(arg.strip()) or (not cmember(x['id'],u['id']) and role(x['workspace_id'],u['id'])!='owner'):return (403,{}) if not cmember(x['id'],u['id']) else None
  conn().execute('update channels set topic=? where id=?',(arg.strip(),x['id']));return 201,{'message':None,'channel':chjson(chan(x['id']))}
 if cmd=='invite':
  if not cmember(x['id'],u['id']):return 403,{}
  if not arg.startswith('@') or not arg[1:].strip():return 400,{}
  q=user_by_id(row('select id from users where username=?',(arg[1:].strip(),))['id']) if row('select id from users where username=?',(arg[1:].strip(),)) else None
  if not q:return (404,{})
  if not role(x['workspace_id'],q['id']):return (403,{})
  conn().execute('insert or ignore into cm values(?,?)',(x['id'],q['id']));return 201,{'message':None,'invited_user':user_json(q)}
 if cmd in ('archive','unarchive'):
  if role(x['workspace_id'],u['id'])!='owner':return 403,{}
  v=cmd=='archive';conn().execute('update channels set is_archived=? where id=?',(v,x['id']));return 201,{'message':None,'channel':chjson(chan(x['id']))}
 return None
def wsframe(obj):
 b=json.dumps(obj,separators=(',',':')).encode();n=len(b)
 if n<126:return bytes([129,n])+b
 return bytes([129,126])+struct.pack('!H',n)+b
SOCKS=[]; LOCK=threading.Lock()
def poll():
 last=0; glast=0
 while True:
  time.sleep(.1)
  for e in rows('select * from events where id>? order by id',(last,)):
   last=e['id'];fr={'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id']};fr.update(json.loads(e['payload']))
   with LOCK:
    for ss,ch,uu in list(SOCKS):
     if ch==e['channel_id']:
      try:ss.sendall(wsframe(fr))
      except:pass
  for e in rows('select * from global_events where id>? order by id',(glast,)):
   glast=e['id'];fr=json.loads(e['payload'])
   with LOCK:
    for ss,ch,uu in list(SOCKS):
     try:ss.sendall(wsframe(fr))
     except:pass
  conn().execute('update presence set last_seen=? where conn_id like ?',(time.time(),f'{NODE}:%'))
threading.Thread(target=poll,daemon=True).start()
def H_ws(self):
 t=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('token',[''])[0];u=token_user(t)
 if not u:return self.send_error(401)
 key=self.headers.get('Sec-WebSocket-Key')
 if not key:return self.send_error(400)
 accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
 self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
 s=self.connection;cid=f'{NODE}:{id(s)}';conn().execute('insert into presence values(?,?,?)',(cid,u['id'],time.time()));sub=None
 with LOCK: SOCKS.append((s,None,u['id']))
 try:
  while True:
   h=s.recv(2)
   if not h:break
   n=h[1]&127
   if n==126:n=struct.unpack('!H',s.recv(2))[0]
   mask=s.recv(4);data=s.recv(n);data=bytes(data[i]^mask[i%4] for i in range(n));d=json.loads(data)
   if d.get('type')=='subscribe':
    sub=d['channel_id']
    with LOCK:
     for i,z in enumerate(SOCKS):
      if z[0] is s:SOCKS[i]=(s,sub,u['id'])
    head=row('select coalesce(max(event_id),0) n from events where channel_id=?',(sub,))['n'];s.sendall(wsframe({'type':'subscribed','channel_id':sub,'head_event_id':head}));
    with LOCK:
     for i,z in enumerate(SOCKS):
      if z[0] is s: SOCKS[i]=(s,sub,u['id'])
 except:pass
 finally:
  conn().execute('delete from presence where conn_id=?',(cid,))
  with LOCK:
   SOCKS[:]=[z for z in SOCKS if z[0] is not s]
H.ws=H_ws
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
