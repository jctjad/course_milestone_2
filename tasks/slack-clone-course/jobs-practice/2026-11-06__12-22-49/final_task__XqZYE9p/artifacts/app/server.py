import os,sys,json,re,sqlite3,secrets,hashlib,base64,datetime,threading,socket,struct,time
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
DB='/var/lib/huddle/huddle.db'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30,isolation_level=None); c.execute('PRAGMA busy_timeout=30000'); c.execute('PRAGMA busy_timeout=30000'); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER); CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id)); CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id)); CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji)); CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));'''); c.commit(); c.close()
def uj(r): return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def user(uid):
 c=db(); r=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return uj(r) if r else None
def tok(t):
 c=db(); r=c.execute('select user_id from tokens where token=?',(t,)).fetchone(); c.close(); return r['user_id'] if r else None
def auth(h):
 x=h.get('Authorization',''); return tok(x[7:]) if x.startswith('Bearer ') else None
def chan(cid):
 c=db(); r=c.execute('select * from channels where id=?',(cid,)).fetchone(); c.close(); return r
def cj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def member(cid,uid):
 c=db(); x=c.execute('select 1 from cm where channel_id=? and user_id=?',(cid,uid)).fetchone(); c.close(); return bool(x)
def role(w,uid):
 c=db(); x=c.execute('select role from wm where workspace_id=? and user_id=?',(w,uid)).fetchone(); c.close(); return x['role'] if x else None
def msg(r):
 c=db(); u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone(); rr=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(r['id'],)).fetchall(); replies=c.execute('select count(*) n from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()['n']; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':uj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rr],'mentions':[]}
def emit(cid,kind,payload):
 c=db(); n=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(cid,)).fetchone()['n']; c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(cid,n,kind,json.dumps(payload))); c.commit(); c.close(); return n
subs=[]; slock=threading.Lock()
def pump():
 last={}
 while True:
  try:
   c=db(); rows=c.execute('select * from events order by id').fetchall(); c.close()
   with slock:
    for e in rows:
     if e['id']<=last.get('e',0): continue
     last['e']=e['id']; frame={'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**json.loads(e['payload'])}
     for s in list(subs):
      if e['channel_id'] in s[1]:
       try: s[0](frame)
       except: pass
  except: pass
  time.sleep(.1)
threading.Thread(target=pump,daemon=True).start()
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,code,x):
  b=json.dumps(x).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path; uid=auth(self.headers)
  if path=='/': return self.html()
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path.startswith('/api/ws'): return self.websocket(parse_qs(p.query).get('token',[''])[0])
  if path=='/api/auth/me':
   return self.sendj(200,{'user':user(uid)}) if uid else self.sendj(401,{'error':'unauthorized'})
  if path=='/api/workspaces' and uid:
   c=db(); xs=c.execute('select w.* from workspaces w join wm on w.id=wm.workspace_id where wm.user_id=?',(uid,)).fetchall(); c.close(); return self.sendj(200,{'workspaces':[dict(x) for x in xs]})
  if path.startswith('/api/workspaces/') and uid:
   slug=path.split('/')[3]; c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.sendj(404,{})
   if not role(w['id'],uid):return self.sendj(403,{})
   cs=c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],)).fetchall(); c.close()
   return self.sendj(200,{'workspace':dict(w),'channels':[cj(x) for x in cs]})
  m=re.match(r'/api/channels/(\d+)(?:/(messages|members|pins))?',path)
  if m and uid:
   ch=chan(int(m[1]))
   if not ch:return self.sendj(404,{})
   if not member(ch['id'],uid):return self.sendj(403,{})
   c=db()
   if m[2]=='members': xs=c.execute('select u.* from users u join cm on u.id=cm.user_id where cm.channel_id=?',(ch['id'],)).fetchall(); c.close(); return self.sendj(200,{'members':[uj(x) for x in xs]})
   if m[2]=='messages' or not m[2]:
    xs=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc',(ch['id'],)).fetchall(); c.close(); return self.sendj(200,{'messages':[msg(x) for x in xs],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies',path)
  if m and uid:
   c=db(); xs=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(m[1],)).fetchall(); c.close(); return self.sendj(200,{'replies':[msg(x) for x in xs],'next_cursor':None})
  return self.sendj(401 if not uid and path.startswith('/api/') else 404,{})
 def do_POST(self):
  p=urlparse(self.path); path=p.path; uid=auth(self.headers)
  if path=='/api/auth/register':
   try:
    d=self.body(); u=d['username']; pw=d['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(pw)<8: raise ValueError()
    c=db(); c.execute('insert into users(username,password,display_name) values(?,?,?)',(u,pw,d.get('display_name',u))); x=c.execute('select * from users where username=?',(u,)).fetchone(); t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,x['id'],)); c.commit(); c.close(); return self.sendj(201,{'user':uj(x),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   except:return self.sendj(400,{})
  if path=='/api/auth/login':
   d=self.body(); c=db(); x=c.execute('select * from users where username=? and password=?',(d.get('username'),d.get('password'))).fetchone()
   if not x:c.close(); return self.sendj(401,{})
   t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,x['id'])); c.commit(); c.close(); return self.sendj(200,{'user':uj(x),'token':t})
  if not uid:return self.sendj(401,{})
  if path=='/api/workspaces':
   d=self.body()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{})
   try:
    c=db(); c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d['name'],uid)); w=c.execute('select * from workspaces where slug=?',(d['slug'],)).fetchone(); c.execute('insert into wm values(?,?,?)',(w['id'],uid,'owner')); c.execute('insert into channels(workspace_id,name) values(?,?)',(w['id'],'general')); ch=c.execute('select * from channels where workspace_id=?',(w['id'],)).fetchone(); c.execute('insert into cm values(?,?)',(ch['id'],uid)); c.commit(); c.close(); return self.sendj(201,{'workspace':dict(w),'general_channel':cj(ch)})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels',path)
  if m:
   d=self.body(); c=db(); w=c.execute('select * from workspaces where slug=?',(m[1],)).fetchone()
   if not w:return self.sendj(404,{})
   if role(w['id'],uid) not in ('owner',):return self.sendj(403,{})
   name=d.get('name','').lower().replace(' ','-')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.sendj(400,{})
   try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic',''))); ch=c.execute('select * from channels where id=last_insert_rowid()').fetchone(); c.execute('insert into cm values(?,?)',(ch['id'],uid)); c.commit(); c.close(); return self.sendj(201,{'channel':cj(ch)})
   except sqlite3.IntegrityError:c.close(); return self.sendj(409,{})
  m=re.match(r'/api/channels/(\d+)/(join|messages|archive|unarchive)',path)
  if m:
   ch=chan(int(m[1]))
   if not ch:return self.sendj(404,{})
   if m[2]=='join':
    if ch['is_private']:return self.sendj(403,{})
    c=db(); c.execute('insert or ignore into wm values(?,?,?)',(ch['workspace_id'],uid,'member')); c.execute('insert or ignore into cm values(?,?)',(ch['id'],uid)); c.commit(); c.close(); return self.sendj(200,{'channel':cj(chan(ch['id']))})
   if m[2] in ('archive','unarchive'):
    if role(ch['workspace_id'],uid)!='owner': return self.sendj(403,{})
    c=db(); c.execute('update channels set is_archived=? where id=?',(m[2]=='archive',ch['id'])); c.commit(); c.close(); return self.sendj(200,{'channel':cj(chan(ch['id']))})
   d=self.body(); body=d.get('body','')
   if ch['is_archived']:return self.sendj(423,{})
   if not member(ch['id'],uid): return self.sendj(403,{})
   if not body.strip():return self.sendj(400,{})
   c=db(); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],uid,body,d.get('parent_id'),now())); r=c.execute('select * from messages where id=last_insert_rowid()').fetchone(); c.commit(); c.close(); x=msg(r)
   c=db(); w=c.execute('select workspace_id from channels where id=?',(ch['id'],)).fetchone(); names=re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body); ids=[]
   for n in names:
    q=c.execute('select u.id from users u join wm on wm.user_id=u.id where wm.workspace_id=? and u.username=?',(w['workspace_id'],n)).fetchone()
    if q and q['id']!=uid and q['id'] not in ids: ids.append(q['id'])
   c.close(); x['mentions']=ids; emit(ch['id'],'message.reply' if r['parent_id'] else 'message.created',{'message':x}); return self.sendj(201,{'message':x})
  m=re.match(r'/api/messages/(\d+)/reactions(?:/(.*))?$',path)
  if m:
   c=db(); mid=int(m[1]); r=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not r:return self.sendj(404,{})
   e=self.body().get('emoji'); c.execute('insert or ignore into reactions values(?,?,?)',(mid,uid,e)); c.commit(); c.close(); return self.sendj(200,{'reactions':msg(r)['reactions']})
  return self.sendj(404,{})
 def do_PATCH(self):
  uid=auth(self.headers)
  if not uid:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)',self.path)
  if m:
   d=self.body(); c=db(); r=c.execute('select * from messages where id=?',(m[1],)).fetchone()
   if not r:return self.sendj(404,{})
   if r['author_id']!=uid:return self.sendj(403,{})
   if r['deleted']:return self.sendj(409,{})
   if not d.get('body','').strip():return self.sendj(400,{})
   c.execute('update messages set body=?,edited_at=? where id=?',(d['body'],now(),m[1])); c.commit(); r=c.execute('select * from messages where id=?',(m[1],)).fetchone(); c.close(); x=msg(r); emit(r['channel_id'],'message.updated',{'message':x}); return self.sendj(200,{'message':x})
  m=re.match(r'/api/channels/(\d+)',self.path)
  if m:
   d=self.body(); c=db(); c.execute('update channels set topic=? where id=?',(d.get('topic',''),m[1])); c.commit(); c.close(); return self.sendj(200,{'channel':cj(chan(int(m[1])))})
  return self.sendj(404,{})
 def do_DELETE(self):
  uid=auth(self.headers); m=re.match(r'/api/messages/(\d+)',self.path)
  if not uid:return self.sendj(401,{})
  if m:
   c=db(); r=c.execute('select * from messages where id=?',(m[1],)).fetchone()
   if not r:return self.sendj(404,{})
   if r['author_id']!=uid and role(chan(r['channel_id'])['workspace_id'],uid)!='owner':return self.sendj(403,{})
   if not r['deleted']: c.execute('update messages set deleted=1 where id=?',(m[1],)); c.commit(); emit(r['channel_id'],'message.deleted',{'message_id':int(m[1])})
   c.close(); return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def html(self):
  b=open('/app/static/index.html','rb').read(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def websocket(self,t):
  uid=tok(t)
  if not uid:return self.send_error(401); 
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send_error(400)
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','WebSocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  chans=set()
  def out(x):
   b=json.dumps(x).encode(); h=bytes([0x81,len(b)]) if len(b)<126 else bytes([0x81,126])+struct.pack('!H',len(b)); self.connection.sendall(h+b)
  with slock: subs.append((out,chans))
  try:
   while True:
    z=self.connection.recv(2)
    if not z:break
    n=z[1]&127
    if n==126:n=struct.unpack('!H',self.connection.recv(2))[0]
    mask=self.connection.recv(4); data=self.connection.recv(n); data=bytes(data[i]^mask[i%4] for i in range(n)); d=json.loads(data)
    if d.get('type')=='subscribe':
     cid=int(d['channel_id']); chans.add(cid); q=db(); head=q.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(cid,)).fetchone()['n']; q.close(); out({'type':'subscribed','channel_id':cid,'head_event_id':head})
  except: pass
  with slock:
   try: subs.remove((out,chans))
   except: pass
def run(): ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
if __name__=='__main__': run()
