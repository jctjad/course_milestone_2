#!/usr/bin/env python3
import sys,os,json,re,sqlite3,secrets,hashlib,base64,threading,time,socket,struct
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; PORT=sys.argv[1]; NODE=(int(PORT)-8000 if PORT!='irc' else -1)
clients=[]; clients_lock=threading.Lock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=2,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True); c=db()
 c.executescript('''PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,seq INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY, payload TEXT);''')
 c.commit(); c.close()
def userjson(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chanjson(r): return {k:(bool(r[k]) if k in ('is_private','is_dm','is_archived') else r[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def getuser(uid):
 c=db(); r=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return r
def auth(h):
 if not h.startswith('Bearer '): return None
 c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(h[7:],)).fetchone(); c.close(); return r
def msgjson(r):
 a=getuser(r['author_id']); c=db()
 rs=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(r['id'],)).fetchall()
 replies=c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0]; c.close()
 ch=c.fetchone() if False else None
 c2=db(); ch=c2.execute('select * from channels where id=?',(r['channel_id'],)).fetchone(); c2.close()
 mentions=extract_mentions(r['body'],ch,r['author_id']) if ch else []
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userjson(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files_for(r['id']),'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs],'mentions':mentions}
def validate_timezone(tz):
 try:
  from zoneinfo import ZoneInfo
  ZoneInfo(tz); return True
 except: return False
def validate_profile(fields):
 limits={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048)}
 for k,(lo,hi) in limits.items():
  if k in fields and (not isinstance(fields[k],str) or not (lo<=len(fields[k])<=hi)): raise ValueError()
 if 'timezone' in fields and (not isinstance(fields['timezone'],str) or not validate_timezone(fields['timezone'])): raise ValueError()
 return True
def update_profile(uid,fields):
 allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; fields={k:v for k,v in fields.items() if k in allowed}
 validate_profile(fields)
 if fields:
  c=db(); c.execute('update users set '+','.join(k+'=?' for k in fields)+' where id=?',list(fields.values())+[uid]); c.commit(); r=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return r
 return getuser(uid)
def broadcast_global(frame):
 c=db(); c.execute('insert into global_events(payload) values(?)',(json.dumps(frame),)); c.commit(); c.close(); notify_nodes(0)
def global_pump():
 last=0
 while True:
  try:
   c=db(); rows=c.execute('select id,payload from global_events where id>? order by id',(last,)).fetchall(); c.close()
   for r in rows:
    last=r['id']; raw=json.loads(r['payload'])
    with clients_lock:
     for item in list(clients):
      try: ws_send(item[0],json.dumps(raw).encode())
      except:
       try: clients.remove(item); item[0].close()
       except: pass
  except: pass
  time.sleep(.1)
def validate_group_handle(handle):
 return bool(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',str(handle))) and handle not in ('here','channel')
def group_to_json(r):
 return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':[int(x) for x in (r['user_ids'] or '').split(',') if x]}
def get_group(wid,handle):
 c=db(); r=c.execute('select * from groups where workspace_id=? and handle=?',(wid,handle)).fetchone(); c.close(); return r
def create_group(wid,creator,handle,name,ids):
 c=db()
 try:
  c.execute('insert into groups(workspace_id,creator_id,handle,name,user_ids) values(?,?,?,?,?)',(wid,creator,handle,name,','.join(str(int(x)) for x in ids))); c.commit()
 except sqlite3.IntegrityError: c.close(); raise
 r=c.execute('select * from groups where workspace_id=? and handle=?',(wid,handle)).fetchone(); c.close(); return r
def update_group(group,**fields):
 c=db(); vals=[]; args=[]
 if 'name' in fields: vals.append('name=?'); args.append(fields['name'])
 if 'user_ids' in fields: vals.append('user_ids=?'); args.append(','.join(str(int(x)) for x in fields['user_ids']))
 if vals:c.execute('update groups set '+','.join(vals)+' where id=?',args+[group['id']]);c.commit()
 r=c.execute('select * from groups where id=?',(group['id'],)).fetchone();c.close();return r
def delete_group(group):
 c=db(); c.execute('delete from groups where id=?',(group['id'],));c.commit();c.close()
def online_user_ids():
 c=db(); rs=c.execute('select distinct user_id from presence where last_seen>?',(time.time()-1.5,)).fetchall();c.close();return {r[0] for r in rs}
def extract_mentions(body,channel,author_id):
 c=db(); out=[]
 members={r[0] for r in c.execute('select user_id from cm where channel_id=?',(channel['id'],)).fetchall()}
 w=channel['workspace_id']
 for handle in re.findall(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  if handle=='channel': ids=members
  elif handle=='here': ids=members & online_user_ids()
  else:
   g=c.execute('select user_ids from groups where workspace_id=? and handle=?',(w,handle.lower())).fetchone() if w else None
   if g: ids={int(x) for x in (g[0] or '').split(',') if x}
   else:
    z=c.execute('select id from users where username=?',(handle,)).fetchone(); ids={z[0]} if z else set()
  out.extend(x for x in ids if x!=author_id and x in members)
 c.close(); return list(dict.fromkeys(out))
def file_to_json(r):
 return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def get_file(fid):
 c=db(); r=c.execute('select * from files where id=?',(fid,)).fetchone(); c.close(); return r
def files_for(mid):
 c=db(); rs=c.execute('select f.* from files f join message_files mf on f.id=mf.file_id where mf.message_id=?',(mid,)).fetchall(); c.close(); return [file_to_json(x) for x in rs]
def save_file(uid,filename,content_type,data):
 c=db(); c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(uid,filename,content_type,len(data),now(),data)); fid=c.execute('select last_insert_rowid()').fetchone()[0]; c.commit(); r=c.execute('select * from files where id=?',(fid,)).fetchone(); c.close(); return r
def attach_files(mid,uid,ids):
 c=db()
 for fid in ids:
  r=c.execute('select * from files where id=?',(int(fid),)).fetchone()
  if not r or r['uploader_id']!=uid or c.execute('select 1 from message_files where file_id=?',(int(fid),)).fetchone(): c.close(); raise ValueError()
 for fid in ids:c.execute('insert into message_files(message_id,file_id) values(?,?)',(mid,int(fid)))
 c.commit(); c.close()
def get_or_create_dm(a,b):
 lo,hi=sorted((int(a),int(b))); c=db()
 r=c.execute("select * from channels where is_dm=1 and name=?",(f'dm-{lo}-{hi}',)).fetchone()
 if not r:
  c.execute("insert into channels(name,is_private,is_dm,workspace_id) values(?,1,1,NULL)",(f'dm-{lo}-{hi}',)); cid=c.execute('select last_insert_rowid()').fetchone()[0]
  c.execute('insert into cm values(?,?)',(cid,lo)); c.execute('insert into cm values(?,?)',(cid,hi)); c.commit(); r=c.execute('select * from channels where id=?',(cid,)).fetchone()
 c.close(); return r
def reactions_for(message_id):
 c=db()
 rows=c.execute('select emoji,user_id from reactions where message_id=? order by rowid',(message_id,)).fetchall()
 c.close()
 out=[]
 for r in rows:
  x=next((z for z in out if z['emoji']==r['emoji']),None)
  if not x:
   x={'emoji':r['emoji'],'count':0,'user_ids':[]}; out.append(x)
  x['user_ids'].append(r['user_id']); x['count']=len(x['user_ids'])
 return out
def add_reaction(message_id,user_id,emoji):
 c=db()
 cur=c.execute('insert or ignore into reactions(message_id,user_id,emoji) values(?,?,?)',(message_id,user_id,emoji))
 c.commit(); changed=cur.rowcount>0; c.close()
 return changed
def remove_reaction(message_id,user_id,emoji):
 c=db(); cur=c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(message_id,user_id,emoji))
 c.commit(); changed=cur.rowcount>0; c.close()
 return changed
def edit_message(message, body):
 c=db(); stamp=now(); c.execute('update messages set body=?,edited_at=? where id=?',(body,stamp,message['id'])); c.commit()
 r=c.execute('select * from messages where id=?',(message['id'],)).fetchone(); c.close(); return r
def delete_message(message):
 c=db(); cur=c.execute('update messages set deleted=1 where id=? and deleted=0',(message['id'],)); c.commit(); changed=cur.rowcount>0; c.close(); return changed
def run_slash_command(channel,user,body):
 if body.startswith('//'): return ('message',body)
 parts=body[1:].split(' ',1); cmd=parts[0].lower(); arg=parts[1].strip() if len(parts)>1 else ''
 if cmd=='me':
  if not arg: raise ValueError()
  return ('message',body)
 if cmd=='shrug': return ('message',(arg+' ' if arg else '')+'¯\\_(ツ)_/¯')
 if cmd=='topic': return ('topic',arg)
 if cmd=='invite':
  if not arg.startswith('@') or len(arg)<=1: raise ValueError()
  return ('invite',arg[1:])
 if cmd in ('archive','unarchive'): return (cmd,'')
 raise ValueError()
def get_by_name(n):
 c=db(); r=c.execute('select * from users where username=?',(n,)).fetchone(); c.close(); return r
def emit(cid,kind,payload):
 c=db(); c.execute('update channels set seq=seq+1 where id=?',(cid,)); eid=c.execute('select seq from channels where id=?',(cid,)).fetchone()[0]
 c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(cid,eid,kind,json.dumps(payload))); c.commit(); c.close(); notify_nodes(cid)
 return {'type':kind,'event_id':eid,'channel_id':cid,**payload}
def broadcast(frame):
 raw=json.dumps(frame).encode()
 with clients_lock:
  for item in list(clients):
   s,chans=item
   if chans and frame.get('channel_id') not in chans: continue
   try: ws_send(s,raw)
   except: 
    try: clients.remove(item); s.close()
    except: pass

def redis_cmd(parts):
 out=b'*'+str(len(parts)).encode()+b'\r\n'
 for x in parts:
  x=str(x).encode(); out+=b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n'
 return out
def notify_nodes(channel_id):
 try:
  z=socket.create_connection(('127.0.0.1',6379),timeout=.15)
  z.sendall(redis_cmd(['PUBLISH','huddle:events',str(channel_id)])); z.close()
 except: pass
def redis_listener():
 while True:
  z=None
  try:
   z=socket.create_connection(('127.0.0.1',6379),timeout=1)
   z.sendall(redis_cmd(['SUBSCRIBE','huddle:events']))
   z.settimeout(1)
   while True:
    b=z.recv(4096)
    if not b: raise OSError()
  except:
   try:
    if z:z.close()
   except:pass
   time.sleep(.5)

def pump():
 last=0
 while True:
  try:
   c=db(); rows=c.execute('select rowid as _rid, channel_id, event_id, kind, payload from events where rowid>? order by rowid', (last,)).fetchall(); c.close()
   for r in rows:
    last=r['_rid']; broadcast({'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id'],**json.loads(r['payload'])})
  except: pass
  time.sleep(.1)
def ws_send(s,b):
 h=bytes([0x81]); n=len(b)
 if n<126: h+=bytes([n])
 elif n<65536: h+=bytes([126])+struct.pack('>H',n)
 else: h+=bytes([127])+struct.pack('>Q',n)
 s.sendall(h+b)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x): pass
 def send(self,code,obj=None,ctype='application/json',extra=None):
  b=b'' if obj is None else (obj if isinstance(obj,bytes) else json.dumps(obj).encode())
  self.send_response(code); self.send_header('Content-Length',str(len(b))); self.send_header('Content-Type',ctype)
  for k,v in (extra or {}).items(): self.send_header(k,v)
  self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def req(self,method,path):
  u=auth(self.headers.get('Authorization','')); 
  if not u: self.send(401,{'error':'unauthorized'}); return None
  return u
 def do_GET(self):
  if self.path.split('?')[0]=='/':
   try:
    b=open('/app/static/index.html','rb').read(); self.send(200,b,'text/html; charset=utf-8')
   except: self.send(404,{})
   return
  if self.path=='/api/health': return self.send(200,{'status':'ok','node_id':NODE})
  if self.path.startswith('/api/ws'): return self.websocket()
  u=self.req('GET',self.path)
  if not u:return
  p=self.path.split('?')[0]; c=db()
  if p=='/api/auth/me': return self.send(200,{'user':userjson(u)})
  um=re.match(r'/api/users/(\d+)$',p)
  if um:
   r=getuser(int(um[1]))
   if not r:return self.send(404,{})
   return self.send(200,{'user':userjson(r)})
  fm=re.match(r'/api/files/(\d+)(/download)?$',p)
  if fm:
   f=get_file(fm[1])
   if not f:return self.send(404,{})
   if fm[2]: return self.send(200,f['data'],f['content_type'],{'Content-Disposition':'attachment; filename="'+f['filename']+'"'})
   return self.send(200,{'file':file_to_json(f)})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm[1],)).fetchone()
   if not w:return self.send(404,{})
   if not c.execute('select 1 from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.send(403,{})
   rs=c.execute('select * from groups where workspace_id=? order by id',(w['id'],)).fetchall()
   return self.send(200,{'groups':[group_to_json(x) for x in rs]})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm[1],)).fetchone()
   if not w:return self.send(404,{})
   if not c.execute('select 1 from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.send(403,{})
   if not validate_group_handle(d.get('handle','')):return self.send(400,{})
   ids=d.get('user_ids') or []
   try:r=create_group(w['id'],u['id'],d['handle'],d.get('name',''),ids)
   except sqlite3.IntegrityError:return self.send(409,{})
   return self.send(201,{'group':group_to_json(r)})
  if p=='/api/workspaces':
   rs=c.execute('select w.* from workspaces w join wm on w.id=wm.workspace_id where wm.user_id=? order by w.id',(u['id'],)).fetchall()
   return self.send(200,{'workspaces':[dict(x) for x in rs]})
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m[1],)).fetchone()
   if not w:return self.send(404,{})
   if not c.execute('select 1 from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.send(403,{})
   cs=c.execute('select * from channels where workspace_id=? and is_archived=0 and (is_private=0 or id in(select channel_id from cm where user_id=?))',(w['id'],u['id'])).fetchall()
   return self.send(200,{'workspace':dict(w),'channels':[chanjson(x) for x in cs]})
  m=re.match(r'/api/channels/(\d+)/members',p)
  if m:
   rs=c.execute('select u.* from users u join cm on u.id=cm.user_id where cm.channel_id=?',(m[1],)).fetchall(); return self.send(200,{'members':[userjson(x) for x in rs]})
  m=re.match(r'/api/messages/(\d+)/replies$',p)
  if m:
   parent=c.execute('select * from messages where id=? and deleted=0',(m[1],)).fetchone()
   if not parent:return self.send(404,{})
   rs=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(m[1],)).fetchall()
   return self.send(200,{'replies':[msgjson(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=c.execute('select * from channels where id=?',(m[1],)).fetchone()
   if not ch:return self.send(404,{})
   if ch['is_private'] and not c.execute('select 1 from cm where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.send(403,{})
   rs=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc limit 200',(ch['id'],)).fetchall(); return self.send(200,{'messages':[msgjson(x) for x in rs],'next_cursor':None})
  c.close(); return self.send(404,{})
 def do_POST(self):
  if self.path=='/api/auth/register':
   try:d=self.body(); un=d['username']; pw=d['password']
   except:return self.send(400,{})
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.send(400,{})
   c=db()
   try:c.execute('insert into users(username,password,display_name) values(?,?,?)',(un,pw,d.get('display_name') or un));c.commit()
   except sqlite3.IntegrityError:return c.close() or self.send(409,{})
   r=c.execute('select * from users where username=?',(un,)).fetchone(); t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id'],));c.commit();c.close();return self.send(201,{'user':userjson(r),'token':t})
  if self.path=='/api/auth/login':
   d=self.body();c=db();r=c.execute('select * from users where username=? and password=?',(d.get('username'),d.get('password'))).fetchone()
   if not r:c.close();return self.send(401,{})
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id'],));c.commit();c.close();return self.send(200,{'user':userjson(r),'token':t})
  u=self.req('POST',self.path)
  if not u:return
  if self.path=='/api/files':
   n=int(self.headers.get('Content-Length','0'))
   if n>10485760+1048576:return self.send(413,{})
   raw=self.rfile.read(n); ct=self.headers.get('Content-Type','')
   m=re.search(r'boundary=([^;]+)',ct)
   if not m:return self.send(400,{})
   boundary=('--'+m.group(1).strip('"')).encode()
   part=next((x for x in raw.split(boundary) if b'Content-Disposition' in x and b'name="file"' in x),None)
   if not part:return self.send(400,{})
   head,sep,data=part.partition(bytes([13,10,13,10]))
   if data.endswith(bytes([13,10])): data=data[:-2]
   if len(data)>10485760:return self.send(413,{})
   fm=re.search(br'filename="([^"]*)"',head)
   tm=re.search(br'Content-Type:[ \t]*([^\r\n]+)',head,re.I)
   if not fm:return self.send(400,{})
   f=save_file(u['id'],fm.group(1).decode(errors='replace'),(tm.group(1).decode().strip() if tm else 'application/octet-stream'),data)
   return self.send(201,{'file':file_to_json(f)})
  d=self.body(); c=db(); p=self.path
  if p=='/api/dms':
   try: other=int(d.get('user_id'))
   except: c.close(); return self.send(400,{})
   if other==u['id']: c.close(); return self.send(400,{})
   if not getuser(other): c.close(); return self.send(404,{})
   c.close(); return self.send(200,{'channel_id':get_or_create_dm(u['id'],other)['id']})
  m=re.match(r'/api/messages/(\d+)/reactions$',p)
  if m:
   mid=int(m[1]); r=c.execute('select * from messages where id=? and deleted=0',(mid,)).fetchone()
   if not r:return self.send(404,{})
   ch=c.execute('select * from channels where id=?',(r['channel_id'],)).fetchone()
   if ch['is_private'] and not c.execute('select 1 from cm where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.send(403,{})
   emoji=str(d.get('emoji',''))
   if not emoji:return self.send(400,{})
   changed=add_reaction(mid,u['id'],emoji); rs=reactions_for(mid)
   if changed:
    frame=emit(ch['id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':rs})
   return self.send(200,{'reactions':rs})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm[1],)).fetchone()
   if not w:return self.send(404,{})
   if not c.execute('select 1 from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.send(403,{})
   if not validate_group_handle(d.get('handle','')):return self.send(400,{})
   try:g=create_group(w['id'],u['id'],d.get('handle'),d.get('name',''),d.get('user_ids') or [])
   except (sqlite3.IntegrityError,ValueError):return self.send(409,{})
   return self.send(201,{'group':group_to_json(g)})
  if p=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.send(400,{})
   try:c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d['name'],u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into wm values(?,?,?)',(wid,u['id'],'owner'));c.execute("insert into channels(workspace_id,name) values(?,?)",(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();w=c.execute('select * from workspaces where id=?',(wid,)).fetchone();ch=c.execute('select * from channels where id=?',(cid,)).fetchone();c.close();return self.send(201,{'workspace':dict(w),'general_channel':chanjson(ch)})
   except sqlite3.IntegrityError:c.close();return self.send(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m[1],)).fetchone(); role=c.execute('select role from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not w:return self.send(404,{})
   if not role:return self.send(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.send(400,{})
   if d.get('is_private') and role['role']!='owner':return self.send(403,{})
   if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],d['name'])).fetchone():return self.send(409,{})
   c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit();ch=c.execute('select * from channels where id=?',(cid,)).fetchone();c.close();return self.send(201,{'channel':chanjson(ch)})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',p)
  if m:
   ch=c.execute('select * from channels where id=?',(m[1],)).fetchone()
   if not ch:return self.send(404,{})
   role=c.execute('select role from wm where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
   if not role or role['role']!='owner':return self.send(403,{})
   val=1 if m[2]=='archive' else 0
   c.execute('update channels set is_archived=? where id=?',(val,ch['id'])); c.commit()
   ch=c.execute('select * from channels where id=?',(ch['id'],)).fetchone(); c.close()
   return self.send(200,{'channel':chanjson(ch)})
  m=re.match(r'/api/channels/(\d+)/join',p)
  if m:
   ch=c.execute('select * from channels where id=?',(m[1],)).fetchone()
   if not ch:return self.send(404,{})
   if ch['is_private']:return self.send(403,{})
   if not c.execute('select 1 from wm where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone():c.execute('insert into wm values(?,?,?)',(ch['workspace_id'],u['id'],'member'))
   c.execute('insert or ignore into cm values(?,?)',(ch['id'],u['id']));c.commit();c.close();return self.send(200,{'channel':chanjson(ch)})
  m=re.match(r'/api/messages/(\d+)/replies$',p)
  if m:
   parent=c.execute('select * from messages where id=? and deleted=0',(m[1],)).fetchone()
   if not parent:return self.send(404,{})
   rs=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(m[1],)).fetchall()
   return self.send(200,{'replies':[msgjson(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=c.execute('select * from channels where id=?',(m[1],)).fetchone()
   if not ch:return self.send(404,{})
   member=c.execute('select 1 from cm where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone()
   if not member:return self.send(403,{})
   body=d.get('body','')
   if not body.strip():return self.send(400,{})
   if body.startswith('/') and not body.startswith('//'):
    try: action,arg=run_slash_command(ch,u,body)
    except ValueError:return self.send(400,{})
    if action=='unarchive' or not ch['is_archived']:
     if action=='message': body=arg
     elif action=='topic':
      if not arg or len(arg)>250:return self.send(400,{})
      c.execute('update channels set topic=? where id=?',(arg,ch['id']));c.commit(); ch=c.execute('select * from channels where id=?',(ch['id'],)).fetchone();c.close()
      return self.send(201,{'message':None,'channel':chanjson(ch)})
     elif action=='invite':
      target=c.execute('select * from users where username=?',(arg,)).fetchone()
      if not target:return self.send(404,{})
      if not c.execute('select 1 from wm where workspace_id=? and user_id=?',(ch['workspace_id'],target['id'])).fetchone():return self.send(403,{})
      c.execute('insert or ignore into cm values(?,?)',(ch['id'],target['id']));c.commit();c.close()
      return self.send(201,{'message':None,'invited_user':userjson(target)})
     elif action in ('archive','unarchive'):
      role=c.execute('select role from wm where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
      if not role or role['role']!='owner':return self.send(403,{})
      val=1 if action=='archive' else 0
      c.execute('update channels set is_archived=? where id=?',(val,ch['id']));c.commit();ch=c.execute('select * from channels where id=?',(ch['id'],)).fetchone();c.close()
      return self.send(201,{'message':None,'channel':chanjson(ch)})
    else:return self.send(423,{})
   if ch['is_archived']:return self.send(423,{})
   ids=d.get('file_ids') or []
   try: ids=[int(x) for x in ids]
   except: return self.send(400,{})
   for fid in ids:
    fr=c.execute('select * from files where id=?',(fid,)).fetchone()
    if not fr or fr['uploader_id']!=u['id'] or c.execute('select 1 from message_files where file_id=?',(fid,)).fetchone(): return self.send(400,{})
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],u['id'],body,d.get('parent_id'),now()));mid=c.execute('select last_insert_rowid()').fetchone()[0]
   for fid in ids:c.execute('insert into message_files(message_id,file_id) values(?,?)',(mid,fid))
   r=c.execute('select * from messages where id=?',(mid,)).fetchone();c.commit();c.close(); frame=emit(ch['id'],'message.reply' if d.get('parent_id') else 'message.created',{'message':msgjson(r)});return self.send(201,{'message':msgjson(r)})
  c.close();return self.send(404,{})
 def do_DELETE(self):
  u=self.req('DELETE',self.path)
  if not u:return
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if gm:
   c=db(); w=c.execute('select * from workspaces where slug=?',(gm[1],)).fetchone(); g=get_group(w['id'],gm[2]) if w else None
   if not g:return self.send(200,{'deleted':True})
   role=c.execute('select role from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):return self.send(403,{})
   delete_group(g); return self.send(200,{'deleted':True})
  dm=re.match(r'/api/messages/(\d+)$',self.path)
  if dm:
   c=db()
   r=c.execute('select * from messages where id=?',(dm[1],)).fetchone()
   if not r:c.close();return self.send(404,{})
   ch=c.execute('select * from channels where id=?',(r['channel_id'],)).fetchone()
   role=c.execute('select role from wm where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
   if r['author_id']!=u['id'] and (not role or role['role']!='owner'):
    c.close();return self.send(403,{})
   c.close()
   if not r['deleted']:
    delete_message(r); emit(ch['id'],'message.deleted',{'message_id':r['id']})
   return self.send(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',self.path)
  if not m:return self.send(404,{})
  mid=int(m[1]); emoji=__import__('urllib.parse',fromlist=['unquote']).unquote(m[2])
  c=db(); r=c.execute('select * from messages where id=? and deleted=0',(mid,)).fetchone()
  if not r:c.close();return self.send(404,{})
  ch=c.execute('select * from channels where id=?',(r['channel_id'],)).fetchone()
  allowed=not ch['is_private'] or c.execute('select 1 from cm where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone()
  c.close()
  if not allowed:return self.send(403,{})
  changed=remove_reaction(mid,u['id'],emoji); rs=reactions_for(mid)
  if changed: emit(ch['id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':rs})
  return self.send(200,{'reactions':rs})
 def do_PATCH(self):
  u=self.req('PATCH',self.path)
  if not u:return
  d=self.body();c=db()
  if self.path=='/api/users/me':
   try:r=update_profile(u['id'],d)
   except ValueError:return self.send(400,{})
   obj=userjson(r); broadcast_global({'type':'user.updated','user':obj})
   return self.send(200,{'user':obj})
  mm=re.match(r'/api/messages/(\d+)$',self.path)
  if mm:
   r=c.execute('select * from messages where id=?',(mm[1],)).fetchone()
   if not r:return self.send(404,{})
   if r['deleted']:return self.send(409,{})
   if r['author_id']!=u['id']:return self.send(403,{})
   body=d.get('body','')
   if not str(body).strip():return self.send(400,{})
   c.close(); r=edit_message(r,body); frame=emit(r['channel_id'],'message.updated',{'message':msgjson(r)})
   return self.send(200,{'message':msgjson(r)})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm[1],)).fetchone(); g=get_group(w['id'],gm[2]) if w else None
   if not w:return self.send(404,{})
   if not g:return self.send(404,{})
   role=c.execute('select role from wm where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):return self.send(403,{})
   return self.send(200,{'group':group_to_json(update_group(g,**d))})
  m=re.match(r'/api/channels/(\d+)',self.path)
  if m:
   ch=c.execute('select * from channels where id=?',(m[1],)).fetchone(); role=c.execute('select role from wm where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone() if ch else None
   if not ch:return self.send(404,{})
   if not role or (not c.execute('select 1 from cm where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone() and role['role']!='owner'):return self.send(403,{})
   if len(d.get('topic',''))>250:return self.send(400,{})
   c.execute('update channels set topic=? where id=?',(d.get('topic',''),ch['id']));c.commit();ch=c.execute('select * from channels where id=?',(ch['id'],)).fetchone();c.close();return self.send(200,{'channel':chanjson(ch)})
  return self.send(404,{})
 def do_OPTIONS(self): self.send(204)
 def websocket(self):
  tok=self.path.split('token=',1)[-1]; u=auth('Bearer '+tok)
  if not u:return self.send(401); 
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send(400)
  s=self.connection; accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  chans=set(); conn_id=secrets.token_hex(12)
  pc=db(); pc.execute('insert or replace into presence(conn_id,user_id,last_seen) values(?,?,?)',(conn_id,u['id'],time.time())); pc.commit(); pc.close()
  with clients_lock:clients.append((s,chans))
  try:
   while True:
    h=self.rfile.read(2)
    if not h:break
    opcode=h[0]&15; n=h[1]&127
    if n==126:n=struct.unpack('>H',self.rfile.read(2))[0]
    elif n==127:n=struct.unpack('>Q',self.rfile.read(8))[0]
    mask=self.rfile.read(4) if h[1]&128 else b''
    data=self.rfile.read(n)
    if mask:data=bytes(data[i]^mask[i%4] for i in range(n))
    if opcode==8: break
    if opcode==1:
     try:
      q=json.loads(data.decode())
      if q.get('type')=='subscribe':
       cid=int(q['channel_id']); chans.add(cid)
       c=db(); row=c.execute('select seq from channels where id=?',(cid,)).fetchone(); c.close()
       ws_send(s,json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':row[0] if row else 0}).encode())
      elif q.get('type')=='resume':
       cid=int(q['channel_id']); since=int(q.get('since_event_id',0)); chans.add(cid)
       c=db()
       rows=c.execute('select event_id,kind,payload from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall()
       row=c.execute('select seq from channels where id=?',(cid,)).fetchone()
       c.close()
       for ev in rows:
        ws_send(s,json.dumps({'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid,**json.loads(ev['payload'])}).encode())
       ws_send(s,json.dumps({'type':'resumed','channel_id':cid,'head_event_id':row[0] if row else 0}).encode())
     except: pass
  except:pass
  pc=db(); pc.execute('delete from presence where conn_id=?',(conn_id,)); pc.commit(); pc.close()
  with clients_lock:
   for item in list(clients):
    if item[0] is s: clients.remove(item)

irc_nicks=set(); irc_lock=threading.Lock(); irc_clients=[]
def irc_channel_name(workspace,channel):
 return '#'+workspace['slug']+'/'+channel['name']
def irc_find_channel(name):
 if not name.startswith('#') or '/' not in name[1:]: return None
 slug,nm=name[1:].split('/',1); c=db()
 r=c.execute('select c.* from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(slug,nm)).fetchone(); c.close(); return r
def irc_pump():
 last=0
 while True:
  try:
   c=db(); rows=c.execute("select rowid as rid,channel_id,payload from events where rowid>? and kind='message.created' order by rowid",(last,)).fetchall(); c.close()
   for r in rows:
    last=r['rid']; m=json.loads(r['payload'])['message']; ch=get_channel_row(r['channel_id'])
    if not ch: continue
    c=db(); w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone(); c.close(); name=irc_channel_name(w,ch)
    a=m['author']['username']; line=':'+a+'!'+a+'@localhost PRIVMSG '+name+' :'+m['body']
    with irc_lock:
     for st,so in list(irc_clients):
      if name in st.get('joined',set()): irc_send(so,line)
  except: pass
  time.sleep(.1)
def get_channel_row(cid):
 c=db(); r=c.execute('select * from channels where id=?',(cid,)).fetchone(); c.close(); return r
def irc_send(sock,line):
 try: sock.sendall((line+'\r\n').encode())
 except: pass
def irc_reply(sock,code,nick,text):
 irc_send(sock,':huddle %s %s %s'%(code,nick or '*',text))
def irc_token_user(token):
 c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(token,)).fetchone(); c.close(); return r
def irc_register(sock,state):
 if state.get('user') and state.get('nick') and state.get('usercmd') and not state.get('registered'):
  state['registered']=True; n=state['nick']
  for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created now'),('004','huddle huddle 1.0 o o'),('005','CHANTYPES=# :are supported')]:
   irc_reply(sock,code,n,text)
  irc_reply(sock,'422',n,'MOTD File is missing')
def irc_client(sock,addr):
 state={'nick':None,'user':None,'usercmd':False,'registered':False,'joined':set()}; buf=b''
 try:
  with irc_lock: irc_clients.append((state,sock))
  sock.settimeout(300)
  while True:
   x=sock.recv(4096)
   if not x: break
   buf+=x
   while b'\r\n' in buf or b'\n' in buf:
    if b'\r\n' in buf: line,buf=buf.split(b'\r\n',1)
    else: line,buf=buf.split(b'\n',1)
    parts=line.decode(errors='replace').strip().split(' ')
    if not parts or not parts[0]: continue
    cmd=parts[0].upper(); rest=parts[1:]
    if cmd=='PASS':
     token=rest[0] if rest else ''
     state['user']=irc_token_user(token)
     if not state['user']: irc_reply(sock,'464',state.get('nick') or '*','Password incorrect')
    elif cmd=='NICK':
     n=rest[0] if rest else ''
     if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',n): irc_reply(sock,'432',state.get('nick') or '*','Erroneous nickname'); continue
     with irc_lock:
      collision=n.lower() in irc_nicks and n.lower()!=str(state.get('nick','')).lower()
      if not collision:
       if state.get('nick'): irc_nicks.discard(state['nick'].lower())
       irc_nicks.add(n.lower())
     if collision: irc_reply(sock,'433',state.get('nick') or '*',n+' Nickname is already in use')
     else: state['nick']=n; irc_register(sock,state)
    elif cmd=='USER':
     if len(rest)>=1: state['usercmd']=True
     irc_register(sock,state)
    elif cmd=='JOIN':
     name=rest[0] if rest else ''; ch=irc_find_channel(name)
     if not ch or ch['is_private']: irc_reply(sock,'403',state.get('nick') or '*',name+' No such channel'); continue
     c=db(); c.execute('insert or ignore into wm(workspace_id,user_id,role) values(?,?,?)',(ch['workspace_id'],state['user']['id'],'member')); c.execute('insert or ignore into cm(channel_id,user_id) values(?,?)',(ch['id'],state['user']['id'])); c.commit(); w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone(); c.close()
     state['joined'].add(name); prefix=':'+state['nick']+'!'+state['user']['username']+'@localhost'
     irc_send(sock,prefix+' JOIN '+name); irc_reply(sock,'353',state['nick'],'= '+name+' :'+state['nick']); irc_reply(sock,'366',state['nick'],name+' End of NAMES')
    elif cmd in ('NAMES','WHO','TOPIC','MODE'):
     name=rest[0] if rest and rest[0].startswith('#') else (next(iter(state['joined']),'')); ch=irc_find_channel(name)
     if ch:
      if cmd=='NAMES': irc_reply(sock,'353',state['nick'],'= '+name+' :'+state['nick']); irc_reply(sock,'366',state['nick'],name+' End of NAMES')
      elif cmd=='WHO': irc_reply(sock,'315',state['nick'],name+' End of WHO list')
      elif cmd=='TOPIC': irc_reply(sock,'331',state['nick'],name+' No topic is set')
      else: irc_reply(sock,'324',state['nick'],name+' +nt')
    elif cmd=='LIST':
     irc_reply(sock,'323',state['nick'],'End of LIST')
    elif cmd=='PRIVMSG':
     if len(rest)>=2:
      name=rest[0]; body=' '.join(rest[1:]).lstrip(':'); ch=irc_find_channel(name)
      if ch and name in state['joined'] and body:
       c=db(); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],state['user']['id'],body,None,now())); mid=c.execute('select last_insert_rowid()').fetchone()[0]; r=c.execute('select * from messages where id=?',(mid,)).fetchone(); c.commit(); c.close(); emit(ch['id'],'message.created',{'message':msgjson(r)})
    elif cmd=='PING':
     text=' '.join(rest).lstrip(':'); irc_send(sock,':huddle PONG huddle :'+text)
    elif cmd=='QUIT': return
    elif cmd not in ('PONG','CAP'):
     irc_reply(sock,'421',state.get('nick') or '*',cmd+' Unknown command')
 finally:
  with irc_lock:
   irc_clients[:] = [(st,so) for st,so in irc_clients if so is not sock]
   if state.get('nick'): irc_nicks.discard(state['nick'].lower())
  try:sock.close()
  except:pass
def irc_server():
 import socket
 q=socket.socket(); q.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); q.bind(('0.0.0.0',6667)); q.listen(100)
 while True:
  sock,addr=q.accept(); threading.Thread(target=irc_client,args=(sock,addr),daemon=True).start()

if __name__=='__main__':
 init()
 if PORT=='irc':
  threading.Thread(target=irc_pump,daemon=True).start(); irc_server()
 else:
  threading.Thread(target=pump,daemon=True).start(); threading.Thread(target=global_pump,daemon=True).start(); threading.Thread(target=redis_listener,daemon=True).start(); ThreadingHTTPServer(('127.0.0.1',int(PORT)),H).serve_forever()
