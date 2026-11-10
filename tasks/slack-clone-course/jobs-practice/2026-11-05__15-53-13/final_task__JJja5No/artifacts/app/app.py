import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,socket,threading,time
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
DB='/var/lib/huddle/huddle.db'; NODE=int(os.environ.get('NODE_ID','0')); PORT=8000+NODE
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,UNIQUE(channel_id,event_id));'''); c.commit(); c.close()
def ujson(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def user_from_token(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def get_user_by_username(n):
 c=db();r=c.execute('SELECT * FROM users WHERE username=?',(n,)).fetchone();c.close();return r
def ws(slug):
 c=db();r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();c.close();return r
def chan(i):
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone();c.close();return r
def role(w,u):
 c=db();r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w,u)).fetchone();c.close();return r['role'] if r else None
def cjson(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'] or '','is_archived':bool(x['is_archived'])}
def members(cid):
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN cm m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall();c.close();return r
def addmem(cid,uid):
 c=db();c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,uid));c.commit();c.close()
def canread(x,uid): return (not x['is_private'] and role(x['workspace_id'],uid)) or any(m['id']==uid for m in members(x['id']))
def event(cid,kind,payload):
 c=db();c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,n,kind,json.dumps(payload),now()));c.commit();c.close();return {'type':kind,'event_id':n,'channel_id':cid,**payload}
def mention_ids(body,cid,author):
 names=re.findall(r'(?<!\\w)@([A-Za-z0-9_]+)',body or '')
 out=[]
 for n in names:
  if n=='channel':
   out += [x['id'] for x in members(cid) if x['id']!=author]
  else:
   z=get_user_by_username(n)
   if z and z['id']!=author and any(x['id']==z['id'] for x in members(cid)) and z['id'] not in out: out.append(z['id'])
 return list(dict.fromkeys(out))
def reactions(mid):
 c=db();r=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(mid,)).fetchall();c.close(); out={}
 for x in r: out.setdefault(x['emoji'],[]).append(x['user_id'])
 return [{'emoji':k,'count':len(v),'user_ids':v} for k,v in out.items()]
def mjson(m):
 c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(m['author_id'],)).fetchone(); rc=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],)).fetchone()['n'];c.close()
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':ujson(a),'body':m['body'],'parent_id':m['parent_id'],'reply_count':rc,'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[],'reactions':reactions(m['id']) if not m['deleted'] else [],'mentions':mention_ids(m['body'],m['channel_id'],m['author_id'])}
subs=[]; subslock=threading.Lock(); IRC_CLIENTS=[]; irclock=threading.Lock()
def broadcast(fr):
 with subslock:
  for s in list(subs):
   if s['cid']==fr.get('channel_id'): ws_send(s['sock'],fr)
def ws_send(sock,o):
 try:
  b=json.dumps(o).encode(); n=len(b)
  if n<126: h=b'\x81'+bytes([n])
  elif n<65536: h=b'\x81\x7e'+struct.pack('!H',n)
  else: h=b'\x81\x7f'+struct.pack('!Q',n)
  sock.sendall(h+b)
 except: pass
def irc_event(fr):
 if fr.get('type')!='message.created': return
 ch=chan(fr['channel_id'])
 if not ch: return
 c0=db(); w=c0.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); c0.close()
 name=f'#{w["slug"]}/{ch["name"]}'
 author=fr.get('message',{}).get('author',{})
 line=f':{author.get("username","user")}!{author.get("username","user")}@localhost PRIVMSG {name} :{fr.get("message",{}).get("body","")}'
 with irclock:
  for z in list(IRC_CLIENTS):
   sock,nick,uid,joined=z
   if ch['id'] in joined:
    try: sock.sendall((line+'\r\n').encode())
    except: pass
def pump():
 seen={}
 while True:
  try:
   c=db(); rows=c.execute('SELECT * FROM events ORDER BY rowid').fetchall();c.close()
   for r in rows:
    key=(r['channel_id'],r['event_id'])
    if key not in seen:
     seen[key]=1; fr={'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id'],**json.loads(r['payload'])}; broadcast(fr); irc_event(fr)
  except: pass
  time.sleep(.1)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,obj):
  b=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def auth(self):
  z=self.headers.get('Authorization','');return user_from_token(z[7:] if z.startswith('Bearer ') else '')
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or '{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/': return self.page()
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/api/ws': return self.websocket(parse_qs(p.query).get('token',[''])[0])
  u=self.auth()
  if path=='/api/auth/me': return self.sendj(200,{'user':ujson(u)} if u else {'error':'unauthorized'}) if u else self.sendj(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   if not u:return self.sendj(401,{})
   c=db()
   r=c.execute('SELECT w.* FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
   c.close()
   return self.sendj(200,{'workspaces':[dict(x) for x in r]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=ws(m.group(1))
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   c=db();cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close();return self.sendj(200,{'workspace':dict(w),'channels':[cjson(x) for x in cs]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   x=chan(int(m.group(1))); return self.sendj(404,{}) if not x else self.sendj(200,{'members':[ujson(z) for z in members(x['id'])]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   x=chan(int(m.group(1))); 
   if not x:return self.sendj(404,{})
   if not canread(x,u['id']):return self.sendj(403,{})
   c=db();r=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(x['id'],)).fetchall();c.close();return self.sendj(200,{'messages':[mjson(z) for z in r],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   c=db();r=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(int(m.group(1)),)).fetchall();c.close();return self.sendj(200,{'replies':[mjson(z) for z in r],'next_cursor':None})
  return self.sendj(404,{})
 def do_POST(self):
  p=urlparse(self.path); path=p.path; u=self.auth()
  if path=='/api/auth/register':
   d=self.body()
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.sendj(400,{'error':'invalid'})
   c=db()
   try:
    c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username']));uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();return self.sendj(201,{'user':ujson(r),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   finally:c.close()
  if path=='/api/auth/login':
   d=self.body();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
   if not r:c.close();return self.sendj(401,{})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.sendj(200,{'user':ujson(r),'token':t})
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   x=chan(int(m.group(1)))
   if not x:return self.sendj(404,{})
   if role(x['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
   val=1 if m.group(2)=='archive' else 0
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,x['id']));c.commit();c.close();return self.sendj(200,{'channel':cjson(chan(x['id']))})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   z=getmsg(int(m.group(1))); d=self.body()
   if not z:return self.sendj(404,{})
   c=db();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(z['id'],u['id'],d.get('emoji','')));c.commit();c.close();return self.sendj(200,{'reactions':reactions(z['id'])})
  m=re.match(r'/api/messages/(\d+)/reactions/([^/]+)$',path)
  if m:
   z=getmsg(int(m.group(1)))
   if not z:return self.sendj(404,{})
   c=db();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(z['id'],u['id'],m.group(2)));c.commit();c.close();return self.sendj(200,{'reactions':reactions(z['id'])})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=ws(m.group(1));d=self.body(); name=d.get('name','')
   if not w:return self.sendj(404,{})
   if role(w['id'],u['id'])!='owner':return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.sendj(400,{})
   c=db()
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));i=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(i,u['id']));c.commit();return self.sendj(201,{'channel':cjson(chan(i))})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   finally:c.close()
  if path=='/api/workspaces':
   d=self.body()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{})
   c=db()
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d['name'],u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute("INSERT INTO channels(workspace_id,name) VALUES(?, 'general')",(wid,));i=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(i,u['id']));c.commit();return self.sendj(201,{'workspace':dict(ws(d['slug'])),'general_channel':cjson(chan(i))})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   finally:c.close()
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   x=chan(int(m.group(1)))
   if not x:return self.sendj(404,{})
   if x['is_private']:return self.sendj(403,{})
   c=db();c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(x['id'],u['id']));c.commit();c.close();return self.sendj(200,{'channel':cjson(x)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   x=chan(int(m.group(1)));d=self.body()
   if not x:return self.sendj(404,{})
   if x['is_archived']:return self.sendj(423,{})
   if not canread(x,u['id']):return self.sendj(403,{})
   if not d.get('body','').strip():return self.sendj(400,{})
   c=db();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(x['id'],u['id'],d['body'],d.get('parent_id'),now()));i=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();c.close();mj=mjson(getmsg(i));event(x['id'],'message.reply' if d.get('parent_id') else 'message.created',{'message':mj});return self.sendj(201,{'message':mj})
  return self.sendj(404,{})
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   z=getmsg(int(m.group(1)))
   if not z:return self.sendj(404,{})
   if z['deleted']:return self.sendj(409,{})
   if z['author_id']!=u['id']:return self.sendj(403,{})
   d=self.body()
   if not d.get('body','').strip():return self.sendj(400,{})
   c=db();c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),z['id']));c.commit();c.close()
   mj=mjson(getmsg(z['id']));event(z['channel_id'],'message.updated',{'message':mj})
   return self.sendj(200,{'message':mj})
  m=re.match(r'/api/channels/(\d+)$',self.path)
  if m:
   x=chan(int(m.group(1)))
   if not x:return self.sendj(404,{})
   if not canread(x,u['id']) and role(x['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
   d=self.body()
   if 'topic' in d and len(d['topic'])>250:return self.sendj(400,{})
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',x['topic']),x['id']));c.commit();c.close();return self.sendj(200,{'channel':cjson(chan(x['id']))})
 def do_DELETE(self):
  u=self.auth()
  if not u:return self.sendj(401,{})
  rm=re.match(r'/api/messages/(\d+)/reactions/([^/]+)$',self.path)
  if rm:
   z=getmsg(int(rm.group(1)))
   if not z:return self.sendj(404,{})
   c=db();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(z['id'],u['id'],rm.group(2)));c.commit();c.close()
   return self.sendj(200,{'reactions':reactions(z['id'])})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if not u:return self.sendj(401,{})
  if not m:return self.sendj(404,{})
  z=getmsg(int(m.group(1)))
  if not z:return self.sendj(404,{})
  x=chan(z['channel_id'])
  if z['author_id']!=u['id'] and role(x['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
  if not z['deleted']:
   c=db();c.execute('UPDATE messages SET deleted=1 WHERE id=?',(z['id'],));c.commit();c.close();event(z['channel_id'],'message.deleted',{'message_id':z['id']})
  return self.sendj(200,{'deleted':True})
 def page(self):
  b=open('/app/static/index.html','rb').read();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def websocket(self,tok):
  u=user_from_token(tok)
  if not u:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key');acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode();self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers()
  while True:
   h=self.rfile.read(2)
   if not h:break
   n=h[1]&127
   if n==126:n=struct.unpack('!H',self.rfile.read(2))[0]
   mask=self.rfile.read(4);data=bytearray(self.rfile.read(n))
   for i in range(n):data[i]^=mask[i%4]
   try:
    d=json.loads(data); 
    if d.get('type')=='subscribe':
     s={'sock':self.connection,'cid':int(d['channel_id'])}
     with subslock:subs.append(s)
     c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(s['cid'],)).fetchone()['n'];c.close();ws_send(self.connection,{'type':'subscribed','channel_id':s['cid'],'head_event_id':head})
   except: pass
  return
def getmsg(i):
 c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(i,)).fetchone();c.close();return r
def irc():
 clients=[];lock=threading.Lock()
 def send(s,line):
  try:s.sendall((line+'\r\n').encode())
  except:pass
 def client(s):
  tok=None;nick=None;u=None;joined=set()
  while True:
   try:
    line=s.recv(4096)
    if not line:break
    for q in line.decode(errors='ignore').splitlines():
     a=q.split();cmd=a[0].upper() if a else ''
     if cmd=='PASS':tok=a[1] if len(a)>1 else '';u=user_from_token(tok);send(s,':huddle 464 * :Password incorrect') if not u else None
     elif cmd=='NICK':
      nn=a[1] if len(a)>1 else ''
      with lock: collision=any(x[1]==nn for x in clients)
      if collision:send(s,f':huddle 433 * {nn} :Nickname in use')
      else:nick=nn
     elif cmd=='USER' and u and nick:
      with lock:clients.append((s,nick,u));
      with irclock: IRC_CLIENTS.append((s,nick,u['id'],joined))
      send(s,f':huddle 001 {nick} :Welcome');send(s,f':huddle 002 {nick} :Your host');send(s,f':huddle 003 {nick} :Created');send(s,f':huddle 004 {nick} :huddle');send(s,f':huddle 005 {nick} :CHANTYPES=#');send(s,f':huddle 422 {nick} :MOTD File is missing')
     elif cmd=='PING':send(s,':huddle PONG '+(' '.join(a[1:])))
     elif cmd=='QUIT':
      try:s.shutdown(socket.SHUT_RDWR)
      except:pass
      try:s.close()
      except:pass
      return
     elif cmd=='JOIN' and len(a)>1:
      name=a[1]; mm=re.match(r'#([^/]+)/(.+)',name); x=ws(mm.group(1)) if mm else None
      if mm and x:
       c=db();ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(x['id'],mm.group(2))).fetchone();c.close()
      else:ch=None
      if not ch:send(s,f':huddle 403 {nick} {name} :No such channel')
      else:
       joined.add(ch['id']);addmem(ch['id'],u['id']);
       with irclock:
        for z in IRC_CLIENTS:
         if z[0] is s: z[3].add(ch['id'])
       send(s,f':{nick}!{u["username"]}@localhost JOIN {name}');send(s,f':huddle 353 {nick} = {name} :{nick}');send(s,f':huddle 366 {nick} {name} :End of NAMES list')
     elif cmd=='PRIVMSG' and len(a)>2 and a[1].startswith('#'):
      mm=re.match(r'#([^/]+)/(.+)',a[1]); x=ws(mm.group(1)) if mm else None
      if x:
       c=db();ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(x['id'],mm.group(2))).fetchone();c.close()
       if ch and ch['id'] in joined:
        body=q.split(' :',1)[1] if ' :' in q else ' '.join(a[2:]);c=db();c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],u['id'],body,now()));i=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();c.close();event(ch['id'],'message.created',{'message':mjson(getmsg(i))})
     elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): pass
     elif cmd and cmd not in ('NICK','USER','PASS'):send(s,f':huddle 421 {nick or "*"} {cmd} :Unknown command')
   except:break
  with lock:
   for z in clients[:]:
    if z[0] is s:clients.remove(z)
  with irclock:
   IRC_CLIENTS[:]=[z for z in IRC_CLIENTS if z[0] is not s]
  try:s.shutdown(socket.SHUT_RDWR)
  except:pass
  try:s.close()
  except:pass
 srv=ThreadingTCPServer(('0.0.0.0',6667),client);srv.serve_forever()
class ThreadingTCPServer:
 def __init__(self,a,f):self.s=socket.socket();self.s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);self.s.bind(a);self.s.listen();self.f=f
 def serve_forever(self):
  while 1:
   c,_=self.s.accept();threading.Thread(target=self.f,args=(c,),daemon=True).start()
if __name__=='__main__':
 init();threading.Thread(target=pump,daemon=True).start()
 if NODE==0: threading.Thread(target=irc,daemon=True).start()
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
