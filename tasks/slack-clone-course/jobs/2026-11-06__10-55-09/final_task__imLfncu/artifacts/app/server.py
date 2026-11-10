import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,time,threading
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from zoneinfo import ZoneInfo
DB='/var/lib/huddle/huddle.db'; FILEDIR='/var/lib/huddle/files'
os.makedirs(FILEDIR,exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row
 return c
def init():
 c=db(); c.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,seq INTEGER,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
'''); c.commit(); c.close()
def q(sql,args=(),one=False): 
 c=db()
 try: r=c.execute(sql,args); out=r.fetchone() if one else r.fetchall(); return out
 finally: c.close()
def exe(sql,args=()):
 c=db()
 try: r=c.execute(sql,args); c.commit(); return r.lastrowid
 finally:c.close()
def userj(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chj(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'],'is_archived':bool(x['is_archived'])}
def fj(x): return {k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def tokenuser(t): 
 x=q('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,),True); return x
def auth(h):
 a=h.headers.get('Authorization','')
 if a.startswith('Bearer '):
  return tokenuser(a[7:])
 parsed=urlparse(h.path)
 t=parse_qs(parsed.query).get('token',[''])[0]
 if t.startswith('Bearer '): t=t[7:]
 return tokenuser(t) if t else None
def channel(cid): return q('SELECT * FROM channels WHERE id=?',(cid,),True)
def role(w,u): 
 x=q('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w,u),True); return x['role'] if x else None
def member(cid,u): return bool(q('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,u),True))
def readable(ch,u): return bool(ch and (member(ch['id'],u) if ch['is_private'] else (ch['workspace_id'] is None or role(ch['workspace_id'],u))))
def rx(mid):
 rows=q('SELECT emoji,user_id,seq FROM reactions WHERE message_id=? ORDER BY seq',(mid,)); out=[]
 for r in rows:
  z=next((x for x in out if x['emoji']==r['emoji']),None)
  if not z:z={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(z)
  z['user_ids'].append(r['user_id']);z['count']+=1
 return out
def files(mid):
 return [fj(x) for x in q('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(mid,))]
def msgj(m):
 u=q('SELECT * FROM users WHERE id=?',(m['author_id'],),True)
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':userj(u),'body':m['body'],'parent_id':m['parent_id'],'reply_count':q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],),True)['n'],'created_at':m['created_at'],'edited_at':m['edited_at'],'files':files(m['id']),'reactions':rx(m['id']),'mentions':mentions(m['body'],channel(m['channel_id']),m['author_id'])}
def mentions(body,ch,author):
 ids=[]
 members=[x['user_id'] for x in q('SELECT user_id FROM cm WHERE channel_id=?',(ch['id'],))]
 for m in re.finditer(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  s=m.group(1)
  if s=='channel': cand=members
  elif s=='here': cand=[x['user_id'] for x in q('SELECT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,)) if x['user_id'] in members]
  else:
   g=q('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],s),True) if ch['workspace_id'] else None
   if g: cand=[int(x) for x in json.loads(g['user_ids']) if int(x) in members]
   else:
    u=q('SELECT id FROM users WHERE username=?',(s,),True); cand=[u['id']] if u and (not ch['workspace_id'] or role(ch['workspace_id'],u['id'])) else []
  for i in cand:
   if i!=author and i not in ids:ids.append(i)
 return ids
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
 eid=(n['n']+1 if n else 1)
 if n:c.execute('UPDATE counters SET n=? WHERE channel_id=?',(eid,cid))
 else:c.execute('INSERT INTO counters VALUES(?,?)',(cid,eid))
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,eid,kind,json.dumps(payload)));c.commit();c.close();return eid
def create_msg(cid,uid,body,parent=None,fileids=[]):
 ch=channel(cid)
 if ch['is_archived']: raise ValueError('archived')
 if not readable(ch,uid): raise PermissionError()
 for fid in fileids:
  f=q('SELECT * FROM files WHERE id=? AND uploader_id=?',(fid,uid),True)
  if not f or q('SELECT 1 FROM attachments WHERE file_id=?',(fid,),True): raise ValueError('file')
 c=db(); c.execute('BEGIN IMMEDIATE'); mid=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,uid,body,parent,now())).lastrowid
 for fid in fileids:c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
 c.commit();c.close(); m=q('SELECT * FROM messages WHERE id=?',(mid,),True); emit(cid,'message.reply' if parent else 'message.created',{'message':msgj(m)});return m
def broadcast_global(frame):
 exe('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(NULL,NULL,?,?)',(frame.get('type','global'),json.dumps(frame)))
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def send(self,status,obj=None,headers={}):
  raw=isinstance(obj,(bytes,bytearray)); b=bytes(obj) if raw else (json.dumps(obj,separators=(',',':')).encode() if obj is not None else b'');self.send_response(status)
  self.send_header('Content-Length',str(len(b)))
  if obj is not None and not raw:self.send_header('Content-Type','application/json')
  for k,v in headers.items():self.send_header(k,v)
  self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
 def js(self):
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def u(self):
  x=auth(self); 
  if not x:self.send(401,{'error':'unauthorized'});return None
  return x
 def route(self):
  p=urlparse(self.path); return p.path.strip('/').split('/')
 def do_GET(self):
  if self.path=='/api/health': return self.send(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if self.path.startswith('/api/ws'): return self.ws()
  u=self.u()
  if not u:return
  a=self.route()
  try:
   if a[:3]==['api','auth','me']:return self.send(200,{'user':userj(u)})
   if a[:2]==['api','users']:
    x=q('SELECT * FROM users WHERE id=?',(a[2],),True);return self.send(200,{'user':userj(x)}) if x else self.send(404,{})
   if a[:2]==['api','workspaces'] and len(a)==3:
    w=q('SELECT * FROM workspaces WHERE slug=?',(a[2],),True)
    if not w:return self.send(404,{})
    if not role(w['id'],u['id']):return self.send(403,{})
    cs=[chj(x) for x in q('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))',(w['id'],u['id']))]
    return self.send(200,{'workspace':dict(w),'channels':cs})
   if len(a)>=4 and a[:3]==['api','workspaces',a[2]] and a[3]=='groups':
    w=q('SELECT * FROM workspaces WHERE slug=?',(a[2],),True)
    if not w:return self.send(404,{})
    if not role(w['id'],u['id']):return self.send(403,{})
    gs=[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]
    return self.send(200,{'groups':gs})
   if a[:2]==['api','workspaces'] and len(a)==2:
    ws=[dict(x) for x in q('SELECT * FROM workspaces WHERE id IN (SELECT workspace_id FROM wm WHERE user_id=?)',(u['id'],))];return self.send(200,{'workspaces':ws})
   if a[:2]==['api','channels'] and len(a)>=4 and a[3]=='members':
    c=channel(a[2]);return self.send(403,{}) if not readable(c,u['id']) else self.send(200,{'members':[userj(q('SELECT * FROM users WHERE id=?',(x['user_id'],),True)) for x in q('SELECT user_id FROM cm WHERE channel_id=?',(c['id'],))]})
   if a[:2]==['api','channels'] and len(a)>=4 and a[3]=='messages':
    c=channel(a[2])
    if not readable(c,u['id']):return self.send(403,{})
    ms=[msgj(x) for x in q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(c['id'],))];return self.send(200,{'messages':ms,'next_cursor':None})
   if a[:2]==['api','files']:
    f=q('SELECT * FROM files WHERE id=?',(a[2],),True)
    if not f:return self.send(404,{})
    if len(a)>3:return self.send(200,bytes(f['data']),{'Content-Type':f['content_type'],'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
    return self.send(200,{'file':fj(f)})
   if len(a)>=3 and a[:3]==['api','workspaces',a[2]] and len(a)>3 and a[3]=='groups':
    w=q('SELECT * FROM workspaces WHERE slug=?',(a[2],),True)
    if not role(w['id'],u['id']):return self.send(403,{})
    gs=[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))];return self.send(200,{'groups':gs})
   return self.send(404,{})
  except Exception as e:self.send(500,{'error':str(e)})
 def do_POST(self):
  if self.path=='/api/auth/register':
   d=self.js()
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return self.send(400,{})
   try:uid=exe('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username']))
   except:return self.send(409,{})
   t=secrets.token_urlsafe(24);exe('INSERT INTO tokens VALUES(?,?)',(t,uid));return self.send(201,{'user':userj(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':t})
  if self.path=='/api/auth/login':
   d=self.js();u=q('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password')),True)
   if not u:return self.send(401,{})
   t=secrets.token_urlsafe(24);exe('INSERT INTO tokens VALUES(?,?)',(t,u['id']));return self.send(200,{'user':userj(u),'token':t})
  u=self.u()
  if not u:return
  a=self.route();d={} if (a[:2]==['api','files']) else self.js()
  try:
   if a[:2]==['api','workspaces'] and len(a)==2:
    if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.send(400,{})
    if q('SELECT 1 FROM workspaces WHERE slug=?',(d['slug'],),True):return self.send(409,{})
    c=db();c.execute('BEGIN');wid=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id'])).lastrowid;c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'));cid=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')).lastrowid;c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();c.close();return self.send(201,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(wid,),True)),'general_channel':chj(channel(cid))})
   if a[:2]==['api','workspaces'] and len(a)==4 and a[3]=='channels':
    w=q('SELECT * FROM workspaces WHERE slug=?',(a[2],),True)
    if not w or not role(w['id'],u['id']):return self.send(403,{})
    if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.send(400,{})
    if d.get('is_private') and role(w['id'],u['id'])!='owner':return self.send(403,{})
    if q('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],d['name']),True):return self.send(409,{})
    cid=exe('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));exe('INSERT INTO cm VALUES(?,?)',(cid,u['id']));return self.send(201,{'channel':chj(channel(cid))})
   if len(a)>=3 and a[:3]==['api','channels',a[2]] and a[3] in ('archive','unarchive'):
    c=channel(a[2])
    if not c:return self.send(404,{})
    if not c['workspace_id'] or role(c['workspace_id'],u['id'])!='owner':return self.send(403,{})
    exe('UPDATE channels SET is_archived=? WHERE id=?',(1 if a[3]=='archive' else 0,c['id']))
    return self.send(200,{'channel':chj(channel(c['id']))})
   if len(a)>=3 and a[:3]==['api','channels',a[2]] and a[3]=='join':
    c=channel(a[2])
    if not c:return self.send(404,{})
    if c['is_private']:return self.send(403,{})
    if c['workspace_id'] and not role(c['workspace_id'],u['id']):exe('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(c['workspace_id'],u['id'],'member'))
    exe('INSERT OR IGNORE INTO cm VALUES(?,?)',(c['id'],u['id']));return self.send(200,{'channel':chj(c)})
   if a[:2]==['api','channels'] and a[3]=='messages':
    c=channel(a[2])
    if not c:return self.send(404,{})
    if not readable(c,u['id']):return self.send(403,{})
    body=d.get('body','')
    if c['is_archived'] and body != '/unarchive':return self.send(423,{})
    if not isinstance(body,str) or not body.strip():return self.send(400,{})
    if body.startswith('/') and not body.startswith('//'):
     parts=body.split(' ',1);cmd=parts[0];arg=parts[1].strip() if len(parts)>1 else ''
     if cmd=='/me':
      if not arg:return self.send(400,{})
     elif cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
     elif cmd=='/topic':
      if not arg or len(arg)>250:return self.send(400,{})
      if not member(c['id'],u['id']):return self.send(403,{})
      exe('UPDATE channels SET topic=? WHERE id=?',(arg,c['id']));return self.send(201,{'message':None,'channel':chj(channel(c['id']))})
     elif cmd=='/invite':
      if not member(c['id'],u['id']) or not arg.startswith('@'):return self.send(400 if not arg.startswith('@') else 403,{})
      x=q('SELECT * FROM users WHERE username=?',(arg[1:],),True)
      if not x:return self.send(404,{})
      if c['workspace_id'] and not role(c['workspace_id'],x['id']):return self.send(403,{})
      exe('INSERT OR IGNORE INTO cm VALUES(?,?)',(c['id'],x['id']));return self.send(201,{'message':None,'invited_user':userj(x)})
     elif cmd in ('/archive','/unarchive'):
      if role(c['workspace_id'],u['id'])!='owner':return self.send(403,{})
      exe('UPDATE channels SET is_archived=? WHERE id=?',(cmd=='/archive',c['id']));return self.send(201,{'message':None,'channel':chj(channel(c['id']))})
     else:return self.send(400,{})
    try:m=create_msg(c['id'],u['id'],body,d.get('parent_id'),d.get('file_ids',[]));return self.send(201,{'message':msgj(m)})
    except PermissionError:return self.send(403,{})
    except ValueError:return self.send(400,{})
   if a[:2]==['api','dms']:
    x=q('SELECT * FROM users WHERE id=?',(d.get('user_id'),),True)
    if not x or x['id']==u['id']:return self.send(400 if x else 404,{})
    lo,hi=sorted((u['id'],x['id'])); c=q('SELECT * FROM channels WHERE name=?',(f'dm-{lo}-{hi}',),True)
    if not c:
     cid=exe('INSERT INTO channels(name,is_private,is_dm) VALUES(?,1,1)',(f'dm-{lo}-{hi}',));exe('INSERT INTO cm VALUES(?,?)',(cid,u['id']));exe('INSERT INTO cm VALUES(?,?)',(cid,x['id']))
    else:cid=c['id']
    return self.send(200,{'channel_id':cid})
   if a[:2]==['api','messages'] and len(a)>3 and a[3]=='reactions':
    m=q('SELECT * FROM messages WHERE id=?',(a[2],),True);emoji=d.get('emoji')
    if not m:return self.send(404,{})
    if not emoji:return self.send(400,{})
    if not readable(channel(m['channel_id']),u['id']):return self.send(403,{})
    if not q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji),True):
     seq=(q('SELECT max(seq) s FROM reactions WHERE message_id=?',(m['id'],),True)['s'] or 0)+1;exe('INSERT INTO reactions VALUES(?,?,?,?)',(m['id'],u['id'],emoji,seq));emit(m['channel_id'],'reaction.added',{'message_id':m['id'],'emoji':emoji,'user_id':u['id'],'reactions':rx(m['id'])})
    return self.send(200,{'reactions':rx(m['id'])})
   if a[:2]==['api','files']:
    ct=self.headers.get('Content-Type','');raw=self.body()
    mat=re.search(br'name="file"; filename="([^"]*)".*?Content-Type: ([^\r\n]+)\r\n\r\n(.*?)\r\n--',raw,re.S)
    if not mat:return self.send(400,{})
    fn=mat.group(1).decode(errors='replace');typ=mat.group(2).decode();data=mat.group(3)
    if len(data)>10485760:return self.send(413,{})
    fid=exe('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data));return self.send(201,{'file':fj(q('SELECT * FROM files WHERE id=?',(fid,),True))})
   if len(a)>=3 and a[:3]==['api','workspaces',a[2]] and len(a)==4 and a[3]=='groups':
    w=q('SELECT * FROM workspaces WHERE slug=?',(a[2],),True)
    if not w or not role(w['id'],u['id']):return self.send(403,{})
    h=d.get('handle','')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return self.send(400,{})
    try:gid=exe('INSERT INTO groups_(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w['id'],u['id'],h,d.get('name',''),json.dumps(d.get('user_ids',[]))))
    except:return self.send(409,{})
    return self.send(201,{'group':{'id':gid,'handle':h,'name':d.get('name',''),'member_user_ids':d.get('user_ids',[])}})
  except Exception as e:return self.send(500,{'error':str(e)})
 def do_PATCH(self):
  u=self.u()
  if not u:return
  a=self.route();d=self.js()
  if len(a)==5 and a[1]=='workspaces' and a[3]=='groups': return self.do_PATCH_group(u,a,d)
  if a[:2]==['api','users'] and a[2]=='me':
   allowed={'display_name':(1,80),'timezone':(1,100),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
   for k,v in d.items():
    if k in allowed and (not isinstance(v,str) or not allowed[k][0]<=len(v)<=allowed[k][1] or k=='timezone' and not _tz(v)):return self.send(400,{})
   if d:exe('UPDATE users SET '+','.join(k+'=?' for k in d if k in allowed)+' WHERE id=?',tuple(v for k,v in d.items() if k in allowed)+(u['id'],))
   updated=userj(q('SELECT * FROM users WHERE id=?',(u['id'],),True)); broadcast_global({'type':'user.updated','user':updated})
   return self.send(200,{'user':updated})
  if len(a)>=3 and a[:3]==['api','channels',a[2]]:
   c=channel(a[2]); 
   if not c:return self.send(404,{})
   if not readable(c,u['id']):return self.send(403,{})
   if 'topic' not in d or len(d['topic'])>250:return self.send(400,{})
   exe('UPDATE channels SET topic=? WHERE id=?',(d['topic'],c['id']));return self.send(200,{'channel':chj(channel(c['id']))})
  return self.send(404,{})
 def do_PATCH_group(self,u,a,d):
  w=q('SELECT * FROM workspaces WHERE slug=?',(a[2],),True); g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],a[4]),True) if w else None
  if not w or not g:return self.send(404,{})
  if g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.send(403,{})
  fields=[];vals=[]
  if 'name' in d: fields.append('name=?');vals.append(d['name'])
  if 'user_ids' in d: fields.append('user_ids=?');vals.append(json.dumps(d['user_ids']))
  if fields: exe('UPDATE groups_ SET '+','.join(fields)+' WHERE id=?',tuple(vals+[g['id']]))
  x=q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True)
  return self.send(200,{'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])}})
 def do_DELETE(self):
  u=self.u()
  if not u:return
  a=self.route()
  if len(a)>=5 and a[1]=='messages' and a[3]=='reactions':
   m=q('SELECT * FROM messages WHERE id=?',(a[2],),True);em=unquote(a[4])
   if not m:return self.send(404,{})
   if not em:return self.send(400,{})
   if not readable(channel(m['channel_id']),u['id']):return self.send(403,{})
   if q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],em),True):
    exe('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],em));emit(m['channel_id'],'reaction.removed',{'message_id':m['id'],'emoji':em,'user_id':u['id'],'reactions':rx(m['id'])})
   return self.send(200,{'reactions':rx(m['id'])})
  if len(a)>=5 and a[1]=='workspaces' and a[3]=='groups':
   w=q('SELECT * FROM workspaces WHERE slug=?',(a[2],),True); g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],a[4]),True) if w else None
   if not w:return self.send(404,{})
   if g and g['creator_id']!=u['id'] and role(w['id'],u['id'])!='owner':return self.send(403,{})
   if g: exe('DELETE FROM groups_ WHERE id=?',(g['id'],))
   return self.send(200,{'deleted':True})
  return self.send(404,{})
def _tz(x):
 try:ZoneInfo(x);return True
 except:return False
H.ws_clients={}; H.ws_lock=threading.Lock()
def ws_send(sock,obj):
 b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
 if n<126: h=bytes([0x81,n])
 elif n<65536: h=bytes([0x81,126])+struct.pack('>H',n)
 else: h=bytes([0x81,127])+struct.pack('>Q',n)
 sock.sendall(h+b)
def ws_loop():
 while True:
  time.sleep(.1)
  for s,state in list(H.ws_clients.items()):
   try:
    uid,last,cids=state
    rows=q('SELECT * FROM events WHERE id>? ORDER BY id',(last,))
    for e in rows:
     if e['channel_id'] is None:
      frame=json.loads(e['payload']); ws_send(s,frame); last=e['id']; H.ws_clients[s]=(uid,last,cids); continue
     if int(e['channel_id']) not in cids:
      H.ws_clients[s]=(uid,e['id'],cids); last=e['id']; continue
     p=json.loads(e['payload']); frame={'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**p}
     ws_send(s,frame); last=e['id']; H.ws_clients[s]=(uid,last,cids)
   except: H.ws_clients.pop(s,None)
threading.Thread(target=ws_loop,daemon=True).start()
def ws_method(self):
 u=auth(self)
 if not u:self.send_error(401);return
 key=self.headers.get('Sec-WebSocket-Key');self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
 s=self.connection; conn_id=secrets.token_hex(12); exe('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(conn_id,u['id'],time.time())); latest=q('SELECT max(id) AS n FROM events',(),True); H.ws_clients[s]=(u['id'],latest['n'] or 0,set())
 try:
  while True:
   h=s.recv(2)
   if not h:break
   n=h[1]&127
   if n==126:n=struct.unpack('>H',s.recv(2))[0]
   mask=s.recv(4);data=s.recv(n);data=bytes(x^mask[i%4] for i,x in enumerate(data))
   d=json.loads(data)
   if d.get('type')=='subscribe':
    cid=d['channel_id'];head=q('SELECT n FROM counters WHERE channel_id=?',(cid,),True);ws_send(s,{'type':'subscribed','channel_id':cid,'head_event_id':head['n'] if head else 0});H.ws_clients[s]=(u['id'],latest['n'] or 0,{cid})
 except Exception as exc:
  try:
   open('/tmp/ws-debug.log','a').write(repr(exc)+'\\n')
  except: pass
 finally:
  H.ws_clients.pop(s,None)
  try: exe('DELETE FROM presence WHERE conn_id=?',(conn_id,))
  except: pass
H.ws=ws_method
init()
if __name__=='__main__':
 port=int(sys.argv[1]);H.do_GET_orig=H.do_GET
 def dg(self):
  if self.path.startswith('/api/ws'):return H.ws(self)
  return H.do_GET_orig(self)
 H.do_GET=dg
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
