import sys,os,json,re,sqlite3,secrets,hashlib,base64,struct,threading,time,select,socket
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False,isolation_level=None); c.execute('PRAGMA busy_timeout=2000'); c.row_factory=sqlite3.Row; return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True); c=db()
 c.executescript('''PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id)); CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji)); CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE); CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle)); CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,payload TEXT);'''); c.commit(); c.close()
def validate_timezone(tz):
 try: ZoneInfo(tz); return True
 except Exception: return False
def validate_profile(fields):
 limits={'display_name':(1,80),'timezone':(1,128),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
 for k,v in fields.items():
  if k not in limits: continue
  if not isinstance(v,str): return False
  if len(v)<limits[k][0] or len(v)>limits[k][1]: return False
  if k=='timezone' and not validate_timezone(v): return False
 return True
def update_profile(uid,fields):
 allowed={k:v for k,v in fields.items() if k in ('display_name','timezone','avatar_url','status_text','status_emoji')}
 c=db()
 if allowed:
  sets=','.join(k+'=?' for k in allowed); c.execute('update users set '+sets+' where id=?',tuple(allowed.values())+(uid,)); c.commit()
 r=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return r
def broadcast_global(frame):
 c=db(); c.execute('insert into global_events(payload) values(?)',(json.dumps(frame),)); c.commit(); c.close()
def ujson(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'] or 'UTC','avatar_url':r['avatar_url'] or '','status_text':r['status_text'] or '','status_emoji':r['status_emoji'] or ''}
def cjson(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def usertok(tok):
 c=db(); r=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(tok,)).fetchone(); c.close(); return r
def reactions_for(mid):
 c=db(); rs=c.execute('select emoji,user_id from reactions where message_id=? order by rowid',(mid,)).fetchall(); c.close()
 out=[]
 for x in rs:
  z=next((q for q in out if q['emoji']==x['emoji']),None)
  if not z: z={'emoji':x['emoji'],'count':0,'user_ids':[]}; out.append(z)
  z['user_ids'].append(x['user_id']); z['count']=len(z['user_ids'])
 return out
def add_reaction(mid,uid,emoji):
 c=db(); c.execute('insert or ignore into reactions values(?,?,?,?)',(mid,uid,emoji,now())); changed=c.total_changes>0; c.commit(); c.close(); return changed
def remove_reaction(mid,uid,emoji):
 c=db(); c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,uid,emoji)); changed=c.total_changes>0; c.commit(); c.close(); return changed
def edit_message(message, body):
 c=db(); t=now(); c.execute('update messages set body=?,edited_at=? where id=?',(body,t,message['id'])); c.commit(); r=c.execute('select * from messages where id=?',(message['id'],)).fetchone(); c.close(); return r
def delete_message(message):
 c=db(); c.execute('update messages set deleted=1 where id=? and deleted=0',(message['id'],)); changed=c.total_changes>0; c.commit(); c.close(); return changed
def validate_group_handle(h):
 return isinstance(h,str) and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) is not None and h not in ('here','channel')
def group_to_json(r):
 return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'] or '[]')}
def get_group(wid,handle):
 c=db(); r=c.execute('select * from groups where workspace_id=? and handle=?',(wid,handle)).fetchone(); c.close(); return r
def create_group(wid,uid,handle,name,user_ids):
 c=db(); c.execute('insert into groups(workspace_id,creator_id,handle,name,user_ids) values(?,?,?,?,?)',(wid,uid,handle,name,json.dumps([int(x) for x in user_ids]))); c.commit(); r=c.execute('select * from groups where id=?',(c.execute('select last_insert_rowid()').fetchone()[0],)).fetchone(); c.close(); return r
def file_to_json(r):
 return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def get_file(fid):
 c=db(); r=c.execute('select * from files where id=?',(fid,)).fetchone(); c.close(); return r
def get_or_create_dm(a,b):
 if a==b:return None
 lo,hi=sorted((a,b)); c=db(); r=c.execute("select id from channels where is_dm=1 and name=?",(f'dm-{lo}-{hi}',)).fetchone()
 if r: c.close(); return r['id']
 c.execute("insert into channels(workspace_id,name,is_private,is_dm) values(NULL,?,1,1)",(f'dm-{lo}-{hi}',)); cid=c.execute('select last_insert_rowid()').fetchone()[0]
 c.execute('insert into cmembers values(?,?)',(cid,a)); c.execute('insert into cmembers values(?,?)',(cid,b)); c.commit(); c.close(); return cid
def save_file(uid,filename,ctype,data):
 c=db(); c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(uid,filename,ctype,len(data),now(),data)); fid=c.execute('select last_insert_rowid()').fetchone()[0]; c.commit(); r=c.execute('select * from files where id=?',(fid,)).fetchone(); c.close(); return r
def msgjson(r):
 c=db(); a=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 mentions=[]
 for x in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',r['body']):
  z=c.execute('select id from users where username=?',(x,)).fetchone()
  if z and z['id']!=r['author_id']: mentions.append(z['id'])
 ch=c.execute('select * from channels where id=?',(r['channel_id'],)).fetchone()
 if ch and ch['workspace_id']:
  for h in re.findall(r'(?<!\w)@([a-z0-9][a-z0-9_-]*)',r['body']):
   if h=='channel':
    mentions += [x['user_id'] for x in c.execute('select user_id from cmembers where channel_id=? and user_id!=?',(r['channel_id'],r['author_id']))]
   else:
    g=c.execute('select user_ids from groups where workspace_id=? and handle=?',(ch['workspace_id'],h)).fetchone()
    if g:
     ids=json.loads(g['user_ids'] or '[]'); members={x['user_id'] for x in c.execute('select user_id from cmembers where channel_id=?',(r['channel_id'],))}
     mentions += [x for x in ids if x in members and x!=r['author_id']]
   if h=='here':
    online={x['user_id'] for x in c.execute('select distinct user_id from presence where last_seen>?',(time.time()-1.5,))}
    members={x['user_id'] for x in c.execute('select user_id from cmembers where channel_id=?',(r['channel_id'],))}
    mentions += [x for x in members if x in online and x!=r['author_id']]
 c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':ujson(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':(db().execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0]),'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[file_to_json(x) for x in db().execute('select f.* from files f join attachments a on a.file_id=f.id where a.message_id=?',(r['id'],)).fetchall()],'reactions':reactions_for(r['id']),'mentions':list(dict.fromkeys(mentions))}


def run_slash_command(channel,user,body):
 parts=body.split(' ',1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
 if cmd=='/me':
  if not arg:return 400,{}
  return None,{'body':'/me '+arg}
 if cmd=='/shrug':
  return None,{'body':(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'}
 if cmd=='/topic':
  if not arg or len(arg)>250:return 400,{}
  c=db(); member=bool(c.execute('select 1 from cmembers where channel_id=? and user_id=?',(channel['id'],user['id'])).fetchone()); w=c.execute('select owner_id from workspaces where id=?',(channel['workspace_id'],)).fetchone()
  if not member and (not w or w['owner_id']!=user['id']):c.close();return 403,{}
  c.execute('update channels set topic=? where id=?',(arg,channel['id']));c.commit(); r=c.execute('select * from channels where id=?',(channel['id'],)).fetchone();c.close();emit(channel['id'],'channel.updated',cjson(r));return 201,{'message':None,'channel':cjson(r)}
 if cmd=='/invite':
  if not arg.startswith('@') or len(arg)<2:return 400,{}
  c=db(); member=bool(c.execute('select 1 from cmembers where channel_id=? and user_id=?',(channel['id'],user['id'])).fetchone()); target=c.execute('select * from users where username=?',(arg[1:],)).fetchone(); wm=c.execute('select 1 from members where workspace_id=? and user_id=?',(channel['workspace_id'],target['id'] if target else -1)).fetchone(); w=c.execute('select owner_id from workspaces where id=?',(channel['workspace_id'],)).fetchone()
  if not member:c.close();return 403,{}
  if not target:c.close();return 404,{}
  if not wm:c.close();return 403,{}
  c.execute('insert or ignore into cmembers values(?,?)',(channel['id'],target['id']));c.commit();c.close();emit(channel['id'],'member.joined',{'user_id':target['id']});return 201,{'message':None,'invited_user':ujson(target)}
 if cmd in ('/archive','/unarchive'):
  if cmd=='/archive' and channel['is_archived']:return 201,{'message':None,'channel':cjson(channel)}
  c=db();w=c.execute('select owner_id from workspaces where id=?',(channel['workspace_id'],)).fetchone()
  if not w or w['owner_id']!=user['id']:c.close();return 403,{}
  archived=cmd=='/archive';c.execute('update channels set is_archived=? where id=?',(archived,channel['id']));c.commit();r=c.execute('select * from channels where id=?',(channel['id'],)).fetchone();c.close();emit(channel['id'],'channel.archived' if archived else 'channel.unarchived',cjson(r));return 201,{'message':None,'channel':cjson(r)}
 return 400,{}

def notify_nodes(channel_id):
 try:
  z=socket.create_connection(("127.0.0.1",6379),timeout=.15)
  z.sendall(b"*3\r\n$7\r\nPUBLISH\r\n$13\r\nhuddle:events\r\n$"+str(len(str(channel_id))).encode()+b"\r\n"+str(channel_id).encode()+b"\r\n")
  z.close()
 except Exception: pass

def redis_listener():
 while True:
  try:
   z=socket.create_connection(("127.0.0.1",6379),timeout=2)
   z.sendall(b"*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n")
   while z.recv(4096): pass
  except Exception: time.sleep(.5)

def event_pump():
 while True: time.sleep(.1)

def emit(cid,kind,payload):
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE')
  c.execute('insert or ignore into seq(channel_id,n) values(?,0)',(cid,))
  c.execute('update seq set n=n+1 where channel_id=?',(cid,))
  n=c.execute('select n from seq where channel_id=?',(cid,)).fetchone()['n']
  c.execute('insert into events values(?,?,?,?)',(cid,n,kind,json.dumps(payload)))
  c.commit()
 except Exception:
  c.rollback(); c.close(); raise
 c.close(); notify_nodes(cid); return n
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,obj):
  b=json.dumps(obj,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  x=self.headers.get('Authorization',''); return usertok(x[7:]) if x.startswith('Bearer ') else None
 def do_GET(self):
  if self.path.split('?')[0]=='/':
   b=open('/app/static/index.html','rb').read(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  p=self.path.split('?')[0]; q=self.path.split('?',1)[1] if '?' in self.path else ''; u=self.auth()
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if p.startswith('/api/files/'):
   if not u:return self.sendj(401,{})
   z=p.split('/'); fid=z[3]; r=get_file(fid)
   if not r:return self.sendj(404,{})
   if len(z)>4 and z[4]=='download':
    data=r['data']; self.send_response(200); self.send_header('Content-Type',r['content_type']); self.send_header('Content-Disposition','attachment; filename="'+r['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
   return self.sendj(200,{'file':file_to_json(r)})

  if p=='/api/auth/me':
   return self.sendj(200,{'user':ujson(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  gm=re.match(r'/api/workspaces/([^/]+)/groups(?:/(.*))?$',p)
  if gm:
   if not u:return self.sendj(401,{})
   c=db(); w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone()
   if not w:return c.close() or self.sendj(404,{})
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():c.close();return self.sendj(403,{})
   rs=c.execute('select * from groups where workspace_id=?',(w['id'],)).fetchall();c.close();return self.sendj(200,{'groups':[group_to_json(x) for x in rs]})
  um=re.match(r'/api/users/(\\d+)$',p)
  if um:
   if not u:return self.sendj(401,{})
   c=db(); r=c.execute('select * from users where id=?',(um.group(1),)).fetchone(); c.close()
   return self.sendj(200,{'user':ujson(r)}) if r else self.sendj(404,{})
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   if not u:return self.sendj(401,{})
   slug=p.split('/')[3]; c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return c.close() or self.sendj(404,{})
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone(): c.close(); return self.sendj(403,{})
   cs=c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],)).fetchall(); c.close()
   return self.sendj(200,{'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':'open'},'channels':[cjson(x) for x in cs]})
  if p=='/api/workspaces':
   if not u:return self.sendj(401,{})
   c=db(); rs=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall(); c.close(); return self.sendj(200,{'workspaces':[{'id':x['id'],'slug':x['slug'],'name':x['name'],'owner_id':x['owner_id'],'join_mode':'open'} for x in rs]})
  m=re.match(r'/api/channels/(\d+)/members$',p)
  if m:
   if not u:return self.sendj(401,{})
   c=db(); rs=c.execute('select u.* from users u join cmembers m on m.user_id=u.id where m.channel_id=?',(m.group(1),)).fetchall(); c.close(); return self.sendj(200,{'members':[ujson(x) for x in rs]})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   if not u:return self.sendj(401,{})
   c=db(); ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone()
   if not ch:return c.close() or self.sendj(404,{})
   if ch['is_private'] and not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone(): c.close(); return self.sendj(403,{})
   rs=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc',(ch['id'],)).fetchall(); c.close(); return self.sendj(200,{'messages':[msgjson(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies$',p)
  if m:
   if not u:return self.sendj(401,{})
   c=db(); parent=c.execute('select * from messages where id=? and deleted=0',(m.group(1),)).fetchone()
   if not parent:return c.close() or self.sendj(404,{})
   rs=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(parent['id'],)).fetchall(); c.close()
   return self.sendj(200,{'replies':[msgjson(x) for x in rs],'next_cursor':None})
  if p.startswith('/api/users/'):
   if not u:return self.sendj(401,{})
   c=db(); r=c.execute('select * from users where id=?',(p.rsplit('/',1)[1],)).fetchone(); c.close(); return self.sendj(200,{'user':ujson(r)}) if r else self.sendj(404,{})
  return self.sendj(404,{})
 def do_POST(self):
  p=self.path.split('?')[0]; u=self.auth()
  if p=='/api/auth/register':
   try:
    x=self.body(); un=x['username']; pw=x['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: return self.sendj(400,{})
    c=db(); c.execute('insert into users(username,password,display_name,timezone) values(?,?,?,?)',(un,pw,x.get('display_name') or un,'UTC')); uid=c.execute('select last_insert_rowid()').fetchone()[0]; tok=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(tok,uid)); c.commit(); r=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return self.sendj(201,{'user':ujson(r),'token':tok})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  if p=='/api/auth/login':
   x=self.body(); c=db(); r=c.execute('select * from users where username=? and password=?',(x.get('username'),x.get('password'))).fetchone()
   if not r:c.close(); return self.sendj(401,{})
   tok=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(tok,r['id'])); c.commit(); c.close(); return self.sendj(200,{'user':ujson(r),'token':tok})
  if not u:return self.sendj(401,{})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   x=self.body(); c=db(); w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone()
   if not w:return c.close() or self.sendj(404,{})
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():c.close();return self.sendj(403,{})
   if not validate_group_handle(x.get('handle')):c.close();return self.sendj(400,{})
   try:r=create_group(w['id'],u['id'],x['handle'],x.get('name',''),x.get('user_ids',[]));return self.sendj(201,{'group':group_to_json(r)})
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
  if p=='/api/dms':
   x=self.body(); tid=x.get('user_id')
   if not tid or int(tid)==u['id']:return self.sendj(400,{})
   c=db(); target=c.execute('select id from users where id=?',(tid,)).fetchone(); c.close()
   if not target:return self.sendj(404,{})
   return self.sendj(200,{'channel_id':get_or_create_dm(u['id'],int(tid))})
  if p=='/api/files':
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.sendj(400,{})
   n=int(self.headers.get('Content-Length','0'))
   raw=self.rfile.read(n); import re as _re
   mm=_re.search(br'boundary=([^;]+)',ct.encode()); 
   if not mm:return self.sendj(400,{})
   boundary=b'--'+mm.group(1).strip(b'"'); part=next((x for x in raw.split(boundary) if b'name="file"' in x),None)
   if not part:return self.sendj(400,{})
   head,data=part.split(b'\r\n\r\n',1); data=data.rsplit(b'\r\n',1)[0]
   fm=_re.search(br'filename="([^"]*)"',head); tm=_re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
   if not fm:return self.sendj(400,{})
   if len(data)>10485760:return self.sendj(413,{})
   r=save_file(u['id'],fm.group(1).decode(),(tm.group(1).decode() if tm else 'application/octet-stream'),data); return self.sendj(201,{'file':file_to_json(r)})
  rm=re.match(r'/api/messages/(\d+)/reactions$',p)
  if rm:
   x=self.body(); emoji=x.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{})
   c=db(); m=c.execute('select * from messages where id=? and deleted=0',(rm.group(1),)).fetchone()
   if not m:return c.close() or self.sendj(404,{})
   ch=c.execute('select * from channels where id=?',(m['channel_id'],)).fetchone()
   allowed=(not ch['is_private']) or bool(c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone())
   c.close()
   if not allowed:return self.sendj(403,{})
   changed=add_reaction(m['id'],u['id'],emoji); rx=reactions_for(m['id'])
   if changed: emit(ch['id'],'reaction.added',{'message_id':m['id'],'emoji':emoji,'user_id':u['id'],'reactions':rx})
   return self.sendj(200,{'reactions':rx})
  if p=='/api/workspaces':
   x=self.body()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')): return self.sendj(400,{})
   c=db()
   try:
    c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(x['slug'],x['name'],u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,0,?)',(wid,'general','')); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,u['id'])); c.commit(); w=c.execute('select * from workspaces where id=?',(wid,)).fetchone(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone(); c.close(); return self.sendj(201,{'workspace':{'id':wid,'slug':w['slug'],'name':w['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':cjson(ch)})
   except sqlite3.IntegrityError:c.close(); return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   x=self.body(); c=db(); w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return c.close() or self.sendj(404,{})
   if w['owner_id']!=u['id'] and x.get('is_private'):c.close();return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')):c.close();return self.sendj(400,{})
   try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],x['name'],int(bool(x.get('is_private'))),x.get('topic',''))); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,u['id']));c.commit();r=c.execute('select * from channels where id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'channel':cjson(r)})
   except sqlite3.IntegrityError:c.close();return self.sendj(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',p)
  if m:
   c=db(); ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone()
   if not ch:return c.close() or self.sendj(404,{})
   if ch['is_private']:c.close();return self.sendj(403,{})
   c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],u['id']));c.commit();c.close();return self.sendj(200,{'channel':cjson(ch)})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',p)
  if m:
   c=db();ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone();w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
   if w['owner_id']!=u['id']:c.close();return self.sendj(403,{})
   c.execute('update channels set is_archived=? where id=?',(m.group(2)=='archive',ch['id']));c.commit();r=c.execute('select * from channels where id=?',(ch['id'],)).fetchone();c.close();return self.sendj(200,{'channel':cjson(r)})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   x=self.body(); c=db();ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone()
   if not ch:return c.close() or self.sendj(404,{})
   body=x.get('body','')
   if isinstance(body,str) and body.startswith('/') and not body.startswith('//'):
    code,result=run_slash_command(ch,u,body)
    if code is not None:c.close();return self.sendj(code,result)
    x['body']=result['body']
   if ch['is_archived'] and not (isinstance(body,str) and body.startswith('/unarchive')):c.close();return self.sendj(423,{})
   if not str(x.get('body','')).strip():c.close();return self.sendj(400,{})
   fids=x.get('file_ids') or []
   for fid in fids:
    z=c.execute('select f.* from files f where f.id=? and f.uploader_id=? and not exists(select 1 from attachments a where a.file_id=f.id)',(fid,u['id'])).fetchone()
    if not z:c.close();return self.sendj(400,{})
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],u['id'],x['body'],x.get('parent_id'),now())); mid=c.execute('select last_insert_rowid()').fetchone()[0]
   for fid in fids: c.execute('insert into attachments(message_id,file_id) values(?,?)',(mid,int(fid)))
   c.commit(); r=c.execute('select * from messages where id=?',(mid,)).fetchone(); c.close(); obj=msgjson(r); emit(ch['id'],'message.created',obj); return self.sendj(201,{'message':obj})
  return self.sendj(404,{})
 def do_DELETE(self):
  p=self.path.split('?')[0]; u=self.auth()
  if not u:return self.sendj(401,{})
  if p=='/api/dms':
   x=self.body(); tid=x.get('user_id')
   if not tid or int(tid)==u['id']:return self.sendj(400,{})
   c=db(); target=c.execute('select id from users where id=?',(tid,)).fetchone(); c.close()
   if not target:return self.sendj(404,{})
   return self.sendj(200,{'channel_id':get_or_create_dm(u['id'],int(tid))})
  if p=='/api/files':
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.sendj(400,{})
   n=int(self.headers.get('Content-Length','0'))
   raw=self.rfile.read(n); import re as _re
   mm=_re.search(br'boundary=([^;]+)',ct.encode()); 
   if not mm:return self.sendj(400,{})
   boundary=b'--'+mm.group(1).strip(b'"'); part=next((x for x in raw.split(boundary) if b'name="file"' in x),None)
   if not part:return self.sendj(400,{})
   head,data=part.split(b'\r\n\r\n',1); data=data.rsplit(b'\r\n',1)[0]
   fm=_re.search(br'filename="([^"]*)"',head); tm=_re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
   if not fm:return self.sendj(400,{})
   if len(data)>10485760:return self.sendj(413,{})
   r=save_file(u['id'],fm.group(1).decode(),(tm.group(1).decode() if tm else 'application/octet-stream'),data); return self.sendj(201,{'file':file_to_json(r)})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if gm:
   c=db(); w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone(); g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],gm.group(2))).fetchone() if w else None
   if g and u['id'] not in (g['creator_id'],w['owner_id']):c.close();return self.sendj(403,{})
   if g:c.execute('delete from groups where id=?',(g['id'],));c.commit()
   c.close();return self.sendj(200,{'deleted':True})
  rm=re.match(r'/api/messages/(\d+)/reactions/(.+)$',p)
  if rm:
   from urllib.parse import unquote
   emoji=unquote(rm.group(2))
   if not emoji:return self.sendj(400,{})
   c=db(); msg=c.execute('select * from messages where id=? and deleted=0',(rm.group(1),)).fetchone()
   if not msg:return c.close() or self.sendj(404,{})
   ch=c.execute('select * from channels where id=?',(msg['channel_id'],)).fetchone(); allowed=(not ch['is_private']) or bool(c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone()); c.close()
   if not allowed:return self.sendj(403,{})
   changed=remove_reaction(msg['id'],u['id'],emoji); rx=reactions_for(msg['id'])
   if changed: emit(ch['id'],'reaction.removed',{'message_id':msg['id'],'emoji':emoji,'user_id':u['id'],'reactions':rx})
   return self.sendj(200,{'reactions':rx})
  mm=re.match(r'/api/messages/(\d+)$',p)
  if mm:
   c=db(); msg=c.execute('select * from messages where id=?',(mm.group(1),)).fetchone()
   if not msg:return c.close() or self.sendj(404,{})
   w=c.execute('select owner_id from workspaces w join channels ch on ch.workspace_id=w.id where ch.id=?',(msg['channel_id'],)).fetchone(); c.close()
   if u['id']!=msg['author_id'] and (not w or u['id']!=w['owner_id']):return self.sendj(403,{})
   if not msg['deleted']:
    delete_message(msg); emit(msg['channel_id'],'message.deleted',{'message_id':msg['id']})
   return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.sendj(401,{})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path.split('?')[0])
  if gm:
   x=self.body(); c=db(); w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone(); g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],gm.group(2))).fetchone() if w else None
   if not g:return c.close() or self.sendj(404,{})
   if u['id'] not in (g['creator_id'],w['owner_id']):c.close();return self.sendj(403,{})
   c.execute('update groups set name=?,user_ids=? where id=?',(x.get('name',g['name']),json.dumps(x.get('user_ids',json.loads(g['user_ids']))),g['id']));c.commit();r=c.execute('select * from groups where id=?',(g['id'],)).fetchone();c.close();return self.sendj(200,{'group':group_to_json(r)})
  if self.path.split('?')[0]=='/api/users/me':
   fields=self.body()
   if not validate_profile(fields):return self.sendj(400,{})
   r=update_profile(u['id'],fields); broadcast_global({'type':'user.updated','user':ujson(r)}); return self.sendj(200,{'user':ujson(r)})
  m=re.match(r'/api/messages/(\d+)$',self.path.split('?')[0])
  if m:
   x=self.body(); body=x.get('body')
   if not isinstance(body,str) or not body.strip():return self.sendj(400,{})
   c=db(); msg=c.execute('select * from messages where id=?',(m.group(1),)).fetchone(); c.close()
   if not msg:return self.sendj(404,{})
   if msg['deleted']:return self.sendj(409,{})
   if msg['author_id']!=u['id']:return self.sendj(403,{})
   r=edit_message(msg,body); obj=msgjson(r); emit(r['channel_id'],'message.updated',{'message':obj}); return self.sendj(200,{'message':obj})
  m=re.match(r'/api/channels/(\d+)$',self.path.split('?')[0])
  if m:
   x=self.body();c=db();ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone();w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
   if w['owner_id']!=u['id'] and not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():c.close();return self.sendj(403,{})
   if len(x.get('topic',''))>250:c.close();return self.sendj(400,{})
   c.execute('update channels set topic=? where id=?',(x.get('topic',''),ch['id']));c.commit();r=c.execute('select * from channels where id=?',(ch['id'],)).fetchone();c.close();return self.sendj(200,{'channel':cjson(r)})
  return self.sendj(404,{})
 def do_OPTIONS(self): self.send_response(204);self.send_header('Content-Length','0');self.end_headers()
class W(H):
 def do_GET(self):
  if self.path.startswith('/api/ws'):
   tok=self.path.split('token=',1)[-1];u=usertok(tok)
   if not u:return self.send_error(401)
   key=self.headers.get('Sec-WebSocket-Key')
   if not key:return super().do_GET()
   acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers()
   self.connection.settimeout(0.2); subscribed={}; last={}; conn_id=secrets.token_hex(12); pc=db(); global_last=pc.execute('select coalesce(max(id),0) n from global_events').fetchone()['n']; pc.execute('insert or replace into presence values(?,?,?)',(conn_id,u['id'],time.time())); pc.commit(); pc.close()
   def out(o):
    b=json.dumps(o).encode(); n=len(b); h=bytes([129,n]) if n<126 else (bytes([129,126])+n.to_bytes(2,'big') if n<65536 else bytes([129,127])+n.to_bytes(8,'big')); self.connection.sendall(h+b)
   def exact(n):
    b=b''
    while len(b)<n:
     z=self.connection.recv(n-len(b))
     if not z: raise EOFError()
     b+=z
    return b
   while True:
    try:
     pc=db(); pc.execute('update presence set last_seen=? where conn_id=?',(time.time(),conn_id)); pc.commit(); pc.close(); ready,_,_=select.select([self.connection],[],[],0.1)
     if ready:
      z=exact(2); ln=z[1]&127
      if ln==126: ln=int.from_bytes(exact(2),'big')
      elif ln==127: ln=int.from_bytes(exact(8),'big')
      mask=exact(4) if z[1]&128 else None
      data=exact(ln)
      if mask: data=bytes(data[i]^mask[i%4] for i in range(ln))
      x=json.loads(data.decode())
      if x.get('type') in ('subscribe','resume'):
       cid=int(x['channel_id']); subscribed[cid]=1
       c=db(); head=c.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(cid,)).fetchone()['n']
       if x['type']=='resume':
        since=int(x.get('since_event_id',0))
        rs=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall()
        c.close()
        for r in rs:
         ev=json.loads(r['payload']); ev.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':cid}); out(ev)
        out({'type':'resumed','channel_id':cid,'head_event_id':head}); last[cid]=head
       else:
        c.close(); out({'type':'subscribed','channel_id':cid,'head_event_id':head}); last[cid]=head
     gc=db(); grs=gc.execute('select id,payload from global_events where id>? order by id',(global_last,)).fetchall(); gc.close()
     for gr in grs: out(json.loads(gr['payload'])); global_last=gr['id']
     for cid in list(subscribed):
      c=db(); rs=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last.get(cid,0))).fetchall(); c.close()
      for r in rs:
       pl=json.loads(r['payload']); ev={'type':r['kind'],'event_id':r['event_id'],'channel_id':cid}; ev.update({'message':pl} if r['kind']=='message.created' else pl); out(ev); last[cid]=r['event_id']
    except (EOFError,ConnectionError,BrokenPipeError,ConnectionResetError):
     pc=db(); pc.execute('delete from presence where conn_id=?',(conn_id,)); pc.commit(); pc.close(); break
    except Exception as e:
     pc=db(); pc.execute('delete from presence where conn_id=?',(conn_id,)); pc.commit(); pc.close(); open('/tmp/huddle-ws-errors.log','a').write(repr(e)+'\n')
  return super().do_GET()
if __name__=='__main__':
 init(); threading.Thread(target=redis_listener,daemon=True).start(); threading.Thread(target=event_pump,daemon=True).start(); port=int(sys.argv[1]); ThreadingHTTPServer(('127.0.0.1',port),W).serve_forever()
