import os, re, json, uuid, sqlite3, hashlib, secrets, threading, time, mimetypes
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
DB='/var/lib/huddle/huddle.db'; FILEDIR='/var/lib/huddle/files'
NODE=int(os.environ.get('NODE_ID','0')); PORT=8000+NODE
os.makedirs(FILEDIR,exist_ok=True)
conns={}; lock=threading.RLock()
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 c=db(); c.executescript('''
 PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT);
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
 CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);
 '''); c.commit(); c.close()
def q(sql,args=(),one=False):
 c=db()
 try: r=c.execute(sql,args); x=r.fetchone() if one else r.fetchall(); c.commit(); return x
 finally:c.close()
def userj(u):
 return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chan(c): return {k:(bool(c[k]) if k in ('is_private','is_dm','is_archived') else c[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def ufrom(tok):
 r=q('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(tok,),True); return r
def auth(h):
 a=h.headers.get('Authorization',''); return ufrom(a[7:] if a.startswith('Bearer ') else '')
def msgj(m):
 u=q('select * from users where id=?',(m['author_id'],),True)
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':userj(u),'body':m['body'],'parent_id':m['parent_id'],'reply_count':q('select count(*) n from messages where parent_id=?',(m['id'],),True)['n'],'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[filej(x) for x in q('select f.* from files f join attachments a on a.file_id=f.id where a.message_id=?',(m['id'],))],'reactions':reactions(m['id']),'mentions':mentions(m['body'],m['channel_id'],m['author_id'])}
def filej(f): return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def reactions(mid):
 rows=q('select emoji,user_id from reactions where message_id=? order by rowid',(mid,)); out=[]
 for r in rows:
  z=next((x for x in out if x['emoji']==r['emoji']),None)
  if not z: z={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(z)
  z['user_ids'].append(r['user_id']);z['count']=len(z['user_ids'])
 return out
def members(cid): return q('select u.* from users u join cm on cm.user_id=u.id where cm.channel_id=?',(cid,))
def readable(c,uid):
 if c['is_dm']: return bool(q('select 1 from cm where channel_id=? and user_id=?',(c['id'],uid),True))
 return bool(q('select 1 from wm where workspace_id=? and user_id=?',(c['workspace_id'],uid),True)) and (not c['is_private'] or bool(q('select 1 from cm where channel_id=? and user_id=?',(c['id'],uid),True)))
def emit(cid,kind,payload):
 c=db(); c.execute('insert or ignore into counters(channel_id,n) values(?,0)',(cid,)); c.execute('update counters set n=n+1 where channel_id=?',(cid,)); n=c.execute('select n from counters where channel_id=?',(cid,)).fetchone()[0]; fr={'type':kind,'event_id':n,'channel_id':cid,**payload}; c.execute('insert into events values(?,?,?,?,?)',(cid,n,kind,json.dumps(fr),now()));c.commit();c.close();return fr
def broadcast_global(frame):
 c=db(); c.execute('insert into global_events(payload) values(?)',(json.dumps(frame),)); c.commit(); c.close()
def broadcast(fr, global_only=False):
 with lock:
  for s in list(conns.values()):
   if global_only or fr.get('global',False) or fr.get('channel_id') in s['subs']: ws_send(s['sock'],fr)
def poll():
 r=q('select coalesce(max(rowid),0) n from events',(),True); last=r['n'] if r else 0
 g=q('select coalesce(max(id),0) n from global_events',(),True); glast=g['n'] if g else 0
 while True:
  try:
   rows=q('select rowid,* from events where rowid>? order by rowid',(last,))
   for r in rows: last=r['rowid']; broadcast(json.loads(r['payload']))
   grows=q('select id,payload from global_events where id>? order by id',(glast,))
   for r in grows: glast=r['id']; broadcast(json.loads(r['payload']), True)
   with lock:
    for s in list(conns.values()):
     if time.time()-s['seen']>1: q('update presence set last_seen=? where conn_id=?',(time.time(),s['id']));s['seen']=time.time()
  except: pass
  time.sleep(.1)
def ws_send(sock,o):
 b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
 if n < 126: h=bytes([129,n])
 elif n < 65536: h=bytes([129,126])+n.to_bytes(2,'big')
 else: h=bytes([129,127])+n.to_bytes(8,'big')
 sock.sendall(h+b)
def validate_tz(x):
 try: ZoneInfo(x);return True
 except:return False
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def out(self,status,obj=None,headers={}):
  b=b'' if obj is None else (obj if isinstance(obj,bytes) else json.dumps(obj).encode())
  self.send_response(status);self.send_header('Content-Length',str(len(b)))
  for k,v in headers.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return self.rfile.read(n)
 def js(self): return json.loads(self.body() or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path; u=auth(self)
  if path=='/api/health': return self.out(200,{'status':'ok','node_id':NODE})
  if path=='/api/ws': return self.websocket()
  if path.startswith('/api/files/') and path.endswith('/download'):
   f=q('select * from files where id=?',(path.split('/')[3],),True)
   if not f:return self.out(404,{'error':'not found'})
   return self.out(200,bytes(f['data']),{'Content-Type':f['content_type'],'Content-Disposition':'attachment; filename="'+f['filename']+'"'})
  if not u:return self.out(401,{'error':'unauthorized'})
  if path=='/api/auth/me':return self.out(200,{'user':userj(u)})
  if path.startswith('/api/users/'):
   x=q('select * from users where id=?',(path.split('/')[-1],),True);return self.out(200,{'user':userj(x)}) if x else self.out(404,{})
  if path=='/api/workspaces':
   ws=q('select w.* from workspaces w join wm on wm.workspace_id=w.id where wm.user_id=?',(u['id'],));return self.out(200,{'workspaces':[dict(x) for x in ws]})
  if path.startswith('/api/workspaces/'):
   bits=path.split('/'); w=q('select * from workspaces where slug=?',(bits[3],),True)
   if not w:return self.out(404,{})
   if path.endswith('/groups'):
    if not q('select 1 from wm where workspace_id=? and user_id=?',(w['id'],u['id']),True):return self.out(403,{})
    return self.out(200,{'groups':[groupj(x) for x in q('select * from groupsx where workspace_id=?',(w['id'],))]})
   if not q('select 1 from wm where workspace_id=? and user_id=?',(w['id'],u['id']),True):return self.out(403,{})
   cs=q('select c.* from channels c join cm on cm.channel_id=c.id where c.workspace_id=? and cm.user_id=? and c.is_archived=0',(w['id'],u['id']))
   return self.out(200,{'workspace':dict(w),'channels':[chan(x) for x in cs]})
  if path.startswith('/api/channels/') and path.endswith('/members'):
   c=q('select * from channels where id=?',(path.split('/')[3],),True)
   return self.out(404,{}) if not c else self.out(200,{'members':[userj(x) for x in members(c['id'])]})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   c=q('select * from channels where id=?',(path.split('/')[3],),True)
   if not c:return self.out(404,{})
   if not readable(c,u['id']):return self.out(403,{})
   ms=q('select * from messages where channel_id=? and parent_id is null order by id desc',(c['id'],));return self.out(200,{'messages':[msgj(x) for x in ms],'next_cursor':None})
  if path.startswith('/api/files/'):
   f=q('select * from files where id=?',(path.split('/')[3],),True);return self.out(404,{}) if not f else self.out(200,{'file':filej(f)})
  return self.out(404,{})
 def do_POST(self):
  path=urlparse(self.path).path; u=auth(self)
  if path=='/api/auth/register':
   d=self.js()
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.out(400,{})
   if q('select 1 from users where username=?',(d['username'],),True):return self.out(409,{})
   uid=q('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?) returning id',(d['username'],d['password'],d.get('display_name') or d['username'],'UTC','','',''),True)['id']; tok=secrets.token_urlsafe(24);q('insert into tokens values(?,?)',(tok,uid));return self.out(201,{'user':userj(q('select * from users where id=?',(uid,),True)),'token':tok})
  if path=='/api/auth/login':
   d=self.js();x=q('select * from users where username=? and password=?',(d.get('username'),d.get('password')),True)
   if not x:return self.out(401,{})
   t=secrets.token_urlsafe(24);q('insert into tokens values(?,?)',(t,x['id']));return self.out(200,{'user':userj(x),'token':t})
  if not u:return self.out(401,{})
  if path=='/api/workspaces':
   d=self.js()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.out(400,{})
   if q('select 1 from workspaces where slug=?',(d['slug'],),True):return self.out(409,{})
   c=db();c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into wm values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name,is_private) values(?,?,0)',(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();c.close();return self.out(201,{'workspace':dict(q('select * from workspaces where id=?',(wid,),True)),'general_channel':chan(q('select * from channels where id=?',(cid,),True))})
  if path.startswith('/api/workspaces/') and path.endswith('/groups'):
   w=q('select * from workspaces where slug=?',(path.split('/')[3],),True)
   if not w:return self.out(404,{})
   role=q('select role from wm where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role:return self.out(403,{})
   d=self.js(); h=d.get('handle','')
   if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.out(400,{})
   if q('select 1 from groupsx where workspace_id=? and handle=?',(w['id'],h),True):return self.out(409,{})
   ids=d.get('user_ids',[])
   if not isinstance(ids,list) or not all(isinstance(x,int) for x in ids):return self.out(400,{})
   c=db();c.execute('insert into groupsx(workspace_id,creator_id,handle,name,user_ids) values(?,?,?,?,?)',(w['id'],u['id'],h,d.get('name',''),json.dumps(ids)));gid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit();c.close()
   return self.out(201,{'group':groupj(q('select * from groupsx where id=?',(gid,),True))})
  if path.startswith('/api/workspaces/') and path.endswith('/channels'):
   w=q('select * from workspaces where slug=?',(path.split('/')[3],),True);d=self.js()
   if not w:return self.out(404,{})
   role=q('select role from wm where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role:return self.out(403,{})
   if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',d.get('name','')) or len(d['name'])>32:return self.out(400,{})
   if d.get('is_private') and role['role']!='owner':return self.out(403,{})
   if q('select 1 from channels where workspace_id=? and name=?',(w['id'],d['name']),True):return self.out(409,{})
   c=db();c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();c.close();return self.out(201,{'channel':chan(q('select * from channels where id=?',(cid,),True))})
  if path.startswith('/api/channels/') and (path.endswith('/archive') or path.endswith('/unarchive')):
   c=q('select * from channels where id=?',(path.split('/')[3],),True)
   if not c:return self.out(404,{})
   if c['workspace_id'] is None:return self.out(403,{})
   role=q('select role from wm where workspace_id=? and user_id=?',(c['workspace_id'],u['id']),True)
   if not role or role['role']!='owner':return self.out(403,{})
   archived=1 if path.endswith('/archive') else 0
   q('update channels set is_archived=? where id=?',(archived,c['id']))
   fresh=q('select * from channels where id=?',(c['id'],),True)
   fr=emit(c['id'],'channel.archived' if archived else 'channel.unarchived',{'channel':chan(fresh)})
   return self.out(200,{'channel':chan(fresh)})
  if path.startswith('/api/channels/') and path.endswith('/join'):
   c=q('select * from channels where id=?',(path.split('/')[3],),True)
   if not c:return self.out(404,{})
   if c['is_private']:return self.out(403,{})
   q('insert or ignore into cm values(?,?)',(c['id'],u['id']));q('insert or ignore into wm values(?,?,?)',(c['workspace_id'],u['id'],'member'));return self.out(200,{'channel':chan(c)})
  if path=='/api/dms':
   d=self.js(); other=q('select * from users where id=?',(d.get('user_id'),),True)
   if not other or other['id']==u['id']:return self.out(400 if other else 404,{})
   a,b=sorted((u['id'],other['id']));x=q('select channel_id from dms where a=? and b=?',(a,b),True)
   if x:return self.out(200,{'channel_id':x['channel_id']})
   c=db();c.execute('insert into channels(name,is_private,is_dm) values(?,?,1)',(f'dm-{a}-{b}',1));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into dms values(?,?,?)',(a,b,cid));c.execute('insert into cm values(?,?),(?,?)',(cid,a,cid,b));c.commit();c.close();return self.out(200,{'channel_id':cid})
  if path=='/api/files':
   raw=self.body(); ct=self.headers.get('Content-Type',''); m=re.search('boundary=([^;]+)',ct)
   if len(raw)>10485760+100000:return self.out(413,{})
   if not m:return self.out(400,{})
   part=raw.split(('--'+m.group(1)).encode())
   p=next((x for x in part if b'name="file"' in x),None)
   if not p:return self.out(400,{})
   head,data=p.split(b'\r\n\r\n',1);data=data.rsplit(b'\r\n',1)[0]; fn=re.search(br'filename="([^"]*)"',head).group(1).decode(); typ=(re.search(br'Content-Type:\s*([^\r\n]+)',head) or [None,b'application/octet-stream'])[1].decode()
   if len(data)>10485760:return self.out(413,{})
   c=db();c.execute('insert into files(uploader_id,filename,content_type,size,data,created_at) values(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),data,now()));fid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit();c.close();return self.out(201,{'file':filej(q('select * from files where id=?',(fid,),True))})
  if path.startswith('/api/messages/') and path.endswith('/reactions'):
   mid=path.split('/')[3];m=q('select * from messages where id=?',(mid,),True)
   if not m:return self.out(404,{})
   c=q('select * from channels where id=?',(m['channel_id'],),True)
   if not readable(c,u['id']):return self.out(403,{})
   e=self.js().get('emoji')
   if not isinstance(e,str) or not e:return self.out(400,{})
   before=len(q('select * from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],e)))
   q('insert or ignore into reactions values(?,?,?,?)',(mid,u['id'],e,now()))
   if not before: emit(c['id'],'reaction.added',{'message_id':int(mid),'emoji':e,'user_id':u['id'],'reactions':reactions(mid)})
   return self.out(200,{'reactions':reactions(mid)})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   cid=path.split('/')[3];c=q('select * from channels where id=?',(cid,),True);d=self.js()
   if not c:return self.out(404,{})
   if not readable(c,u['id']):return self.out(403,{})
   if c['is_archived'] and not (isinstance(d.get('body'),str) and d.get('body','').strip().startswith('/unarchive')):return self.out(423,{})
   body=d.get('body','')
   if not isinstance(body,str) or not body:return self.out(400,{})
   if body.startswith('/') and not body.startswith('//'):
    cmd,arg=(body[1:].split(' ',1)+[''])[:2]
    if cmd=='me' and not arg.strip():return self.out(400,{})
    if cmd=='shrug':body=(arg.strip()+' ' if arg.strip() else '')+'¯\\_(ツ)_/¯'
    elif cmd=='topic':
     if not arg.strip() or len(arg)>250:return self.out(400,{})
     if not (q('select 1 from cm where channel_id=? and user_id=?',(c['id'],u['id']),True) or q('select 1 from wm where workspace_id=? and user_id=? and role=?',(c['workspace_id'],u['id'],'owner'),True)):return self.out(403,{})
     q('update channels set topic=? where id=?',(arg.strip(),c['id']))
     fresh=q('select * from channels where id=?',(c['id'],),True)
     return self.out(201,{'message':None,'channel':chan(fresh)})
    elif cmd=='invite':
     if not readable(c,u['id']):return self.out(403,{})
     if not arg.startswith('@') or not arg[1:].strip():return self.out(400,{})
     target=q('select * from users where username=?',(arg[1:].strip(),),True)
     if not target:return self.out(404,{})
     if c['workspace_id'] is None or not q('select 1 from wm where workspace_id=? and user_id=?',(c['workspace_id'],target['id']),True):return self.out(403,{})
     q('insert or ignore into cm values(?,?)',(c['id'],target['id']))
     return self.out(201,{'message':None,'invited_user':userj(target)})
    elif cmd in ('archive','unarchive'):
     if not q('select 1 from wm where workspace_id=? and user_id=? and role="owner"',(c['workspace_id'],u['id']),True):return self.out(403,{})
     archived=1 if cmd=='archive' else 0
     q('update channels set is_archived=? where id=?',(archived,c['id']))
     fresh=q('select * from channels where id=?',(c['id'],),True)
     emit(c['id'],'channel.archived' if archived else 'channel.unarchived',{'channel':chan(fresh)})
     return self.out(201,{'message':None,'channel':chan(fresh)})
    elif cmd not in ('me','shrug'):return self.out(400,{})
   c0=db();c0.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),now()));mid=c0.execute('select last_insert_rowid()').fetchone()[0]
   for fid in d.get('file_ids',[]): 
    f=q('select * from files where id=?',(fid,),True)
    if not f or f['uploader_id']!=u['id'] or q('select 1 from attachments where file_id=?',(fid,),True):c0.rollback();c0.close();return self.out(400,{})
    c0.execute('insert into attachments values(?,?)',(mid,fid))
   c0.commit();c0.close();m=q('select * from messages where id=?',(mid,),True);fr=emit(c['id'],'message.created',{'message':msgj(m)});return self.out(201,{'message':msgj(m)})
  return self.out(404,{})
 def do_PATCH(self):
  u=auth(self)
  if not u:return self.out(401,{})
  p=urlparse(self.path).path;d=self.js()
  if p=='/api/users/me':
   fields={}
   for k,v in d.items():
    if k in ('display_name','status_text','status_emoji','avatar_url') and (not isinstance(v,str) or len(v)<(1 if k=='display_name' else 0) or len(v)>{'display_name':80,'status_text':100,'status_emoji':64,'avatar_url':2048}[k]):return self.out(400,{})
    if k=='timezone' and (not isinstance(v,str) or not validate_tz(v)):return self.out(400,{})
    if k in ('display_name','timezone','status_text','status_emoji','avatar_url'):fields[k]=v
   if fields:q('update users set '+','.join(k+'=?' for k in fields)+' where id=?',tuple(fields.values())+(u['id'],))
   x=userj(q('select * from users where id=?',(u['id'],),True));fr={'type':'user.updated','user':x}
   broadcast_global(fr)
   return self.out(200,{'user':x})
  if p.startswith('/api/channels/'):
   c=q('select * from channels where id=?',(p.split('/')[3],),True)
   if not c:return self.out(404,{})
   if not readable(c,u['id']):return self.out(403,{})
   if 'topic' in d and (not isinstance(d['topic'],str) or len(d['topic'])>250):return self.out(400,{})
   q('update channels set topic=? where id=?',(d['topic'],c['id']));return self.out(200,{'channel':chan(q('select * from channels where id=?',(c['id'],),True))})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   bits=p.split('/');w=q('select * from workspaces where slug=?',(bits[3],),True);g=q('select * from groupsx where workspace_id=? and handle=?',(w['id'],bits[5]),True)
   if not g:return self.out(404,{})
   role=q('select role from wm where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role or (g['creator_id']!=u['id'] and role['role']!='owner'):return self.out(403,{})
   q('update groupsx set name=?,user_ids=? where id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['user_ids']))),g['id']));return self.out(200,{'group':groupj(q('select * from groupsx where id=?',(g['id'],),True))})
  return self.out(404,{})
 def do_DELETE(self):
  u=auth(self)
  if not u:return self.out(401,{})
  p=urlparse(self.path).path
  if '/reactions/' in p:
   z=p.split('/');m=q('select * from messages where id=?',(z[3],),True)
   if not m:return self.out(404,{})
   c=q('select * from channels where id=?',(m['channel_id'],),True)
   if not readable(c,u['id']):return self.out(403,{})
   e=unquote(z[5])
   if not e:return self.out(400,{})
   before=q('select 1 from reactions where message_id=? and user_id=? and emoji=?',(m['id'],u['id'],e),True)
   if before:
    q('delete from reactions where message_id=? and user_id=? and emoji=?',(m['id'],u['id'],e))
    fr=emit(c['id'],'reaction.removed',{'message_id':int(m['id']),'emoji':e,'user_id':u['id'],'reactions':reactions(m['id'])})
   return self.out(200,{'reactions':reactions(m['id'])})
  if '/groups/' in p:
   b=p.split('/');w=q('select * from workspaces where slug=?',(b[3],),True);g=q('select * from groupsx where workspace_id=? and handle=?',(w['id'],b[5]),True)
   if g:
    role=q('select role from wm where workspace_id=? and user_id=?',(w['id'],u['id']),True)
    if not role or (g['creator_id']!=u['id'] and role['role']!='owner'):return self.out(403,{})
    q('delete from groupsx where id=?',(g['id'],))
   return self.out(200,{'deleted':True})
  return self.out(404,{})
 def websocket(self):
  tok=parse_qs(urlparse(self.path).query).get('token',[''])[0];u=ufrom(tok)
  if not u:
   try: self.request.shutdown(2)
   except: pass
   self.request.close()
   return
  s=self.request; key=self.headers.get('Sec-WebSocket-Key'); import base64,hashlib
  accept=base64.b64encode(hashlib.sha1(key.encode()+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  sid=secrets.token_hex(8);state={'sock':s,'subs':set(),'global':True,'id':sid,'seen':time.time()};conns[sid]=state;q('insert into presence values(?,?,?)',(sid,u['id'],time.time()))
  try:
   while True:
    h=s.recv(2)
    if not h:break
    ln=h[1]&127
    if ln==126:ln=int.from_bytes(s.recv(2),'big')
    mask=s.recv(4);data=s.recv(ln);data=bytes(data[i]^mask[i%4] for i in range(ln))
    d=json.loads(data)
    if d.get('type')=='subscribe':state['subs'].add(d['channel_id']);ws_send(s,{'type':'subscribed','channel_id':d['channel_id'],'head_event_id':(q('select n from counters where channel_id=?',(d['channel_id'],),True) or {'n':0})['n']})
    state['seen']=time.time()
  except:pass
  finally:
   conns.pop(sid,None);q('delete from presence where conn_id=?',(sid,))
 def log_message(self,*a):pass
def groupj(g):return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}
def mentions(body,cid,aid):
 c=q('select * from channels where id=?',(cid,),True);out=[]
 for x in re.findall(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  if x=='channel': ids=[z['id'] for z in members(cid)]
  elif x=='here': ids=[z['id'] for z in members(cid) if q('select 1 from presence where user_id=? and last_seen>?',(z['id'],time.time()-1.5),True)]
  else:
   g=q('select user_ids from groupsx where workspace_id=? and handle=?',(c['workspace_id'],x),True) if c['workspace_id'] else None
   ids=json.loads(g['user_ids']) if g else [z['id'] for z in q('select * from users where username=?',(x,))]
   ids=[i for i in ids if q('select 1 from cm where channel_id=? and user_id=?',(cid,i),True)]
  for i in ids:
   if i!=aid and i not in out:out.append(i)
 return out
if __name__=='__main__':
 init();threading.Thread(target=poll,daemon=True).start();ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
