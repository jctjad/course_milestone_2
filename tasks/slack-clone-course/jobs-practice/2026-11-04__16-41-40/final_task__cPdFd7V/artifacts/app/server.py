import sys,os,re,json,sqlite3,hashlib,secrets,threading,time,socket,base64,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from datetime import datetime,timezone
DB='/var/lib/huddle/huddle.db'
IRC_CLIENTS=[]; IRC_LOCK=threading.Lock(); IRC_NICKS=set()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
'''); c.commit(); c.close()
def userj(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def channelj(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'] or '','is_archived':bool(x['is_archived'])}
def auth(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def token(h):
 v=h or ''; return auth(v[7:] if v.startswith('Bearer ') else v)
def channel(cid):
 c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return r
def workspace(slug):
 c=db(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); c.close(); return r
def role(w,u):
 c=db(); r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); c.close(); return r['role'] if r else None
def member(ch,u):
 c=db(); r=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch,u)).fetchone(); c.close(); return bool(r)
def reactions(mid):
 c=db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(mid,)).fetchall(); c.close(); d={}
 for r in rows:d.setdefault(r['emoji'],[]).append(r['user_id'])
 return [{'emoji':e,'count':len(v),'user_ids':v} for e,v in d.items()]
def msgj(r):
 c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); n=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':n,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':reactions(r['id']),'mentions':[]}
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload))); c.commit(); c.close(); return n
def create_msg(cid,uid,body,parent=None):
 c=db(); t=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,uid,body,parent,t)); mid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close()
 j=msgj(r); ch=channel(cid); w=workspace_by_id(ch['workspace_id']); ids=[]
 for name in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
  if name=='channel':
   c=db(); rows=c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(cid,uid)).fetchall(); c.close(); ids += [x['user_id'] for x in rows]
  else:
   c=db(); x=c.execute('SELECT u.id FROM users u JOIN wm m ON m.user_id=u.id WHERE m.workspace_id=? AND u.username=? AND u.id!=?',(w['id'],name,uid)).fetchone(); c.close()
   if x: ids.append(x['id'])
 j['mentions']=list(dict.fromkeys(ids))
 emit(cid,'message.reply' if parent else 'message.created',{'message':j}); return j
def frame_event(e):
 return {'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**json.loads(e['payload'])}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def out(self,code,obj,typ='application/json'):
  b=obj if isinstance(obj,bytes) else json.dumps(obj).encode(); self.send_response(code);self.send_header('Content-Type',str(typ));self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def read(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def au(self):
  u=token(self.headers.get('Authorization')); 
  if not u:self.out(401,{'error':'unauthorized'})
  return u
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/': self.send_response(200); b=open('/app/static/index.html','rb').read(); self.send_header('Content-Type','text/html'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if path=='/api/health': return self.out(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if path=='/api/auth/me':
   u=self.au()
   if u:return self.out(200,{'user':userj(u)})
  if path=='/api/workspaces':
   u=self.au()
   if not u:return
   c=db(); ws=c.execute('SELECT w.* FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return self.out(200,{'workspaces':[dict(x) for x in ws]})
  m=re.match(r'^/api/workspaces/([^/]+)$',path)
  if m:
   u=self.au();w=workspace(m.group(1))
   if not u:return
   if not w:return self.out(404,{'error':'not found'})
   if not role(w['id'],u['id']):return self.out(403,{'error':'forbidden'})
   c=db();ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))',(w['id'],u['id'])).fetchall();c.close();return self.out(200,{'workspace':dict(w),'channels':[channelj(x) for x in ch]})
  m=re.match(r'^/api/channels/(\d+)/messages$',path)
  if m:
   u=self.au();ch=channel(int(m.group(1)))
   if not u:return
   if not ch:return self.out(404,{})
   if ch['is_private'] and not member(ch['id'],u['id']):return self.out(403,{})
   c=db();rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(ch['id'],)).fetchall();c.close();return self.out(200,{'messages':[msgj(x) for x in rs],'next_cursor':None})
  m=re.match(r'^/api/messages/(\d+)/replies$',path)
  if m:
   u=self.au();c=db();rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m.group(1),)).fetchall();c.close()
   if not u:return
   return self.out(200,{'replies':[msgj(x) for x in rs],'next_cursor':None})
  m=re.match(r'^/api/channels/(\d+)/members$',path)
  if m:
   u=self.au();c=db();rs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(m.group(1),)).fetchall();c.close()
   if not u:return
   return self.out(200,{'members':[userj(x) for x in rs]})
  if path=='/api/ws': return self.websocket()
  if path.startswith('/api/'): return self.out(404,{'error':'not found'})
  return self.out(404,{})
 def do_POST(self):
  p=urlparse(self.path);path=p.path; b=self.read()
  if path=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(b.get('username',''))) or len(str(b.get('password','')))<8:return self.out(400,{'error':'invalid'})
   c=db()
   try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username']));uid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.commit()
   except sqlite3.IntegrityError:c.close();return self.out(409,{'error':'duplicate'})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.out(201,{'user':userj(u),'token':t})
  if path=='/api/auth/login':
   c=db();u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
   if not u:c.close();return self.out(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.commit();c.close();return self.out(200,{'user':userj(u),'token':t})
  u=self.au()
  if not u:return
  m=re.match(r'^/api/workspaces/([^/]+)/channels$',path)
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',str(b.get('slug',''))) or not b.get('name'):return self.out(400,{})
   c=db()
   try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']));wid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.out(201,{'workspace':dict(w),'general_channel':channelj(ch)})
   except sqlite3.IntegrityError:c.close();return self.out(409,{})
  if m:
   w=workspace(m.group(1))
   if not w:return self.out(404,{})
   if not role(w['id'],u['id']):return self.out(403,{})
   name=b.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.out(400,{})
   if b.get('is_private') and role(w['id'],u['id'])!='owner':return self.out(403,{})
   c=db()
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')));cid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.out(201,{'channel':channelj(r)})
   except sqlite3.IntegrityError:c.close();return self.out(409,{})
  m=re.match(r'^/api/channels/(\d+)/join$',path)
  if m:
   ch=channel(int(m.group(1)))
   if not ch:return self.out(404,{})
   w=workspace_by_id(ch['workspace_id'])
   if ch['is_private']:return self.out(403,{})
   c=db();c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(w['id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();c.close();return self.out(200,{'channel':channelj(ch)})
  m=re.match(r'^/api/channels/(\d+)/messages$',path)
  if m:
   ch=channel(int(m.group(1)))
   if not ch:return self.out(404,{})
   if not member(ch['id'],u['id']):return self.out(403,{})
   if ch['is_archived']:return self.out(423,{})
   body=str(b.get('body',''))
   if not body.strip():return self.out(400,{})
   return self.out(201,{'message':create_msg(ch['id'],u['id'],body,b.get('parent_id'))})
  m=re.match(r'^/api/messages/(\d+)/reactions$',path)
  if m:
   c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(m.group(1),u['id'],b.get('emoji','👍')));c.commit();vals=reactions(int(m.group(1)));c.close();return self.out(200,{'reactions':vals})
  m=re.match(r'^/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   ch=channel(int(m.group(1)));w=workspace_by_id(ch['workspace_id'])
   if role(w['id'],u['id'])!='owner':return self.out(403,{})
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(m.group(2)=='archive',ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close();return self.out(200,{'channel':channelj(r)})
  return self.out(404,{})
 def do_PATCH(self):
  b=self.read();u=self.au()
  if not u:return
  m=re.match(r'^/api/messages/(\d+)$',self.path)
  if m:
   c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not r:c.close();return self.out(404,{})
   if r['deleted']:c.close();return self.out(409,{})
   if r['author_id']!=u['id']:c.close();return self.out(403,{})
   if not str(b.get('body','')).strip():c.close();return self.out(400,{})
   c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),r['id']));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();c.close();j=msgj(r);emit(r['channel_id'],'message.updated',{'message':j});return self.out(200,{'message':j})
  m=re.match(r'^/api/channels/(\d+)$',self.path)
  if m:
   ch=channel(int(m.group(1)));w=workspace_by_id(ch['workspace_id'])
   if not member(ch['id'],u['id']) and role(w['id'],u['id'])!='owner':return self.out(403,{})
   if len(str(b.get('topic','')))>250:return self.out(400,{})
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close();return self.out(200,{'channel':channelj(r)})
  return self.out(404,{})
 def do_DELETE(self):
  u=self.au()
  if not u:return
  m=re.match(r'^/api/messages/(\d+)$',self.path)
  if not m:return self.out(404,{})
  c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
  if not r:c.close();return self.out(404,{})
  w=workspace_by_id(channel(r['channel_id'])['workspace_id'])
  if r['author_id']!=u['id'] and role(w['id'],u['id'])!='owner':c.close();return self.out(403,{})
  if not r['deleted']:
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));c.commit();c.close();emit(r['channel_id'],'message.deleted',{'message_id':r['id']})
  c.close();return self.out(200,{'deleted':True})
 def websocket(self):
  t=parse_qs(urlparse(self.path).query).get('token',[''])[0]; u=auth(t)
  if not u:
   self.send_error(401); return
  key=self.headers.get('Sec-WebSocket-Key','')
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.close_connection=True
  self.connection.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
  def send(x):
   b=json.dumps(x).encode(); n=len(b)
   if n<126: h=bytes([129,n])
   elif n<65536: h=bytes([129,126])+struct.pack('!H',n)
   else: h=bytes([129,127])+struct.pack('!Q',n)
   self.connection.sendall(h+b)
  subs={}; last={}
  self.connection.settimeout(.2)
  while True:
   try:
    h=self.connection.recv(2)
    if not h: break
    n=h[1]&127
    if n==126: n=struct.unpack('!H',self.connection.recv(2))[0]
    elif n==127: n=struct.unpack('!Q',self.connection.recv(8))[0]
    mask=self.connection.recv(4); data=self.connection.recv(n)
    data=bytes(data[i]^mask[i%4] for i in range(n)); q=json.loads(data)
    if q.get('type') in ('subscribe','resume'):
     cid=int(q['channel_id']); since=int(q.get('since_event_id',0)); subs[cid]=True
     if q.get('type')=='subscribe':
      cc=db(); head=cc.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; cc.close(); last[cid]=head
      send({'type':'subscribed','channel_id':cid,'head_event_id':head})
     else:
      last[cid]=since
      send({'type':'resumed','channel_id':cid,'head_event_id':since})
   except socket.timeout:
    pass
   except Exception:
    break
   c=db()
   for cid in list(subs):
    for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last.get(cid,0))).fetchall():
     send(frame_event(e)); last[cid]=e['event_id']
   c.close()
def workspace_by_id(i):
 c=db();r=c.execute('SELECT * FROM workspaces WHERE id=?',(i,)).fetchone();c.close();return r
def irc_send(s,line):
 try: s.sendall((line+'\r\n').encode())
 except: pass
def irc_name(ch):
 w=workspace_by_id(ch['workspace_id']);return '#'+w['slug']+'/'+ch['name']
def irc_loop():
 srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(('0.0.0.0',6667));srv.listen()
 def client(s):
  u=None;nick=None;joined={};last=0; rejected=False
  with IRC_LOCK:IRC_CLIENTS.append((s,lambda: u,lambda: joined,nick))
  try:
   buf=b''
   while True:
    x=s.recv(4096)
    if not x:break
    buf+=x
    while b'\r\n' in buf:
     line,buf=buf.split(b'\r\n',1); z=line.decode(errors='ignore').split(' ',2);cmd=z[0].upper()
     if cmd=='PASS':
      u=auth(z[1] if len(z)>1 else '')
      if not u: rejected=True; irc_send(s,':huddle 464 * Password incorrect')
     elif rejected:
      continue
     elif cmd=='NICK':
      nn=z[1] if len(z)>1 else ''
      with IRC_LOCK: taken=nn in IRC_NICKS
      if taken:irc_send(s,':huddle 433 * '+nn+' Nick in use')
      else:
       nick=nn
       with IRC_LOCK: IRC_NICKS.add(nn)
     elif cmd=='USER' and u and nick:
      for code,text in [('001','Welcome'),('002','Your host'),('003','Created'),('004','huddle'),('005','CHANTYPES=#')]:irc_send(s,':huddle '+code+' '+nick+' '+text)
      irc_send(s,':huddle 422 '+nick+' MOTD File')
     elif cmd=='PING':irc_send(s,':huddle PONG '+(z[-1] if len(z)>1 else ''))
     elif cmd=='QUIT':return
     elif cmd=='JOIN' and len(z)>1:
      name=z[1];c=db();ch=c.execute("SELECT * FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE '#' || w.slug || '/' || c.name=?",(name,)).fetchone();c.close()
      if not ch:irc_send(s,':huddle 403 '+nick+' '+name+' No such channel')
      else:
       joined[ch['id']]=ch;add_channel_member(ch['id'],u['id']);irc_send(s,':'+nick+'!'+u['username']+'@host JOIN '+name);irc_send(s,':huddle 353 '+nick+' = '+name+' :'+nick);irc_send(s,':huddle 366 '+nick+' '+name+' End')
     elif cmd=='PRIVMSG' and len(z)>2 and z[1].startswith('#'):
      name=z[1];body=z[2][1:] if z[2].startswith(':') else z[2];c=db();ch=c.execute("SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE '#' || w.slug || '/' || c.name=?",(name,)).fetchone();c.close()
      if ch:create_msg(ch['id'],u['id'],body)
     elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): pass
     else: irc_send(s,':huddle 421 '+(nick or '*')+' '+cmd+' Unknown command')
  finally:
   with IRC_LOCK:
    try: IRC_CLIENTS.remove(next(q for q in IRC_CLIENTS if q[0]==s))
    except: pass
    if nick: IRC_NICKS.discard(nick)
   s.close()
 while True:
  s,_=srv.accept();threading.Thread(target=client,args=(s,),daemon=True).start()
def add_channel_member(cid,uid):
 c=db();c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,uid));c.commit();c.close()
def pump_irc():
 seen={}
 while True:
  c=db()
  for e in c.execute("SELECT e.*,m.author_id,m.body FROM events e LEFT JOIN messages m ON json_extract(e.payload,'$.message.id')=m.id WHERE e.kind='message.created' ORDER BY e.id").fetchall():
   if e['id']<=seen.get('x',0):continue
   seen['x']=e['id'];ch=channel(e['channel_id']); name=irc_name(ch); author=auth_user(e['author_id'])
   with IRC_LOCK:
    for s,getu,gj,n in list(IRC_CLIENTS):
     if e['channel_id'] in gj():irc_send(s,':'+author['username']+'!'+author['username']+'@host PRIVMSG '+name+' :'+e['body'])
  c.close();time.sleep(.15)
def auth_user(i):
 c=db();r=c.execute('SELECT * FROM users WHERE id=?',(i,)).fetchone();c.close();return r
if __name__=='__main__':
 init()
 if '--init' in sys.argv: sys.exit(0)
 if '--irc' in sys.argv:
  threading.Thread(target=pump_irc,daemon=True).start();irc_loop()
 else:
  port=int(os.environ.get('PORT','8000'));ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
