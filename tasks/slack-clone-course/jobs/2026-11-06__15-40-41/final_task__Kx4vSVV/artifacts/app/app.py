import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,socket,struct,datetime,urllib.parse
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
def conn():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def now():
 return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 c=conn(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,seq INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
'''); c.close()
init()
def user_json(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def token_user(tok):
 if not tok:return None
 c=conn(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close(); return r
def auth(h):
 return token_user((h or '').replace('Bearer ','',1)) if h and h.startswith('Bearer ') else None
def channel(cid):
 c=conn(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return r
def ws(slug):
 c=conn(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); c.close(); return r
def role(w,u):
 c=conn(); r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); c.close(); return r['role'] if r else None
def member(cid,u):
 c=conn(); x=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,u)).fetchone(); c.close(); return bool(x)
def cjson(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def mentions_for(body,cid,author):
 c=conn(); ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch: c.close(); return []
 users=c.execute('SELECT u.id,u.username FROM users u JOIN wm m ON u.id=m.user_id WHERE m.workspace_id=?',(ch['workspace_id'],)).fetchall()
 mem={x['username']:x['id'] for x in users}; out=[]
 for x in re.findall(r'(?<!\\w)@([A-Za-z0-9_]+)',body):
  if x=='channel':
   out += [int(z['id']) for z in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(cid,author)).fetchall()]
  elif x in mem and mem[x]!=author and mem[x] not in out: out.append(mem[x])
 c.close(); return list(dict.fromkeys(out))
def msgjson(r):
 c=conn(); au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)).fetchall()
 rep=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_json(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':rep,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs],'mentions':mentions_for(r['body'],r['channel_id'],r['author_id'])}
def emit(cid,kind,payload):
 c=conn(); c.execute('BEGIN IMMEDIATE'); q=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,q,kind,json.dumps(payload))); c.commit(); c.close(); return {'type':kind,'event_id':q,'channel_id':cid,**payload}
def create_msg(cid,uid,body,parent=None):
 c=conn(); c.execute('BEGIN IMMEDIATE'); ts=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,uid,body,parent,ts)); i=c.execute('SELECT last_insert_rowid()').fetchone()[0]; r=c.execute('SELECT * FROM messages WHERE id=?',(i,)).fetchone(); c.commit(); c.close(); j=msgjson(r); emit(cid,'message.reply' if parent else 'message.created',{'message':j}); return j
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def send(self,status,obj=None,ctype='application/json'):
  b=(json.dumps(obj).encode() if obj is not None and ctype=='application/json' else (obj or b'')); self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.send_header('Connection','close'); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or '{}')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/': return self.send(200,open('/app/static/index.html','rb').read(), 'text/html')
  if path=='/api/health': return self.send(200,{'status':'ok','node_id':NODE})
  if path=='/api/ws': return self.websocket()
  u=auth(self.headers.get('Authorization'))
  if path=='/api/auth/me': return self.send(200,{'user':user_json(u)} if u else {'error':'unauthorized'}) if u else self.send(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   if not u:return self.send(401,{})
   c=conn(); a=c.execute('SELECT w.* FROM workspaces w JOIN wm m ON w.id=m.workspace_id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close(); return self.send(200,{'workspaces':[dict(x) for x in a]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   if not u:return self.send(401,{})
   w=ws(m.group(1))
   if not w:return self.send(404,{})
   if not role(w['id'],u['id']):return self.send(403,{})
   c=conn(); a=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))',(w['id'],u['id'])).fetchall(); c.close(); return self.send(200,{'workspace':dict(w),'channels':[cjson(x) for x in a]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   ch=channel(int(m.group(1)))
   if not ch:return self.send(404,{})
   c=conn(); a=c.execute('SELECT u.* FROM users u JOIN cm m ON u.id=m.user_id WHERE m.channel_id=?',(ch['id'],)).fetchall(); c.close(); return self.send(200,{'members':[user_json(x) for x in a]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   if not u:return self.send(401,{})
   ch=channel(int(m.group(1)))
   if not ch:return self.send(404,{})
   if ch['is_private'] and not member(ch['id'],u['id']):return self.send(403,{})
   c=conn(); a=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(ch['id'],)).fetchall(); c.close(); return self.send(200,{'messages':[msgjson(x) for x in a],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   if not u:return self.send(401,{})
   c=conn(); a=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(int(m.group(1)),)).fetchall(); c.close(); return self.send(200,{'replies':[msgjson(x) for x in a],'next_cursor':None})
  return self.send(404,{})
 def do_POST(self):
  p=urllib.parse.urlparse(self.path); path=p.path; u=auth(self.headers.get('Authorization'))
  if path=='/api/auth/register':
   d=self.body(); un=d.get('username',''); pw=d.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.send(400,{'error':'invalid username or password'})
   c=conn()
   try:
    c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,d.get('display_name') or un)); i=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,i)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(i,)).fetchone(); return self.send(201,{'user':user_json(r),'token':t})
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   finally:c.close()
  if path=='/api/auth/login':
   d=self.body(); c=conn(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
   if not r:c.close();return self.send(401,{})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return self.send(200,{'user':user_json(r),'token':t})
  if not u:return self.send(401,{})
  if path=='/api/workspaces':
   d=self.body(); 
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.send(400,{})
   c=conn()
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d['name'],u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.send(201,{'workspace':dict(w),'general_channel':cjson(ch)})
   except sqlite3.IntegrityError:c.close();return self.send(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=ws(m.group(1)); d=self.body()
   if not w:return self.send(404,{})
   if role(w['id'],u['id'])!='owner':return self.send(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.send(400,{})
   c=conn()
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));i=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(i,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone();c.close();return self.send(201,{'channel':cjson(r)})
   except sqlite3.IntegrityError:c.close();return self.send(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=channel(int(m.group(1)))
   if not ch:return self.send(404,{})
   if ch['is_private']:return self.send(403,{})
   c=conn();c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));c.commit();c.close();return self.send(200,{'channel':cjson(channel(ch['id']))})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   d=self.body();ch=channel(int(m.group(1)))
   if not ch:return self.send(404,{})
   if ch['is_archived']:return self.send(423,{})
   if not member(ch['id'],u['id']):return self.send(403,{})
   b=d.get('body','')
   if not b.strip():return self.send(400,{})
   return self.send(201,{'message':create_msg(ch['id'],u['id'],b,d.get('parent_id'))})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   d=self.body();c=conn();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(int(m.group(1)),u['id'],d.get('emoji','')));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone();c.close();return self.send(200,{'reactions':msgjson(r)['reactions']})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   ch=channel(int(m.group(1)))
   if role(ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
   c=conn();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(m.group(2)=='archive',ch['id']));c.commit();c.close();return self.send(200,{'channel':cjson(channel(ch['id']))})
  return self.send(404,{})
 def do_PATCH(self):
  u=auth(self.headers.get('Authorization'));p=self.path
  if not u:return self.send(401,{})
  d=self.body();m=re.match(r'/api/channels/(\d+)$',p)
  if m:
   ch=channel(int(m.group(1)))
   if not ch:return self.send(404,{})
   if not member(ch['id'],u['id']) and role(ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
   if len(d.get('topic',''))>250:return self.send(400,{})
   c=conn();c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),ch['id']));c.commit();c.close();return self.send(200,{'channel':cjson(channel(ch['id']))})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   c=conn();r=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone();c.close()
   if not r:return self.send(404,{})
   if r['deleted']:return self.send(409,{})
   if r['author_id']!=u['id']:return self.send(403,{})
   if not d.get('body','').strip():return self.send(400,{})
   c=conn();c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),r['id']));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();c.close();j=msgjson(r);emit(r['channel_id'],'message.updated',{'message':j});return self.send(200,{'message':j})
  return self.send(404,{})
 def do_DELETE(self):
  u=auth(self.headers.get('Authorization'))
  if not u:return self.send(401,{})
  m=re.match(r'/api/messages/(\d+)(?:/reactions/(.*))?$',self.path)
  if not m:return self.send(404,{})
  c=conn();r=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
  if not r:c.close();return self.send(404,{})
  if m.group(2) is not None:
   c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],urllib.parse.unquote(m.group(2))));c.commit();c.close();return self.send(200,{'reactions':msgjson(r)['reactions']})
  if r['author_id']!=u['id'] and role(channel(r['channel_id'])['workspace_id'],u['id'])!='owner':c.close();return self.send(403,{})
  if not r['deleted']:
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));c.commit();emit(r['channel_id'],'message.deleted',{'message_id':r['id']})
  c.close();return self.send(200,{'deleted':True})
 def websocket(self):
  key=self.headers.get('Sec-WebSocket-Key');u=token_user(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0])
  if not key or not u:return self.send(401,{})
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  s=self.connection;s.settimeout(0.1); chans=set(); last={}; 
  def frame(x):
   b=json.dumps(x).encode(); n=len(b); h=bytes([129])+ (bytes([n]) if n<126 else bytes([126])+struct.pack('!H',n));return h+b
  try:
   while True:
    try:
     h=s.recv(2)
     if not h:break
     n=h[1]&127
     if n==126:n=struct.unpack('!H',s.recv(2))[0]
     mask=s.recv(4); data=s.recv(n); data=bytes(data[i]^mask[i%4] for i in range(n)); d=json.loads(data)
     if d.get('type')=='subscribe':
      cid=int(d['channel_id']);chans.add(cid);c=conn();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];c.close();s.sendall(frame({'type':'subscribed','channel_id':cid,'head_event_id':head}));last[cid]=head
    except socket.timeout: pass
    c=conn()
    for cid in list(chans):
     a=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last.get(cid,0))).fetchall()
     for e in a:s.sendall(frame({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])}));last[cid]=e['event_id']
    c.close()
  except Exception:pass
 def log_message(self,*a):pass
if __name__=='__main__':
 init();ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
