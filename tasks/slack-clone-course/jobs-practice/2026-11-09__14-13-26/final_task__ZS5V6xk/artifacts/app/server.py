#!/usr/bin/env python3
import os,re,json,sqlite3,secrets,hashlib,datetime,threading,urllib.parse,base64,struct,fcntl,socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; os.makedirs('/var/lib/huddle',exist_ok=True); LOCK=threading.RLock()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,check_same_thread=False,timeout=30); c.row_factory=sqlite3.Row; c.execute('pragma busy_timeout=30000'); c.execute('alter table ws_presence add column last_seen integer default 0') if False else None
 c.executescript('''create table if not exists users(id integer primary key,username text unique,password text,display_name text,timezone text default 'UTC',avatar_url text default '',status_text text default '',status_emoji text default '');
 create table if not exists tokens(token text primary key,user_id integer);
 create table if not exists workspaces(id integer primary key,slug text unique,name text,owner_id integer,join_mode text default 'open');
 create table if not exists members(workspace_id integer,user_id integer,role text,primary key(workspace_id,user_id));
 create table if not exists channels(id integer primary key,workspace_id integer,name text,is_private integer default 0,is_dm integer default 0,topic text default '',is_archived integer default 0);
 create table if not exists cmembers(channel_id integer,user_id integer,primary key(channel_id,user_id));
 create table if not exists messages(id integer primary key,channel_id integer,author_id integer,body text,parent_id integer,created_at text,edited_at text,deleted integer default 0,event_id integer);
 create table if not exists reactions(message_id integer,user_id integer,emoji text,primary key(message_id,user_id,emoji));
 create table if not exists pins(message_id integer primary key,pinned_by integer,pinned_at text);
 create table if not exists files(id integer primary key,uploader_id integer,filename text,content_type text,size integer,created_at text,data blob);
 create table if not exists invitations(code text primary key,workspace_id integer,invited_user_id integer,max_uses integer default 1,uses integer default 0,expires integer); create table if not exists events(channel_id integer,event_id integer,type text,payload text,primary key(channel_id,event_id)); create table if not exists read_state(user_id integer,channel_id integer,last_read_event_id integer default 0,primary key(user_id,channel_id)); create table if not exists message_files(message_id integer,file_id integer,primary key(message_id,file_id)); create table if not exists groups(id integer primary key,workspace_id integer,handle text, name text, creator_id integer, unique(workspace_id,handle)); create table if not exists ws_presence(channel_id integer,user_id integer,token text,last_seen integer default 0,primary key(channel_id,user_id)); create table if not exists user_updates(id integer primary key autoincrement,user_id integer,payload text); create table if not exists group_members(group_id integer,user_id integer,primary key(group_id,user_id));
 ''');
 try: c.execute('alter table ws_presence add column last_seen integer default 0')
 except sqlite3.OperationalError: pass
 c.commit(); return c
def row(o):
 if not o: return None
 d=dict(o)
 for k in ('is_private','is_dm','is_archived'):
  if k in d: d[k]=bool(d[k])
 return d
def emit(c,cid,kind,payload=None):
 eid=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(cid,)).fetchone()['n']
 z={'type':kind,'event_id':eid,'channel_id':cid}
 if payload:z.update(payload)
 c.execute('insert into events values(?,?,?,?)',(cid,eid,kind,json.dumps(z)))
 return eid
def user(u):
 return {'id':u['id'],'username':u['username'],'display_name':u['display_name'],'timezone':u['timezone'],'avatar_url':u['avatar_url'],'status_text':u['status_text'],'status_emoji':u['status_emoji']}
def msg(c,m):
 d=row(m); u=c.execute('select * from users where id=?',(d['author_id'],)).fetchone(); d['author']=user(u); d['parent_id']=d['parent_id'];
 d['files']=[]
 for fx in c.execute('select f.* from files f join message_files mf on mf.file_id=f.id where mf.message_id=?',(d['id'],)):
  d['files'].append({'id':fx['id'],'uploader_id':fx['uploader_id'],'filename':fx['filename'],'content_type':fx['content_type'],'size':fx['size'],'created_at':fx['created_at']})
 d['reply_count']=c.execute('select count(*) n from messages where parent_id=? and deleted=0',(d['id'],)).fetchone()['n']; d['reactions']=[]
 for x in c.execute('select emoji,user_id from reactions where message_id=?',(d['id'],)):
  z=next((q for q in d['reactions'] if q['emoji']==x['emoji']),None)
  if not z: z={'emoji':x['emoji'],'count':0,'user_ids':[]}; d['reactions'].append(z)
  z['count']+=1; z['user_ids'].append(x['user_id'])
 d.pop('event_id',None)
 mentioned=[]
 text=d.get('body','')
 if re.search(r'(?<![\w@])@channel\b',text,re.I):
  mentioned += [x['user_id'] for x in c.execute('select user_id from cmembers where channel_id=?',(d['channel_id'],)) if x['user_id']!=d['author_id']]
 if re.search(r'(?<![\w@])@here\b',text,re.I):
  mentioned += [x['user_id'] for x in c.execute("select cm.user_id from cmembers cm join ws_presence wp on wp.user_id=cm.user_id and wp.channel_id=-1 and wp.last_seen>cast(strftime('%s','now') as integer)-1 where cm.channel_id=?",(d['channel_id'],)) if x['user_id']!=d['author_id']]
 ws=c.execute('select workspace_id from channels where id=?',(d['channel_id'],)).fetchone()
 if ws:
  names=set(re.findall(r'(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_-]*)\b',text))
  names.discard('channel')
  for n in names:
   x=c.execute('select u.id from users u join members m on m.user_id=u.id where m.workspace_id=? and lower(u.username)=lower(?)',(ws['workspace_id'],n)).fetchone()
   if x and x['id']!=d['author_id']: mentioned.append(x['id'])
   g=c.execute('select id from groups where workspace_id=? and lower(handle)=lower(?)',(ws['workspace_id'],n)).fetchone()
   if g:
    mentioned += [z['user_id'] for z in c.execute('select gm.user_id from group_members gm join cmembers cm on cm.user_id=gm.user_id where gm.group_id=? and cm.channel_id=?',(g['id'],d['channel_id'])) if z['user_id']!=d['author_id']]
 d['mentions']=list(dict.fromkeys(mentioned)); return d
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def send(self,status,obj=None,typ='application/json'):
  b=(json.dumps(obj,separators=(',',':')).encode() if obj is not None else b'')
  self.send_response(status); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Access-Control-Allow-Headers','Authorization,Content-Type'); self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  t=self.headers.get('Authorization','').replace('Bearer ',''); c=db(); x=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone(); return c,x
 def route(self):
  return urllib.parse.urlparse(self.path).path.split('/'),urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
 def do_OPTIONS(self): self.send(204)
 def do_GET(self):
  if urllib.parse.urlparse(self.path).path=='/api/ws': return self.websocket()
  self.dispatch('GET')
 def websocket(self):
  q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); tok=q.get('token',[''])[0]
  c=db(); u=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(tok,)).fetchone()
  if not u:
   self.send_response(401); self.send_header('Content-Length','0'); self.end_headers(); return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send(400,{'error':'websocket required'})
  import hashlib,base64,time
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','WebSocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  self.request.settimeout(0.5); channel=None; sent=0; user_cursor=0
  c.execute('insert or replace into ws_presence(channel_id,user_id,token,last_seen) values(?,?,?,?)',(-1,u['id'],tok,int(__import__('time').time()))); c.commit()
  def out(o):
   b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
   h=bytes([129,n]) if n<126 else bytes([129,126])+struct.pack('!H',n)
   self.request.sendall(h+b)
  def inp():
   h=self.request.recv(2)
   if not h: raise EOFError('websocket closed')
   n=h[1]&127
   if n==126:n=struct.unpack('!H',self.request.recv(2))[0]
   mask=self.request.recv(4); b=self.request.recv(n)
   return json.loads(bytes(x^mask[i%4] for i,x in enumerate(b)).decode())
  try:
   while True:
    try:
     c.execute('update ws_presence set last_seen=? where user_id=?',(int(__import__('time').time()),u['id'])); c.commit()
     for ur in c.execute('select * from user_updates where id>? order by id',(user_cursor,)):
      out(json.loads(ur['payload'])); user_cursor=ur['id']
     z=inp()
     if z:
      typ=z.get('type'); channel=z.get('channel_id')
      if typ=='subscribe':
       sent=0; head=c.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(channel,)).fetchone()['n']; c.execute('insert or replace into ws_presence(channel_id,user_id,token,last_seen) values(?,?,?,?)',(channel,u['id'],tok,int(__import__('time').time()))); c.commit(); out({'type':'subscribed','channel_id':channel,'head_event_id':head})
      elif typ=='resume':
       channel=z.get('channel_id'); since=int(z.get('since_event_id',0)); rows=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(channel,since)).fetchall()
       for r in rows: out(json.loads(r['payload']))
       head=c.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(channel,)).fetchone()['n']; out({'type':'resumed','channel_id':channel,'head_event_id':head}); sent=head
    except socket.timeout: pass
    if channel is not None:
     rows=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(channel,sent)).fetchall()
     for r in rows: out(json.loads(r['payload'])); sent=r['event_id']
  except (BrokenPipeError,ConnectionResetError,EOFError,ValueError, socket.timeout): pass
  finally:
   if channel is not None:
    c.execute('delete from ws_presence where user_id=?',(u['id'],)); c.commit()
 def do_POST(self): self.dispatch('POST')
 def do_PATCH(self): self.dispatch('PATCH')
 def do_DELETE(self): self.dispatch('DELETE')
 def dispatch(self,method):
  try:
   return self._dispatch(method)
  finally:
   try:
    if hasattr(self,'_db') and self._db: self._db.close()
   except Exception: pass
 def _dispatch(self,method):
  p,q=self.route(); c,u=self.auth(); self._db=c; node=int(os.getenv('NODE_ID','0'))
  if p==['','api','health']: return self.send(200,{'status':'ok','node_id':node})
  if p[:3]==['','api','auth'] and p[3:] == ['me']:
   return self.send(200,{'user':user(u)} if u else {'error':'unauthorized'},'application/json') if u else self.send(401,{'error':'unauthorized'})
  if p[:3]==['','api','auth'] and p[3:] in (['register'],['login']):
   b=self.body(); typ=p[3]
   if typ=='register':
    if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return self.send(400,{'error':'invalid'})
    try:
     cur=c.execute('insert into users(username,password,display_name) values(?,?,?)',(b['username'],b['password'],b.get('display_name',b['username']))); uid=cur.lastrowid
    except: return self.send(409,{'error':'duplicate'})
   else:
    x=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
    if not x:return self.send(401,{'error':'invalid'})
    uid=x['id']
   x=c.execute('select * from users where id=?',(uid,)).fetchone(); t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); c.commit(); return self.send(201 if typ=='register' else 200,{'user':user(x),'token':t})
  if not u:return self.send(401,{'error':'unauthorized'})
  if p[1:3]==['api','dms'] and method=='POST':
   b=self.body(); tid=b.get('user_id')
   if tid==u['id']:return self.send(400,{})
   target=c.execute('select id from users where id=?',(tid,)).fetchone()
   if not target:return self.send(404,{})
   ids=sorted([u['id'],tid]); name='dm-'+str(ids[0])+'-'+str(ids[1])
   ch=c.execute('select * from channels where is_dm=1 and name=?',(name,)).fetchone()
   if not ch:
    cur=c.execute('insert into channels(workspace_id,name,is_dm) values(?,?,1)',(0,name)); cid=cur.lastrowid
    c.execute('insert into cmembers values(?,?)',(cid,u['id'])); c.execute('insert into cmembers values(?,?)',(cid,tid)); c.commit()
   else: cid=ch['id']
   return self.send(200,{'channel_id':cid})
  if p[1:3]==['api','files']:
   if method=='POST' and len(p)==3:
    ct=self.headers.get('Content-Type','')
    if 'multipart/form-data' not in ct:return self.send(400,{})
    from email.parser import BytesParser
    from email.policy import default
    n=int(self.headers.get('Content-Length','0'))
    if n>11*1024*1024:return self.send(413,{})
    raw=self.rfile.read(n); form=BytesParser(policy=default).parsebytes(('Content-Type: '+ct+'\r\n\r\n').encode()+raw)
    part=next((x for x in form.iter_parts() if x.get_param('name',header='content-disposition')=='file'),None)
    if not part:return self.send(400,{})
    data=part.get_payload(decode=True) or b''
    if len(data)>10485760:return self.send(413,{})
    fn=part.get_filename() or 'file'; typ=part.get_content_type(); cur=c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data)); c.commit()
    f=c.execute('select * from files where id=?',(cur.lastrowid,)).fetchone()
    return self.send(201,{'file':{'id':f['id'],'uploader_id':f['uploader_id'],'filename':f['filename'],'content_type':f['content_type'],'size':f['size'],'created_at':f['created_at']}})
   if len(p)>=4:
    f=c.execute('select * from files where id=?',(p[3],)).fetchone()
    if not f:return self.send(404,{})
    if len(p)==5 and p[4]=='download':
     data=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
    return self.send(200,{'file':{'id':f['id'],'uploader_id':f['uploader_id'],'filename':f['filename'],'content_type':f['content_type'],'size':f['size'],'created_at':f['created_at']}})
  try:
   if p[1:3]==['api','users'] and len(p)==4:
    if method=='GET': x=c.execute('select * from users where id=?',(p[3],)).fetchone(); return self.send(200,{'user':user(x)}) if x else self.send(404,{})
    b=self.body(); fields=['display_name','timezone','avatar_url','status_text','status_emoji']
    if 'timezone' in b and b['timezone'] not in ('UTC','America/New_York','America/Los_Angeles','Europe/London','Asia/Tokyo'): return self.send(400,{})
    if 'display_name' in b and (not isinstance(b['display_name'],str) or not b['display_name'] or len(b['display_name'])>200): return self.send(400,{})
    if 'status_text' in b and (not isinstance(b['status_text'],str) or len(b['status_text'])>500): return self.send(400,{})
    vals=[b[k] for k in fields if k in b]
    if vals:
     c.execute('update users set '+','.join(k+'=?' for k in fields if k in b)+' where id=?',(*vals,u['id']))
     updated=user(c.execute('select * from users where id=?',(u['id'],)).fetchone())
     c.execute('insert into user_updates(user_id,payload) values(?,?)',(u['id'],json.dumps({'type':'user.updated','user':updated})))
     c.commit()
    return self.send(200,{'user':user(c.execute('select * from users where id=?',(u['id'],)).fetchone())})
   if p[1:3]==['api','workspaces'] and len(p)==3 and method=='POST':
    b=self.body(); slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): return self.send(400,{'error':'invalid slug'})
    if c.execute('select 1 from workspaces where slug=?',(slug,)).fetchone(): return self.send(409,{'error':'duplicate'})
    cur=c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,b.get('name',''),u['id'])); wid=cur.lastrowid
    c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); ch=c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); c.execute('insert into cmembers values(?,?)',(ch.lastrowid,u['id'])); c.commit()
    return self.send(201,{'workspace':row(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':row(c.execute('select * from channels where id=?',(ch.lastrowid,)).fetchone())})
   if p[1:3]==['api','workspaces'] and len(p)==3 and method=='GET':
    return self.send(200,{'workspaces':[row(x) for x in c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],))]})
   if p[1:3]==['api','workspaces'] and len(p)>=4:
    w=c.execute('select * from workspaces where slug=?',(p[3],)).fetchone()
    if not w:return self.send(404,{})
    if len(p)==4 and method=='PATCH':
     me=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
     if not me or me['role'] not in ('owner','admin'): return self.send(403,{'error':'forbidden'})
     b=self.body()
     if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'): return self.send(400,{'error':'invalid join_mode'})
     fields=[]; vals=[]
     if 'name' in b: fields.append('name=?'); vals.append(b['name'])
     if 'join_mode' in b: fields.append('join_mode=?'); vals.append(b['join_mode'])
     if fields:
      vals.append(w['id']); c.execute('update workspaces set '+','.join(fields)+' where id=?',vals); c.commit()
     return self.send(200,{'workspace':row(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
    if len(p)==4 and method=='GET':
     wm=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
     if not wm:return self.send(403,{})
     include=q.get('include_archived',[''])[0].lower() in ('1','true')
     rows=list(c.execute('select * from channels where workspace_id=?'+('' if include else ' and is_archived=0'),(w['id'],)))
     cs=[]
     for x in rows:
      if not x['is_private'] or c.execute('select 1 from cmembers where channel_id=? and user_id=?',(x['id'],u['id'])).fetchone():
       cs.append(row(x))
     states=[]
     for x in cs:
      rs=c.execute('select last_read_event_id from read_state where user_id=? and channel_id=?',(u['id'],x['id'])).fetchone()
      cursor=rs['last_read_event_id'] if rs else 0
      unread=0; mentions=0
      for mm in c.execute('select * from messages where channel_id=? and deleted=0 and event_id>? and author_id!=?',(x['id'],cursor,u['id'])):
       unread+=1
       if u['id'] in msg(c,mm).get('mentions',[]): mentions+=1
      states.append({'channel_id':x['id'],'last_read_event_id':cursor,'unread_count':unread,'mention_count':mentions})
     return self.send(200,{'workspace':row(w),'channels':cs,'read_state':states})
    if len(p)==5 and p[4]=='members': return self.send(200,{'members':[row(x) for x in c.execute('select user_id,role from members where workspace_id=?',(w['id'],))]})
    if len(p)==5 and p[4]=='channels' and method=='POST':
     b=self.body(); mr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
     if not mr or mr['role']=='guest' or (b.get('is_private',False) and mr['role'] not in ('owner','admin')): return self.send(403,{'error':'forbidden'})
     if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return self.send(400,{'error':'invalid channel'})
     if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],b.get('name'))).fetchone(): return self.send(409,{'error':'duplicate'})
     cur=c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic',''))); cid=cur.lastrowid;c.execute('insert into cmembers values(?,?)',(cid,u['id']));c.commit();return self.send(201,{'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   if p[1:3]==['api','workspaces'] and len(p)>=6 and p[4]=='members' and method=='PATCH':
    w=c.execute('select * from workspaces where slug=?',(p[3],)).fetchone()
    if not w:return self.send(404,{})
    target=c.execute('select * from users where id=?',(p[5],)).fetchone()
    if not target:return self.send(404,{})
    me=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    old=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target['id'])).fetchone()
    if not old:return self.send(404,{})
    role=self.body().get('role')
    if role not in ('owner','admin','member','guest'):return self.send(400,{'error':'invalid role'})
    if old['role']=='owner' or target['id']==w['owner_id'] or role=='owner':return self.send(403 if role!='owner' else 400,{'error':'forbidden'})
    if not me or me['role'] not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
    if me['role']=='admin' and old['role']=='admin':return self.send(403,{'error':'forbidden'})
    c.execute('update members set role=? where workspace_id=? and user_id=?',(role,w['id'],target['id']));c.commit()
    return self.send(200,{'member':{'user_id':target['id'],'role':role}})
   if p[1:3]==['api','workspaces'] and len(p)==5 and p[4]=='transfer_ownership' and method=='POST':
    w=c.execute('select * from workspaces where slug=?',(p[3],)).fetchone()
    if not w:return self.send(404,{})
    me=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not me or me['role']!='owner':return self.send(403,{})
    tid=self.body().get('new_owner_id'); target=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],tid)).fetchone()
    if not target or target['role']!='admin':return self.send(400,{'error':'target must be admin'})
    c.execute('update members set role=? where workspace_id=? and user_id=?',('admin',w['id'],u['id']))
    c.execute('update members set role=? where workspace_id=? and user_id=?',('owner',w['id'],tid))
    c.execute('update workspaces set owner_id=? where id=?',(tid,w['id']));c.commit()
    return self.send(200,{'workspace':row(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
   if p[1:3]==['api','workspaces'] and len(p)>=5 and p[4]=='groups':
    w=c.execute('select * from workspaces where slug=?',(p[3],)).fetchone()
    if not w:return self.send(404,{})
    me=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not me:return self.send(403,{})
    def gout(g):
     return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('select user_id from group_members where group_id=?',(g['id'],))]}
    if len(p)==5 and method=='GET': return self.send(200,{'groups':[gout(x) for x in c.execute('select * from groups where workspace_id=?',(w['id'],))]})
    if len(p)==5 and method=='POST':
     b=self.body(); h=b.get('handle','')
     if h.lower()=='here' or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,31}',h):return self.send(400,{})
     if c.execute('select 1 from groups where workspace_id=? and handle=?',(w['id'],h)).fetchone():return self.send(409,{})
     cur=c.execute('insert into groups(workspace_id,handle,name,creator_id) values(?,?,?,?)',(w['id'],h,b.get('name',''),u['id'])); gid=cur.lastrowid
     for uid in b.get('user_ids',[]): 
      if c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],uid)).fetchone(): c.execute('insert or ignore into group_members values(?,?)',(gid,uid))
     c.commit();return self.send(201,{'group':gout(c.execute('select * from groups where id=?',(gid,)).fetchone())})
    if len(p)==6:
     g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],p[5])).fetchone()
     if not g and method=='DELETE':return self.send(200,{'deleted':True})
     if not g:return self.send(404,{})
     allowed=u['id']==g['creator_id'] or me['role']=='owner'
     if not allowed:return self.send(403,{})
     if method=='PATCH':
      b=self.body(); fields=[]; vals=[]
      if 'name' in b:fields.append('name=?');vals.append(b['name'])
      if 'user_ids' in b:
       c.execute('delete from group_members where group_id=?',(g['id'],))
       for uid in b['user_ids']:
        if c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],uid)).fetchone():c.execute('insert or ignore into group_members values(?,?)',(g['id'],uid))
      if fields: vals += [g['id']]; c.execute('update groups set '+','.join(fields)+' where id=?',vals)
      c.commit();return self.send(200,{'group':gout(c.execute('select * from groups where id=?',(g['id'],)).fetchone())})
     if method=='DELETE':
      c.execute('delete from group_members where group_id=?',(g['id'],));c.execute('delete from groups where id=?',(g['id'],));c.commit();return self.send(200,{'deleted':True})
   if p[1:3]==['api','workspaces'] and len(p)>=5 and p[4]=='invitations':
    w=c.execute('select * from workspaces where slug=?',(p[3],)).fetchone()
    if not w:return self.send(404,{})
    me=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if len(p)==5 and method=='POST':
     if not me or me['role'] not in ('owner','admin'):return self.send(403,{})
     b=self.body(); target=None
     if b.get('invited_user_id') is not None: target=c.execute('select id from users where id=?',(b['invited_user_id'],)).fetchone()
     elif b.get('invited_username'): target=c.execute('select id from users where username=?',(b['invited_username'],)).fetchone()
     if b.get('invited_user_id') is not None and not target:return self.send(404,{})
     if b.get('invited_username') and not target:return self.send(404,{})
     code=secrets.token_urlsafe(18); mx=1 if target else int(b.get('max_uses',1)); exp=int(__import__('time').time())+int(b.get('expires_in_seconds',0)) if b.get('expires_in_seconds') else None
     c.execute('insert into invitations(code,workspace_id,invited_user_id,max_uses,expires) values(?,?,?,?,?)',(code,w['id'],target['id'] if target else None,mx,exp));c.commit()
     return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target['id'] if target else None,'max_uses':mx,'uses':0,'expires_at':exp}})
    if len(p)==5 and method=='GET':
     if not me or me['role'] not in ('owner','admin'):return self.send(403,{})
     import time
     xs=[]
     for x in c.execute('select * from invitations where workspace_id=?',(w['id'],)):
      if x['uses']<x['max_uses'] and (not x['expires'] or x['expires']>int(time.time())): xs.append({'code':x['code'],'workspace_id':x['workspace_id'],'invited_user_id':x['invited_user_id'],'max_uses':x['max_uses'],'uses':x['uses'],'expires_at':x['expires']})
     return self.send(200,{'invitations':xs})
    if len(p)==6 and method=='DELETE':
     if not me or me['role'] not in ('owner','admin'):return self.send(403,{})
     x=c.execute('select 1 from invitations where workspace_id=? and code=?',(w['id'],p[5])).fetchone()
     if not x:return self.send(404,{})
     c.execute('delete from invitations where workspace_id=? and code=?',(w['id'],p[5]));c.commit();return self.send(200,{'deleted':True})
   if p[1:3]==['api','invitations'] and len(p)==5 and p[4]=='accept' and method=='POST':
    import time
    x=c.execute('select * from invitations where code=?',(p[3],)).fetchone()
    if not x or x['uses']>=x['max_uses'] or (x['expires'] and x['expires']<=int(time.time())):return self.send(404,{})
    if x['invited_user_id'] is not None and x['invited_user_id']!=u['id']:return self.send(403,{})
    c.execute('insert or ignore into members values(?,?,?)',(x['workspace_id'],u['id'],'member'))
    c.execute('update invitations set uses=uses+1 where code=?',(p[3],));c.commit()
    return self.send(200,{'workspace_id':x['workspace_id'],'accepted':True})
   if p[1:3]==['api','channels'] and len(p)>=4 and len(p)==5 and p[4] in ('leave','archive','unarchive') and method=='POST':
    cid=int(p[3]); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
    if not ch:return self.send(404,{})
    wm=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
    if p[4] in ('archive','unarchive'):
     if not wm or wm['role']!='owner':return self.send(403,{})
     c.execute('update channels set is_archived=? where id=?',(1 if p[4]=='archive' else 0,cid)); emit(c,cid,'channel.archived' if p[4]=='archive' else 'channel.unarchived',{'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())})
    else:
     had=c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone(); c.execute('delete from cmembers where channel_id=? and user_id=?',(cid,u['id']))
     if had: emit(c,cid,'member.left',{'user_id':u['id']})
    c.commit();return self.send(200,{'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   if p[1:3]==['api','channels'] and len(p)==5 and p[4]=='messages' and method=='GET':
    cid=int(p[3]); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
    if not ch:return self.send(404,{})
    if (ch['is_private'] or ch['is_dm']) and not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return self.send(403,{})
    try: lim=int(q.get('limit',['50'])[0])
    except: return self.send(400,{})
    if lim<1 or lim>200:return self.send(400,{})
    cur=q.get('cursor',[None])[0]; before=None
    if cur:
     try: before=int(base64.urlsafe_b64decode(cur+'===').decode())
     except: return self.send(400,{})
    if before is None:
     rows=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc limit ?',(cid,lim)).fetchall()
    else:
     rows=c.execute('select * from messages where channel_id=? and deleted=0 and id<? order by id desc limit ?',(cid,before,lim)).fetchall()
    xs=[msg(c,x) for x in rows]
    nxt=base64.urlsafe_b64encode(str(rows[-1]['id']).encode()).decode().rstrip('=') if len(rows)==lim else None
    return self.send(200,{'messages':xs,'next_cursor':nxt})
   if p[1:3]==['api','channels'] and len(p)==5 and p[4]=='read':
    cid=int(p[3]); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
    if not ch:return self.send(404,{})
    if ch['is_dm'] and not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return self.send(403,{})
    rs=c.execute('select last_read_event_id from read_state where user_id=? and channel_id=?',(u['id'],cid)).fetchone()
    cursor=rs['last_read_event_id'] if rs else 0
    if method=='POST':
     try: requested=int(self.body().get('last_read_event_id',0))
     except: return self.send(400,{})
     if requested<0:return self.send(400,{})
     cursor=max(cursor,requested)
     c.execute('insert into read_state(user_id,channel_id,last_read_event_id) values(?,?,?) on conflict(user_id,channel_id) do update set last_read_event_id=excluded.last_read_event_id',(u['id'],cid,cursor));c.commit()
    unread=mentions=0
    for mm in c.execute('select * from messages where channel_id=? and deleted=0 and event_id>? and author_id!=?',(cid,cursor,u['id'])):
     unread+=1
     if u['id'] in msg(c,mm).get('mentions',[]): mentions+=1
    return self.send(200,{'read_state':{'channel_id':cid,'last_read_event_id':cursor,'unread_count':unread,'mention_count':mentions}})
   if p[1:3]==['api','channels'] and len(p)>=4:
    cid=int(p[3]); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
    if not ch:return self.send(404,{})
    if p[4:] == ['join'] and method=='POST':
     mr=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
     if ch['is_private'] and not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return self.send(403,{'error':'forbidden'})
     if mr and mr['role']=='guest':return self.send(403,{'error':'forbidden'})
     ws=c.execute('select join_mode from workspaces where id=?',(ch['workspace_id'],)).fetchone()
     if not mr and ws and ws['join_mode']=='invite_only':return self.send(403,{'error':'forbidden'})
     before=c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone(); c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('insert or ignore into cmembers values(?,?)',(cid,u['id']));
     if not before: emit(c,cid,'member.joined',{'user_id':u['id']})
     c.commit();return self.send(200,{'channel':row(ch)})
    if p[4:] == ['members']: return self.send(200,{'members':[user(x) for x in c.execute('select u.* from users u join cmembers m on m.user_id=u.id where m.channel_id=?',(cid,))]})
    if p[4:] == [] and method=='PATCH':
     if ch['is_archived']:return self.send(423,{})
     b=self.body()
     if len(b.get('topic',''))>250:return self.send(400,{})
     if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone() and c.execute('select owner_id from workspaces where id=?',(ch['workspace_id'],)).fetchone()['owner_id']!=u['id']:return self.send(403,{})
     c.execute('update channels set topic=? where id=?',(b.get('topic',''),cid)); emit(c,cid,'channel.updated',{'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())}); c.commit();return self.send(200,{'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())})
    if p[4:] == ['messages'] and method=='GET':
     if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return self.send(403,{})
     lim=min(int(q.get('limit',['50'])[0]),200); ms=[msg(c,x) for x in c.execute('select * from messages where channel_id=? and deleted=0 order by id desc limit ?',(cid,lim))];return self.send(200,{'messages':ms,'next_cursor':None})
    if p[4:] == ['messages'] and method=='POST':
     if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return self.send(403,{})
     b=self.body(); body=b.get('body','')
     if ch['is_archived'] and not body.startswith('/unarchive'):return self.send(423,{})
     if not body:return self.send(400,{'error':'empty'})
     if body.startswith('/') and not body.startswith('//'):
      parts=body.split(None,1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
      if cmd=='/me':
       if not arg:return self.send(400,{'error':'invalid command'})
       body=body
      elif cmd=='/shrug':
       body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
      elif cmd in ('/topic','/invite','/archive','/unarchive'):
       if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return self.send(403,{})
       wm=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
       if cmd=='/topic':
        if not arg or len(arg)>250:return self.send(400,{})
        c.execute('update channels set topic=? where id=?',(arg,cid)); emit(c,cid,'channel.updated',{'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())}); c.commit()
        return self.send(201,{'message':None,'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())})
       if cmd in ('/archive','/unarchive'):
        if not wm or wm['role']!='owner':return self.send(403,{})
        val=1 if cmd=='/archive' else 0
        c.execute('update channels set is_archived=? where id=?',(val,cid)); emit(c,cid,'channel.archived' if val else 'channel.unarchived',{'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())}); c.commit()
        return self.send(201,{'message':None,'channel':row(c.execute('select * from channels where id=?',(cid,)).fetchone())})
       if cmd=='/invite':
        if not arg or not arg.startswith('@') or not re.fullmatch(r'@[A-Za-z0-9_]+',arg):return self.send(400,{})
        target=c.execute('select * from users where username=?',(arg[1:],)).fetchone()
        if not target:return self.send(404,{})
        if not c.execute('select 1 from members where workspace_id=? and user_id=?',(ch['workspace_id'],target['id'])).fetchone():return self.send(403,{})
        c.execute('insert or ignore into cmembers values(?,?)',(cid,target['id']));c.commit()
        return self.send(201,{'message':None,'invited_user':user(target)})
      if cmd not in ('/me','/shrug'): return self.send(400,{'error':'unknown command'})
     file_ids=b.get('file_ids',[])
     for fid in file_ids:
      f=c.execute('select * from files where id=?',(fid,)).fetchone()
      if not f or f['uploader_id']!=u['id'] or c.execute('select 1 from message_files where file_id=?',(fid,)).fetchone():return self.send(400,{})
     cur=c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id) values(?,?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),now(),0))
     kind='message.reply' if b.get('parent_id') else 'message.created'
     eid=emit(c,cid,kind,{'message':msg(c,c.execute('select * from messages where id=?',(cur.lastrowid,)).fetchone())})
     c.execute('update messages set event_id=? where id=?',(eid,cur.lastrowid))
     for fid in file_ids:c.execute('insert into message_files values(?,?)',(cur.lastrowid,fid))
     c.commit();return self.send(201,{'message':msg(c,c.execute('select * from messages where id=?',(cur.lastrowid,)).fetchone())})
   if p[1:3]==['api','channels'] and len(p)==5 and p[4]=='pins':
    cid=int(p[3]); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
    if not ch:return self.send(404,{})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return self.send(403,{})
    try: lim=int(q.get('limit',['50'])[0])
    except: return self.send(400,{})
    if lim<1 or lim>200:return self.send(400,{})
    cur=q.get('cursor',[None])[0]; before=None
    if cur:
     try: before=int(base64.urlsafe_b64decode(cur+'===').decode())
     except: return self.send(400,{})
    if before is None:
     ps=c.execute('select * from pins where message_id in (select id from messages where channel_id=?) order by pinned_at desc,message_id desc limit ?',(cid,lim+1)).fetchall()
    else:
     ps=c.execute('select * from pins where message_id in (select id from messages where channel_id=?) and message_id<? order by pinned_at desc,message_id desc limit ?',(cid,before,lim+1)).fetchall()
    more=len(ps)>lim; ps=ps[:lim]
    out=[]
    for z in ps:
     mm=c.execute('select * from messages where id=?',(z['message_id'],)).fetchone()
     out.append({'message':msg(c,mm),'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']})
    nxt=base64.urlsafe_b64encode(str(ps[-1]['message_id']).encode()).decode().rstrip('=') if more else None
    return self.send(200,{'pins':out,'next_cursor':nxt})
   if p[1:3]==['api','messages'] and len(p)>=5 and p[4]=='pin':
    mid=int(p[3]); m=c.execute('select * from messages where id=?',(mid,)).fetchone()
    if not m:return self.send(404,{})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(m['channel_id'],u['id'])).fetchone():return self.send(403,{})
    if method=='POST':
     c.execute('insert or ignore into pins(message_id,pinned_by,pinned_at) values(?,?,?)',(mid,u['id'],now())); c.commit()
     z=c.execute('select * from pins where message_id=?',(mid,)).fetchone()
     return self.send(200,{'pin':{'message_id':z['message_id'],'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']}})
    if method=='DELETE':
     c.execute('delete from pins where message_id=?',(mid,));c.commit();return self.send(200,{'unpinned':True})
   if p[1:3]==['api','messages'] and len(p)>=5 and p[4]=='replies' and method=='GET':
    parent=c.execute('select * from messages where id=?',(p[3],)).fetchone()
    if not parent:return self.send(404,{})
    try: lim=int(q.get('limit',['50'])[0])
    except:return self.send(400,{})
    if lim<1 or lim>200:return self.send(400,{})
    cur=q.get('cursor',[None])[0]; after=0
    if cur:
     try: after=int(base64.urlsafe_b64decode(cur+'===').decode())
     except:return self.send(400,{})
    xsrows=c.execute('select * from messages where parent_id=? and deleted=0 and id>? order by id limit ?',(p[3],after,lim)).fetchall()
    xs=[msg(c,x) for x in xsrows]
    nxt=base64.urlsafe_b64encode(str(xsrows[-1]['id']).encode()).decode().rstrip('=') if len(xsrows)==lim else None
    return self.send(200,{'replies':xs,'next_cursor':nxt})
   if p[1:3]==['api','messages'] and len(p)>=4:
    mid=int(p[3]); m=c.execute('select * from messages where id=?',(mid,)).fetchone()
    if not m:return self.send(404,{})
    if p[4:] == ['reactions'] and method=='POST':
     e=self.body().get('emoji')
     if not isinstance(e,str) or not e:return self.send(400,{'error':'emoji required'})
     if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(m['channel_id'],u['id'])).fetchone():return self.send(403,{})
     existed=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],e)).fetchone()
     c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],e))
     if not existed:
      emit(c,m['channel_id'],'reaction.added',{'message_id':mid,'emoji':e,'user_id':u['id'],'reactions':msg(c,m)['reactions']})
     c.commit();return self.send(200,{'reactions':msg(c,m)['reactions']})
    if len(p)>=6 and p[4]=='reactions' and method=='DELETE':
     e=urllib.parse.unquote(p[5])
     if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(m['channel_id'],u['id'])).fetchone():return self.send(403,{})
     existed=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],e)).fetchone()
     c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],e))
     if existed: emit(c,m['channel_id'],'reaction.removed',{'message_id':mid,'emoji':e,'user_id':u['id'],'reactions':msg(c,m)['reactions']})
     c.commit();return self.send(200,{'reactions':msg(c,m)['reactions']})
    if method=='PATCH': b=self.body();c.execute('update messages set body=?,edited_at=? where id=?',(b.get('body',''),now(),mid));c.commit();return self.send(200,{'message':msg(c,c.execute('select * from messages where id=?',(mid,)).fetchone())})
    if method=='DELETE':c.execute('update messages set deleted=1 where id=?',(mid,));c.commit();return self.send(200,{'deleted':True})
   if p[1:3]==['api','search']:
    term=q.get('q',[''])[0].lower()
    try: lim=int(q.get('limit',['50'])[0])
    except: return self.send(400,{})
    if lim<1 or lim>200:return self.send(400,{})
    wid=q.get('workspace_id',[None])[0]
    cur=q.get('cursor',[None])[0]; before=None
    if cur:
     try: before=int(base64.urlsafe_b64decode(cur+'===').decode())
     except: return self.send(400,{})
    sql='select distinct m.* from messages m join channels ch on ch.id=m.channel_id join workspaces w on w.id=ch.workspace_id join members wm on wm.workspace_id=w.id and wm.user_id=? left join cmembers cm on cm.channel_id=ch.id and cm.user_id=? where m.deleted=0 and lower(m.body) like ? and (ch.is_private=0 or cm.user_id is not null)'
    args=[u['id'],u['id'],'%'+term+'%']
    if wid is not None: sql+=' and w.id=?'; args.append(wid)
    if before is not None: sql+=' and m.id<?'; args.append(before)
    sql+=' order by m.id desc limit ?'; args.append(lim+1)
    rows=c.execute(sql,args).fetchall(); more=len(rows)>lim; rows=rows[:lim]; out=[msg(c,x) for x in rows]
    nxt=base64.urlsafe_b64encode(str(rows[-1]['id']).encode()).decode().rstrip('=') if more else None
    return self.send(200,{'results':out,'next_cursor':nxt})
  except Exception as e: return self.send(400,{'error':str(e)})
  return self.send(404,{'error':'not found'})
def main():
 port=int(os.getenv('PORT','8000')); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
if __name__=='__main__':main()
