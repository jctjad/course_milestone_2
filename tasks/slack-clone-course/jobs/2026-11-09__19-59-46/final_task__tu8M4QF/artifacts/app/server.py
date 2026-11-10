import sys,os,re,json,sqlite3,secrets,hashlib,hmac,base64,datetime,urllib.parse,socket,threading,time
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from zoneinfo import ZoneInfo
DB='/var/lib/huddle/huddle.sqlite3'
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=2000'); return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True); c=db()
 c.executescript('''PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,payload TEXT,created_at TEXT);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,payload TEXT,created_at TEXT);
 CREATE TABLE IF NOT EXISTS channel_events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER PRIMARY KEY);
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,expires_at TEXT,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);'''); c.close()
def q(sql,args=(),one=False):
 c=db()
 try:
  r=c.execute(sql,args); return (r.fetchone() if one else r.fetchall())
 finally:c.close()
def groupj(g):
 return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[r['user_id'] for r in q('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))]}
def validate_timezone(tz):
 try: ZoneInfo(tz); return True
 except: return False
def validate_profile(fields):
 limits={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048)}
 for k,v in fields.items():
  if k not in limits: continue
  if not isinstance(v,str) or not (limits[k][0]<=len(v)<=limits[k][1]): raise ValueError()
  if k=='timezone' and not validate_timezone(v): raise ValueError()
 if 'timezone' in fields and (not isinstance(fields['timezone'],str) or not validate_timezone(fields['timezone'])): raise ValueError()
def broadcast_global(frame):
 c=db(); c.execute('INSERT INTO global_events(payload,created_at) VALUES(?,?)',(json.dumps(frame),now())); c.close()
def userj(x):
 return {'id':x['id'],'username':x['username'],'display_name':x['display_name'] or x['username'],'timezone':x['timezone'] or 'UTC','avatar_url':x['avatar_url'] or '','status_text':x['status_text'] or '','status_emoji':x['status_emoji'] or ''}
def chan(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'] or '','is_archived':bool(x['is_archived'])}
def ws(x): return {'id':x['id'],'slug':x['slug'],'name':x['name'],'owner_id':x['owner_id'],'join_mode':x['join_mode']}
def token_user(h):
 a=h.headers.get('Authorization','')
 if not a.startswith('Bearer '): return None
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(a[7:],),True); return r
def read_state_json(user_id,channel_id):
 r=q('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(user_id,channel_id),True)
 last=int(r['last_read_event_id']) if r else 0
 unread=mentions=0
 for ev in events_since(channel_id,last):
  if ev.get('type') not in ('message.created','message.reply'): continue
  m=ev.get('message',{})
  if m.get('author_id')==user_id: continue
  unread+=1
  if user_id in m.get('mentions',[]): mentions+=1
 return {'channel_id':channel_id,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def set_last_read(user_id,channel_id,event_id):
 c=db(); c.execute('INSERT INTO read_states(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=MAX(last_read_event_id,excluded.last_read_event_id)',(user_id,channel_id,int(event_id))); c.close()
 return read_state_json(user_id,channel_id)
def emit_event(channel_id,kind,payload):
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE')
  r=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(channel_id,)).fetchone()
  eid=r['n']; frame={'type':kind,'event_id':eid,'channel_id':channel_id,**payload}
  c.execute('INSERT INTO channel_events VALUES(?,?,?,?,?)',(channel_id,eid,kind,json.dumps(frame),now()))
  c.execute('DELETE FROM channel_events WHERE channel_id=? AND event_id < ?',(channel_id,eid-1000))
  c.commit(); return frame
 finally:c.close()
def head_event_id(channel_id):
 r=q('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(channel_id,),True); return r['n']
def events_since(channel_id,since):
 return [json.loads(x['payload']) for x in q('SELECT payload FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel_id,since))]
def redis_listener():
 while True:
  try:
   z=socket.create_connection(('127.0.0.1',6379),timeout=2)
   z.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n'); z.settimeout(None)
   while True:
    if not z.recv(4096): raise OSError()
  except Exception:
   try:z.close()
   except Exception:pass
   time.sleep(.5)
def ws_frame(obj):
 b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
 if n<126:return bytes([129,n])+b
 if n<65536:return bytes([129,126])+n.to_bytes(2,'big')+b
 return bytes([129,127])+n.to_bytes(8,'big')+b
def ws_read(sock):
 h=sock.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=int.from_bytes(sock.recv(2),'big')
 elif n==127:n=int.from_bytes(sock.recv(8),'big')
 mask=sock.recv(4); data=b''
 while len(data)<n:
  z=sock.recv(n-len(data))
  if not z:return None
  data+=z
 if h[1]&128:data=bytes(data[i]^mask[i%4] for i in range(n))
 return json.loads(data.decode()) if h[0]&15==1 else None
def ws_loop(handler,user):
 sock=handler.request; subs={}; global_after=(q('SELECT COALESCE(MAX(id),0) n FROM global_events',(),True)['n']); conn_id=secrets.token_urlsafe(12); c=db(); c.execute('INSERT INTO presence VALUES(?,?,?)',(conn_id,user['id'],time.time())); c.close()
 sock.settimeout(.25)
 while True:
  c=db(); c.execute('UPDATE presence SET last_seen=? WHERE conn_id=?',(time.time(),conn_id)); c.close()
  for ge in q('SELECT id,payload FROM global_events WHERE id>? ORDER BY id',(global_after,)):
   sock.sendall(ws_frame(json.loads(ge['payload']))); global_after=ge['id']
  for cid,since in list(subs.items()):
   for ev in events_since(cid,since):
    sock.sendall(ws_frame(ev)); subs[cid]=ev['event_id']
  try: m=ws_read(sock)
  except TimeoutError: continue
  except:
   c=db(); c.execute('DELETE FROM presence WHERE conn_id=?',(conn_id,)); c.close(); return
  if m is None:
   c=db(); c.execute('DELETE FROM presence WHERE conn_id=?',(conn_id,)); c.close(); return
  typ=m.get('type'); cid=int(m.get('channel_id',0))
  if typ=='subscribe':
   subs[cid]=head_event_id(cid); sock.sendall(ws_frame({'type':'subscribed','channel_id':cid,'head_event_id':subs[cid]}))
  elif typ=='resume':
   head=head_event_id(cid); earliest=q('SELECT MIN(event_id) n FROM channel_events WHERE channel_id=?',(cid,),True)['n']
   if earliest and m.get('since_event_id',0)<earliest-1:
    sock.sendall(ws_frame({'type':'resume.gap','channel_id':cid,'earliest_event_id':earliest})); continue
   for ev in events_since(cid,int(m.get('since_event_id',0))): sock.sendall(ws_frame(ev))
   subs[cid]=head; sock.sendall(ws_frame({'type':'resumed','channel_id':cid,'head_event_id':head}))
def cursor_make(kind,value):
 return base64.urlsafe_b64encode((kind+':'+str(value)).encode()).decode().rstrip('=')
def cursor_read(raw,kind):
 if not raw:return None
 try:
  x=base64.urlsafe_b64decode(raw+'='*((4-len(raw)%4)%4)).decode()
  k,v=x.split(':',1)
  if k!=kind or not v.isdigit(): raise ValueError()
  return int(v)
 except: raise ValueError()
def msgj(x):
 u=q('SELECT * FROM users WHERE id=?',(x['author_id'],),True)
 rs=q('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(x['id'],))
 return {'id':x['id'],'channel_id':x['channel_id'],'author_id':x['author_id'],'author':userj(u),'body':x['body'],'parent_id':x['parent_id'],'reply_count':q('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(x['id'],),True)['n'],'created_at':x['created_at'],'edited_at':x['edited_at'],'files':[{'id':f['id'],'uploader_id':f['uploader_id'],'filename':f['filename'],'content_type':f['content_type'],'size':f['size'],'created_at':f['created_at']} for f in q('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(x['id'],))],'reactions':[{'emoji':r['emoji'],'count':r['n'],'user_ids':[int(z) for z in r['ids'].split(',')]} for r in rs],'mentions':[r['user_id'] for r in q('SELECT user_id FROM message_mentions WHERE message_id=?',(x['id'],))]}
def role(w,u):
 r=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u),True); return r['role'] if r else None
def addm(w,u,r):
 c=db(); c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w,u,r)); c.close()
def cm(c,u):
 d=db(); d.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(c,u)); d.close()
class H(BaseHTTPRequestHandler):
 server_version='Huddle/1'
 def log_message(self,*a): pass
 def send(self,code,obj=None,typ='application/json'):
  b=((obj.encode() if isinstance(obj,str) else json.dumps(obj,separators=(',',':')).encode()) if obj is not None else b'')
  self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  u=token_user(self); 
  if not u:self.send(401,{'error':'unauthorized'})
  return u
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path; qs=urllib.parse.parse_qs(p.query)
  if path=='/api/health': return self.send(200,{'status':'ok','node_id':self.server.node})
  if path=='/':
   html=open('/app/static/index.html',encoding='utf-8').read()
   return self.send(200,html,'text/html; charset=utf-8')
  if path=='/api/ws':
   tok=qs.get('token',[''])[0]; u=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,),True)
   if not u:return self.send(401,{'error':'websocket authentication required'})
   key=self.headers.get('Sec-WebSocket-Key')
   if not key:return self.send(400,{'error':'websocket key required'})
   accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
   ws_loop(self,u); return
  u=self.auth()
  if not u:return
  if path=='/api/search':
   term=qs.get('q',[''])[0].lower()
   try: lim=int(qs.get('limit',['50'])[0]); assert 1<=lim<=200
   except:return self.send(400,{'error':'invalid limit'})
   try: before=cursor_read(qs.get('cursor',[None])[0],'s')
   except:return self.send(400,{'error':'invalid cursor'})
   sql='SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id JOIN members wm ON wm.workspace_id=c.workspace_id AND wm.user_id=? WHERE m.deleted=0 AND lower(m.body) LIKE ? AND (c.is_private=0 OR EXISTS (SELECT 1 FROM cmembers px WHERE px.channel_id=c.id AND px.user_id=?))'
   args=[u['id'],'%'+term+'%',u['id']]; wid=qs.get('workspace_id',[None])[0]
   if wid is not None:sql+=' AND c.workspace_id=?';args.append(wid)
   if before is not None:sql+=' AND m.id<?';args.append(before)
   sql+=' ORDER BY m.id DESC LIMIT ?';args.append(lim+1);rows=q(sql,tuple(args));nxt=cursor_make('s',rows[lim-1]['id']) if len(rows)>lim else None
   return self.send(200,{'results':[msgj(x) for x in rows[:lim]],'next_cursor':nxt})
  m=re.match(r'^/api/channels/(\d+)/pins$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.send(404,{'error':'not found'})
   if not role(c['workspace_id'],u['id']) or (c['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True)):return self.send(403,{'error':'forbidden'})
   try:lim=int(qs.get('limit',['50'])[0]);assert 1<=lim<=200
   except:return self.send(400,{'error':'invalid limit'})
   try:before=cursor_read(qs.get('cursor',[None])[0],'p')
   except:return self.send(400,{'error':'invalid cursor'})
   sql='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?';args=[c['id']]
   if before is not None:sql+=' AND p.message_id<?';args.append(before)
   sql+=' ORDER BY p.message_id DESC LIMIT ?';args.append(lim+1);rows=q(sql,tuple(args));nxt=cursor_make('p',rows[lim-1]['message_id']) if len(rows)>lim else None
   return self.send(200,{'pins':[{'message':msgj(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows[:lim]],'next_cursor':nxt})
  if path=='/api/auth/me':return self.send(200,{'user':userj(u)})
  m=re.match(r'^/api/workspaces/([^/]+)/members$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if not role(w['id'],u['id']):return self.send(403,{'error':'forbidden'})
   return self.send(200,{'members':[dict(r) for r in q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
  m=re.match(r'^/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if not role(w['id'],u['id']):return self.send(403,{'error':'forbidden'})
   return self.send(200,{'groups':[groupj(g) for g in q('SELECT * FROM groups WHERE workspace_id=?',(w['id'],))]})
  m=re.match(r'^/api/users/(\d+)$',path)
  if m:
   x=q('SELECT * FROM users WHERE id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   return self.send(200,{'user':userj(x)})
  if path.startswith('/api/files/'):
   m=re.match(r'^/api/files/(\d+)(/download)?$',path); f=q('SELECT * FROM files WHERE id=?',(m.group(1),),True) if m else None
   if not f:return self.send(404,{'error':'not found'})
   if m.group(2):
    data=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
   return self.send(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  m=re.match(r'^/api/workspaces/([^/]+)/invitations$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if role(w['id'],u['id']) not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   out=[]; nowv=now()
   for x in q('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)',(w['id'],nowv)):
    out.append({'code':x['code'],'workspace_id':x['workspace_id'],'max_uses':x['max_uses'],'uses':x['uses'],'expires_at':x['expires_at'],'invited_user_id':x['invited_user_id']})
   return self.send(200,{'invitations':out})
  m=re.match(r'^/api/messages/(\d+)/pin$',path)
  if m:
   x=q('SELECT m.*,c.workspace_id,c.is_private FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=? AND m.deleted=0',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if not role(x['workspace_id'],u['id']) or (x['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['channel_id'],u['id']),True)):return self.send(403,{'error':'forbidden'})
   old=q('SELECT * FROM pins WHERE message_id=?',(x['id'],),True)
   if old:return self.send(200,{'pin':dict(old)})
   t=now(); c=db(); c.execute('INSERT INTO pins VALUES(?,?,?)',(x['id'],u['id'],t)); c.close()
   return self.send(200,{'pin':{'message_id':x['id'],'pinned_by':u['id'],'pinned_at':t}})
  m=re.match(r'^/api/channels/(\d+)/pins$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.send(404,{'error':'not found'})
   if c['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   try:lim=int(qs.get('limit',['50'])[0]);assert 1<=lim<=200
   except:return self.send(400,{'error':'invalid limit'})
   try:before=cursor_read(qs.get('cursor',[None])[0],'p')
   except:return self.send(400,{'error':'invalid cursor'})
   sql='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?';args=[c['id']]
   if before is not None:sql+=' AND p.message_id<?';args.append(before)
   sql+=' ORDER BY p.message_id DESC LIMIT ?';args.append(lim+1);rows=q(sql,tuple(args));nxt=cursor_make('p',rows[lim-1]['message_id']) if len(rows)>lim else None
   return self.send(200,{'pins':[{'message':msgj(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows[:lim]],'next_cursor':nxt})
  if path=='/api/workspaces':
   a=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],));return self.send(200,{'workspaces':[ws(x) for x in a]})
  if path=='/api/workspaces':
   try:b=self.body();assert re.fullmatch(r'[a-z0-9-]{2,32}',b['slug']) and b.get('name')
   except:return self.send(400,{'error':'invalid workspace'})
   try:
    c=db();c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']));w=c.execute('SELECT * FROM workspaces WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone();c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general'));ch=c.execute('SELECT * FROM channels WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone();c.execute('INSERT INTO cmembers VALUES(?,?)',(ch['id'],u['id']));c.close()
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   return self.send(201,{'workspace':ws(w),'general_channel':chan(ch)})
  m=re.match(r'^/api/workspaces/([^/]+)/invitations$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if role(w['id'],u['id']) not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   b=self.body(); target=b.get('invited_user_id')
   if target is None and b.get('invited_username'):
    z=q('SELECT id FROM users WHERE username=?',(b['invited_username'],),True); target=z['id'] if z else None
   try: maxu=max(1,int(b.get('max_uses',1))); exp=int(b['expires_in_seconds']) if b.get('expires_in_seconds') is not None else None
   except:return self.send(400,{'error':'invalid invitation'})
   if target is not None:maxu=1
   expires=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=exp)).isoformat(timespec='milliseconds').replace('+00:00','Z') if exp is not None else None
   code=secrets.token_urlsafe(18); c=db();c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],maxu,0,expires,target));c.close()
   x=q('SELECT * FROM invitations WHERE code=?',(code,),True)
   return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':maxu,'uses':0,'expires_at':expires,'invited_user_id':target}})
  m=re.match(r'^/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   r=role(w['id'],u['id'])
   try:b=self.body(); name=b['name']; private=bool(b.get('is_private',False)); assert re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) and len(name)<=32 and len(b.get('topic',''))<=250
   except:return self.send(400,{'error':'invalid channel'})
   if r not in ('owner','admin') and (r!='member' or private):return self.send(403,{'error':'forbidden'})
   try:
    c=db();c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),b.get('topic','')));x=c.execute('SELECT * FROM channels WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone();c.execute('INSERT INTO cmembers VALUES(?,?)',(x['id'],u['id']));c.close()
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   return self.send(201,{'channel':chan(x)})
  m=re.match(r'^/api/channels/(\d+)/(leave|archive|unarchive)$',path)
  if m:
   x=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   action=m.group(2)
   if action=='leave':
    c=db(); cur=c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id'])); changed=cur.rowcount; c.close()
    if changed: emit_event(x['id'],'member.left',{'user_id':u['id']})
    return self.send(200,{'channel':chan(x)})
   if role(x['workspace_id'],u['id'])!='owner':return self.send(403,{'error':'forbidden'})
   val=1 if action=='archive' else 0
   c=db(); c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,x['id'])); c.close()
   updated=chan(q('SELECT * FROM channels WHERE id=?',(x['id'],),True)); emit_event(x['id'],'channel.archived' if val else 'channel.unarchived',{'channel':updated}); return self.send(200,{'channel':updated})
  m=re.match(r'^/api/channels/(\d+)/join$',path)
  if m:
   x=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   r=role(x['workspace_id'],u['id'])
   if q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id']),True):return self.send(200,{'channel':chan(x)})
   if x['is_private']:return self.send(403,{'error':'forbidden'})
   w=q('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],),True)
   if not r:
    if w['join_mode']!='open':return self.send(403,{'error':'forbidden'})
    addm(x['workspace_id'],u['id'],'member'); r='member'
   if r=='guest':return self.send(403,{'error':'forbidden'})
   cm(x['id'],u['id']); emit_event(x['id'],'member.joined',{'user_id':u['id']}); return self.send(200,{'channel':chan(x)})
  m=re.match(r'^/api/workspaces/([^/]+)$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if not role(w['id'],u['id']):return self.send(403,{'error':'forbidden'})
   inc=qs.get('include_archived',['0'])[0] in ('1','true')
   cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],inc))
   cs=[c for c in cs if not c['is_private'] or q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True)]
   return self.send(200,{'workspace':ws(w),'channels':[chan(c) for c in cs],'read_state':[read_state_json(u['id'],c['id']) for c in cs]})
  m=re.match(r'^/api/channels/(\d+)/members$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.send(404,{'error':'not found'})
   if c['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   if c['is_dm'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   return self.send(200,{'members':[userj(q('SELECT * FROM users WHERE id=?',(r['user_id'],),True)) for r in q('SELECT user_id FROM cmembers WHERE channel_id=?',(c['id'],))]})
  m=re.match(r'^/api/channels/(\d+)/messages$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.send(404,{'error':'not found'})
   if c['is_dm']:
    if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   elif c['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   try: lim=int(qs.get('limit',['50'])[0]); assert 1<=lim<=200
   except:return self.send(400,{'error':'invalid limit'})
   try: before=cursor_read(qs.get('cursor',[None])[0],'h')
   except:return self.send(400,{'error':'invalid cursor'})
   if before is None: rows=q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT ?',(c['id'],lim+1))
   else: rows=q('SELECT * FROM messages WHERE channel_id=? AND id<? AND deleted=0 ORDER BY id DESC LIMIT ?',(c['id'],before,lim+1))
   nxt=cursor_make('h',rows[lim-1]['id']) if len(rows)>lim else None
   return self.send(200,{'messages':[msgj(x) for x in rows[:lim]],'next_cursor':nxt})
  m=re.match(r'^/api/messages/(\d+)/replies$',path)
  if m:
   parent=q('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),),True)
   if not parent:return self.send(404,{'error':'not found'})
   c=q('SELECT * FROM channels WHERE id=?',(parent['channel_id'],),True)
   if c['is_dm']:
    allowed=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True)
   else: allowed=not c['is_private'] or q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True)
   if not allowed:return self.send(403,{'error':'forbidden'})
   try: lim=int(qs.get('limit',['50'])[0]); assert 1<=lim<=200
   except:return self.send(400,{'error':'invalid limit'})
   try: after=cursor_read(qs.get('cursor',[None])[0],'r')
   except:return self.send(400,{'error':'invalid cursor'})
   if after is None: rows=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?',(parent['id'],lim+1))
   else: rows=q('SELECT * FROM messages WHERE parent_id=? AND id>? AND deleted=0 ORDER BY id LIMIT ?',(parent['id'],after,lim+1))
   nxt=cursor_make('r',rows[lim-1]['id']) if len(rows)>lim else None
   return self.send(200,{'replies':[msgj(x) for x in rows[:lim]],'next_cursor':nxt})
  m=re.match(r'^/api/messages/(\d+)/pin$',path)
  if m:
   x=q('SELECT m.*,c.workspace_id,c.is_private FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=? AND m.deleted=0',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if not role(x['workspace_id'],u['id']) or (x['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['channel_id'],u['id']),True)):return self.send(403,{'error':'forbidden'})
   old=q('SELECT * FROM pins WHERE message_id=?',(x['id'],),True)
   if old:return self.send(200,{'pin':dict(old)})
   t=now();c=db();c.execute('INSERT INTO pins VALUES(?,?,?)',(x['id'],u['id'],t));c.close();return self.send(200,{'pin':{'message_id':x['id'],'pinned_by':u['id'],'pinned_at':t}})
  m=re.match(r'^/api/channels/(\d+)/read$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.send(404,{'error':'not found'})
   allowed=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True) if c['is_private'] else bool(role(c['workspace_id'],u['id']))
   if not allowed:return self.send(403,{'error':'forbidden'})
   return self.send(200,{'read_state':read_state_json(u['id'],c['id'])})
  return self.send(404,{'error':'not found'})
 def do_POST(self):
  path=urllib.parse.urlparse(self.path).path
  if path=='/api/auth/register':
   try:b=self.body(); un=b['username']; pw=b['password']; assert re.fullmatch(r'[A-Za-z0-9_]+',un) and len(pw)>=8
   except:return self.send(400,{'error':'invalid registration'})
   try:
    c=db();h=hashlib.sha256(pw.encode()).hexdigest();c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,h,b.get('display_name') or un));uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.close()
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   return self.send(201,{'user':userj(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':t})
  if path=='/api/auth/login':
   b=self.body();x=q('SELECT * FROM users WHERE username=?',(b.get('username'),),True)
   if not x or not hmac.compare_digest(x['password'],hashlib.sha256(str(b.get('password','')).encode()).hexdigest()):return self.send(401,{'error':'invalid credentials'})
   t=secrets.token_urlsafe(24);c=db();c.execute('INSERT INTO tokens VALUES(?,?)',(t,x['id']));c.close();return self.send(200,{'user':userj(x),'token':t})
  u=self.auth()
  if not u:return
  b=None
  m=re.match(r'^/api/channels/(\d+)/read$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.send(404,{'error':'not found'})
   allowed=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True) if c['is_private'] else bool(role(c['workspace_id'],u['id']))
   if not allowed:return self.send(403,{'error':'forbidden'})
   try:eid=int(self.body()['last_read_event_id'])
   except:return self.send(400,{'error':'invalid event id'})
   return self.send(200,{'read_state':set_last_read(u['id'],c['id'],eid)})
  m=re.match(r'^/api/messages/(\d+)/pin$',path)
  if m:
   x=q('SELECT m.*,c.workspace_id,c.is_private FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=? AND m.deleted=0',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if x['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['channel_id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   old=q('SELECT * FROM pins WHERE message_id=?',(x['id'],),True)
   if old:return self.send(200,{'pin':dict(old)})
   t=now();c=db();c.execute('INSERT INTO pins VALUES(?,?,?)',(x['id'],u['id'],t));c.close()
   return self.send(200,{'pin':{'message_id':x['id'],'pinned_by':u['id'],'pinned_at':t}})
  if path=='/api/dms':
   b=self.body();tid=b.get('user_id');z=q('SELECT id FROM users WHERE id=?',(tid,),True)
   if not z:return self.send(404,{'error':'not found'})
   if int(tid)==u['id']:return self.send(400,{'error':'self'})
   a,d=sorted((u['id'],int(tid)));name=f'dm-{a}-{d}';old=q('SELECT id FROM channels WHERE is_dm=1 AND name=?',(name,),True)
   if old:return self.send(200,{'channel_id':old['id']})
   c=db();c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)',(name,1));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,a));c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,d));c.close();return self.send(200,{'channel_id':cid})
  if path=='/api/files':
   n=int(self.headers.get('Content-Length','0'));raw=self.rfile.read(n);ct=self.headers.get('Content-Type','');mm=re.search('boundary=([^;]+)',ct)
   if not mm:return self.send(400,{'error':'file required'})
   data=None;fn='file';typ='application/octet-stream';bd=('--'+mm.group(1).strip('"')).encode()
   for part in raw.split(bd):
    if b'filename=' in part:
     h,_,v=part.partition(b'\r\n\r\n');fm=re.search(br'filename="([^"]*)"',h);tm=re.search(br'Content-Type:\s*([^\r\n]+)',h,re.I);fn=fm.group(1).decode() if fm else fn;typ=tm.group(1).decode() if tm else typ;data=v[:-2] if v.endswith(b'\r\n') else v
   if data is None:return self.send(400,{'error':'file required'})
   if len(data)>10485760:return self.send(413,{'error':'too large'})
   c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.close();f=q('SELECT * FROM files WHERE id=?',(fid,),True);return self.send(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  m=re.match(r'^/api/workspaces/([^/]+)/transfer_ownership$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if role(w['id'],u['id'])!='owner':return self.send(403,{'error':'forbidden'})
   tid=self.body().get('new_owner_id'); tr=role(w['id'],tid) if tid is not None else None
   if tr!='admin':return self.send(400,{'error':'target must be admin'})
   c=db(); c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],w['owner_id'])); c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],tid)); c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(tid,w['id'])); c.close()
   return self.send(200,{'workspace':ws(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  m=re.match(r'^/api/workspaces/([^/]+)/invitations$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if role(w['id'],u['id']) not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   b=self.body(); target=b.get('invited_user_id')
   if target is None and b.get('invited_username'):
    z=q('SELECT id FROM users WHERE username=?',(b['invited_username'],),True); target=z['id'] if z else None
   try:
    maxu=max(1,int(b.get('max_uses',1))); exp=int(b['expires_in_seconds']) if b.get('expires_in_seconds') is not None else None
   except:return self.send(400,{'error':'invalid invitation'})
   if target is not None:maxu=1
   expires=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=exp)).isoformat(timespec='milliseconds').replace('+00:00','Z') if exp is not None else None
   code=secrets.token_urlsafe(18); c=db(); c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],maxu,0,expires,target)); c.close()
   return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':maxu,'uses':0,'expires_at':expires,'invited_user_id':target}})
  m=re.match(r'^/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if not role(w['id'],u['id']):return self.send(403,{'error':'forbidden'})
   b=self.body();h=b.get('handle','')
   if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel') or not b.get('name'):return self.send(400,{'error':'invalid group'})
   try:
    c=db();c.execute('INSERT INTO groups(workspace_id,creator_id,handle,name) VALUES(?,?,?,?)',(w['id'],u['id'],h,b['name']));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for vid in b.get('user_ids',[]):c.execute('INSERT INTO group_users VALUES(?,?)',(gid,vid))
    c.close()
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   return self.send(201,{'group':groupj(q('SELECT * FROM groups WHERE id=?',(gid,),True))})
  m=re.match(r'^/api/invitations/([^/]+)/accept$',path)
  if m:
   x=q('SELECT * FROM invitations WHERE code=?',(m.group(1),),True)
   if not x or x['revoked'] or x['uses']>=x['max_uses'] or (x['expires_at'] and x['expires_at']<=now()):return self.send(404,{'error':'not found'})
   if x['invited_user_id'] and x['invited_user_id']!=u['id']:return self.send(403,{'error':'forbidden'})
   c=db();c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(x['code'],));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.close();w=q('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],),True);return self.send(200,{'workspace':ws(w),'member':dict(q('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True))})
  if path=='/api/workspaces':
   b=self.body()
   try:assert re.fullmatch(r'[a-z0-9-]{2,32}',b['slug']) and b.get('name')
   except:return self.send(400,{'error':'invalid workspace'})
   try:
    c=db();c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,'general')",(wid,));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.close()
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   return self.send(201,{'workspace':ws(q('SELECT * FROM workspaces WHERE id=?',(wid,),True)),'general_channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  m=re.match(r'^/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   b=self.body();name=b.get('name','');private=bool(b.get('is_private'));r=role(w['id'],u['id'])
   if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32 or len(b.get('topic',''))>250:return self.send(400,{'error':'invalid channel'})
   if r not in ('owner','admin') and (r!='member' or private):return self.send(403,{'error':'forbidden'})
   try:
    c=db();c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),b.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.close()
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   return self.send(201,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  m=re.match(r'^/api/channels/(\d+)/join$',path)
  if m:
   x=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id']),True):return self.send(200,{'channel':chan(x)})
   if x['is_private']:return self.send(403,{'error':'forbidden'})
   w=q('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],),True);r=role(x['workspace_id'],u['id'])
   if not r:
    if w['join_mode']!='open':return self.send(403,{'error':'forbidden'})
    addm(x['workspace_id'],u['id'],'member');r='member'
   if r=='guest':return self.send(403,{'error':'forbidden'})
   cm(x['id'],u['id']);emit_event(x['id'],'member.joined',{'user_id':u['id']});return self.send(200,{'channel':chan(x)})
  m=re.match(r'^/api/channels/(\d+)/(leave|archive|unarchive)$',path)
  if m:
   x=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if m.group(2)=='leave':
    c=db();cur=c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id']));c.close()
    if cur.rowcount:emit_event(x['id'],'member.left',{'user_id':u['id']})
    return self.send(200,{'channel':chan(x)})
   if role(x['workspace_id'],u['id'])!='owner':return self.send(403,{'error':'forbidden'})
   val=m.group(2)=='archive';c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(val),x['id']));c.close();z=chan(q('SELECT * FROM channels WHERE id=?',(x['id'],),True));emit_event(x['id'],'channel.archived' if val else 'channel.unarchived',{'channel':z});return self.send(200,{'channel':z})
  m=re.match(r'^/api/messages/(\d+)/reactions$',path)
  if m:
   x=q('SELECT m.*,c.workspace_id,c.is_private FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=? AND m.deleted=0',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if x['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['channel_id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   e=self.body().get('emoji','')
   if not e:return self.send(400,{'error':'emoji'})
   c=db();ch=c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(x['id'],u['id'],e));changed=ch.rowcount;c.close();r=msgj(q('SELECT * FROM messages WHERE id=?',(x['id'],),True))['reactions']
   if changed:emit_event(x['channel_id'],'reaction.added',{'message_id':x['id'],'emoji':e,'user_id':u['id'],'reactions':r})
   return self.send(200,{'reactions':r})
  m=re.match(r'^/api/channels/(\d+)/messages$',path)
  if m:
   x=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id']),True):return self.send(403,{'error':'forbidden'})
   try:b=self.body();assert b.get('body','').strip()
   except:return self.send(400,{'error':'empty body'})
   rawbody=b['body']
   if rawbody.startswith('/') and not rawbody.startswith('//'):
    parts=rawbody[1:].split(' ',1); cmd=parts[0].lower(); arg=parts[1].strip() if len(parts)>1 else ''
    if cmd=='me':
     if not arg:return self.send(400,{'error':'text required'})
     b['body']=rawbody
    elif cmd=='shrug':
     b['body']=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd in ('topic','invite','archive','unarchive'):
     if cmd!='unarchive' and x['is_archived']:return self.send(423,{'error':'archived'})
     if cmd=='topic':
      if not arg or len(arg)>250:return self.send(400,{'error':'invalid topic'})
      if not (q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id']),True) or role(x['workspace_id'],u['id'])=='owner'):return self.send(403,{'error':'forbidden'})
      c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,x['id']));c.close(); ch=chan(q('SELECT * FROM channels WHERE id=?',(x['id'],),True));emit_event(x['id'],'channel.updated',{'channel':ch});return self.send(201,{'message':None,'channel':ch})
     if cmd=='invite':
      if not arg.startswith('@') or not arg[1:]:return self.send(400,{'error':'username required'})
      if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id']),True):return self.send(403,{'error':'forbidden'})
      target=q('SELECT * FROM users WHERE username=?',(arg[1:],),True)
      if not target:return self.send(404,{'error':'not found'})
      if not role(x['workspace_id'],target['id']):return self.send(403,{'error':'outside workspace'})
      cm(x['id'],target['id']);ch=userj(target);emit_event(x['id'],'member.joined',{'user_id':target['id']});return self.send(201,{'message':None,'invited_user':ch})
     if cmd in ('archive','unarchive'):
      if role(x['workspace_id'],u['id'])!='owner':return self.send(403,{'error':'forbidden'})
      val=1 if cmd=='archive' else 0;c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,x['id']));c.close();ch=chan(q('SELECT * FROM channels WHERE id=?',(x['id'],),True));emit_event(x['id'],'channel.archived' if val else 'channel.unarchived',{'channel':ch});return self.send(201,{'message':None,'channel':ch})
    else:return self.send(400,{'error':'unknown command'})
   if x['is_archived']:return self.send(423,{'error':'archived'})
   fids=b.get('file_ids',[])
   if not isinstance(fids,list):return self.send(400,{'error':'invalid files'})
   for fid in fids:
    if not q('SELECT 1 FROM files f WHERE f.id=? AND f.uploader_id=? AND NOT EXISTS (SELECT 1 FROM message_files mf WHERE mf.file_id=f.id)',(fid,u['id']),True):return self.send(400,{'error':'invalid file'})
   body=b['body']; names=[]
   for mm in re.finditer(r'(?<![A-Za-z0-9_.])@([A-Za-z0-9][A-Za-z0-9_-]*)',body):
    n=mm.group(1)
    if n=='channel':
     names += [int(v['user_id']) for v in q('SELECT user_id FROM cmembers WHERE channel_id=?',(x['id'],))]
    elif n=='here':
     online={int(v['user_id']) for v in q('SELECT user_id FROM presence WHERE last_seen>?',(time.time()-30,))}; names += [int(v['user_id']) for v in q('SELECT user_id FROM cmembers WHERE channel_id=?',(x['id'],)) if int(v['user_id']) in online]
    else:
     gg=q('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(x['workspace_id'],n),True)
     if gg:
      names += [int(v['user_id']) for v in q('SELECT user_id FROM group_users WHERE group_id=? AND user_id IN (SELECT user_id FROM cmembers WHERE channel_id=?)',(gg['id'],x['id']))]
     else:
      v=q('SELECT id FROM users WHERE username=?',(n,),True)
      if v and role(x['workspace_id'],v['id']) and v['id']!=u['id']: names.append(v['id'])
   names=sorted(set(v for v in names if v!=u['id']))
   c=db();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(x['id'],u['id'],body,b.get('parent_id'),now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   for vid in names:c.execute('INSERT OR IGNORE INTO message_mentions VALUES(?,?)',(mid,vid))
   for fid in fids:c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid))
   z=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close(); emit_event(x['id'],'message.reply' if b.get('parent_id') else 'message.created',{'message':msgj(z)}); return self.send(201,{'message':msgj(z)})
  return self.send(404,{'error':'not found'})
 def do_DELETE(self):
  u=self.auth()
  if not u:return
  p=urllib.parse.urlparse(self.path).path
  m=re.match(r'^/api/messages/(\d+)$',p)
  m=re.match(r'^/api/messages/(\d+)$',p)
  if m:
   x=q('SELECT m.*,c.workspace_id FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   allowed=x['author_id']==u['id'] or role(x['workspace_id'],u['id'])=='owner'
   if not allowed:return self.send(403,{'error':'forbidden'})
   if not x['deleted']:
    c=db();c.execute('UPDATE messages SET deleted=1 WHERE id=?',(x['id'],));c.execute('DELETE FROM reactions WHERE message_id=?',(x['id'],));c.close()
    emit_event(x['channel_id'],'message.deleted',{'message_id':x['id']})
   return self.send(200,{'deleted':True})
  m=re.match(r'^/api/messages/(\d+)/reactions/(.+)$',p)
  if m:
   x=q('SELECT m.*,c.workspace_id,c.is_private FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=? AND m.deleted=0',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if not role(x['workspace_id'],u['id']) or (x['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['channel_id'],u['id']),True)):return self.send(403,{'error':'forbidden'})
   emoji=urllib.parse.unquote(m.group(2))
   c=db();cur=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(x['id'],u['id'],emoji));changed=cur.rowcount;c.close()
   reactions=msgj(q('SELECT * FROM messages WHERE id=?',(x['id'],),True))['reactions']
   if changed: emit_event(x['channel_id'],'reaction.removed',{'message_id':x['id'],'emoji':emoji,'user_id':u['id'],'reactions':reactions})
   return self.send(200,{'reactions':reactions})
  m=re.match(r'^/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True);g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'] if w else -1,m.group(2)),True)
   if not w:return self.send(404,{'error':'not found'})
   if not g:return self.send(200,{'deleted':True})
   if u['id']!=g['creator_id'] and role(w['id'],u['id'])!='owner':return self.send(403,{'error':'forbidden'})
   c=db();c.execute('DELETE FROM group_users WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.close();return self.send(200,{'deleted':True})
  m=re.match(r'^/api/messages/(\d+)/pin$',p)
  if m:
   x=q('SELECT m.*,c.workspace_id,c.is_private FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=? AND m.deleted=0',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if not role(x['workspace_id'],u['id']) or (x['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['channel_id'],u['id']),True)):return self.send(403,{'error':'forbidden'})
   c=db();c.execute('DELETE FROM pins WHERE message_id=?',(x['id'],));c.close();return self.send(200,{'unpinned':True})
  m=re.match(r'^/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
  if not m:return self.send(404,{'error':'not found'})
  w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); x=q('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(m.group(2),w['id'] if w else -1),True)
  if not w:return self.send(404,{'error':'not found'})
  if role(w['id'],u['id']) not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
  if not x:return self.send(404,{'error':'not found'})
  c=db();c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(x['code'],));c.close();return self.send(200,{'deleted':True})
 def do_PATCH(self):
  u=self.auth()
  if not u:return
  p=urllib.parse.urlparse(self.path).path;b=self.body()
  if p=='/api/users/me':
   try:
    fields=b; validate_profile(fields)
   except: return self.send(400,{'error':'invalid profile'})
   allowed=('display_name','timezone','avatar_url','status_text','status_emoji')
   fields={k:v for k,v in fields.items() if k in allowed}
   if fields:
    c=db(); c.execute('UPDATE users SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',tuple(fields.values())+(u['id'],)); c.close()
   z=q('SELECT * FROM users WHERE id=?',(u['id'],),True); uj=userj(z); broadcast_global({'type':'user.updated','user':uj})
   return self.send(200,{'user':uj})
  m=re.match(r'^/api/messages/(\d+)$',p)
  if m:
   x=q('SELECT m.*,c.workspace_id FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if x['deleted']:return self.send(409,{'error':'deleted'})
   if x['author_id']!=u['id']:return self.send(403,{'error':'forbidden'})
   try: body=b['body']; assert isinstance(body,str) and body.strip()
   except:return self.send(400,{'error':'empty body'})
   edited=now(); c=db(); c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,edited,x['id'])); c.close()
   z=q('SELECT * FROM messages WHERE id=?',(x['id'],),True); frame=msgj(z); emit_event(x['channel_id'],'message.updated',{'message':frame})
   return self.send(200,{'message':frame})
  m=re.match(r'^/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True);g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'] if w else -1,m.group(2)),True)
   if not w:return self.send(404,{'error':'not found'})
   if not g:return self.send(404,{'error':'not found'})
   if u['id']!=g['creator_id'] and role(w['id'],u['id'])!='owner':return self.send(403,{'error':'forbidden'})
   c=db()
   if 'name' in b:c.execute('UPDATE groups SET name=? WHERE id=?',(b['name'],g['id']))
   if 'user_ids' in b:
    c.execute('DELETE FROM group_users WHERE group_id=?',(g['id'],))
    for vid in b['user_ids']:c.execute('INSERT INTO group_users VALUES(?,?)',(g['id'],vid))
   c.close();return self.send(200,{'group':groupj(q('SELECT * FROM groups WHERE id=?',(g['id'],),True))})
  m=re.match(r'^/api/workspaces/([^/]+)/members/(\d+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   target_id=int(m.group(2)); target=role(w['id'],target_id)
   if target is None:return self.send(404,{'error':'not found'})
   nr=b.get('role')
   if nr not in ('admin','member','guest'):return self.send(400,{'error':'invalid role'})
   actor=role(w['id'],u['id'])
   if target=='owner' or target_id==w['owner_id']:return self.send(403,{'error':'forbidden'})
   if actor not in ('owner','admin') or (actor=='admin' and target=='admin'):return self.send(403,{'error':'forbidden'})
   c=db();c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(nr,w['id'],target_id));c.close()
   return self.send(200,{'member':dict(q('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target_id),True))})
  m=re.match(r'^/api/workspaces/([^/]+)/transfer_ownership$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if role(w['id'],u['id'])!='owner':return self.send(403,{'error':'forbidden'})
   tid=b.get('new_owner_id'); tr=role(w['id'],tid) if tid is not None else None
   if tr!='admin':return self.send(400,{'error':'target must be admin'})
   c=db();c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],w['owner_id']));c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],tid));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(tid,w['id']));c.close()
   return self.send(200,{'workspace':ws(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  m=re.match(r'^/api/workspaces/([^/]+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.send(404,{'error':'not found'})
   if role(w['id'],u['id']) not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.send(400,{'error':'invalid join mode'})
   if 'name' in b and not isinstance(b['name'],str):return self.send(400,{'error':'invalid name'})
   c=db()
   if 'name' in b:c.execute('UPDATE workspaces SET name=? WHERE id=?',(b['name'],w['id']))
   if 'join_mode' in b:c.execute('UPDATE workspaces SET join_mode=? WHERE id=?',(b['join_mode'],w['id']))
   c.close()
   return self.send(200,{'workspace':ws(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  m=re.match(r'^/api/channels/(\d+)$',p)
  if m:
   x=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not x:return self.send(404,{'error':'not found'})
   if x['is_archived']:return self.send(423,{'error':'archived'})
   if len(b.get('topic',''))>250:return self.send(400,{'error':'topic'})
   if not (q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id']),True) or role(x['workspace_id'],u['id'])=='owner'):return self.send(403,{'error':'forbidden'})
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),x['id']));c.close(); updated=chan(q('SELECT * FROM channels WHERE id=?',(x['id'],),True)); emit_event(x['id'],'channel.updated',{'channel':updated}); return self.send(200,{'channel':updated})
  return self.send(404,{'error':'not found'})

irc_nicks=set(); irc_clients={}; irc_lock=threading.Lock(); irc_seen={}
def irc_reply(sock,code,nick,text):
 sock.sendall((':huddle '+code+' '+(nick or '*')+' '+text+'\r\n').encode())
def irc_channel_name(w,c): return '#'+w['slug']+'/'+c['name']
def irc_find_channel(name):
 if not name.startswith('#') or '/' not in name[1:]: return None
 slug,cn=name[1:].split('/',1)
 w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
 return q('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],cn),True) if w else None
def irc_send(sock,text): sock.sendall((text+'\r\n').encode())
def irc_client(sock,addr):
 user=None; nick=None; passed=False; registered=False; joined=set(); buf=b''
 try:
  while True:
   z=sock.recv(4096)
   if not z:return
   buf+=z
   while b'\r\n' in buf:
    raw,buf=buf.split(b'\r\n',1); line=raw.decode('utf8','ignore')
    if not line:continue
    a=line.split(' ',1); cmd=a[0].upper(); arg=a[1] if len(a)>1 else ''
    if cmd=='PASS':
     user=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(arg.strip(),),True)
     if user:passed=True
     else:irc_reply(sock,'464',nick,'Password incorrect')
    elif cmd=='NICK':
     nn=arg.strip().lstrip(':')
     if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',nn):irc_reply(sock,'432',nick,'Erroneous nickname');continue
     with irc_lock:
      if any(nn.lower()==x.lower() and x!=nick for x in irc_nicks):irc_reply(sock,'433',nick,nn+' Nickname is already in use');continue
      if nick:irc_nicks.discard(nick)
      nick=nn;irc_nicks.add(nick)
    elif cmd=='USER':
     if passed and nick and not registered:
      registered=True
      for code,text in [('001','Welcome to Huddle'),('002','Your host is Huddle'),('003','This server was created now'),('004','huddle huddle 1 0'),('005','CHANTYPES=# :are supported'),('422','MOTD File is missing')]:irc_reply(sock,code,nick,text)
      with irc_lock: irc_clients[sock]={'user':user,'nick':nick,'joined':joined}
    elif cmd=='PING':
     text=arg.strip().lstrip(':');irc_send(sock,':huddle PONG '+(nick or '*')+' :'+text)
    elif cmd=='QUIT':return
    elif cmd in ('JOIN','NAMES','WHO','LIST','TOPIC','MODE','PRIVMSG'):
     target=arg.split()[0] if arg.split() else ''
     c=irc_find_channel(target) if target else None
     if cmd=='JOIN':
      if not c or c['is_private']:irc_reply(sock,'403',nick,target+' No such channel');continue
      w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True); nm=irc_channel_name(w,c)
      addm(c['workspace_id'],user['id'],'member'); cm(c['id'],user['id']); joined.add(c['id'])
      irc_send(sock,':'+nick+'!'+user['username']+'@localhost JOIN '+nm)
      names=' '.join([q('SELECT username FROM users WHERE id=?',(x['user_id'],),True)['username'] for x in q('SELECT user_id FROM cmembers WHERE channel_id=?',(c['id'],))])
      irc_reply(sock,'353',nick,'= '+nm+' :'+names);irc_reply(sock,'366',nick,nm+' End of NAMES list')
     elif not c or c['id'] not in joined:
      if cmd=='LIST':irc_reply(sock,'321',nick,'Channel :Users Name')
      elif cmd in ('NAMES','WHO','TOPIC','MODE'):irc_reply(sock,'421',nick,cmd+' accepted')
      else:irc_reply(sock,'421',nick,cmd+' Unknown command')
     elif cmd=='PRIVMSG':
      text=arg.split(' :',1)[1] if ' :' in arg else ''
      if text:
       c0=q('SELECT * FROM channels WHERE id=?',(c['id'],),True); dbx=db();dbx.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(c['id'],user['id'],text,now())); mid=dbx.execute('SELECT last_insert_rowid()').fetchone()[0];z=dbx.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();dbx.close();emit_event(c['id'],'message.created',{'message':msgj(z)})
     elif cmd=='NAMES':irc_reply(sock,'353',nick,'= '+target+' :'+nick);irc_reply(sock,'366',nick,target+' End of NAMES list')
     elif cmd=='WHO':irc_reply(sock,'352',nick,target+' '+nick+' localhost huddle '+nick+' H :0 '+nick);irc_reply(sock,'315',nick,target+' End of WHO list')
     elif cmd=='LIST':irc_reply(sock,'322',nick,target+' 1 :'+(c['topic'] or ''));irc_reply(sock,'323',nick,'End of LIST')
     elif cmd=='TOPIC':irc_reply(sock,'331',nick,target+' No topic') if not c['topic'] else irc_reply(sock,'332',nick,target+' '+c['topic'])
     elif cmd=='MODE':irc_reply(sock,'324',nick,target+' +')
    else:irc_reply(sock,'421',nick or '*',cmd+' Unknown command')
 except Exception:pass
 finally:
  with irc_lock:
   if nick:irc_nicks.discard(nick)
   irc_clients.pop(sock,None)
  try:sock.close()
  except:pass
def irc_pump():
 while True:
  try:
   rows=q("SELECT channel_id,event_id,payload FROM channel_events WHERE kind='message.created' ORDER BY rowid")
   for x in rows:
    key=(x['channel_id'],x['event_id'])
    if key in irc_seen:continue
    irc_seen[key]=1; ev=json.loads(x['payload']); m=ev['message']; c=q('SELECT * FROM channels WHERE id=?',(x['channel_id'],),True); w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
    au=q('SELECT username FROM users WHERE id=?',(m['author_id'],),True); nm=irc_channel_name(w,c)
    with irc_lock:
     for so,info in list(irc_clients.items()):
      if c['id'] in info['joined']:
       try: irc_send(so,':'+au['username']+'!'+au['username']+'@localhost PRIVMSG '+nm+' :'+m['body'])
       except: pass
  except: pass
  time.sleep(.1)
def irc_server():
 srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM);srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(('0.0.0.0',6667));srv.listen(100)
 threading.Thread(target=irc_pump,daemon=True).start()
 while True:
  c,a=srv.accept();threading.Thread(target=irc_client,args=(c,a),daemon=True).start()
if __name__ == '__main__':
 if len(sys.argv)>1 and sys.argv[1]=='irc':irc_server()
 else:
  node=int(sys.argv[1]) if len(sys.argv)>1 else 0
  threading.Thread(target=redis_listener,daemon=True).start()
  srv=ThreadingHTTPServer(('127.0.0.1',8000+node),H);srv.node=node;srv.serve_forever()
