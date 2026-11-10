import os,sqlite3,json,re,hashlib,secrets,base64,datetime,threading,time,urllib.parse,struct,email.parser,email.policy
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from zoneinfo import ZoneInfo
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT)'''); c.commit(); c.close()
def user_from_token(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def user_to_json(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def get_user_by_username(x):
 c=db();r=c.execute('SELECT * FROM users WHERE username=?',(x,)).fetchone();c.close();return r
def ws(slug):
 c=db();r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();c.close();return r
def role(w,u):
 c=db();r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone();c.close();return r['role'] if r else None
def chan(i):
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone();c.close();return r
def chjson(r): return {**dict(r),'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'is_archived':bool(r['is_archived'])}
def addm(cid,uid):
 c=db();c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,uid));c.commit();c.close()
def ismem(cid,uid):
 c=db();r=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone();c.close();return bool(r)
def members(cid):
 c=db();rs=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall();c.close();return [user_to_json(x) for x in rs]
def readable(ch,uid): return bool(ch and (not ch['is_dm'] and (not ch['is_private'] and role(ch['workspace_id'],uid)) or ismem(ch['id'],uid) or role(ch['workspace_id'],uid)=='owner' if ch['workspace_id'] else ismem(ch['id'],uid)))
def reactions(mid):
 c=db();rs=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall();c.close();o=[]
 for r in rs:
  x=next((z for z in o if z['emoji']==r['emoji']),None)
  if not x:o.append({'emoji':r['emoji'],'count':1,'user_ids':[r['user_id']]})
  else:x['user_ids'].append(r['user_id']);x['count']+=1
 return o
def files_for(mid):
 c=db();rs=c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(mid,)).fetchall();c.close();return [filejson(x) for x in rs]
def filejson(f): return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def msgjson(r):
 d=dict(r); u=getuser(r['author_id']); d.update(author=user_to_json(u),files=files_for(r['id']),reactions=reactions(r['id']),mentions=mentions(r['body'],r['channel_id'],r['author_id']),reply_count=countreplies(r['id'])); return d
def getuser(i):
 c=db();r=c.execute('SELECT * FROM users WHERE id=?',(i,)).fetchone();c.close();return r
def countreplies(i):
 c=db();n=c.execute('SELECT count(*) n FROM messages WHERE parent_id=?',(i,)).fetchone()['n'];c.close();return n
def mentions(body,cid,author):
 ch=chan(cid); ids=[]; c=db()
 for x in re.findall(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  if x in ('channel','here'):
   if x=='channel': ids += [z['id'] for z in c.execute('SELECT u.id FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=? AND u.id!=?',(cid,author))]
   else:
    ids += [z['user_id'] for z in c.execute('SELECT user_id FROM presence WHERE last_seen>? AND user_id!=?',(time.time()-1.5,author))]
  else:
   g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],x)).fetchone() if ch['workspace_id'] else None
   if g: ids += [int(i) for i in json.loads(g['user_ids']) if int(i)!=author and ismem(cid,int(i))]
   else:
    u=c.execute('SELECT id FROM users WHERE username=?',(x,)).fetchone()
    if u and u['id']!=author and (not ch['workspace_id'] or role(ch['workspace_id'],u['id'])) and ismem(cid,u['id']):ids.append(u['id'])
 c.close();return list(dict.fromkeys(ids))
def emit(cid,kind,payload):
 c=db(); n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];p=dict(payload);p.update(type=kind,event_id=n,channel_id=cid);c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,n,kind,json.dumps(p)));c.commit();c.close();return p
def global_emit(p):
 c=db();c.execute('INSERT INTO global_events(payload) VALUES(?)',(json.dumps(p),));c.commit();c.close()
clients=[]; lock=threading.Lock()
def poll():
 while True:
  try:
   c=db(); rows=c.execute('SELECT id,payload FROM events WHERE id>?',(poll.last,)).fetchall(); gs=c.execute('SELECT id,payload FROM global_events WHERE id>?',(poll.glast,)).fetchall();c.close()
   for r in rows:
    poll.last=r['id']; broadcast(json.loads(r['payload']))
   for r in gs: poll.glast=r['id'];broadcast(json.loads(r['payload']))
  except: pass
  time.sleep(.1)
poll.last=0;poll.glast=0
def broadcast(p):
 with lock:
  for x in list(clients):
   try:
    if p.get('type')=='user.updated' or (p.get('channel_id') in x['subs'] and p.get('event_id',0)>x.get('floor',{}).get(p.get('channel_id'),-1)): ws_send(x['sock'],p)
   except: pass
def ws_send(s,o):
 b=json.dumps(o,separators=(',',':')).encode(); h=bytes([0x81]); n=len(b)
 if n<126:h+=bytes([n])
 elif n<65536:h+=bytes([126])+struct.pack('>H',n)
 else:h+=bytes([127])+struct.pack('>Q',n)
 s.sendall(h+b)
def validate_group(h):return bool(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h)) and h not in ('here','channel')
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,code,o,headers={}):
  b=json.dumps(o).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)))
  for k,v in headers.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def auth(self):
  h=self.headers.get('Authorization','');return user_from_token(h[7:] if h.startswith('Bearer ') else None)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return self.rfile.read(n)
 def js(self):
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def do_GET(self):
  if self.path.startswith('/api/ws'): return self.websocket()
  p=self.path; u=self.auth()
  if p=='/api/health':return self.sendj(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if p=='/api/auth/me':return self.sendj(200,{'user':user_to_json(u)})
  m=re.fullmatch(r'/api/users/(\d+)',p)
  if m:
   x=getuser(int(m[1]));return self.sendj(200,{'user':user_to_json(x)}) if x else self.sendj(404,{})
  m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
  if m:
   w=ws(m[1])
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close()
   return self.sendj(200,{'workspace':dict(w),'channels':[chjson(x) for x in rs]})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
  if m:
   w=ws(m[1])
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return self.sendj(200,{'groups':[groupjson(x) for x in rs]})
  m=re.fullmatch(r'/api/channels/(\d+)/members',p)
  if m:
   ch=chan(int(m[1]));return self.sendj(404,{}) if not ch else (self.sendj(403,{}) if not readable(ch,u['id']) else self.sendj(200,{'members':members(ch['id'])}))
  m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=chan(int(m[1])); 
   if not ch:return self.sendj(404,{})
   if not readable(ch,u['id']):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL ORDER BY id DESC',(ch['id'],)).fetchall();c.close();return self.sendj(200,{'messages':[msgjson(x) for x in rs],'next_cursor':None})
  m=re.fullmatch(r'/api/files/(\d+)(/download)?',p)
  if m:
   c=db();f=c.execute('SELECT * FROM files WHERE id=?',(int(m[1]),)).fetchone();c.close()
   if not f:return self.sendj(404,{})
   if m[2]:
    self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(f['size']));self.end_headers();self.wfile.write(f['data'])
   else:self.sendj(200,{'file':filejson(f)})
   return
  return self.sendj(404,{})
 def do_POST(self):
  p=self.path;u=self.auth()
  if p=='/api/auth/register':
   d=self.js()
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.sendj(400,{})
   c=db()
   try:c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username'],'UTC','','',''));c.commit();uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();x=getuser(uid);return self.sendj(201,{'user':user_to_json(x),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   finally:c.close()
  if p=='/api/auth/login':
   d=self.js();x=get_user_by_username(d.get('username',''))
   if not x or x['password']!=d.get('password'):return self.sendj(401,{})
   t=secrets.token_urlsafe(24);c=db();c.execute('INSERT INTO tokens VALUES(?,?)',(t,x['id']));c.commit();c.close();return self.sendj(200,{'user':user_to_json(x),'token':t})
  if not u:return self.sendj(401,{})
  m=re.fullmatch(r'/api/channels/(\d+)/(archive|unarchive)',p)
  if m:
   ch=chan(int(m[1]))
   if not ch:return self.sendj(404,{})
   if not ch['workspace_id'] or role(ch['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
   archived=(m[2]=='archive')
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(archived),ch['id']));c.commit();c.close()
   return self.sendj(200,{'channel':chjson(chan(ch['id']))})
  if p=='/api/workspaces':
   d=self.js()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{})
   c=db()
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,0)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':chjson(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   finally:c.close()
  m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
  if m:
   w=ws(m[1]);d=self.js()
   if not w:return self.sendj(404,{})
   if role(w['id'],u['id']) not in ('owner','member'):return self.sendj(403,{})
   if d.get('is_private') and role(w['id'],u['id'])!='owner':return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.sendj(400,{})
   c=db()
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'channel':chjson(chan(cid))})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   finally:c.close()
  m=re.fullmatch(r'/api/channels/(\d+)/join',p)
  if m:
   ch=chan(int(m[1]))
   if not ch:return self.sendj(404,{})
   if ch['is_private']:return self.sendj(403,{})
   addm(ch['id'],u['id']); 
   if ch['workspace_id'] and not role(ch['workspace_id'],u['id']):dbx=lambda:None; c=db();c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.commit();c.close()
   return self.sendj(200,{'channel':chjson(ch)})
  m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=chan(int(m[1]));d=self.js()
   if not ch:return self.sendj(404,{})
   if not readable(ch,u['id']):return self.sendj(403,{})
   if ch['is_archived'] and not (isinstance(d.get('body'),str) and d.get('body','').startswith('/unarchive')):return self.sendj(423,{})
   body=d.get('body','')
   if not body:return self.sendj(400,{})
   if body.startswith('/') and not body.startswith('//'):
    cmd,arg=(body[1:].split(' ',1)+[''])[:2];arg=arg.strip()
    if cmd=='me':
     if not arg:return self.sendj(400,{})
    elif cmd=='shrug':body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='topic':
     if not arg or len(arg)>250 or (not ismem(ch['id'],u['id']) and role(ch['workspace_id'],u['id'])!='owner'):return self.sendj(403,{})
     c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));c.commit();c.close();return self.sendj(201,{'message':None,'channel':chjson(chan(ch['id']))})
    elif cmd=='invite':
     if not arg.startswith('@'):return self.sendj(400,{})
     x=get_user_by_username(arg[1:]); 
     if not x:return self.sendj(404,{})
     if not role(ch['workspace_id'],x['id']):return self.sendj(403,{})
     if not ismem(ch['id'],u['id']):return self.sendj(403,{})
     addm(ch['id'],x['id']);return self.sendj(201,{'message':None,'invited_user':user_to_json(x)})
    elif cmd in ('archive','unarchive'):
     if role(ch['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
     c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(cmd=='archive',ch['id']));c.commit();c.close();return self.sendj(201,{'message':None,'channel':chjson(chan(ch['id']))})
    else:return self.sendj(400,{})
   ids=d.get('file_ids',[]);c=db()
   for fid in ids:
    f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=?',(fid,u['id'])).fetchone()
    if not f or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone():c.close();return self.sendj(400,{})
   c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']
   for fid in ids:c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
   c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close();j=msgjson(r);emit(ch['id'],'message.created',{'message':j});return self.sendj(201,{'message':j})
  m=re.fullmatch(r'/api/messages/(\d+)/reactions',p)
  if m:
   mid=int(m[1]);d=self.js(); emoji=d.get('emoji','');return self.react(mid,u,emoji,False)
  if p=='/api/dms':
   d=self.js();x=getuser(d.get('user_id'))
   if not x:return self.sendj(404,{})
   if x['id']==u['id']:return self.sendj(400,{})
   a,b=sorted((u['id'],x['id']));c=db();r=c.execute('SELECT id FROM channels WHERE is_dm=1 AND name=?',(f'dm-{a}-{b}',)).fetchone()
   if not r:c.execute('INSERT INTO channels(name,is_private,is_dm) VALUES(?,1,1)',(f'dm-{a}-{b}',));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,b));c.commit()
   else:cid=r['id']
   c.close();return self.sendj(200,{'channel_id':cid})
  if p=='/api/files':
   raw=self.body();ct=self.headers.get('Content-Type','');bm=re.search(r'boundary=(?:"([^"]+)"|([^; ]+))',ct)
   if not bm:return self.sendj(400,{})
   boundary=(bm.group(1) or bm.group(2)).encode();part=None
   for seg in raw.split(b'--'+boundary)[1:]:
    if seg.startswith(b'--'):continue
    seg=seg.lstrip(b'\r\n')
    if b'\r\n\r\n' not in seg:continue
    hb,data=seg.split(b'\r\n\r\n',1)
    if data.endswith(b'\r\n'):data=data[:-2]
    disp=re.search(br'Content-Disposition:\s*[^\r\n]*name="file"[^\r\n]*',hb,re.I)
    if disp:
     fm=re.search(br'filename="([^"]*)"',disp.group(0),re.I)
     cm=re.search(br'Content-Type:\s*([^\r\n]+)',hb,re.I)
     part=(fm.group(1).decode(errors='replace') if fm else 'file',(cm.group(1).decode().strip() if cm else 'application/octet-stream'),data)
     break
   if not part:return self.sendj(400,{})
   filename,ctype,data=part
   if len(data)>10485760:return self.sendj(413,{})
   c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,data,created_at) VALUES(?,?,?,?,?,?)',(u['id'],filename,ctype,len(data),data,now()));c.commit();f=c.execute('SELECT * FROM files WHERE id=?',(c.execute('SELECT last_insert_rowid() x').fetchone()['x'],)).fetchone();c.close();return self.sendj(201,{'file':filejson(f)})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
  if m:
   w=ws(m[1]);d=self.js()
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   if not validate_group(d.get('handle','')):return self.sendj(400,{})
   c=db()
   try:c.execute('INSERT INTO groups_(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],d['handle'],d.get('name',''),json.dumps(d.get('user_ids',[]))));c.commit();return self.sendj(201,{'group':groupjson(c.execute('SELECT * FROM groups_ WHERE id=?',(c.execute('SELECT last_insert_rowid() x').fetchone()['x'],)).fetchone())})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   finally:c.close()
  return self.sendj(404,{})
 def react(self,mid,u,emoji,remove):
  if not emoji:return self.sendj(400,{})
  c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close()
  if not r:return self.sendj(404,{})
  ch=chan(r['channel_id'])
  if not readable(ch,u['id']):return self.sendj(403,{})
  c=db()
  old=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
  changed=False
  if remove:
   if old:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));changed=True
  elif not old:
   c.execute('INSERT INTO reactions VALUES(?,?,?,?)',(mid,u['id'],emoji,now()));changed=True
  c.commit();c.close(); rs=reactions(mid)
  if changed:emit(ch['id'],'reaction.removed' if remove else 'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':rs})
  return self.sendj(200,{'reactions':rs})
 def do_PATCH(self):
  p=self.path;u=self.auth()
  if not u:return self.sendj(401,{})
  d=self.js()
  if p=='/api/users/me':
   vals={}
   for k,v in d.items():
    if k not in ('display_name','timezone','avatar_url','status_text','status_emoji'):continue
    if not isinstance(v,str):return self.sendj(400,{})
    lim={'display_name':80,'timezone':200,'avatar_url':2048,'status_text':100,'status_emoji':64}[k]
    if len(v)>lim or k=='display_name' and not v or k=='timezone':
     if k=='timezone':
      try:ZoneInfo(v)
      except:return self.sendj(400,{})
     else:return self.sendj(400,{})
    vals[k]=v
   if 'timezone' in vals:
    try:ZoneInfo(vals['timezone'])
    except:return self.sendj(400,{})
   if vals:
    c=db();c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],));c.commit();c.close()
   x=getuser(u['id']);j=user_to_json(x);global_emit({'type':'user.updated','user':j});return self.sendj(200,{'user':j})
  m=re.fullmatch(r'/api/channels/(\d+)',p)
  if m:
   ch=chan(int(m[1]))
   if not ch:return self.sendj(404,{})
   if not readable(ch,u['id']):return self.sendj(403,{})
   if 'topic' not in d or not isinstance(d['topic'],str) or len(d['topic'])>250:return self.sendj(400,{})
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id']));c.commit();c.close();return self.sendj(200,{'channel':chjson(chan(ch['id']))})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
  if m:
   w=ws(m[1]);g=getgroup(w['id'],m[2]) if w else None
   if not g:return self.sendj(404,{})
   if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.sendj(403,{})
   c=db(); vals={}
   if 'name' in d:vals['name']=d['name']
   if 'user_ids' in d:vals['user_ids']=json.dumps(d['user_ids'])
   if vals:c.execute('UPDATE groups_ SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(g['id'],));c.commit()
   g=getgroup(w['id'],m[2]);c.close();return self.sendj(200,{'group':groupjson(g)})
  return self.sendj(404,{})
 def do_DELETE(self):
  p=self.path;u=self.auth()
  if not u:return self.sendj(401,{})
  m=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',p)
  if m:return self.react(int(m[1]),u,urllib.parse.unquote(m[2]),True)
  m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
  if m:
   w=ws(m[1]);g=getgroup(w['id'],m[2]) if w else None
   if not g:return self.sendj(200,{'deleted':True})
   if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.sendj(403,{})
   c=db();c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();c.close();return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def websocket(self):
  q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
  u=user_from_token(q.get('token',[''])[0]) or self.auth()
  if not u:
   self.send_error(401);return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:self.send_error(400);return
  import hashlib
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  sock=self.connection;x={'sock':sock,'subs':set(),'floor':{},'user_id':u['id'],'conn_id':secrets.token_hex(8)}
  c=db();c.execute('INSERT INTO presence VALUES(?,?,?)',(x['conn_id'],u['id'],time.time()));c.commit();c.close()
  with lock:clients.append(x)
  try:
   while True:
    h=sock.recv(2)
    if not h:break
    n=h[1]&127
    if n==126:n=struct.unpack('>H',sock.recv(2))[0]
    elif n==127:n=struct.unpack('>Q',sock.recv(8))[0]
    mask=sock.recv(4);data=sock.recv(n);data=bytes(data[i]^mask[i%4] for i in range(n))
    if h[0]&15==8:break
    try:
     q=json.loads(data); typ=q.get('type')
     if typ in ('subscribe','resume'):
      cid=int(q['channel_id']);x['subs'].add(cid);c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];c.close()
      if typ=='resume':
       for r in db().execute('SELECT payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(q.get('since_event_id',0)))):ws_send(sock,json.loads(r['payload']))
       x['floor'][cid]=head
       ws_send(sock,{'type':'resumed','channel_id':cid,'head_event_id':head})
      else:
       x['floor'][cid]=head
       ws_send(sock,{'type':'subscribed','channel_id':cid,'head_event_id':head})
    except:pass
    c=db();c.execute('UPDATE presence SET last_seen=? WHERE conn_id=?',(time.time(),x['conn_id']));c.commit();c.close()
  finally:
   with lock:
    if x in clients:clients.remove(x)
   c=db();c.execute('DELETE FROM presence WHERE conn_id=?',(x['conn_id'],));c.commit();c.close()
def getgroup(w,h):
 c=db();r=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w,h)).fetchone();c.close();return r
def groupjson(g):return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}
def run(port):
 s=ThreadingHTTPServer(('127.0.0.1',port),H);s.serve_forever()
if __name__=='__main__':
 init();threading.Thread(target=poll,daemon=True).start();run(int(os.environ.get('PORT','8000')))
