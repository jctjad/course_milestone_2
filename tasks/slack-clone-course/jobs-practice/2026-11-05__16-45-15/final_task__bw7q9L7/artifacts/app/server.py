import os,sys,json,re,sqlite3,hashlib,secrets,datetime,threading,time,socket,base64,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
DB='/var/lib/huddle/huddle.db'; FILES='/var/lib/huddle/files'
os.makedirs(os.path.dirname(DB),exist_ok=True); os.makedirs(FILES,exist_ok=True)
SCHEMA='''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);'''
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript(SCHEMA); c.commit(); c.close()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def userj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def tokenuser(c,t):
 r=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(t,)).fetchone(); return r
def auth(h,c):
 x=h.get('Authorization',''); return tokenuser(c,x[7:] if x.startswith('Bearer ') else '')
def chan(r):
 return dict(id=r['id'],workspace_id=r['workspace_id'],name=r['name'],is_private=bool(r['is_private']),is_dm=bool(r['is_dm']),topic=r['topic'] or '',is_archived=bool(r['is_archived']))
def msg(c,r):
 reactions=[]
 for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
  ids=[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]
  reactions.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 author=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 replies=c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(author),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':reactions,'mentions':mentions(c,r)}
def mentions(c,r):
 out=[]; body=r['body']
 for name in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
  u=c.execute('select id from users where username=?',(name,)).fetchone()
  if u and u[0]!=r['author_id'] and c.execute('select 1 from wm w join channels ch on ch.workspace_id=w.workspace_id where w.user_id=? and ch.id=?',(u[0],r['channel_id'])).fetchone() and u[0] not in out: out.append(u[0])
 if re.search(r'(?<!\w)@channel\b',body):
  for x in c.execute('select user_id from cm where channel_id=?',(r['channel_id'],)):
   if x[0]!=r['author_id'] and x[0] not in out: out.append(x[0])
 return out
def emit(c,cid,kind,payload):
 c.execute('insert or ignore into counters(channel_id,n) values(?,0)',(cid,)); c.execute('update counters set n=n+1 where channel_id=?',(cid,)); n=c.execute('select n from counters where channel_id=?',(cid,)).fetchone()[0]
 c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(cid,n,kind,json.dumps(payload))); c.commit()
 return {'type':kind,'event_id':n,'channel_id':cid,**payload}
def getch(c,i): return c.execute('select * from channels where id=?',(i,)).fetchone()
WS=[]; IRC=[]; LOCK=threading.Lock()
def broadcast(frame):
 with LOCK:
  for q in WS:
   if q[0]==frame['channel_id']: q[1].append(frame)
def pump():
 last={}
 while True:
  try:
   c=db()
   for r in c.execute('select * from events order by id'):
    if r['id']<=last.get('e',0): continue
    last['e']=r['id']; f={'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id'],**json.loads(r['payload'])}; broadcast(f)
    if r['kind']=='message.created':
     m=f['message']; cr=c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(r['channel_id'],)).fetchone()
     if cr:
      name='#'+cr['slug']+'/'+cr['name']
      with LOCK:
       for cl in IRC:
        if r['channel_id'] in cl[2] and m['author_id']!=cl[1]:
         sendline(cl[0],':%s!%s@localhost PRIVMSG %s :%s'%(m['author']['username'],m['author']['username'],name,m['body']))
   c.close()
  except: pass
  time.sleep(.08)
def frame(s,obj):
 b=json.dumps(obj).encode()
 if len(b)<126: h=b'\x81'+bytes([len(b)])
 elif len(b)<65536: h=b'\x81\x7e'+struct.pack('>H',len(b))
 else: h=b'\x81\x7f'+struct.pack('>Q',len(b))
 s.sendall(h+b)
def wsread(s):
 h=s.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=struct.unpack('>H',s.recv(2))[0]
 elif n==127:n=struct.unpack('>Q',s.recv(8))[0]
 mask=s.recv(4) if (h[1]&128) else b'\0\0\0\0'
 b=s.recv(n); return bytes(b[i]^mask[i%4] for i in range(n)).decode()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def sendj(self,status,o):
  b=json.dumps(o).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); c=db()
  if p.path=='/': b=open('/app/static/index.html','rb').read(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); c.close(); return
  if p.path=='/api/health': self.sendj(200,{'status':'ok','node_id':int(self.server.server_port)-8000}); c.close(); return
  if p.path=='/api/ws':
   t=parse_qs(p.query).get('token',[''])[0]; u=tokenuser(c,t)
   if not u: self.send_error(401); c.close(); return
   key=self.headers.get('Sec-WebSocket-Key'); self.send_response(101); self.send_header('Upgrade','WebSocket'); self.send_header('Connection','Upgrade'); import hashlib as hh
   self.send_header('Sec-WebSocket-Accept',base64.b64encode(hh.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()); self.end_headers()
   q=[]; 
   try:
    while True:
     x=wsread(self.connection)
     if not x: break
     z=json.loads(x); cid=int(z.get('channel_id',0))
     if z['type'] in ('subscribe','resume'):
      if z['type']=='resume':
       for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,int(z.get('since_event_id',0)))):
        q.append({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
      frame(self.connection,{'type':'subscribed' if z['type']=='subscribe' else 'resumed','channel_id':cid,'head_event_id':c.execute('select coalesce(n,0) from counters where channel_id=?',(cid,)).fetchone()[0] if c.execute('select n from counters where channel_id=?',(cid,)).fetchone() else 0})
      with LOCK: WS.append((cid,q))
      while True:
       if q: frame(self.connection,q.pop(0))
       self.connection.settimeout(.2)
       try:
        y=wsread(self.connection)
        if y: break
       except socket.timeout: pass
   except: pass
   with LOCK:
    for x in WS:
     if x[1] is q: WS.remove(x)
   c.close(); return
  m=re.match(r'/api/auth/me$',p.path)
  u=auth(self.headers,c)
  if m:
   if not u: self.sendj(401,{'error':'unauthorized'})
   else: self.sendj(200,{'user':userj(u)})
   c.close(); return
  if not u: self.sendj(401,{'error':'unauthorized'}); c.close(); return
  if p.path=='/api/workspaces':
   self.sendj(200,{'workspaces':[dict(id=x['id'],slug=x['slug'],name=x['name'],owner_id=x['owner_id'],join_mode='open') for x in c.execute('select w.* from workspaces w join wm on wm.workspace_id=w.id where wm.user_id=?',(u['id'],))]}); c.close(); return
  m=re.match(r'/api/workspaces/([^/]+)$',p.path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m[1],)).fetchone()
   if not w:self.sendj(404,{});c.close();return
   if not c.execute('select 1 from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():self.sendj(403,{});c.close();return
   cs=[chan(x) for x in c.execute('select * from channels where workspace_id=? and is_archived=0 and (is_private=0 or id in (select channel_id from cm where user_id=?))',(w['id'],u['id']))]
   self.sendj(200,{'workspace':dict(id=w['id'],slug=w['slug'],name=w['name'],owner_id=w['owner_id'],join_mode='open'),'channels':cs});c.close();return
  m=re.match(r'/api/channels/(\d+)/members$',p.path)
  if m:
   ch=getch(c,m[1]); self.sendj(200,{'members':[userj(c.execute('select * from users where id=?',(x[0],)).fetchone()) for x in c.execute('select user_id from cm where channel_id=?',(m[1],))]});c.close();return
  m=re.match(r'/api/channels/(\d+)/messages$',p.path)
  if m:
   ch=getch(c,m[1])
   if not ch or not c.execute('select 1 from cm where channel_id=? and user_id=?',(m[1],u['id'])).fetchone():self.sendj(403,{});c.close();return
   rows=c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(m[1],)).fetchall()
   self.sendj(200,{'messages':[msg(c,x) for x in rows],'next_cursor':None});c.close();return
  m=re.match(r'/api/messages/(\d+)/replies$',p.path)
  if m:
   rows=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(m[1],)).fetchall();self.sendj(200,{'replies':[msg(c,x) for x in rows],'next_cursor':None});c.close();return
  self.sendj(404,{});c.close()
 def do_POST(self):
  self.route('POST')
 def do_PATCH(self): self.route('PATCH')
 def do_DELETE(self): self.route('DELETE')
 def route(self,method):
  p=urlparse(self.path).path;c=db(); data=self.body() if method!='DELETE' else {}
  if p=='/api/auth/register':
   un=data.get('username','');pw=data.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:self.sendj(400,{'error':'invalid username or password'});c.close();return
   if c.execute('select 1 from users where username=?',(un,)).fetchone():self.sendj(409,{});c.close();return
   c.execute('insert into users(username,password,display_name) values(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),data.get('display_name',un)));uid=c.execute('select last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));c.commit();self.sendj(201,{'user':userj(c.execute('select * from users where id=?',(uid,)).fetchone()),'token':t});c.close();return
  if p=='/api/auth/login':
   r=c.execute('select * from users where username=? and password=?',(data.get('username'),hashlib.sha256(data.get('password','').encode()).hexdigest())).fetchone()
   if not r:self.sendj(401,{});c.close();return
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();self.sendj(200,{'user':userj(r),'token':t});c.close();return
  u=auth(self.headers,c)
  if not u:self.sendj(401,{});c.close();return
  if p=='/api/workspaces':
   slug=data.get('slug',''); name=data.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):self.sendj(400,{});c.close();return
   if c.execute('select 1 from workspaces where slug=?',(slug,)).fetchone():self.sendj(409,{});c.close();return
   c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,name,u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into wm values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();self.sendj(201,{'workspace':dict(id=wid,slug=slug,name=name,owner_id=u['id'],join_mode='open'),'general_channel':chan(c.execute('select * from channels where id=?',(cid,)).fetchone())});c.close();return
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m[1],)).fetchone(); n=data.get('name','')
   if not w or w['owner_id']!=u['id']:self.sendj(403,{});c.close();return
   if not re.fullmatch(r'[a-z0-9-]{1,32}',n):self.sendj(400,{});c.close();return
   if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],n)).fetchone():self.sendj(409,{});c.close();return
   c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],n,int(bool(data.get('is_private'))),data.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();self.sendj(201,{'channel':chan(c.execute('select * from channels where id=?',(cid,)).fetchone())});c.close();return
  m=re.match(r'/api/channels/(\d+)/join$',p)
  if m:
   ch=getch(c,m[1])
   if not ch:self.sendj(404,{});c.close();return
   if ch['is_private']:self.sendj(403,{});c.close();return
   c.execute('insert or ignore into wm values(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('insert or ignore into cm values(?,?)',(m[1],u['id']));c.commit();self.sendj(200,{'channel':chan(ch)});c.close();return
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',p)
  if m:
   ch=getch(c,m[1]);w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
   if w['owner_id']!=u['id']:self.sendj(403,{});c.close();return
   c.execute('update channels set is_archived=? where id=?',(m[2]=='archive',m[1]));c.commit();self.sendj(200,{'channel':chan(getch(c,m[1]))});c.close();return
  m=re.match(r'/api/channels/(\d+)$',p)
  if m and method=='PATCH':
   if len(data.get('topic',''))>250:self.sendj(400,{});c.close();return
   c.execute('update channels set topic=? where id=?',(data.get('topic',''),m[1]));c.commit();self.sendj(200,{'channel':chan(getch(c,m[1]))});c.close();return
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   ch=getch(c,m[1]); body=data.get('body','')
   if not body.strip():self.sendj(400,{});c.close();return
   if ch['is_archived']:self.sendj(423,{});c.close();return
   if not c.execute('select 1 from cm where channel_id=? and user_id=?',(m[1],u['id'])).fetchone():self.sendj(403,{});c.close();return
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(m[1],u['id'],body,data.get('parent_id'),now()));mid=c.execute('select last_insert_rowid()').fetchone()[0];r=c.execute('select * from messages where id=?',(mid,)).fetchone();mj=msg(c,r);f=emit(c,int(m[1]),'message.created',{'message':mj});c.close();self.sendj(201,{'message':mj});return
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   r=c.execute('select * from messages where id=?',(m[1],)).fetchone()
   if not r:self.sendj(404,{});c.close();return
   if method=='PATCH':
    if r['deleted']:self.sendj(409,{});c.close();return
    if r['author_id']!=u['id']:self.sendj(403,{});c.close();return
    if not data.get('body','').strip():self.sendj(400,{});c.close();return
    c.execute('update messages set body=?,edited_at=? where id=?',(data['body'],now(),m[1]));r=c.execute('select * from messages where id=?',(m[1],)).fetchone();mj=msg(c,r);emit(c,r['channel_id'],'message.updated',{'message':mj});c.close();self.sendj(200,{'message':mj});return
   if method=='DELETE':
    if r['deleted']:self.sendj(200,{'deleted':True});c.close();return
    w=c.execute('select * from workspaces where id=(select workspace_id from channels where id=?)',(r['channel_id'],)).fetchone()
    if r['author_id']!=u['id'] and w['owner_id']!=u['id']:self.sendj(403,{});c.close();return
    c.execute('update messages set deleted=1 where id=?',(m[1],));c.commit();emit(c,r['channel_id'],'message.deleted',{'message_id':int(m[1])});c.close();self.sendj(200,{'deleted':True});return
  m=re.match(r'/api/messages/(\d+)/reactions(?:/(.*))?$',p)
  if m:
   mid=int(m[1])
   if method=='POST':c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],data.get('emoji','')))
   else:c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],unquote(m[2] or '')))
   c.commit(); r=c.execute('select * from messages where id=?',(mid,)).fetchone(); self.sendj(200,{'reactions':msg(c,r)['reactions']});c.close();return
  self.sendj(404,{});c.close()
def sendline(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def irc_client(s):
 user=None;nick=None;joined=set();buf=b''
 try:
  while True:
   z=s.recv(4096)
   if not z:break
   buf+=z
   while b'\r\n' in buf:
    line,buf=buf.split(b'\r\n',1); a=line.decode(errors='ignore').split(' '); cmd=a[0].upper()
    if cmd=='PASS':
     c=db();user=tokenuser(c,a[1] if len(a)>1 else '');c.close()
     if not user:sendline(s,':huddle 464 * Password incorrect')
    elif cmd=='NICK':
     nn=a[1] if len(a)>1 else ''
     with LOCK: collision=any(x[3]==nn for x in IRC)
     if collision:sendline(s,':huddle 433 * '+nn+' Nickname is already in use')
     else:nick=nn
    elif cmd=='USER' and user and nick:
     sendline(s,':huddle 001 '+nick+' :Welcome');sendline(s,':huddle 002 '+nick+' :Your host');sendline(s,':huddle 003 '+nick+' :Server created');sendline(s,':huddle 004 '+nick+' huddle 1');sendline(s,':huddle 005 '+nick+' CHANTYPES=#');sendline(s,':huddle 422 '+nick+' :MOTD File is missing')
     with LOCK:IRC.append([s,user['id'],joined,nick])
    elif cmd=='PING':sendline(s,':huddle PONG '+(' '.join(a[1:])).lstrip(':'))
    elif cmd=='QUIT':return
    elif cmd=='JOIN' and len(a)>1:
     c=db(); name=a[1]; q=re.match(r'#([^/]+)/(.+)',name); ch=None
     if q: ch=c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(q[1],q[2])).fetchone()
     if not ch or ch['is_private']:sendline(s,':huddle 403 '+nick+' '+name+' :No such channel')
     else:
      joined.add(ch['id']);c.execute('insert or ignore into cm values(?,?)',(ch['id'],user['id']));c.commit();sendline(s,':%s!%s@localhost JOIN %s'%(nick,nick,name));sendline(s,':huddle 353 '+nick+' = '+name+' :'+nick);sendline(s,':huddle 366 '+nick+' '+name+' :End of NAMES')
     c.close()
    elif cmd=='PRIVMSG' and len(a)>2:
     name=a[1];text=' '.join(a[2:]).lstrip(':');c=db();q=re.match(r'#([^/]+)/(.+)',name);ch=c.execute('select ch.* from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(q[1],q[2])).fetchone() if q else None
     if ch:
      c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(ch['id'],user['id'],text,now()));mid=c.execute('select last_insert_rowid()').fetchone()[0];emit(c,ch['id'],'message.created',{'message':msg(c,c.execute('select * from messages where id=?',(mid,)).fetchone())})
     c.close()
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):sendline(s,':huddle 221 '+nick+' :OK')
    elif cmd not in ('USER',):sendline(s,':huddle 421 '+nick+' '+cmd+' :Unknown command')
 finally:
  with LOCK:
   IRC[:]=[x for x in IRC if x[0] is not s]
  try:s.close()
  except: pass
def irc():
 x=socket.socket();x.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);x.bind(('0.0.0.0',6667));x.listen()
 while True:
  s,_=x.accept();threading.Thread(target=irc_client,args=(s,),daemon=True).start()
if __name__=='__main__':
 init();threading.Thread(target=pump,daemon=True).start()
 if '--irc' in sys.argv: irc()
 else: ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
