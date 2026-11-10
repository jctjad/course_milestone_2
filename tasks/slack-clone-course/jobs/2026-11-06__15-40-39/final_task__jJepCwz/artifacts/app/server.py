import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,threading,time,socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from datetime import datetime,timezone
DB='/var/lib/huddle/huddle.db'; DATA='/var/lib/huddle/files'
os.makedirs(DATA,exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,created TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
'''); c.commit(); c.close()
def ujson(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def user(tok):
 if not tok:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(tok,)).fetchone();c.close();return r
def tok(h): return user((h or '').replace('Bearer ','',1))
def ws(slug):
 c=db();r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();c.close();return r
def chan(cid):
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return r
def cjson(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def role(wid,uid):
 c=db();r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone();c.close();return r['role'] if r else None
def member(cid,uid):
 c=db();x=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone();c.close();return bool(x)
def msgjson(r):
 c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)).fetchall()
 n=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0];c.close()
 mentions=[]
 ch=chan(r['channel_id']); c2=db(); members=c2.execute('SELECT u.id,u.username FROM users u JOIN wm ON wm.user_id=u.id WHERE wm.workspace_id=?',(ch['workspace_id'],)).fetchall(); c2.close()
 for z in re.findall(r'(?<!\\w)@([A-Za-z0-9_]+)',r['body']):
  if z=='channel': mentions += [int(x['id']) for x in members if int(x['id'])!=r['author_id']]
  else:
   mentions += [int(x['id']) for x in members if x['username']==z and int(x['id'])!=r['author_id']]
 mentions=list(dict.fromkeys(mentions))
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':ujson(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':n,'created_at':r['created'],'edited_at':r['edited'],'files':[],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rs],'mentions':mentions}
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); q=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,q,kind,json.dumps(payload)));c.commit();c.close();return q
def addm(cid,uid):
 c=db();c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,uid));c.commit();c.close()
def create_msg(cid,uid,body,parent=None):
 c=db(); t=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created) VALUES(?,?,?,?,?)',(cid,uid,body,parent,t)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close(); j=msgjson(r);emit(cid,'message.reply' if parent else 'message.created',{'message':j});return j
subs=[]; sublock=threading.Lock(); irc_seen={}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,o,status=200):
  b=json.dumps(o).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def read(self):
  n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  x=tok(self.headers.get('Authorization')); 
  if not x:self.sendj({'error':'unauthorized'},401)
  return x
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/': 
   b=open('/app/static/index.html','rb').read();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  if path=='/api/health': return self.sendj({'status':'ok','node_id':int(self.server.server_port)-8000})
  if path=='/api/ws': return self.websocket()
  u=self.auth()
  if not u:return
  if path=='/api/auth/me':return self.sendj({'user':ujson(u)})
  if path=='/api/workspaces':
   c=db(); rs=c.execute('SELECT w.* FROM workspaces w JOIN wm ON wm.workspace_id=w.id WHERE wm.user_id=? ORDER BY w.id',(u['id'],)).fetchall(); c.close()
   return self.sendj({'workspaces':[dict(x) for x in rs]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=ws(m.group(1))
   if not w:return self.sendj({'error':'not found'},404)
   if not role(w['id'],u['id']):return self.sendj({'error':'forbidden'},403)
   c=db(); cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close()
   return self.sendj({'workspace':dict(w),'channels':[cjson(x) for x in cs]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   ch=chan(int(m.group(1))); 
   if not ch:return self.sendj({},404)
   c=db(); rs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall();c.close();return self.sendj({'members':[ujson(x) for x in rs]})
  m=re.match(r'/api/channels/(\\d+)$',path)
  if m:
   ch=chan(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if role(ch['workspace_id'],u['id']) not in ('owner','member') and not member(ch['id'],u['id']):return self.sendj({},403)
   d=self.read()
   if 'topic' in d and len(d['topic'])>250:return self.sendj({},400)
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',ch['topic']),ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close();return self.sendj({'channel':cjson(r)})
  m=re.match(r'/api/channels/(\\d+)/(archive|unarchive)$',path)
  if m:
   ch=chan(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if role(ch['workspace_id'],u['id'])!='owner':return self.sendj({},403)
   val=1 if m.group(2)=='archive' else 0
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close();return self.sendj({'channel':cjson(r)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=chan(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if ch['is_private'] and not member(ch['id'],u['id']):return self.sendj({},403)
   c=db();rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(ch['id'],)).fetchall();c.close();return self.sendj({'messages':[msgjson(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   c=db();rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(int(m.group(1)),)).fetchall();c.close();return self.sendj({'replies':[msgjson(x) for x in rs],'next_cursor':None})
  return self.sendj({'error':'not found'},404)
 def do_POST(self):
  p=urlparse(self.path);path=p.path
  if path=='/api/auth/register':
   d=self.read(); un=d.get('username','');pw=d.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.sendj({'error':'invalid username or password'},400)
   c=db()
   try:c.execute('INSERT INTO users(username,password,display_name,created) VALUES(?,?,?,?)',(un,pw,d.get('display_name') or un,now()));uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit()
   except sqlite3.IntegrityError:c.close();return self.sendj({'error':'duplicate'},409)
   r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.sendj({'user':ujson(r),'token':t},201)
  if path=='/api/auth/login':
   d=self.read();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
   if not r:c.close();return self.sendj({'error':'invalid credentials'},401)
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.sendj({'user':ujson(r),'token':t})
  u=self.auth()
  if not u:return
  d=self.read()
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj({},400)
   c=db()
   try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d['name'],u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute("INSERT INTO channels(workspace_id,name) VALUES(?, 'general')",(wid,));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit()
   except sqlite3.IntegrityError:c.close();return self.sendj({},409)
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj({'workspace':dict(w),'general_channel':cjson(ch)},201)
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=ws(m.group(1))
   if not w:return self.sendj({},404)
   if role(w['id'],u['id'])!='owner' and d.get('is_private'):return self.sendj({},403)
   name=re.sub(r'[^a-z0-9-]','-',d.get('name','').lower()).strip('-')
   if not name or len(name)>32:return self.sendj({},400)
   c=db()
   if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():c.close();return self.sendj({},409)
   c.execute('INSERT INTO channels(workspace_id,name,topic,is_private) VALUES(?,?,?,?)',(w['id'],name,d.get('topic',''),int(bool(d.get('is_private')))));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj({'channel':cjson(r)},201)
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=chan(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if ch['is_private'] and role(ch['workspace_id'],u['id']) is None:return self.sendj({},403)
   c=db();c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.commit();c.close();addm(ch['id'],u['id']);return self.sendj({'channel':cjson(ch)})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   ch=chan(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if role(ch['workspace_id'],u['id'])!='owner':return self.sendj({},403)
   val=1 if m.group(2)=='archive' else 0
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close()
   return self.sendj({'channel':cjson(r)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=chan(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if ch['is_archived']:return self.sendj({},423)
   if not member(ch['id'],u['id']):addm(ch['id'],u['id'])
   body=d.get('body','')
   if not body.strip():return self.sendj({},400)
   return self.sendj({'message':create_msg(ch['id'],u['id'],body,d.get('parent_id'))},201)
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   mid=int(m.group(1));c=db();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],d.get('emoji','👍')));c.commit();rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall();c.close();return self.sendj({'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rs]})
  return self.sendj({},404)
 def do_PATCH(self):
  u=self.auth()
  if not u:return
  m=re.match(r'/api/channels/(\d+)$',self.path)
  if m:
   ch=chan(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if not member(ch['id'],u['id']) and role(ch['workspace_id'],u['id'])!='owner':return self.sendj({},403)
   d=self.read()
   if 'topic' not in d or len(d.get('topic',''))>250:return self.sendj({},400)
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close()
   return self.sendj({'channel':cjson(r)})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
   if not r:return self.sendj({},404)
   if r['deleted']:return self.sendj({},409)
   if r['author_id']!=u['id']:return self.sendj({},403)
   d=self.read();body=d.get('body','')
   if not body.strip():return self.sendj({},400)
   c.execute('UPDATE messages SET body=?,edited=? WHERE id=?',(body,now(),r['id']));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();c.close();j=msgjson(r);emit(r['channel_id'],'message.updated',{'message':j});return self.sendj({'message':j})
  return self.sendj({},404)
 def do_DELETE(self):
  u=self.auth()
  if not u:return
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if not m:return self.sendj({},404)
  c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
  if not r:return self.sendj({},404)
  ch=chan(r['channel_id'])
  if r['author_id']!=u['id'] and role(ch['workspace_id'],u['id'])!='owner':c.close();return self.sendj({},403)
  if not r['deleted']:
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));c.commit();emit(r['channel_id'],'message.deleted',{'message_id':r['id']})
  c.close();return self.sendj({'deleted':True})
 def do_OPTIONS(self): self.send_response(204);self.send_header('Content-Length','0');self.end_headers()
 def websocket(self):
  t=parse_qs(urlparse(self.path).query).get('token',[''])[0]
  if not user(t):self.send_response(401);self.end_headers();return
  key=self.headers.get('Sec-WebSocket-Key');acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers()
  self.connection.settimeout(20); sub={'h':self,'channels':set(),'last':{}}
  with sublock:subs.append(sub)
  try:
   while True:
    b=self.connection.recv(2)
    if not b:break
    ln=b[1]&127
    if ln==126:ln=struct.unpack('>H',self.connection.recv(2))[0]
    elif ln==127:ln=struct.unpack('>Q',self.connection.recv(8))[0]
    mask=self.connection.recv(4);data=bytes(x^mask[i%4] for i,x in enumerate(self.connection.recv(ln)));d=json.loads(data)
    if d.get('type')=='subscribe':
     cid=int(d['channel_id']);sub['channels'].add(cid);c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];c.close();self.wsout({'type':'subscribed','channel_id':cid,'head_event_id':head})
  except:pass
  finally:
   with sublock:
    if sub in subs:subs.remove(sub)
 def wsout(self,o):
  b=json.dumps(o).encode();n=len(b);hdr=bytes([129,n]) if n<126 else bytes([129,126])+struct.pack('>H',n);self.connection.sendall(hdr+b)
def pump():
 while True:
  try:
   with sublock:
    ss=list(subs)
   for s in ss:
    for cid in list(s['channels']):
     last=s['last'].get(cid,0);c=db();rs=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall();c.close()
     for r in rs:
      x={'type':r['kind'],'event_id':r['event_id'],'channel_id':cid};x.update(json.loads(r['payload']))
      try:s['h'].wsout(x)
      except:pass
      s['last'][cid]=r['event_id']
  except:pass
  try:
   c=db(); channels=c.execute("SELECT DISTINCT channel_id FROM events WHERE kind IN ('message.created','message.reply')").fetchall(); c.close()
   for rr in channels:
    cid=rr['channel_id']; last=irc_seen.get(cid,0); c=db(); evs=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? AND kind IN ('message.created','message.reply') ORDER BY event_id",(cid,last)).fetchall(); ch=c.execute("SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?",(cid,)).fetchone(); c.close()
    for ev in evs:
     payload=json.loads(ev['payload']); mm=payload.get('message',{}); au=mm.get('author',{}); name='#'+ch['slug']+'/'+ch['name'] if ch else ''
     line=':'+au.get('username','user')+'!'+au.get('username','user')+'@localhost PRIVMSG '+name+' :'+mm.get('body','')
     with IRC.lock:
      clients=list(IRC.clients)
     for cl in clients:
      if cid in cl.joined:
       try: cl.out(line)
       except: pass
     irc_seen[cid]=ev['event_id']
  except: pass
  time.sleep(.1)
class IRC:
 clients=[];lock=threading.Lock()
 def __init__(self,s):self.s=s;self.user=None;self.nick='*';self.joined=set()
 def out(self,x):self.s.sendall((x+'\r\n').encode())
 def run(self):
  buf=b''
  try:
   while True:
    z=self.s.recv(4096)
    if not z:break
    buf+=z
    while b'\n' in buf:
     line,buf=buf.split(b'\n',1);self.cmd(line.decode(errors='ignore').strip('\r'))
  except:pass
  finally:
   with IRC.lock:
    if self in IRC.clients:IRC.clients.remove(self)
   try:self.s.close()
   except:pass
 def cmd(self,l):
  a=l.split();cmd=a[0].upper() if a else ''
  if cmd=='PASS':
   self.user=user(a[1] if len(a)>1 else ''); 
   if not self.user:self.out(':huddle 464 * Password incorrect')
  elif cmd=='NICK' and len(a)>1:
   with IRC.lock:
    if any(x.nick==a[1] for x in IRC.clients):self.out(':huddle 433 * Nickname in use');return
   self.nick=a[1]
  elif cmd=='USER' and self.user:
   with IRC.lock:IRC.clients.append(self)
   for n in ['001','002','003','004','005']:self.out(':huddle '+n+' '+self.nick+' huddle')
   self.out(':huddle 422 '+self.nick+' MOTD File is missing')
  elif cmd=='PING':self.out('PONG '+(a[-1] if len(a)>1 else ''))
  elif cmd=='QUIT':raise Exception()
  elif cmd=='JOIN' and len(a)>1:
   name=a[1];m=re.match(r'#([^/]+)/(.+)',name);ch=None
   if m:
    w=ws(m.group(1)); 
    if w:
     c=db();ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],m.group(2))).fetchone();c.close()
   if not ch:self.out(':huddle 403 '+self.nick+' '+name+' No such channel');return
   addm(ch['id'],self.user['id']);self.joined.add(ch['id']);self.out(':'+self.nick+'!'+self.user['username']+'@localhost JOIN '+name);self.out(':huddle 353 '+self.nick+' = '+name+' :'+self.nick);self.out(':huddle 366 '+self.nick+' '+name+' End of NAMES list')
  elif cmd=='PRIVMSG' and len(a)>2:
   name=a[1];body=l.split(' :',1)[1] if ' :' in l else ' '.join(a[2:]);m=re.match(r'#([^/]+)/(.+)',name)
   if m:
    w=ws(m.group(1));c=db() if w else None;ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],m.group(2))).fetchone() if c else None
    if c:c.close()
    if ch:create_msg(ch['id'],self.user['id'],body)
  elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):pass
  else:self.out(':huddle 421 '+self.nick+' '+cmd+' Unknown command')
def ircserver():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
 while True:
  x,_=s.accept();threading.Thread(target=IRC(x).run,daemon=True).start()
if __name__=='__main__':
 init(); port=int(sys.argv[1]); threading.Thread(target=pump,daemon=True).start();
 if port==8003: threading.Thread(target=ircserver,daemon=True).start()
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
