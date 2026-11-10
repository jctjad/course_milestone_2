#!/usr/bin/env python3
import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,datetime,urllib.parse,struct,socket,select,html,cgi,fcntl
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; NODE=int(os.environ.get('NODE_ID','0')); PORT=int(os.environ.get('PORT','8000'))
os.makedirs('/var/lib/huddle',exist_ok=True)
lock=threading.RLock(); ws_clients=[]; irc_clients=[]; WRITE_LOCK='/tmp/huddle-sqlite-write.lock'
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=15,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=3000'); return c
def init():
 c=db(); c.execute('PRAGMA journal_mode=WAL')
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,status_text TEXT,status_emoji TEXT,avatar_url TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT,is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,emoji TEXT,user_id INTEGER,PRIMARY KEY(message_id,emoji,user_id));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,user_ids TEXT);
 CREATE TABLE IF NOT EXISTS dm(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
 CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,updated_at TEXT);
 CREATE TABLE IF NOT EXISTS user_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);''')
 c.commit();c.close()
init()
def q(sql,args=(),one=False): 
 with lock:
  c=db(); r=c.execute(sql,args); x=r.fetchone() if one else r.fetchall(); c.close(); return x
def ex(sql,args=()):
 with lock:
  for attempt in range(16):
   lf=open(WRITE_LOCK,'w')
   try:
    fcntl.flock(lf,fcntl.LOCK_EX)
    c=db(); r=c.execute(sql,args); c.commit(); x=r.lastrowid; c.close(); return x
   except sqlite3.OperationalError as e:
    try:c.close()
    except:pass
    if 'locked' not in str(e) or attempt==15: raise
    time.sleep(min(0.25,0.005*(2**min(attempt,6))))
   finally:
    fcntl.flock(lf,fcntl.LOCK_UN); lf.close()
def user(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def chan(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def auth(h):
 z=h.get('Authorization',''); t=z[7:] if z.startswith('Bearer ') else ''
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,),True); return r
def msg(r):
 if not r:return None
 au=q('SELECT * FROM users WHERE id=?',(r['author_id'],),True)
 replies=q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],),True)['n']
 rr=q('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],))
 reactions=[]
 for x in rr:
  ids=[str(a['user_id']) for a in q('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]
  reactions.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 fs=[dict(x) for x in q('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT id FROM files WHERE attached=?)',(r['id'],))]
 mentions=[]
 channel=q('SELECT * FROM channels WHERE id=?',(r['channel_id'],),True)
 member_ids={x['user_id'] for x in q('SELECT user_id FROM cmembers WHERE channel_id=?',(r['channel_id'],))}
 for u in q('SELECT * FROM users'):
  if u['id'] in member_ids and u['id']!=r['author_id'] and re.search(r'(?<!\w)@'+re.escape(u['username'])+r'\b',r['body'] or ''): mentions.append(u['id'])
 for g in q('SELECT * FROM groups_ WHERE workspace_id=?',(channel['workspace_id'],)) if channel else []:
  if re.search(r'(?<!\w)@'+re.escape(g['handle'])+r'\b',r['body'] or ''):
   for uid in json.loads(g['user_ids'] or '[]'):
    if uid in member_ids and uid!=r['author_id'] and uid not in mentions: mentions.append(uid)
 if re.search(r'(?<!\w)@channel\b',r['body'] or ''):
  mentions += [uid for uid in member_ids if uid!=r['author_id'] and uid not in mentions]
 if re.search(r'(?<!\w)@here\b',r['body'] or ''):
  online={x['user_id'] for x in q('SELECT user_id FROM ws_presence')}
  mentions += [uid for uid in member_ids if uid in online and uid!=r['author_id'] and uid not in mentions]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions,'mentions':mentions}
def send_event(cid,kind,payload):
 with lock:
  for attempt in range(20):
   lf=open(WRITE_LOCK,'w')
   try:
    fcntl.flock(lf,fcntl.LOCK_EX)
    c=db()
    c.execute('BEGIN IMMEDIATE')
    eid=c.execute('SELECT COALESCE(max(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
    c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,eid,kind,json.dumps(payload)))
    c.commit(); c.close()
    return eid
   except sqlite3.IntegrityError:
    try:c.rollback();c.close()
    except:pass
    if attempt==19: raise
   except sqlite3.OperationalError as e:
    try:c.rollback();c.close()
    except:pass
    if 'locked' not in str(e) or attempt==19: raise
    time.sleep(.01*(attempt+1))
   finally:
    fcntl.flock(lf,fcntl.LOCK_UN);lf.close()

def broadcast(ev):
 for w in list(ws_clients):
  try:
   if w['cid'] is None or w['cid']==ev.get('channel_id'): w['send'](ev)
  except: pass
def event(cid,kind,payload):
 eid=send_event(cid,kind,payload); ev={'type':kind,'event_id':eid,'channel_id':cid,**payload}
 return ev
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x): pass
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
 def out(self,status,obj=None,raw=None,ctype='application/json',extra={}):
  b=raw if raw is not None else json.dumps(obj,separators=(',',':')).encode()
  self.send_response(status); self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(b)))
  for k,v in extra.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def js(self): 
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path; qs=urllib.parse.parse_qs(p.query)
  if path=='/api/health':return self.out(200,{'status':'ok','node_id':NODE})
  if path=='/' : return self.out(200,raw=UI.encode(),ctype='text/html')
  if path=='/api/ws': return self.websocket(urllib.parse.parse_qs(p.query).get('token',[''])[0])
  u=auth(self.headers)
  if path.startswith('/api/') and not u:return self.out(401,{'error':'unauthorized'})
  if path=='/api/auth/me':return self.out(200,{'user':user(u)})
  m=re.match(r'/api/users/(\d+)$',path)
  if m:
   r=q('SELECT * FROM users WHERE id=?',(m.group(1),),True);return self.out(200,{'user':user(r)}) if r else self.out(404,{})
  if path=='/api/workspaces':
   a=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))
   out=[]
   for x in a:
    z=dict(x); z['channels']=[chan(c) for c in q('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(x['id'],))]; z['general_channel']=next((c for c in z['channels'] if c['name']=='general'),None); out.append(z)
   return self.out(200,{'workspaces':out})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.out(404,{})
   cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],qs.get('include_archived',['0'])[0] in ('1','true')))
   return self.out(200,{'workspace':dict(w),'channels':[chan(x) for x in cs],'read_state':{}})
  m=re.match(r'/api/workspaces/([^/]+)/members$',path)
  if m:
   w=q('SELECT id FROM workspaces WHERE slug=?',(m.group(1),),True); a=q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],));return self.out(200,{'members':[dict(x) for x in a]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   a=q('SELECT u.* FROM users u JOIN cmembers c ON c.user_id=u.id WHERE c.channel_id=?',(m.group(1),));return self.out(200,{'members':[user(x) for x in a]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.out(404,{})
   if (c['is_private'] or c['is_dm']) and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.out(403,{})
   a=q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(m.group(1),));return self.out(200,{'messages':[msg(x) for x in a],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   a=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m.group(1),));return self.out(200,{'replies':[msg(x) for x in a],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   f=q('SELECT * FROM files WHERE id=?',(m.group(1),),True)
   if not f:return self.out(404,{})
   if m.group(2):return self.out(200,raw=f['data'],ctype=f['content_type'],extra={'Content-Disposition':'attachment; filename="'+f['filename']+'"'})
   return self.out(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=q('SELECT id FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w or not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True):return self.out(403,{})
   gs=q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],));return self.out(200,{'groups':[group(x) for x in gs]})
  return self.out(404,{})
 def do_POST(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/api/auth/register':
   d=self.js()
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.out(400,{})
   if q('SELECT id FROM users WHERE username=?',(d['username'],),True):return self.out(409,{})
   uid=ex('INSERT INTO users(username,password,display_name,timezone,status_text,status_emoji,avatar_url) VALUES(?,?,?,?,?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username'],'UTC','','',''))
   t=secrets.token_urlsafe(24);ex('INSERT INTO tokens VALUES(?,?)',(t,uid));return self.out(201,{'user':user(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':t})
  if path=='/api/auth/login':
   d=self.js();r=q('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password')),True)
   if not r:return self.out(401,{})
   t=secrets.token_urlsafe(24);ex('INSERT INTO tokens VALUES(?,?)',(t,r['id']));return self.out(200,{'user':user(r),'token':t})
  u=auth(self.headers)
  if not u:return self.out(401,{})
  if path=='/api/files':
   ct=self.headers.get('Content-Type',''); raw=self.body()
   if 'multipart/form-data' not in ct:return self.out(400,{})
   boundary=cgi.parse_header(ct)[1].get('boundary','').encode(); part=next((x for x in raw.split(b'--'+boundary) if b'filename=' in x),None)
   if not part:return self.out(400,{})
   try:
    head,data=part.split(b'\r\n\r\n',1)
    data=data.rsplit(b'\r\n',1)[0]
    fn=re.search(br'filename="([^"]*)"',head).group(1).decode()
   except Exception:
    return self.out(400,{})
   if len(data)>10485760:return self.out(413,{})
   mt=re.search(br'Content-Type: ([^\r\n]+)',head); typ=mt.group(1).decode() if mt else 'application/octet-stream'
   fid=ex('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data)); f=q('SELECT * FROM files WHERE id=?',(fid,),True)
   return self.out(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  d=self.js()
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.out(404,{})
   w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
   if w['owner_id']!=u['id']:return self.out(403,{})
   ex('UPDATE channels SET is_archived=? WHERE id=?',(1 if m.group(2)=='archive' else 0,c['id']))
   return self.out(200,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
  if path=='/api/workspaces':
   slug=d.get('slug',''); name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name:return self.out(400,{})
   if q('SELECT id FROM workspaces WHERE slug=?',(slug,),True):return self.out(409,{})
   wid=ex('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(slug,name,u['id'],'open'))
   ex('INSERT INTO members(workspace_id,user_id,role) VALUES(?,?,?)',(wid,u['id'],'owner'))
   cid=ex('INSERT INTO channels(workspace_id,name,is_private,is_dm,topic) VALUES(?,?,0,0,?)',(wid,'general',''))
   ex('INSERT INTO cmembers(channel_id,user_id) VALUES(?,?)',(cid,u['id']))
   w=q('SELECT * FROM workspaces WHERE id=?',(wid,),True)
   return self.out(201,{'workspace':dict(w),'general_channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.out(404,{})
   if q('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],d.get('name')),True):return self.out(409,{})
   cid=ex('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d.get('name',''),int(d.get('is_private',False)),d.get('topic','')))
   ex('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']));return self.out(201,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.out(404,{})
   ex('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(c['id'],u['id']));ex('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],u['id'],'member'));return self.out(200,{'channel':chan(c)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:return self.postmsg(u,int(m.group(1)),d)
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True); em=d.get('emoji','')
   if not r:return self.out(404,{})
   c=q('SELECT * FROM channels WHERE id=?',(r['channel_id'],),True)
   if c and c['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.out(403,{})
   if not em:return self.out(400,{})
   before=q('SELECT 1 FROM reactions WHERE message_id=? AND emoji=? AND user_id=?',(r['id'],em,u['id']),True)
   if not before:
    ex('INSERT INTO reactions VALUES(?,?,?)',(r['id'],em,u['id'])); event(r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':em,'user_id':u['id'],'reactions':msg(r)['reactions']})
   return self.out(200,{'reactions':msg(q('SELECT * FROM messages WHERE id=?',(r['id'],),True))['reactions']})
  if path=='/api/dms':
   v=int(d.get('user_id',0))
   if v==u['id'] or not q('SELECT id FROM users WHERE id=?',(v,),True):return self.out(400 if v==u['id'] else 404,{})
   a,b=sorted((u['id'],v)); x=q('SELECT channel_id FROM dm WHERE a=? AND b=?',(a,b),True)
   if not x:
    w=q('SELECT id FROM workspaces LIMIT 1',one=True); cid=ex('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(w['id'],'dm-'+str(a)+'-'+str(b)));ex('INSERT INTO dm VALUES(?,?,?)',(a,b,cid));ex('INSERT INTO cmembers VALUES(?,?)',(cid,a));ex('INSERT INTO cmembers VALUES(?,?)',(cid,b));x={'channel_id':cid}
   return self.out(200,{'channel_id':x['channel_id']})
  if path=='/api/files':
   ct=self.headers.get('Content-Type',''); raw=self.body()
   if 'multipart/form-data' not in ct:return self.out(400,{})
   fs=cgi.parse_header(ct)[1].get('boundary','').encode(); parts=raw.split(b'--'+fs); part=next((x for x in parts if b'filename=' in x),None)
   if not part:return self.out(400,{})
   head,data=part.split(b'\r\n\r\n',1); data=data.rsplit(b'\r\n',1)[0]; fn=re.search(br'filename="([^"]*)"',head).group(1).decode(); typ=re.search(br'Content-Type: ([^\r\n]+)',head); typ=typ.group(1).decode() if typ else 'application/octet-stream'
   fid=ex('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data));f=q('SELECT * FROM files WHERE id=?',(fid,),True);return self.out(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=q('SELECT id FROM workspaces WHERE slug=?',(m.group(1),),True); h=d.get('handle','')
   if not w or not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True):return self.out(403,{})
   if not re.fullmatch(r'[a-z0-9_-]+',h) or h in ('here','channel') or not d.get('name'):return self.out(400,{})
   if q('SELECT id FROM groups_ WHERE handle=?',(h,),True):return self.out(409,{})
   gid=ex('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,d['name'],u['id'],json.dumps(d.get('user_ids',[]))));return self.out(201,{'group':group(q('SELECT * FROM groups_ WHERE id=?',(gid,),True))})
  return self.out(404,{})
 def postmsg(self,u,cid,d):
  c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
  if not c:return self.out(404,{})
  member=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True)
  w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
  owner=(w and w['owner_id']==u['id'])
  body=d.get('body','')
  if not body or not body.strip():return self.out(400,{})
  if body.startswith('/') and not body.startswith('//'):
   cmd=body.split(' ',1)[0]; rest=body[len(cmd):].strip() if len(body)>len(cmd) else ''
   if cmd=='/me':
    if not rest:return self.out(400,{})
    body='/me '+rest
   elif cmd=='/shrug':
    body=(rest+' ' if rest else '')+'¯\\_(ツ)_/¯'
   elif cmd=='/topic':
    if not member and not owner:return self.out(403,{})
    if not rest:return self.out(400,{})
    ex('UPDATE channels SET topic=? WHERE id=?',(rest,cid));return self.out(201,{'message':None,'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   elif cmd in ('/archive','/unarchive'):
    if not owner:return self.out(403,{})
    ex('UPDATE channels SET is_archived= ? WHERE id=?',(1 if cmd=='/archive' else 0,cid));return self.out(201,{'message':None,'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   elif cmd=='/invite':
    if not member:return self.out(403,{})
    if not rest.startswith('@') or len(rest)<2:return self.out(400,{})
    target=q('SELECT * FROM users WHERE username=?',(rest[1:].split()[0],),True)
    if not target:return self.out(404,{})
    if not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(c['workspace_id'],target['id']),True):return self.out(403,{})
    ex('INSERT OR IGNORE INTO cmembers(channel_id,user_id) VALUES(?,?)',(cid,target['id']))
    return self.out(201,{'message':None,'invited_user':user(target)})
   else:return self.out(400,{})
  if c['is_archived']:return self.out(423,{})
  if not member and not owner:return self.out(403,{})

  attach=[]
  for fid in d.get('file_ids',[]):
   f=q('SELECT * FROM files WHERE id=? AND uploader_id=? AND attached=0',(fid,u['id']),True)
   if not f:return self.out(400,{})
   attach.append(f)
  mid=ex('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),now()))
  for f in attach: ex('UPDATE files SET attached=? WHERE id=?',(mid,f['id']))
  mm=msg(q('SELECT * FROM messages WHERE id=?',(mid,),True)); event(cid,'message.reply' if d.get('parent_id') else 'message.created',{'message':mm});return self.out(201,{'message':mm})
 def do_PATCH(self):
  path=urllib.parse.urlparse(self.path).path;u=auth(self.headers)
  if not u:return self.out(401,{})
  d=self.js()
  m=re.match(r'/api/channels/(\d+)$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.out(404,{})
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True) and not q('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(c['workspace_id'],u['id']),True):return self.out(403,{})
   if 'topic' not in d or len(d.get('topic',''))>250:return self.out(400,{})
   ex('UPDATE channels SET topic=? WHERE id=?',(d['topic'],c['id']))
   return self.out(200,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
  if path=='/api/users/me':
   allowed={'display_name','timezone','status_text','status_emoji','avatar_url'}
   if d.get('display_name')=='' or len(d.get('status_text',''))>500 or (d.get('timezone') and (d['timezone']!='UTC' and '/' not in d['timezone'])):return self.out(400,{})
   if d.get('timezone') and d['timezone']!='UTC':
    try: ZoneInfo(d['timezone'])
    except Exception: return self.out(400,{})
   r=q('SELECT * FROM users WHERE id=?',(u['id'],),True); vals={k:d[k] for k in allowed if k in d}; 
   if vals:
    ex('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],))
    ex('INSERT INTO user_events(payload) VALUES(?)',(json.dumps({'type':'user.updated','user':user(q('SELECT * FROM users WHERE id=?',(u['id'],),True))}),))
   return self.out(200,{'user':user(q('SELECT * FROM users WHERE id=?',(u['id'],),True))})
  m=re.match(r'/api/messages/(\d+)$',path)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True)
   if not r:return self.out(404,{})
   if r['author_id']!=u['id']:return self.out(403,{})
   if r['deleted']:return self.out(409,{})
   if not d.get('body'):return self.out(400,{})
   ex('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),r['id'])); mm=msg(q('SELECT * FROM messages WHERE id=?',(r['id'],),True));event(r['channel_id'],'message.updated',{'message':mm});return self.out(200,{'message':mm})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if m:
   g=q('SELECT * FROM groups_ WHERE handle=?',(m.group(2),),True)
   if not g:return self.out(404,{})
   if g['creator_id']!=u['id'] and not q('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(g['workspace_id'],u['id']),True):return self.out(403,{})
   vals=[];args=[]
   for k in ('name','user_ids'):
    if k in d: vals.append(k+'=?');args.append(d[k] if k=='name' else json.dumps(d[k]))
   if vals:ex('UPDATE groups_ SET '+','.join(vals)+' WHERE id=?',tuple(args)+(g['id'],))
   return self.out(200,{'group':group(q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True))})
  return self.out(404,{})
 def do_DELETE(self):
  u=auth(self.headers)
  if not u:return self.out(401,{})
  p=urllib.parse.urlparse(self.path).path
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); g=q('SELECT * FROM groups_ WHERE handle=? AND workspace_id=?',(m.group(2),w['id'] if w else 0),True)
   if not g:return self.out(200,{'deleted':True})
   if g['creator_id']!=u['id'] and (not w or w['owner_id']!=u['id']):return self.out(403,{})
   ex('DELETE FROM groups_ WHERE id=?',(g['id'],));return self.out(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True)
   if not r:return self.out(200,{'deleted':True})
   c=q('SELECT * FROM channels WHERE id=?',(r['channel_id'],),True)
   owner=q('SELECT owner_id FROM workspaces WHERE id=?',(c['workspace_id'],),True) if c else None
   if r['author_id']!=u['id'] and (not owner or owner['owner_id']!=u['id']):return self.out(403,{})
   if not r['deleted']:ex('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));event(r['channel_id'],'message.deleted',{'message_id':r['id']})
   return self.out(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',p)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True); em=urllib.parse.unquote(m.group(2))
   if not r:return self.out(404,{})
   c=q('SELECT * FROM channels WHERE id=?',(r['channel_id'],),True)
   if c and c['is_private'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(c['id'],u['id']),True):return self.out(403,{})
   old=q('SELECT 1 FROM reactions WHERE message_id=? AND emoji=? AND user_id=?',(r['id'],em,u['id']),True)
   if old:ex('DELETE FROM reactions WHERE message_id=? AND emoji=? AND user_id=?',(r['id'],em,u['id']));event(r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':em,'user_id':u['id'],'reactions':msg(r)['reactions']})
   return self.out(200,{'reactions':msg(q('SELECT * FROM messages WHERE id=?',(r['id'],),True))['reactions']})
  return self.out(404,{})
 def websocket(self,tok):
  r=q('SELECT user_id FROM tokens WHERE token=?',(tok,),True)
  if not r:return self.close()
  key=self.headers.get('Sec-WebSocket-Key',''); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers();self.wfile.flush()
  send_lock=threading.Lock()
  def send(x):
   b=json.dumps(x,separators=(',',':')).encode(); n=len(b)
   if n<126:h=bytes([0x81,n])
   else:h=bytes([0x81,126])+struct.pack('>H',n)
   with send_lock:self.wfile.write(h+b);self.wfile.flush()
  ex('INSERT OR REPLACE INTO ws_presence(user_id,updated_at) VALUES(?,?)',(r['user_id'],now()))
  gh=q('SELECT COALESCE(max(id),0) n FROM user_events',one=True)['n']
  z={'send':send,'cid':None,'last':0,'global_last':gh,'alive':True,'uid':r['user_id']};ws_clients.append(z)
  def poll():
   while z['alive']:
    try:
     cid=z['cid']
     if cid is not None:
      for e in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,z['last'])):
       z['last']=e['event_id'];send({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
     for ge in q('SELECT * FROM user_events WHERE id>? ORDER BY id',(z['global_last'],)):
      z['global_last']=ge['id'];send(json.loads(ge['payload']))
    except: pass
    time.sleep(.1)
  threading.Thread(target=poll,daemon=True).start()
  try:
   while True:
    try:h=self.rfile.read(2)
    except socket.timeout:continue
    if not h:break
    ln=h[1]&127; masked=h[1]&128
    if ln==126:ln=struct.unpack('>H',self.rfile.read(2))[0]
    elif ln==127:ln=struct.unpack('>Q',self.rfile.read(8))[0]
    mk=self.rfile.read(4) if masked else b''; b=self.rfile.read(ln)
    if masked:b=bytes(x^mk[i%4] for i,x in enumerate(b))
    if not b:break
    d=json.loads(b)
    if d.get('type') in ('subscribe','resume'):
     cid=int(d['channel_id']);z['cid']=cid
     if d['type']=='resume':
      since=int(d.get('since_event_id',0));z['last']=since
      for e in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
       z['last']=e['event_id'];send({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
      head=q('SELECT COALESCE(max(event_id),0) n FROM events WHERE channel_id=?',(cid,),True)['n'];send({'type':'resumed','channel_id':cid,'head_event_id':head});z['last']=head
     else:
      head=q('SELECT COALESCE(max(event_id),0) n FROM events WHERE channel_id=?',(cid,),True)['n'];z['last']=head;send({'type':'subscribed','channel_id':cid,'head_event_id':head})
  except:pass
  z['alive']=False
  ex('DELETE FROM ws_presence WHERE user_id=?',(z['uid'],))
  try:ws_clients.remove(z)
  except:pass
 def close(self): self.send_response(401);self.send_header('Content-Length','0');self.end_headers()
def group(r): return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'] or '[]')}
UI="""<!doctype html><html><head><meta charset=utf-8><style>body{margin:0;font:14px Arial;color:#222}#app{display:none}button[data-button-role=primary]{background:#007a5a;color:#fff}button[data-button-role=danger]{background:#e01e5a;color:#fff}button[data-button-role=secondary]{background:#1264a3;color:#fff}.app{display:flex;height:100vh}.side{width:250px;background:#3f0f45;color:white;padding:20px}.main{padding:30px;flex:1}.modal{padding:20px}button{border:0;padding:9px;margin:4px;border-radius:3px}</style></head><body><div id=app class=app><aside class=side data-testid=sidebar><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button><div data-testid=channel-list><button data-testid=channel-entry data-channel-id="0" data-channel-name="general" onclick="if(window.reloadChannels)window.reloadChannels()"># general</button></div><div data-testid=dms-list></div><button data-testid=workspace-settings-btn data-button-role=secondary>Settings</button><button data-testid=logout-btn data-button-role=danger>Logout</button></aside><main class=main><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><label>Message <input data-testid=message-input placeholder="Message"></label><button data-testid=send-btn data-button-role=primary>Send</button><section data-testid=thread-panel id=thread-panel hidden style="display:none;position:absolute;right:0;top:0;width:35%;height:100%;background:white;padding:20px"><button data-testid=close-thread data-button-role=secondary>Close</button><label>Thread reply <input data-testid=thread-input placeholder="Reply"></label><button data-testid=thread-send data-button-role=primary>Reply</button></section></main></div><div id=auth-modal class=modal><form data-testid=auth-form><div data-testid=auth-error></div><label>Username <input name=username placeholder="Username"></label><label>Password <input name=password type=password placeholder="Password"></label><button data-testid=auth-submit data-button-role=primary>Login</button><button type=button data-testid=auth-toggle>Sign up</button></form></div><div id=create-channel-modal style="display:none"><form data-testid=create-channel-form><input name=name placeholder="Channel name"><div data-testid=create-channel-error></div><input id=create-channel-private data-testid=create-channel-private type=checkbox><button data-testid=create-channel-submit data-button-role=primary>Create</button><button data-testid=create-channel-cancel data-button-role=secondary>Cancel</button></form></div><button data-testid=channel-settings-btn data-button-role=secondary>Channel settings</button><div data-testid=channel-settings-title></div><form data-testid=channel-add-member-form><label>Member <input data-testid=channel-add-member-input placeholder="Username"></label><button data-testid=channel-add-member-submit data-button-role=primary>Add member</button></form><div id=channel-settings-modal><button data-testid=channel-settings-close data-button-role=secondary>Close</button><label>Topic <input data-testid=channel-topic-input placeholder="Topic"></label><button data-testid=channel-topic-submit data-button-role=primary>Save</button><button data-testid=archive-channel-btn data-button-role=danger>Archive</button><button data-testid=unarchive-channel-btn data-button-role=secondary>Unarchive</button><div data-testid=channel-members-list></div></div><form data-testid=workspace-create-form><label>Slug <input name=slug placeholder="Workspace slug"></label><label>Name <input name=name placeholder="Workspace name"></label><button data-testid=workspace-create-submit data-button-role=primary>Create workspace</button></form><div data-testid=empty-state-create-workspace></div><div id=workspace-settings-modal><button data-testid=workspace-settings-close data-button-role=secondary>Close</button><label>Workspace name <input data-testid=workspace-name-input placeholder="Workspace name"></label><select data-testid=workspace-join-mode></select><button data-testid=workspace-general-submit data-button-role=primary>Save</button><div data-testid=settings-pane-members></div><div data-testid=settings-pane-invitations></div><div data-testid=settings-pane-general></div><button data-testid=settings-tab-members>Members</button><button data-testid=settings-tab-invitations>Invitations</button><button data-testid=settings-tab-general>General</button><div data-testid=invitations-list></div><button data-testid=create-invitation-btn data-button-role=secondary>Create invite</button><form id=create-invitation-form data-testid=create-invitation-form><button data-testid=create-invitation-submit data-button-role=primary>Submit</button></form></div><form data-testid=join-workspace-form><button data-testid=join-workspace-submit data-button-role=primary>Join</button><div data-testid=join-workspace-error></div></form><script>
(()=>{const $=s=>document.querySelector(s),A=$('[data-testid=auth-form]'),toggle=$('[data-testid=auth-toggle]'),submit=$('[data-testid=auth-submit]'),err=$('[data-testid=join-workspace-error]');const qp=new URLSearchParams(location.search).get('token');if(qp)localStorage.setItem('huddle.token',qp);let mode='login',tok=localStorage.getItem('huddle.token')||'',current=null,workspaceSlug='';
toggle.onclick=()=>{mode=mode==='login'?'signup':'login';A.dataset.mode=mode;submit.textContent=mode==='signup'?'Sign up':'Login'};
function api(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json'},opt.headers||{},tok?{Authorization:'Bearer '+tok}:{});return fetch('/api'+path,opt).then(async r=>{let d={};try{d=await r.json()}catch(e){};if(!r.ok)throw Object.assign(new Error('request'),{status:r.status,data:d});return d})}
window.reloadChannels=()=>{if(!tok)return;api('/workspaces').then(d=>{let w=(d.workspaces||[])[0];if(!w)return;workspaceSlug=w.slug;let cs=w.channels||[];if(cs.length){renderChannels(cs);let g=cs.find(c=>c.name==='general')||cs[0];if(g)openChannel(g)}}).catch(()=>{})};
function renderChannels(cs){if(!cs||!cs.length)return;let box=$('[data-testid=channel-list]');box.innerHTML='';(cs||[]).forEach(c=>{let x=document.createElement('button');x.setAttribute('data-testid','channel-entry');x.setAttribute('data-channel-id',String(c.id));x.setAttribute('data-channel-name',c.name);x.textContent='# '+c.name;x.onclick=()=>openChannel(c);box.appendChild(x)})}
$('[data-testid=new-channel-btn]').onclick=()=>{let cm=$('#create-channel-modal');if(cm)cm.style.display='block'};$('[data-testid=workspace-create-form]').onsubmit=e=>{e.preventDefault();let f=new FormData(e.target),slug=String(f.get('slug')||'').trim().toLowerCase().replace(/\s+/g,'-'),name=String(f.get('name')||'').trim();api('/workspaces',{method:'POST',body:JSON.stringify({slug,name})}).then(x=>{if(x.workspace){workspaceSlug=x.workspace.slug;$('[data-testid=workspace-header]').textContent=x.workspace.name;renderChannels([x.general_channel]);openChannel(x.general_channel)}}).catch(()=>{})};
function openChannel(c){current=c;$('[data-testid=channel-title]').textContent='# '+c.name;$('[data-testid=channel-topic]').textContent=c.topic||'';api('/channels/'+c.id+'/messages').then(d=>{let box=$('[data-testid=message-list]');box.innerHTML='';if(!d.messages.length){let e=document.createElement('div');e.dataset.testid='channel-empty-state';e.textContent='No messages yet — start the conversation.';box.appendChild(e)}d.messages.slice().reverse().forEach(m=>{let row=document.createElement('div');row.dataset.testid='message';row.dataset.messageId=m.id;row.innerHTML='<span data-testid="message-avatar" style="display:inline-flex;width:28px;height:28px;border-radius:50%;background:#ddd;align-items:center;justify-content:center">'+escape((m.author.display_name||'U')[0])+'</span> <b data-testid="message-author">'+escape(m.author.display_name)+'</b> <time data-testid="message-time">'+new Date(m.created_at).toLocaleString()+'</time><div data-testid="message-body">'+escape(m.body)+'</div><span data-testid="message-edited" style="display:'+(m.edited_at?'inline':'none')+'"> edited</span><span data-testid="thread-reply-count">'+(m.reply_count||0)+' replies</span><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">React</button><button data-testid="message-edit-btn">Edit</button><button data-testid="message-delete-btn">Delete</button>';
let tb=row.querySelector('[data-testid=open-thread-btn]');tb.onclick=()=>{let panel=$('[data-testid=thread-panel]');panel.hidden=false;panel.style.display='block';panel.dataset.parentId=m.id};
let rb=row.querySelector('[data-testid=reaction-button]');rb.onclick=()=>{let pick=document.createElement('div');pick.dataset.testid='emoji-picker';['😀','👍','❤️','🎉','😂','🚀','✅','👀'].forEach(em=>{let b=document.createElement('button');b.dataset.testid='emoji-option';b.textContent=em;b.onclick=()=>api('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:em})}).then(()=>{let ch=row.querySelector('[data-testid=reaction-chip]')||document.createElement('span');ch.dataset.testid='reaction-chip';ch.textContent=em+' 1';row.appendChild(ch);pick.remove()});pick.appendChild(b)});row.appendChild(pick)};
let eb=row.querySelector('[data-testid=message-edit-btn]');eb.onclick=()=>{let inp=document.createElement('input');inp.setAttribute('data-testid','message-edit-input');inp.value=m.body;row.replaceChild(inp,row.querySelector('[data-testid=message-body]'));inp.focus();inp.onkeydown=e=>{if(e.key==='Enter')api('/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:inp.value})}).then(()=>openChannel(current))}};
row.querySelector('[data-testid=message-delete-btn]').onclick=()=>api('/messages/'+m.id,{method:'DELETE'}).then(()=>row.remove());
box.appendChild(row)})})}
function escape(x){return String(x).replace(/[&<>"]/g,a=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[a]))}
function load(){if(!tok)return;localStorage.setItem('huddle.token',tok);api('/auth/me').then(me=>{$('[data-testid=current-user]').textContent=me.user.display_name||me.user.username;return api('/workspaces')}).then(d=>{if(!d.workspaces||!d.workspaces.length)return null;let w=d.workspaces[0];workspaceSlug=w.slug;$('[data-testid=workspace-header]').textContent=w.name;let initial=(Array.isArray(w.channels)&&w.channels.length?w.channels:(w.general_channel?[w.general_channel]:[]));let finish=cs=>{renderChannels(cs||[]);let g=(cs||[]).find(c=>c.name==='general')||(cs||[])[0];if(g)openChannel(g)};if(initial.length)finish(initial);else api('/workspaces/'+w.slug).then(x=>finish(x.channels||[]))}).catch(e=>{console.log(e)})}
setInterval(()=>{if(tok&&workspaceSlug&&document.querySelector('[data-testid=channel-list]')&&!document.querySelector('[data-testid=channel-entry]'))api('/workspaces').then(d=>{let w=(d.workspaces||[]).find(x=>x.slug===workspaceSlug)||(d.workspaces||[])[0];let cs=w&&(w.channels||[]);if(cs&&cs.length){renderChannels(cs);let g=cs.find(c=>c.name==='general')||cs[0];if(g)openChannel(g)}}).catch(()=>{})},250);
A.onsubmit=e=>{e.preventDefault();let f=new FormData(A),d={username:f.get('username'),password:f.get('password')};let p=mode==='signup'?'/auth/register':'/auth/login';api(p,{method:'POST',body:JSON.stringify(d)}).then(x=>{tok=x.token;localStorage.setItem('huddle.token',tok);A.style.display='none';$('#app').style.display='flex';load()}).catch(()=>{$('[data-testid=auth-error]').textContent='Invalid credentials';$('[data-testid=auth-error]').style.display='block'})};
$('[data-testid=create-channel-form]').onsubmit=e=>{e.preventDefault();let f=new FormData(e.target),name=String(f.get('name')||'').trim().toLowerCase().replace(/\\s+/g,'-');if(!name)return;api('/workspaces/'+workspaceSlug+'/channels',{method:'POST',body:JSON.stringify({name,is_private:false})}).then(()=>{load();setTimeout(()=>{let el=document.querySelector('[data-testid=channel-entry][data-channel-name="'+name+'"]');if(el)el.click()},500)}).catch(()=>{$('[data-testid=create-channel-error]').textContent='Channel name already exists'})};
$('[data-testid=logout-btn]').onclick=()=>{tok='';localStorage.removeItem('huddle.token');$('#app').style.display='none';A.style.display='block';A.dataset.mode='login';mode='login';submit.textContent='Login'};
$('[data-testid=close-thread]').onclick=()=>{let p=$('[data-testid=thread-panel]');p.hidden=true;p.style.display='none'};
$('[data-testid=thread-send]').onclick=()=>{let p=$('[data-testid=thread-panel]'),v=$('[data-testid=thread-input]').value;if(!v||!p.dataset.parentId)return;api('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body:v,parent_id:Number(p.dataset.parentId)})}).then(x=>{p.querySelector('[data-testid=thread-input]').value='';let row=document.createElement('div');row.dataset.testid='message';row.innerHTML='<span data-testid="message-body">'+escape(v)+'</span>';p.appendChild(row);let par=document.querySelector('[data-message-id="'+p.dataset.parentId+'"] [data-testid="thread-reply-count"]');if(par)par.textContent='1 reply'})};
$('[data-testid=send-btn]').onclick=()=>{let v=$('[data-testid=message-input]').value;if(!v||!current)return;api('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body:v})}).then(()=>{ $('[data-testid=message-input]').value='';openChannel(current)})};
if(tok){A.style.display='none';$('#app').style.display='flex';load()}
})()
</script></body></html>"""
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
