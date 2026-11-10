import os,sys,json,re,sqlite3,secrets,hashlib,base64,datetime,threading,socket,struct,time,urllib.parse
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; os.makedirs('/var/lib/huddle',exist_ok=True)
def conn():
 c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=2000'); return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 c=conn(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));'''); c.commit(); c.close()
def row(q,a=()):
 c=conn(); x=c.execute(q,a).fetchone(); c.close(); return x
def allr(q,a=()):
 c=conn(); x=c.execute(q,a).fetchall(); c.close(); return x
def ujson(u): return {'id':u['id'],'username':u['username'],'display_name':u['display_name'],'timezone':u['timezone'],'avatar_url':u['avatar_url'],'status_text':u['status_text'],'status_emoji':u['status_emoji']}
def cjson(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'],'is_archived':bool(x['is_archived'])}
def user_from_token(t): return row('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(t,))
def get_workspace_by_slug(s): return row('SELECT * FROM workspaces WHERE slug=?',(s,))
def get_channel(i): return row('SELECT * FROM channels WHERE id=?',(i,))
def role(w,u): x=row('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w,u)); return x['role'] if x else None
def members(i): return allr('SELECT u.* FROM users u JOIN cm ON u.id=cm.user_id WHERE cm.channel_id=?',(i,))
def addmem(i,u):
 c=conn(); c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(i,u)); c.commit(); c.close()
def msgjson(m):
 au=row('SELECT * FROM users WHERE id=?',(m['author_id'],)); rs=allr('SELECT body FROM messages WHERE channel_id=? AND parent_id=? AND deleted=0',(m['channel_id'],m['id']))
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':ujson(au),'body':m['body'],'parent_id':m['parent_id'],'reply_count':len(rs),'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[],'reactions':[{'emoji':r['emoji'],'count':len(allr('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(m['id'],r['emoji']))),'user_ids':[x['user_id'] for x in allr('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(m['id'],r['emoji']))]} for r in allr('SELECT DISTINCT emoji FROM reactions WHERE message_id=?',(m['id'],))],'mentions':mentions(m['body'],m['channel_id'],m['author_id'])}
def mentions(body,ch,author):
 out=[]; names=re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body)
 for n in names:
  if n=='channel': out += [x['id'] for x in members(ch) if x['id']!=author]; continue
  x=row('SELECT u.id FROM users u JOIN wm ON u.id=wm.user_id JOIN channels c ON c.workspace_id=wm.workspace_id JOIN cm ON cm.user_id=u.id AND cm.channel_id=c.id WHERE c.id=? AND u.username=?',(ch,n))
  if x and x['id']!=author: out.append(x['id'])
 return list(dict.fromkeys(out))
def emit(ch,kind,payload):
 c=conn(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone(); n=(n['n'] if n else 0)+1
 c.execute('INSERT OR REPLACE INTO seq VALUES(?,?)',(ch,n)); c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(ch,n,kind,json.dumps(payload))); c.commit(); c.close(); return {'type':kind,'event_id':n,'channel_id':ch,**payload}
def create_message(ch,uid,body,parent=None):
 if not body.strip(): raise ValueError()
 c=conn(); t=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch,uid,body,parent,t)); i=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); c.close(); m=row('SELECT * FROM messages WHERE id=?',(i,)); emit(ch,'message.reply' if parent else 'message.created',{'message':msgjson(m)}); return m
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,status,d):
  b=json.dumps(d).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or '{}')
 def auth(self):
  z=self.headers.get('Authorization',''); return user_from_token(z[7:] if z.startswith('Bearer ') else '')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/api/ws':
   return self.websocket(p)
  if path=='/': return self.html()
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':int(sys.argv[1])-8000})
  if path=='/api/auth/me':
   u=self.auth(); return self.sendj(200,{'user':ujson(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  u=self.auth()
  if not u: return self.sendj(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   ws=[dict(x) for x in allr('SELECT * FROM workspaces w JOIN wm ON w.id=wm.workspace_id WHERE wm.user_id=?',(u['id'],))]; return self.sendj(200,{'workspaces':ws})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   cs=[cjson(x) for x in allr('SELECT * FROM channels WHERE workspace_id=? AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?)) AND is_archived=0',(w['id'],u['id']))]
   return self.sendj(200,{'workspace':dict(w),'channels':cs})
  m=re.match(r'/api/channels/(\d+)/messages',path)
  if m:
   ch=get_channel(m.group(1)); 
   if not ch:return self.sendj(404,{})
   if not role(ch['workspace_id'],u['id']) or (ch['is_private'] and not row('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id']))):return self.sendj(403,{})
   return self.sendj(200,{'messages':[msgjson(x) for x in allr('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC',(ch['id'],))],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies',path)
  if m:
   return self.sendj(200,{'replies':[msgjson(x) for x in allr('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m.group(1),))],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/members',path)
  if m:return self.sendj(200,{'members':[ujson(x) for x in members(m.group(1))]})
  return self.sendj(404,{})
 def websocket(self,p):
  u=user_from_token(urllib.parse.parse_qs(p.query).get('token',[''])[0])
  if not u:
   self.close_connection=True
   return
  key=self.headers.get('Sec-WebSocket-Key','')
  if not key:
   self.close_connection=True
   return
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols')
  self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  self.close_connection=True
  def frame(obj):
   d=json.dumps(obj,separators=(',',':')).encode(); n=len(d)
   if n<126: h=bytes([129,n])
   elif n<65536: h=bytes([129,126])+struct.pack('!H',n)
   else: h=bytes([129,127])+struct.pack('!Q',n)
   try:self.connection.sendall(h+d)
   except:pass
  def readframe():
   try:
    self.connection.settimeout(.15); h=self.connection.recv(2)
    if len(h)<2:return None
    n=h[1]&127
    if n==126:n=struct.unpack('!H',self.connection.recv(2))[0]
    elif n==127:n=struct.unpack('!Q',self.connection.recv(8))[0]
    mask=self.connection.recv(4); d=b''
    while len(d)<n:
     z=self.connection.recv(n-len(d))
     if not z:return None
     d+=z
    return json.loads(bytes(x^mask[i%4] for i,x in enumerate(d)).decode())
   except socket.timeout:return None
   except:return False
  subscribed={}; last={}
  while True:
   q=readframe()
   if q is False:return
   if q:
    typ=q.get('type'); ch=q.get('channel_id')
    if typ=='subscribe' and ch is not None:
     x=row('SELECT n FROM seq WHERE channel_id=?',(ch,)); head=x['n'] if x else 0
     subscribed[int(ch)]=True; last[int(ch)]=head; frame({'type':'subscribed','channel_id':ch,'head_event_id':head})
    elif typ=='resume' and ch is not None:
     since=int(q.get('since_event_id',0)); es=allr('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since))
     for e in es:
      z=json.loads(e['payload']); frame({'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**z})
     x=row('SELECT n FROM seq WHERE channel_id=?',(ch,)); head=x['n'] if x else since
     last[int(ch)]=head; frame({'type':'resumed','channel_id':ch,'head_event_id':head})
   for ch in list(subscribed):
    es=allr('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last.get(ch,0)))
    for e in es:
     z=json.loads(e['payload']); frame({'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**z}); last[ch]=e['event_id']
 def do_POST(self):
  path=self.path.split('?')[0]; b=self.body()
  if path=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return self.sendj(400,{'error':'invalid username or password'})
   try:
    c=conn(); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(b['username'],b['password'],b.get('display_name') or b['username'])); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); c.close()
   except:
    try: c.rollback(); c.close()
    except: pass
    return self.sendj(409,{'error':'duplicate'})
   return self.login_user(row('SELECT * FROM users WHERE id=?',(uid,)))
  if path=='/api/auth/login':
   x=row('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password')))
   return self.login_user(x) if x else self.sendj(401,{'error':'invalid'})
  u=self.auth()
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return self.sendj(400,{'error':'invalid slug'})
   try:
    c=conn(); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id'])); w=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO wm VALUES(?,?,?)',(w,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general')); g=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cm VALUES(?,?)',(g,u['id'])); c.commit(); c.close(); return self.sendj(201,{'workspace':dict(get_workspace_by_slug(b['slug'])),'general_channel':cjson(get_channel(g))})
   except:
    try: c.rollback(); c.close()
    except: pass
    return self.sendj(409,{'error':'duplicate'})
  m=re.match(r'/api/workspaces/([^/]+)/channels',path)
  if m:
   w=get_workspace_by_slug(m.group(1)); r=role(w['id'],u['id']) if w else None
   if not w:return self.sendj(404,{})
   if not r or (b.get('is_private') and r!='owner'):return self.sendj(403,{})
   try:
    c=conn(); c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); i=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cm VALUES(?,?)',(i,u['id'])); c.commit(); c.close(); return self.sendj(201,{'channel':cjson(get_channel(i))})
   except:
    try: c.rollback(); c.close()
    except: pass
    return self.sendj(409,{'error':'duplicate channel'})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)',path)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{})
   if role(ch['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
   val=1 if m.group(2)=='archive' else 0
   c=conn(); c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id'])); c.commit(); c.close()
   return self.sendj(200,{'channel':cjson(get_channel(ch['id']))})
  m=re.match(r'/api/channels/(\d+)/join',path)
  if m:
   ch=get_channel(m.group(1)); 
   if not ch:return self.sendj(404,{})
   if not role(ch['workspace_id'],u['id']):
    cc=conn(); cc.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member')); cc.commit(); cc.close()
   if ch['is_private']:return self.sendj(403,{})
   addmem(ch['id'],u['id']); return self.sendj(200,{'channel':cjson(ch)})
  m=re.match(r'/api/channels/(\d+)/messages',path)
  if m:
   ch=get_channel(m.group(1)); 
   if not ch:return self.sendj(404,{})
   if ch['is_archived']:return self.sendj(423,{})
   try:return self.sendj(201,{'message':msgjson(create_message(ch['id'],u['id'],b.get('body',''),b.get('parent_id')))})
   except:return self.sendj(400,{'error':'empty body'})
  m=re.match(r'/api/messages/(\d+)/reactions',path)
  if m:
   mid=int(m.group(1)); x=row('SELECT * FROM messages WHERE id=?',(mid,))
   if not x:return self.sendj(404,{})
   c=conn(); c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(mid,u['id'],b.get('emoji','😀'))); c.commit(); c.close()
   return self.sendj(200,{'reactions':msgjson(row('SELECT * FROM messages WHERE id=?',(mid,)))['reactions']})
  return self.sendj(404,{})
 def login_user(self,u):
  if not u:return self.sendj(401,{})
  t=secrets.token_urlsafe(24); c=conn(); c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id'])); c.commit(); c.close(); return self.sendj(201 if self.path.endswith('register') else 200,{'user':ujson(u),'token':t})
 def do_PATCH(self):
  u=self.auth(); b=self.body()
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)',self.path); x=row('SELECT * FROM messages WHERE id=?',(m.group(1),)) if m else None
  if x:
   if x['deleted']:return self.sendj(409,{})
   if x['author_id']!=u['id']:return self.sendj(403,{})
   if not b.get('body','').strip():return self.sendj(400,{})
   c=conn(); c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),x['id'])); c.commit(); c.close(); x=row('SELECT * FROM messages WHERE id=?',(x['id'],)); emit(x['channel_id'],'message.updated',{'message':msgjson(x)}); return self.sendj(200,{'message':msgjson(x)})
  m=re.match(r'/api/channels/(\d+)',self.path)
  if m:
   ch=get_channel(m.group(1)); c=conn(); c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),ch['id'])); c.commit(); c.close(); return self.sendj(200,{'channel':cjson(get_channel(ch['id']))})
  return self.sendj(404,{})
 def do_DELETE(self):
  u=self.auth(); 
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)',self.path); x=row('SELECT * FROM messages WHERE id=?',(m.group(1),)) if m else None
  if not x:return self.sendj(404,{})
  if x['author_id']!=u['id'] and role(get_channel(x['channel_id'])['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
  if not x['deleted']:
   c=conn(); c.execute('UPDATE messages SET deleted=1 WHERE id=?',(x['id'],)); c.commit(); c.close(); emit(x['channel_id'],'message.deleted',{'message_id':x['id']})
  return self.sendj(200,{'deleted':True})
 def html(self):
  b=open('/app/static/index.html','rb').read(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
def ws_clients():
 return []
IRC_NICKS=set(); IRC_LOCK=threading.Lock(); IRC_CLIENTS=[]; IRC_EVENTS={}
def irc_pump():
 while True:
  try:
   es=allr("SELECT * FROM events WHERE kind='message.created' ORDER BY id")
   for e in es:
    if e['id'] in IRC_EVENTS: continue
    IRC_EVENTS[e['id']]=1
    d=json.loads(e['payload']); m=d.get('message',{}); ch=get_channel(e['channel_id'])
    w=get_workspace_by_slug(str(row('SELECT slug FROM workspaces WHERE id=?',(ch['workspace_id'],))['slug']))
    nm='#'+w['slug']+'/'+ch['name']; au=row('SELECT username FROM users WHERE id=?',(m.get('author_id'),)); nick=au['username'] if au else 'user'
    line=':'+nick+'!'+nick+'@host PRIVMSG '+nm+' :'+m.get('body','')
    with IRC_LOCK:
     clients=list(IRC_CLIENTS)
    for cl in clients:
     sock,cnick,joined=cl
     if e['channel_id'] in joined:
      try:sock.sendall((line+'\r\n').encode())
      except:pass
  except Exception: pass
  time.sleep(.1)
def irc():
 s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('0.0.0.0',6667)); s.listen()
 threading.Thread(target=irc_pump,daemon=True).start()
 while 1:
  q,_=s.accept(); threading.Thread(target=irc_client,args=(q,),daemon=True).start()
def irc_client(s):
 token=None; nick=None; user=None; joined={}; nick_blocked=False
 IRC_CLIENTS.append((s,nick,joined))
 def send(x): s.sendall((x+'\r\n').encode())
 try:
  while 1:
   line=s.recv(4096).decode(errors='ignore')
   if not line:break
   for z in line.splitlines():
    a=z.split(); cmd=a[0].upper() if a else ''
    if cmd=='PASS': token=a[1] if len(a)>1 else ''
    elif cmd=='NICK':
     requested=a[1] if len(a)>1 else ''
     if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',requested):continue
     with IRC_LOCK:
      collision=requested.lower() in {x.lower() for x in IRC_NICKS} and requested.lower() != (nick or '').lower()
      if not collision:
       if nick: IRC_NICKS.discard(nick)
       nick=requested; IRC_NICKS.add(nick)
       for j in IRC_CLIENTS:
        if j[0] is s: j=(s,nick,joined); IRC_CLIENTS[IRC_CLIENTS.index(next(x for x in IRC_CLIENTS if x[0] is s))]=j; break
     if collision:
      nick_blocked=True
      send(':huddle 433 '+(nick or '*')+' '+requested+' :Nickname is already in use')
    elif cmd=='USER':
     if nick_blocked: continue
     user=user_from_token(token)
     if not user: send(':huddle 464 * Password incorrect'); continue
     send(':huddle 001 '+nick+' :Welcome'); send(':huddle 002 '+nick+' :Your host'); send(':huddle 003 '+nick+' :Created'); send(':huddle 004 '+nick+' :huddle'); send(':huddle 005 '+nick+' :CHANTYPES=#'); send(':huddle 422 '+nick+' :No MOTD')
    elif cmd=='PING':send('PONG '+(' '.join(a[1:]) if len(a)>1 else ''))
    elif cmd=='QUIT':return
    elif cmd=='JOIN' and len(a)>1:
     nm=a[1]; mm=re.match(r'#([^/]+)/(.+)',nm); ch=None
     if mm:
      w=get_workspace_by_slug(mm[1]); ch=row('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],mm[2])) if w else None
     if not ch:send(':huddle 403 '+nick+' :No such channel')
     else:
      addmem(ch['id'],user['id']); joined[ch['id']]=nm; send(':'+nick+'!'+user['username']+'@host JOIN '+nm); send(':huddle 353 '+nick+' = '+nm+' :'+nick); send(':huddle 366 '+nick+' '+nm+' :End')
    elif cmd=='PRIVMSG' and len(a)>2:
     nm=a[1]; body=z.split(' :',1)[1] if ' :' in z else ''
     mm=re.match(r'#([^/]+)/(.+)',nm); w=get_workspace_by_slug(mm[1]) if mm else None; ch=row('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],mm[2])) if w else None
     if ch and user:create_message(ch['id'],user['id'],body)
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): pass
    else:
     if cmd not in ('PASS','NICK','USER'):send(':huddle 421 '+(nick or '*')+' '+cmd+' :Unknown command')
 finally:
  with IRC_LOCK:
   IRC_CLIENTS[:] = [x for x in IRC_CLIENTS if x[0] is not s]
  if nick:
   with IRC_LOCK: IRC_NICKS.discard(nick)
  s.close()
def main():
 init()
 if len(sys.argv)>1:
  port=int(sys.argv[1]); threading.Thread(target=irc,daemon=True).start() if port==8000 else None
  ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
if __name__=='__main__':main()
