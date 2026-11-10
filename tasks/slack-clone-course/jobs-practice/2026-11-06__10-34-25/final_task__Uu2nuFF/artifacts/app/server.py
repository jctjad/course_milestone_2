import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

DB='/var/lib/huddle/huddle.db'; FILEDIR='/var/lib/huddle/files'
os.makedirs(FILEDIR,exist_ok=True)
NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
lock=threading.RLock(); sockets=[]; wsstate={}

def now():
 return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 c=db()
 c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS wmembers(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
'''); c.commit(); c.close()
init()

def ujson(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def chan(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def user(token):
 if not token:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(token,)).fetchone(); c.close(); return r
def user_by(name):
 c=db();r=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone();c.close();return r
def getch(cid):
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return r
def ws(slug):
 c=db();r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();c.close();return r
def role(wid,uid):
 c=db();r=c.execute('SELECT role FROM wmembers WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone();c.close();return r['role'] if r else None
def cmember(cid,uid):
 c=db();x=c.execute('SELECT 1 FROM members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone();c.close();return bool(x)
def addmem(cid,uid):
 c=db();c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(cid,uid));c.commit();c.close()
def canread(ch,uid): return cmember(ch['id'],uid) if ch['is_private'] or ch['is_dm'] else bool(role(ch['workspace_id'],uid))
def reactions(mid):
 c=db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall();c.close()
 out=[]
 for r in rows:
  x=next((z for z in out if z['emoji']==r['emoji']),None)
  if not x:x={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(x)
  x['user_ids'].append(r['user_id']);x['count']+=1
 return out
def files_for(mid):
 c=db();rs=c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(mid,)).fetchall();c.close()
 return [filejson(x) for x in rs]
def msgjson(r):
 c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n'];c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':ujson(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files_for(r['id']),'reactions':reactions(r['id']),'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def mentions(body,cid,author):
 c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); ids=set()
 mem=[x['user_id'] for x in c.execute('SELECT user_id FROM members WHERE channel_id=?',(cid,))]
 for name in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
  if name=='channel': ids.update(mem);continue
  if name=='here':
   online_rows=c.execute('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,)).fetchall()
   ids.update(x['user_id'] for x in online_rows if x['user_id'] in mem)
   continue
  x=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
  if x and x['id'] in mem:ids.add(x['id'])
 for h in re.findall(r'(?<!\w)@([a-z0-9][a-z0-9_-]{0,31})',body):
  g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],h)).fetchone() if ch['workspace_id'] else None
  if g: ids.update(x for x in json.loads(g['user_ids']) if x in mem)
 c.close();ids.discard(author);return list(ids)
def emit(cid,kind,payload):
 c=db(); mx=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']+1
 frame={'type':kind,'event_id':mx,'channel_id':cid};frame.update(payload);c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,mx,kind,json.dumps(frame)));c.commit();c.close();return frame
def filejson(r): return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def broadcast_global(frame):
 c=db()
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(NULL,NULL,?,?)',
           (frame.get('type','global'),json.dumps(frame)))
 c.commit(); c.close()
def online():
 c=db();r=c.execute('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,)).fetchall();c.close();return [x['user_id'] for x in r]

def ws_send(sock,text):
 b=text.encode(); n=len(b)
 if n<126:h=bytes([0x81,n])
 elif n<65536:h=bytes([0x81,126])+struct.pack('!H',n)
 else:h=bytes([0x81,127])+struct.pack('!Q',n)
 sock.sendall(h+b)
def ws_recv(sock):
 h=sock.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=struct.unpack('!H',sock.recv(2))[0]
 elif n==127:n=struct.unpack('!Q',sock.recv(8))[0]
 mask=sock.recv(4); d=sock.recv(n);return bytes(d[i]^mask[i%4] for i in range(n)).decode()
def poll():
 last=0
 while True:
  try:
   c=db();rs=c.execute('SELECT id,payload FROM events WHERE id>? ORDER BY id',(last,)).fetchall();last=rs[-1]['id'] if rs else last;c.close()
   for r in rs:
    try: frame=json.loads(r['payload'])
    except: continue
    with lock:
     for s in list(sockets):
      try:
       st=wsstate.get(s)
       if 'channel_id' not in frame:
        ws_send(s,r['payload'])
       elif st and st.get('channel')==frame.get('channel_id') and frame.get('event_id',0)>st.get('last',0):
        ws_send(s,r['payload']); st['last']=frame.get('event_id',st.get('last',0))
      except: pass
  except: pass
  time.sleep(.08)
threading.Thread(target=poll,daemon=True).start()
def presence_loop():
 while True:
  try:
   with lock: ids=list(wsstate.values())
   if ids:
    c=db(); c.execute('UPDATE presence SET last_seen=? WHERE last_seen>0',(time.time(),)); c.commit(); c.close()
  except: pass
  time.sleep(.5)
threading.Thread(target=presence_loop,daemon=True).start()
threading.Thread(target=lambda:presence_loop(),daemon=True).start() if False else None

class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,status,obj,headers=None):
  b=json.dumps(obj,separators=(',',':')).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b))); 
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return self.rfile.read(n)
 def auth(self):
  x=self.headers.get('Authorization','');return user(x[7:] if x.startswith('Bearer ') else '')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/api/ws':return self.websocket()
  u=self.auth()
  if path.startswith('/api/') and not u:return self.sendj(401,{'error':'unauthorized'})
  if path=='/api/auth/me':return self.sendj(200,{'user':ujson(u)})
  m=re.match(r'/api/users/(\d+)$',path)
  if m:
   c=db();r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone();c.close();return self.sendj(200,{'user':ujson(r)}) if r else self.sendj(404,{})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=ws(m.group(1))
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close();return self.sendj(200,{'workspace':dict(w),'channels':[chan(x) for x in rs]})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=ws(m.group(1)); 
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return self.sendj(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rs]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   ch=getch(m.group(1))
   if not ch:return self.sendj(404,{})
   if not canread(ch,u['id']):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT u.* FROM users u JOIN members m ON u.id=m.user_id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close();return self.sendj(200,{'members':[ujson(x) for x in rs]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=getch(m.group(1))
   if not ch:return self.sendj(404,{})
   if not canread(ch,u['id']):return self.sendj(403,{})
   c=db();rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(ch['id'],)).fetchall();c.close();return self.sendj(200,{'messages':[msgjson(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   c=db();r=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone();c.close()
   if not r:return self.sendj(404,{})
   if m.group(2):
    b=r['data'];self.send_response(200);self.send_header('Content-Type',r['content_type']);self.send_header('Content-Disposition','attachment; filename="'+r['filename']+'"');self.send_header('Content-Length',str(len(b)));self.end_headers();return self.wfile.write(b)
   return self.sendj(200,{'file':filejson(r)})
  return self.sendj(404,{})
 def do_POST(self):
  p=urlparse(self.path);path=p.path;u=self.auth()
  if path=='/api/auth/register' or path=='/api/auth/login':
   try:d=json.loads(self.body())
   except:return self.sendj(400,{})
   if path.endswith('register'):
    if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.sendj(400,{})
    c=db()
    try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username']));c.commit()
    except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
    uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.close()
   else:
    if not d.get('username') or not d.get('password'):return self.sendj(401,{})
    r=user_by(d['username'])
    if not r or r['password']!=d['password']:return self.sendj(401,{})
    uid=r['id']
   tok=secrets.token_urlsafe(24);c=db();c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.sendj(201 if path.endswith('register') else 200,{'user':ujson(r),'token':tok})
  if path=='/api/auth/me':return self.sendj(401,{})
  if not u:return self.sendj(401,{})
  try:
   rawbody=self.body() if path!='/api/files' else b''
   d=json.loads(rawbody) if rawbody else {}
  except:return self.sendj(400,{})
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{})
   c=db()
   try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO wmembers VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));gid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO members VALUES(?,?)',(gid,u['id']));c.commit()
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();g=c.execute('SELECT * FROM channels WHERE id=?',(gid,)).fetchone();c.close();return self.sendj(201,{'workspace':dict(w),'general_channel':chan(g)})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=ws(m.group(1))
   if not w:return self.sendj(404,{})
   if role(w['id'],u['id'])!='owner':return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.sendj(400,{})
   c=db()
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id']));c.commit()
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
   r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'channel':chan(r)})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=getch(m.group(1))
   if not ch:return self.sendj(404,{})
   if ch['is_private']:return self.sendj(403,{})
   addmem(ch['id'],u['id']);c=db();c.execute('INSERT OR IGNORE INTO wmembers VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.commit();c.close();return self.sendj(200,{'channel':chan(getch(ch['id']))})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   ch=getch(m.group(1))
   if not ch or role(ch['workspace_id'],u['id'])!='owner':return self.sendj(403 if ch else 404,{})
   c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(m.group(2)=='archive',ch['id']));c.commit();c.close();return self.sendj(200,{'channel':chan(getch(ch['id']))})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=getch(m.group(1))
   if not ch:return self.sendj(404,{})
   if not canread(ch,u['id']):return self.sendj(403,{})
   body=d.get('body','')
   if ch['is_archived'] and body.strip() != '/unarchive':return self.sendj(423,{})
   if not isinstance(body,str) or not body.strip():return self.sendj(400,{})
   if body.startswith('/') and not body.startswith('//'):
    parts=body.split(' ',1);cmd=parts[0];arg=parts[1].strip() if len(parts)>1 else ''
    if cmd=='/me':
     if not arg:return self.sendj(400,{})
    elif cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if not arg or len(arg)>250:return self.sendj(400,{})
     if not cmember(ch['id'],u['id']) and role(ch['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
     c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));c.commit();c.close();return self.sendj(201,{'message':None,'channel':chan(getch(ch['id']))})
    elif cmd=='/invite':
     if not re.fullmatch(r'@[A-Za-z0-9_]+',arg):return self.sendj(400,{})
     x=user_by(arg[1:])
     if not x:return self.sendj(404,{})
     if not role(ch['workspace_id'],x['id']):return self.sendj(403,{})
     addmem(ch['id'],x['id']);return self.sendj(201,{'message':None,'invited_user':ujson(x)})
    elif cmd in ('/archive','/unarchive'):
     if role(ch['workspace_id'],u['id'])!='owner':return self.sendj(403,{})
     c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(cmd=='/archive',ch['id']));c.commit();c.close();return self.sendj(201,{'message':None,'channel':chan(getch(ch['id']))})
    else:return self.sendj(400,{})
   c=db();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; 
   fids=d.get('file_ids',[])
   for fid in fids:
    x=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=?',(fid,u['id'])).fetchone()
    if not x or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone():c.rollback();c.close();return self.sendj(400,{})
   for fid in fids:c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
   c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close();frame=emit(ch['id'],'message.created',{'message':msgjson(r)});return self.sendj(201,{'message':msgjson(r)})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   mid=int(m.group(1));c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close()
   if not r:return self.sendj(404,{})
   if not d.get('emoji'):return self.sendj(400,{})
   ch=getch(r['channel_id'])
   if not canread(ch,u['id']):return self.sendj(403,{})
   c=db();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?,?)',(mid,u['id'],d['emoji'],now()));changed=c.total_changes;c.commit();c.close()
   if changed:emit(ch['id'],'reaction.added',{'message_id':mid,'emoji':d['emoji'],'user_id':u['id'],'reactions':reactions(mid)})
   return self.sendj(200,{'reactions':reactions(mid)})
  if path=='/api/dms':
   x=user_by(str(d.get('user_id')) if isinstance(d.get('user_id'),str) else '') or (db().execute('SELECT * FROM users WHERE id=?',(d.get('user_id'),)).fetchone())
   if not x:return self.sendj(404,{})
   if x['id']==u['id']:return self.sendj(400,{})
   a,b=sorted((u['id'],x['id']));c=db();r=c.execute("SELECT * FROM channels WHERE is_dm=1 AND name=?",(f'dm-{a}-{b}',)).fetchone()
   if not r:c.execute('INSERT INTO channels(name,is_private,is_dm) VALUES(?,1,1)',(f'dm-{a}-{b}',));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO members VALUES(?,?)',(cid,a));c.execute('INSERT INTO members VALUES(?,?)',(cid,b));c.commit()
   else:cid=r['id']
   c.close();return self.sendj(200,{'channel_id':cid})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=ws(m.group(1))
   if not w:return self.sendj(404,{})
   if not role(w['id'],u['id']):return self.sendj(403,{})
   h=d.get('handle','')
   if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.sendj(400,{})
   c=db()
   if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():c.close();return self.sendj(409,{})
   ids=d.get('user_ids',[])
   if not isinstance(ids,list):c.close();return self.sendj(400,{})
   c.execute('INSERT INTO groups_(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],h,d.get('name',''),json.dumps(ids)));gid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.commit();r=c.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone();c.close()
   return self.sendj(201,{'group':{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'])}})
  if path=='/api/files':
   raw=self.body();ct=self.headers.get('Content-Type','');m=re.search(r'boundary=([^;]+)',ct)
   if len(raw)>11*1024*1024:return self.sendj(413,{})
   if not m:return self.sendj(400,{})
   boundary=m.group(1).encode();part=raw.split(b'--'+boundary)
   data=None;fn='file';typ='application/octet-stream'
   for z in part:
    if b'\r\n\r\n' not in z:continue
    hd,da=z.split(b'\r\n\r\n',1)
    if b'name="file"' in hd:
     q=re.search(br'filename="([^"]*)"',hd);fn=(q.group(1).decode(errors='replace') if q else fn);q=re.search(br'Content-Type:\s*([^\r\n]+)',hd,re.I);typ=(q.group(1).decode() if q else typ);data=da[:-2] if da.endswith(b'\r\n') else da
   if data is None:return self.sendj(400,{})
   if len(data)>10485760:return self.sendj(413,{})
   c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,data,created_at) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),data,now()));fid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return self.sendj(201,{'file':filejson(r)})
  return self.sendj(404,{})
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.sendj(401,{})
  try:d=json.loads(self.body())
  except:return self.sendj(400,{})
  if self.path=='/api/users/me':
   fields={k:v for k,v in d.items() if k in ('display_name','timezone','avatar_url','status_text','status_emoji')}
   if 'display_name' in fields and (not isinstance(fields['display_name'],str) or not 0<len(fields['display_name'])<=80):return self.sendj(400,{})
   if 'timezone' in fields:
    try:ZoneInfo(fields['timezone'])
    except:return self.sendj(400,{})
   limits={'avatar_url':2048,'status_text':100,'status_emoji':64}
   if any(not isinstance(v,str) or len(v)>limits[k] for k,v in fields.items() if k in limits):return self.sendj(400,{})
   c=db()
   for k,v in fields.items():c.execute(f'UPDATE users SET {k}=? WHERE id=?',(v,u['id']))
   c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.close();broadcast_global({'type':'user.updated','user':ujson(r)});return self.sendj(200,{'user':ujson(r)})
  m=re.match(r'/api/channels/(\d+)$',self.path)
  if m:
   ch=getch(m.group(1))
   if not ch or (not cmember(ch['id'],u['id']) and role(ch['workspace_id'],u['id'])!='owner'):return self.sendj(403 if ch else 404,{})
   if 'topic' not in d or not isinstance(d['topic'],str) or len(d['topic'])>250:return self.sendj(400,{})
   c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id']));c.commit();c.close();return self.sendj(200,{'channel':chan(getch(ch['id']))})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   w=ws(m.group(1)); c=db();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not w:return self.sendj(404,{})
   if not g:return self.sendj(404,{})
   if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.sendj(403,{})
   for k in ('name','user_ids'):
    if k in d:
     v=json.dumps(d[k]) if k=='user_ids' else d[k];c.execute(f'UPDATE groups_ SET {k}=? WHERE id=?',(v,g['id']))
   c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone();c.close()
   return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
  return self.sendj(404,{})
 def do_DELETE(self):
  u=self.auth()
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',self.path)
  if m:
   mid=int(m.group(1));emoji=unquote(m.group(2));c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close()
   if not r:return self.sendj(404,{})
   ch=getch(r['channel_id'])
   if not canread(ch,u['id']):return self.sendj(403,{})
   c=db();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));changed=c.total_changes;c.commit();c.close()
   if changed:emit(ch['id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':reactions(mid)})
   return self.sendj(200,{'reactions':reactions(mid)})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   w=ws(m.group(1))
   if not w:return self.sendj(404,{})
   c=db();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone()
   if g and g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':c.close();return self.sendj(403,{})
   if g:c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit()
   c.close();return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def websocket(self):
  tok=parse_qs(urlparse(self.path).query).get('token',[''])[0];u=user(tok)
  if not u:self.send_error(401);return
  key=self.headers.get('Sec-WebSocket-Key');self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  s=self.connection; cid=''.join(secrets.choice('abcdef0123456789') for _ in range(16));c=db();c.execute('INSERT INTO presence VALUES(?,?,?)',(cid,u['id'],time.time()));c.commit();c.close()
  with lock:sockets.append(s); wsstate[s]={'channel':None,'last':0}
  try:
   while True:
    x=ws_recv(s)
    if not x:break
    d=json.loads(x)
    if d.get('type')=='subscribe':
     c=db();mx=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(d['channel_id'],)).fetchone()['n'];c.close();wsstate[s]={'channel':d['channel_id'],'last':mx};ws_send(s,json.dumps({'type':'subscribed','channel_id':d['channel_id'],'head_event_id':mx}))
  except:pass
  finally:
   with lock:
    if s in sockets:sockets.remove(s)
    wsstate.pop(s,None)
   c=db();c.execute('DELETE FROM presence WHERE conn_id=?',(cid,));c.commit();c.close()
class S(ThreadingHTTPServer): allow_reuse_address=True
S(('127.0.0.1',8000+NODE),H).serve_forever()
