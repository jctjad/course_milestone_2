import sys,os,json,re,sqlite3,traceback,hashlib,secrets,datetime,time,base64,struct,socket,threading,select
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from zoneinfo import ZoneInfo
DB='/var/lib/huddle/huddle.sqlite'
def db():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0,expires_at TEXT,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL,node_id INTEGER);
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));'''); c.close()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def validate_username(x): return isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_]+',x or '') is not None
def validate_password(x): return isinstance(x,str) and len(x)>=8
def hash_password(x): return hashlib.sha256(('huddle:'+x).encode()).hexdigest()
def verify_password(x,h): return hash_password(x)==h
def create_user(u,p,display_name=None):
 c=db(); c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(u,hash_password(p),display_name or u,'UTC','','','')); r=c.execute('SELECT * FROM users WHERE username=?',(u,)).fetchone(); c.close(); return r
def get_user_by_username(u):
 c=db(); r=c.execute('SELECT * FROM users WHERE username=?',(u,)).fetchone(); c.close(); return r
def issue_token(uid):
 t=secrets.token_urlsafe(32); c=db(); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.close(); return t
def user_from_token(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def user_to_json(u): return {k:u[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def get_workspace_by_slug(s):
 c=db(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(s,)).fetchone(); c.close(); return r
def workspace_to_json(w): return {k:w[k] for k in ['id','slug','name','owner_id','join_mode']}
def get_member_role(w,u):
 c=db(); r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); c.close(); return r['role'] if r else None
def add_workspace_member(w,u,role):
 c=db(); c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w,u,role)); r=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); c.close(); return r
def member_to_json(m): return {'user_id':m['user_id'],'role':m['role']}
def get_channel(i):
 c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone(); c.close(); return r
def channel_to_json(x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'] or '','is_archived':bool(x['is_archived'])}
def create_channel(w,n,is_private=False):
 c=db(); c.execute('INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,?)',(w,n,int(is_private))); r=c.execute('SELECT * FROM channels WHERE id=last_insert_rowid()').fetchone(); c.close(); return r
def validate_channel_name(n): return isinstance(n,str) and re.fullmatch(r'[a-z0-9]+(-[a-z0-9]+)*',n or '') is not None and len(n)<=32
def validate_topic(t): return isinstance(t,str) and len(t)<=250
def add_channel_member(ch,u):
 c=db(); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch,u)); c.close()
def remove_channel_member(ch,u):
 c=db(); c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u)); c.close()
def is_channel_member(ch,u):
 c=db(); x=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u)).fetchone(); c.close(); return bool(x)
def list_channel_members(ch):
 c=db(); x=c.execute('SELECT u.* FROM users u JOIN channel_members m ON u.id=m.user_id WHERE m.channel_id=? ORDER BY u.id',(ch,)).fetchall(); c.close(); return x
def update_channel(ch,**fields):
 c=db()
 for k,v in fields.items():
  if k in ('topic','is_archived'): c.execute('UPDATE channels SET '+k+'=? WHERE id=?',(v,ch))
 x=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone(); c.close(); return x
def visible_channels(w,u,include_archived=False):
 c=db(); q='SELECT c.* FROM channels c LEFT JOIN channel_members m ON c.id=m.channel_id AND m.user_id=? WHERE c.workspace_id=? AND (c.is_private=0 OR m.user_id IS NOT NULL)'
 args=[u,w]
 if not include_archived:q+=' AND c.is_archived=0'
 q+=' ORDER BY c.id'; x=c.execute(q,args).fetchall(); c.close(); return x
def require_writable(ch):
 if ch and ch['is_archived']: return False
 return True
def search_messages(uid,text,workspace_id=None):
 c=db(); q='SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE lower(?)'; args=['%'+text+'%']
 if workspace_id is not None:q+=' AND ch.workspace_id=?'; args.append(workspace_id)
 q+=' ORDER BY m.id DESC'; rows=c.execute(q,args).fetchall(); c.close()
 return [x for x in rows if can_read_channel(get_channel(x['channel_id']),uid)]
def pin_message(mid,uid):
 c=db(); x=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
 if not x:c.execute('INSERT INTO pins VALUES(?,?,?)',(mid,uid,now())); x=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
 c.close(); return x
def unpin_message(mid):
 c=db(); c.execute('DELETE FROM pins WHERE message_id=?',(mid,)); c.close()
def list_pins(ch):
 c=db(); x=c.execute('SELECT p.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(ch,)).fetchall(); c.close(); return x
def pin_to_json(x): return {'message_id':x['message_id'],'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}
def get_last_read(uid,ch):
 c=db(); x=c.execute('SELECT last_read_event_id FROM readstate WHERE user_id=? AND channel_id=?',(uid,ch)).fetchone(); c.close(); return x['last_read_event_id'] if x else 0
def set_last_read(uid,ch,event_id):
 c=db(); old=get_last_read(uid,ch); value=max(old,int(event_id)); c.execute('INSERT INTO readstate(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=?',(uid,ch,value,value)); c.close(); return value
def read_state_json(uid,ch):
 last=get_last_read(uid,ch); unread=mentions=0
 for e in events_since(ch,last):
  if e['type'] not in ('message.created','message.reply'): continue
  m=e.get('message') or {}; author=m.get('author_id')
  if author!=uid:
   unread+=1
   if uid in (m.get('mentions') or []): mentions+=1
 return {'channel_id':ch,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def validate_group_handle(h):
 return isinstance(h,str) and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h or '') is not None and h not in ('here','channel')
def group_to_json(g):
 ids=json.loads(g['user_ids']) if isinstance(g['user_ids'],str) else g['user_ids']
 return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}
def create_group(w,creator,handle,name,user_ids):
 c=db(); c.execute('INSERT INTO groups(workspace_id,creator_id,handle,name,user_ids) VALUES(?,?,?,?,?)',(w,creator,handle,name,json.dumps(user_ids))); x=c.execute('SELECT * FROM groups WHERE id=last_insert_rowid()').fetchone(); c.close(); return x
def get_group(w,handle):
 c=db(); x=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w,handle)).fetchone(); c.close(); return x
def update_group(g,**fields):
 c=db()
 if 'name' in fields:c.execute('UPDATE groups SET name=? WHERE id=?',(fields['name'],g['id']))
 if 'user_ids' in fields:c.execute('UPDATE groups SET user_ids=? WHERE id=?',(json.dumps(fields['user_ids']),g['id']))
 x=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone(); c.close(); return x
def delete_group(g):
 c=db(); c.execute('DELETE FROM groups WHERE id=?',(g['id'],)); c.close()
def validate_timezone(tz):
 try: ZoneInfo(tz); return True
 except Exception: return False
def validate_profile(fields):
 limits={'display_name':(1,80),'timezone':(1,255),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
 for k,v in fields.items():
  if k not in limits: continue
  if not isinstance(v,str) or not (limits[k][0]<=len(v)<=limits[k][1]): return False
  if k=='timezone' and not validate_timezone(v): return False
 return True
def update_profile(uid,fields):
 allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; fields={k:v for k,v in fields.items() if k in allowed}
 c=db()
 if fields:
  c.execute('UPDATE users SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',tuple(fields.values())+(uid,))
 x=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return x
def broadcast_global(frame):
 c=db(); c.execute('INSERT INTO global_events(payload,created_at) VALUES(?,?)',(json.dumps(frame),now())); c.close()
def global_events_since(i):
 c=db(); x=c.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(i,)).fetchall(); c.close(); return x
def online_user_ids():
 c=db(); x=c.execute('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,)).fetchall(); c.close(); return {r['user_id'] for r in x}
def extract_mentions(body,channel,author_id):
 ids=[]; members=list_channel_members(channel['id']); member_ids={x['id'] for x in members}
 if re.search(r'(?<![A-Za-z0-9_.])@channel(?![A-Za-z0-9_])',body): ids.extend(x['id'] for x in members if x['id']!=author_id)
 if re.search(r'(?<![A-Za-z0-9_.])@here(?![A-Za-z0-9_])',body):
  ids.extend(x['id'] for x in members if x['id']!=author_id and x['id'] in online_user_ids())
 for m in re.finditer(r'(?<![A-Za-z0-9_.])@([A-Za-z0-9_-]+)',body):
  name=m.group(1)
  if name in ('channel','here'): continue
  g=get_group(channel['workspace_id'],name) if channel['workspace_id'] is not None else None
  if g:
   ids.extend(x for x in json.loads(g['user_ids']) if x in member_ids and x!=author_id); continue
  if re.fullmatch(r'[A-Za-z0-9_]+',name):
   target=get_user_by_username(name)
   if target and target['id']!=author_id and get_member_role(channel['workspace_id'],target['id']) is not None: ids.append(target['id'])
 return list(dict.fromkeys(ids))
def notify_nodes(channel_id):
 try:
  z=socket.create_connection(('127.0.0.1',6379),timeout=.15); v=str(channel_id).encode()
  def enc(parts):
   cr=bytes([13,10]); out=b'*'+str(len(parts)).encode()+cr
   for x in parts:
    x=x if isinstance(x,bytes) else x.encode(); out+=b'$'+str(len(x)).encode()+cr+x+cr
   return out
  z.sendall(enc([b'PUBLISH',b'huddle:events',v])); z.close()
 except Exception: pass
def head_event_id(ch):
 c=db(); x=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone(); c.close(); return x['n']
def events_since(ch,since):
 c=db(); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall(); c.close()
 return [{'type':x['type'],'event_id':x['event_id'],'channel_id':ch,**json.loads(x['payload'])} for x in rows]
def emit_event(ch,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']; c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch,n,kind,json.dumps(payload),now())); c.execute('DELETE FROM events WHERE channel_id=? AND event_id<?',(ch,n-999)); c.commit(); c.close(); notify_nodes(ch)
 return {'type':kind,'event_id':n,'channel_id':ch,**payload}
def can_read_channel(ch,u):
 if not ch:return False
 if ch['is_dm']:return is_channel_member(ch['id'],u)
 return bool(get_member_role(ch['workspace_id'],u)) and (not ch['is_private'] or is_channel_member(ch['id'],u))
def run_slash_command(channel,user,body):
 parts=body[1:].split(' ',1); cmd=parts[0].lower(); arg=parts[1].strip() if len(parts)>1 else ''
 if cmd=='me':
  if not arg: raise ValueError()
  x=create_message(channel['id'],user['id'],'/me '+arg); return 201,{'message':message_to_json(x)}
 if cmd=='shrug':
  text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  x=create_message(channel['id'],user['id'],text); return 201,{'message':message_to_json(x)}
 if cmd=='topic':
  if not is_channel_member(channel['id'],user['id']) and channel['workspace_id'] is not None:
   w=db().execute('SELECT owner_id FROM workspaces WHERE id=?',(channel['workspace_id'],)).fetchone()
   if not w or w['owner_id']!=user['id']: return 403,{}
  if not arg or not validate_topic(arg): raise ValueError()
  ch=update_channel(channel['id'],topic=arg); emit_event(ch['id'],'channel.updated',{'channel':channel_to_json(ch)})
  return 201,{'message':None,'channel':channel_to_json(ch)}
 if cmd=='invite':
  if not is_channel_member(channel['id'],user['id']) or not arg.startswith('@') or not validate_username(arg[1:]): raise ValueError()
  target=get_user_by_username(arg[1:])
  if not target:return 404,{}
  if get_member_role(channel['workspace_id'],target['id']) is None:return 403,{}
  add_channel_member(channel['id'],target['id']); emit_event(channel['id'],'member.joined',{'user_id':target['id']})
  return 201,{'message':None,'invited_user':user_to_json(target)}
 if cmd in ('archive','unarchive'):
  if channel['workspace_id'] is None:return 403,{}
  w=db().execute('SELECT owner_id FROM workspaces WHERE id=?',(channel['workspace_id'],)).fetchone()
  if not w or w['owner_id']!=user['id']:return 403,{}
  desired=cmd=='archive'; ch=get_channel(channel['id'])
  if bool(ch['is_archived'])!=desired:
   ch=update_channel(ch['id'],is_archived=desired)
   emit_event(ch['id'],'channel.archived' if desired else 'channel.unarchived',{'channel':channel_to_json(ch)})
  return 201,{'message':None,'channel':channel_to_json(ch)}
 raise ValueError()
def file_to_json(f): return {k:f[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}
def get_file(i):
 c=db(); x=c.execute('SELECT * FROM files WHERE id=?',(i,)).fetchone(); c.close(); return x
def save_file(uid,filename,content_type,data):
 c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(uid,filename,content_type,len(data),now(),data)); x=c.execute('SELECT * FROM files WHERE id=last_insert_rowid()').fetchone(); c.close(); return x
def get_or_create_dm(a,b):
 if a==b: raise ValueError()
 lo,hi=sorted((a,b)); c=db(); x=c.execute("SELECT * FROM channels WHERE is_dm=1 AND name=?",(f'dm-{lo}-{hi}',)).fetchone()
 if not x:
  c.execute("INSERT INTO channels(name,is_private,is_dm) VALUES(?,1,1)",(f'dm-{lo}-{hi}',)); x=c.execute('SELECT * FROM channels WHERE id=last_insert_rowid()').fetchone()
  c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(x['id'],lo)); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(x['id'],hi))
 c.close(); return x
def attach_files(mid,uid,ids):
 c=db()
 try:
  for fid in ids:
   f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
   if not f or f['uploader_id']!=uid or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone(): raise ValueError()
  for fid in ids:c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid))
  c.close()
 except: c.close(); raise
def create_message(ch,u,body,parent_id=None,file_ids=None):
 c=db(); t=now()
 if parent_id is not None and not c.execute('SELECT 1 FROM messages WHERE id=? AND deleted=0',(parent_id,)).fetchone(): c.close(); return None
 c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(ch,u,body,parent_id,t,int(time.time()*1000)))
 x=c.execute('SELECT * FROM messages WHERE id=last_insert_rowid()').fetchone(); c.close()
 if file_ids:
  try: attach_files(x['id'],u,file_ids); x=get_message(x['id'])
  except: c=db(); c.execute('DELETE FROM messages WHERE id=?',(x['id'],)); c.close(); return None
 emit_event(ch,'message.reply' if parent_id else 'message.created',{'message':message_to_json(x)}); return x
def get_message(i):
 c=db(); x=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(i,)).fetchone(); c.close(); return x
def edit_message(message,body):
 c=db(); t=now(); c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,t,message['id'])); x=c.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone(); c.close(); return x
def delete_message(message):
 c=db(); c.execute('UPDATE messages SET deleted=1 WHERE id=? AND deleted=0',(message['id'],)); changed=c.total_changes>0; c.close(); return changed
def message_to_json(x): return jmsg(x)
def parse_limit(x):
 if x is None:return 50
 try:n=int(x)
 except: raise ValueError()
 if n<1 or n>200: raise ValueError()
 return n
def encode_cursor(pos): return base64.urlsafe_b64encode(json.dumps({'p':int(pos)}).encode()).decode()
def decode_cursor(cur):
 try:
  x=json.loads(base64.urlsafe_b64decode(cur.encode()).decode()); p=int(x['p'])
  if p<0:return None
  return p
 except: return None
def page(items,limit,cursor):
 pos=decode_cursor(cursor) if cursor else 0
 if cursor and pos is None: raise ValueError()
 out=items[pos:pos+limit]; nxt=encode_cursor(pos+limit) if pos+limit<len(items) else None
 return out,nxt
def validate_role(r): return r in ('owner','admin','member','guest')
def is_admin_or_owner(r): return r in ('admin','owner')
def can_create_channel(r,p): return r in ('admin','owner') or (r=='member' and not p)
def can_change_role(a,t,n): return is_admin_or_owner(a) and t!='owner' and n!='owner' and not(a=='admin' and t=='admin')
def set_member_role(w,u,r):
 c=db(); c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(r,w,u)); x=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); c.close(); return x
def validate_join_mode(x): return x in ('open','invite_only')
def update_workspace(w,name=None,join_mode=None):
 c=db()
 if name is not None: c.execute('UPDATE workspaces SET name=? WHERE id=?',(name,w))
 if join_mode is not None: c.execute('UPDATE workspaces SET join_mode=? WHERE id=?',(join_mode,w))
 x=c.execute('SELECT * FROM workspaces WHERE id=?',(w,)).fetchone(); c.close(); return x
def ensure_workspace_member(w,u):
 role=get_member_role(w['id'],u)
 if role:return role
 if w['join_mode']=='open': add_workspace_member(w['id'],u,'member'); return 'member'
 return None
def transfer_ownership(w,n):
 c=db(); old=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(w,)).fetchone()['owner_id']; c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w,old)); c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w,n)); c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(n,w)); c.close()
def create_invitation(w,by,exp=None,max_uses=1,invited_user_id=None):
 code=secrets.token_urlsafe(18); expires=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=exp)).isoformat().replace('+00:00','Z') if exp else None
 c=db(); c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w,by,1 if invited_user_id else max_uses,0,expires,invited_user_id)); x=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone(); c.close(); return x
def get_invitation(code):
 c=db(); x=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone(); c.close(); return x
def is_invitation_usable(x):
 return x and not x['revoked'] and x['uses']<x['max_uses'] and (not x['expires_at'] or x['expires_at']>now())
def invitation_to_json(x): return {k:x[k] for k in ['code','workspace_id','max_uses','uses','expires_at','invited_user_id']}
def redeem_invitation(x,u):
 c=db(); c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(x['code'],)); c.close(); return add_workspace_member(x['workspace_id'],u,'member')
def auth(h):
 if not h or not h.startswith('Bearer '): return None
 return user_from_token(h[7:])
def files_for(mid):
 c=db(); x=c.execute('SELECT f.* FROM files f JOIN message_files mf ON f.id=mf.file_id WHERE mf.message_id=?',(mid,)).fetchall(); c.close(); return [file_to_json(v) for v in x]
def jmsg(r):
 ch=get_channel(r['channel_id'])
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_to_json(getuser(r['author_id'])),'body':r['body'],'parent_id':r['parent_id'],'reply_count':countreplies(r['id']),'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files_for(r['id']),'reactions':reacs(r['id']),'mentions':extract_mentions(r['body'],ch,r['author_id'])}
def getuser(i):
 c=db(); x=c.execute('SELECT * FROM users WHERE id=?',(i,)).fetchone(); c.close(); return x
def countreplies(i):
 c=db(); n=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(i,)).fetchone()['n']; c.close(); return n
def reactions_for(mid):
 c=db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall(); c.close()
 out=[]
 for r in rows:
  found=next((x for x in out if x['emoji']==r['emoji']),None)
  if found: found['user_ids'].append(r['user_id']); found['count']+=1
  else: out.append({'emoji':r['emoji'],'count':1,'user_ids':[r['user_id']]})
 return out
def add_reaction(mid,uid,emoji):
 c=db(); before=c.total_changes; c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(mid,uid,emoji)); changed=c.total_changes>before; c.close(); return changed
def remove_reaction(mid,uid,emoji):
 c=db(); before=c.total_changes; c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,emoji)); changed=c.total_changes>before; c.close(); return changed
def reacs(i): return reactions_for(i)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def setup(self): super().setup(); self.node=int(sys.argv[1]) if len(sys.argv)>1 else 0
 def handle_one_request(self):
  try: return super().handle_one_request()
  except Exception:
   traceback.print_exc(file=open('/tmp/huddle-exception.log','a'))
   try: self.sendj({'error':'internal'},500)
   except Exception: pass
 def sendj(self,obj,status=200):
  b=json.dumps(obj,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def ws_send(self,obj):
  b=json.dumps(obj,separators=(',',':')).encode(); frame=bytes([129])
  n=len(b)
  if n<126: frame+=bytes([n])
  elif n<65536: frame+=bytes([126])+struct.pack('!H',n)
  else: frame+=bytes([127])+struct.pack('!Q',n)
  self.wfile.write(frame+b); self.wfile.flush()
 def do_ws(self):
  q=parse_qs(urlparse(self.path).query); u=user_from_token(q.get('token',[''])[0])
  if not u:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send_error(400)
  acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols'); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',acc); self.end_headers(); self.request.settimeout(None)
  conn_id=secrets.token_urlsafe(16); pc=db(); pc.execute('INSERT OR REPLACE INTO presence(conn_id,user_id,last_seen,node_id) VALUES(?,?,?,?)',(conn_id,u['id'],time.time(),self.node)); pc.close()
  gc=db(); global_seen=gc.execute('SELECT COALESCE(MAX(id),0) n FROM global_events').fetchone()['n']; gc.close(); subs=set(); sent={}; last=time.time()
  def recv():
   try:
    h=self.rfile.read(2)
    if not h:return None
    ln=h[1]&127; masked=h[1]&128
    if ln==126:ln=struct.unpack('!H',self.rfile.read(2))[0]
    elif ln==127:ln=struct.unpack('!Q',self.rfile.read(8))[0]
    mask=self.rfile.read(4) if masked else b''; data=self.rfile.read(ln)
    if masked:data=bytes(v^mask[i%4] for i,v in enumerate(data))
    return json.loads(data)
   except socket.timeout:return ''
   except:return None
  while True:
   try:
    ready,_,_=select.select([self.connection],[],[],.08)
    msg=recv() if ready else ''
    if msg is None:break
    if msg:
     typ=msg.get('type'); ch=int(msg.get('channel_id',0))
     if typ=='subscribe':
      c=get_channel(ch)
      if c and can_read_channel(c,u['id']): subs.add(ch); sent[ch]=head_event_id(ch); self.ws_send({'type':'subscribed','channel_id':ch,'head_event_id':sent[ch]})
     elif typ=='resume':
      since=int(msg.get('since_event_id',0)); rows=events_since(ch,since); earliest=max(1,head_event_id(ch)-999)
      if since and since<earliest-1:self.ws_send({'type':'resume.gap','channel_id':ch,'earliest_event_id':earliest})
      else:
       for e in rows:self.ws_send(e)
       h=head_event_id(ch); self.ws_send({'type':'resumed','channel_id':ch,'head_event_id':h}); sent[ch]=h
    if True:
     for ge in global_events_since(global_seen): self.ws_send(json.loads(ge['payload'])); global_seen=ge['id']
     for cid in list(subs):
      try:
       es=events_since(cid,sent.get(cid,0))
       for e in es:self.ws_send(e); sent[cid]=e['event_id']
      except (BrokenPipeError,ConnectionResetError,TimeoutError): return
     last=time.time()
   except (BrokenPipeError,ConnectionResetError,EOFError): break
   except Exception: continue
  pc=db(); pc.execute('DELETE FROM presence WHERE conn_id=?',(conn_id,)); pc.close()
 def do_GET(self):
  if self.path.startswith('/api/ws') and self.headers.get('Upgrade','').lower()=='websocket': return self.do_ws()
  p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
  if path=='/api/health': return self.sendj({'status':'ok','node_id':self.node})
  if path=='/':
   b=open('/app/static/index.html','rb').read(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); return self.wfile.write(b)
  if path=='/api/ws': return self.send_error(401)
  u=auth(self.headers.get('Authorization'))
  if not u:return self.sendj({'error':'unauthorized'},401)
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   f=get_file(int(m.group(1)))
   if not f:return self.sendj({},404)
   if m.group(2):
    b=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); return self.wfile.write(b)
   return self.sendj({'file':file_to_json(f)})
  if path=='/api/auth/me': return self.sendj({'user':user_to_json(u)})
  if path=='/api/workspaces':
   c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON w.id=m.workspace_id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close(); return self.sendj({'workspaces':[workspace_to_json(x) for x in rows]})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj({},404)
   if not get_member_role(w['id'],u['id']):return self.sendj({},403)
   c=db(); rows=c.execute('SELECT * FROM groups WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall(); c.close()
   return self.sendj({'groups':[group_to_json(x) for x in rows]})
  m=re.match(r'/api/workspaces/([^/]+)(?:/(members|invitations|groups))?$',path)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj({'error':'not found'},404)
   if m.group(2)=='members':
    c=db(); rows=c.execute('SELECT * FROM members WHERE workspace_id=?',(w['id'],)).fetchall(); c.close(); return self.sendj({'members':[member_to_json(x) for x in rows]})
   if m.group(2)=='invitations':
    if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj({},403)
    c=db(); rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses',(w['id'],)).fetchall(); c.close(); return self.sendj({'invitations':[invitation_to_json(x) for x in rows if is_invitation_usable(x)]})
   if not get_member_role(w['id'],u['id']): return self.sendj({},403)
   ch=visible_channels(w['id'],u['id'],q.get('include_archived',['0'])[0] in ('1','true'))
   return self.sendj({'workspace':workspace_to_json(w),'channels':[channel_to_json(x) for x in ch],'read_state':[read_state_json(u['id'],x['id']) for x in ch]})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   return self.sendj({'members':[user_to_json(x) for x in list_channel_members(ch['id'])]})
  m=re.match(r'/api/messages/(\d+)/pin$',path)
  if m:
   msg=get_message(int(m.group(1)))
   if not msg:return self.sendj({},404)
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   return self.sendj({'pin':pin_to_json(pin_message(msg['id'],u['id']))})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   try:n=parse_limit(q.get('limit',[None])[0])
   except:return self.sendj({},400)
   cur=q.get('cursor',[None])[0]
   if 'page' in q or 'offset' in q:return self.sendj({},400)
   boundary=None
   if cur:
    boundary=decode_cursor(cur)
    if boundary is None:return self.sendj({},400)
   c=db()
   if boundary is None: rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?',(ch['id'],n+1)).fetchall()
   else: rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL AND id<? ORDER BY id DESC LIMIT ?',(ch['id'],boundary,n+1)).fetchall()
   c.close()
   more=len(rows)>n; out=rows[:n]; nxt=encode_cursor(out[-1]['id']) if more and out else None
   return self.sendj({'messages':[message_to_json(x) for x in out],'next_cursor':nxt})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   parent=get_message(int(m.group(1)))
   if not parent:return self.sendj({},404)
   ch=get_channel(parent['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   try:n=parse_limit(q.get('limit',[None])[0])
   except:return self.sendj({},400)
   cur=q.get('cursor',[None])[0]; boundary=None
   if cur:
    boundary=decode_cursor(cur)
    if boundary is None:return self.sendj({},400)
   c=db()
   if boundary is None: rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?',(parent['id'],n+1)).fetchall()
   else: rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id LIMIT ?',(parent['id'],boundary,n+1)).fetchall()
   c.close()
   more=len(rows)>n; out=rows[:n]; nxt=encode_cursor(out[-1]['id']) if more and out else None
   return self.sendj({'replies':[message_to_json(x) for x in out],'next_cursor':nxt})
  m=re.match(r'/api/channels/(\d+)/read$',path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   return self.sendj({'read_state':read_state_json(u['id'],ch['id'])})
  if path=='/api/search':
   text=q.get('q',[''])[0]
   try:n=parse_limit(q.get('limit',[None])[0])
   except:return self.sendj({},400)
   rows=search_messages(u['id'],text,q.get('workspace_id',[None])[0])
   cur=q.get('cursor',[None])[0]
   try:out,nxt=page(rows,n,cur)
   except:return self.sendj({},400)
   return self.sendj({'results':[message_to_json(x) for x in out],'next_cursor':nxt})
  m=re.match(r'/api/channels/(\d+)/pins$',path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   try:n=parse_limit(q.get('limit',[None])[0]); out,nxt=page(list_pins(ch['id']),n,q.get('cursor',[None])[0])
   except:return self.sendj({},400)
   return self.sendj({'pins':[{'message':message_to_json(get_message(x['message_id'])),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in out],'next_cursor':nxt})
  if path.startswith('/api/users/'):
   x=getuser(int(path.split('/')[-1])); return self.sendj({'user':user_to_json(x)} if x else {},200 if x else 404)
  return self.sendj({'error':'not found'},404)
 def do_POST(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/files':
   u=auth(self.headers.get('Authorization'))
   if not u:return self.sendj({},401)
   n=int(self.headers.get('Content-Length','0'))
   if n>11*1024*1024:return self.sendj({},413)
   raw=self.rfile.read(n); ct=self.headers.get('Content-Type','')
   m=re.search(r'boundary=([^;]+)',ct)
   if not m:return self.sendj({},400)
   boundary=('--'+m.group(1).strip('"')).encode(); part=next((x for x in raw.split(boundary) if b'filename=' in x),None)
   if not part:return self.sendj({},400)
   head,dat=part.split(b'\r\n\r\n',1); dat=dat.rsplit(b'\r\n',1)[0]
   fn=re.search(br'filename="([^"]*)"',head); typ=re.search(br'Content-Type:\s*([^\r\n]+)',head)
   if not fn:return self.sendj({},400)
   if len(dat)>10485760:return self.sendj({},413)
   f=save_file(u['id'],fn.group(1).decode(errors='replace'),(typ.group(1).decode() if typ else 'application/octet-stream'),dat)
   return self.sendj({'file':file_to_json(f)},201)
  data=self.body()
  if path=='/api/auth/register':
   if not validate_username(data.get('username')) or not validate_password(data.get('password')) or 'email' in data:return self.sendj({},400)
   try:u=create_user(data['username'],data['password'],data.get('display_name'))
   except sqlite3.IntegrityError:return self.sendj({},409)
   return self.sendj({'user':user_to_json(u),'token':issue_token(u['id'])},201)
  if path=='/api/auth/login':
   u=get_user_by_username(data.get('username')); 
   if not u or not verify_password(data.get('password',''),u['password']):return self.sendj({},401)
   return self.sendj({'user':user_to_json(u),'token':issue_token(u['id'])})
  u=auth(self.headers.get('Authorization'))
  if not u:return self.sendj({},401)
  m=re.match(r'/api/channels/(\d+)/read$',path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   try: eid=int(data.get('last_read_event_id'))
   except:return self.sendj({},400)
   set_last_read(u['id'],ch['id'],eid)
   return self.sendj({'read_state':read_state_json(u['id'],ch['id'])})
  if path=='/api/dms':
   target=getuser(int(data.get('user_id',0))) if str(data.get('user_id','')).isdigit() else None
   if not target:return self.sendj({},404)
   if target['id']==u['id']:return self.sendj({},400)
   return self.sendj({'channel_id':get_or_create_dm(u['id'],target['id'])['id']})
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',data.get('slug','')):return self.sendj({},400)
   try:
    c=db(); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(data['slug'],data.get('name',''),u['id'])); w=c.execute('SELECT * FROM workspaces WHERE id=last_insert_rowid()').fetchone(); c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general')); ch=c.execute('SELECT * FROM channels WHERE id=last_insert_rowid()').fetchone(); c.execute('INSERT INTO channel_members VALUES(?,?)',(ch['id'],u['id'])); c.close()
   except sqlite3.IntegrityError:return self.sendj({},409)
   return self.sendj({'workspace':workspace_to_json(w),'general_channel':channel_to_json(ch)},201)
  m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership',path)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj({},404)
   if get_member_role(w['id'],u['id'])!='owner':return self.sendj({},403)
   target=int(data.get('new_owner_id',0))
   if get_member_role(w['id'],target)!='admin':return self.sendj({},400)
   transfer_ownership(w['id'],target)
   return self.sendj({'workspace':workspace_to_json(get_workspace_by_slug(w['slug']))})
  m=re.match(r'/api/workspaces/([^/]+)/channels',path)
  if m:
   w=get_workspace_by_slug(m.group(1)); role=get_member_role(w['id'],u['id']) if w else None
   if not w:return self.sendj({},404)
   if not can_create_channel(role,bool(data.get('is_private'))):return self.sendj({},403)
   if not validate_channel_name(data.get('name','')) or not validate_topic(data.get('topic','')): return self.sendj({},400)
   c0=db(); dup=c0.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],data.get('name',''))).fetchone(); c0.close()
   if dup:return self.sendj({},409)
   try:
    ch=create_channel(w['id'],data.get('name',''),bool(data.get('is_private')))
    if data.get('topic'): ch=update_channel(ch['id'],topic=data['topic'])
    add_channel_member(ch['id'],u['id'])
   except sqlite3.IntegrityError:return self.sendj({},409)
   return self.sendj({'channel':channel_to_json(ch)},201)
  m=re.match(r'/api/channels/(\d+)/join',path)
  if m:
   ch=get_channel(int(m.group(1))); 
   if not ch:return self.sendj({},404)
   c=db(); exists=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(); c.close()
   if exists:return self.sendj({'channel':channel_to_json(ch)})
   role=get_member_role(ch['workspace_id'],u['id']); cw=db(); w=cw.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); cw.close()
   if ch['is_private'] or role=='guest' or (not role and w['join_mode']!='open'):return self.sendj({},403)
   if not role:add_workspace_member(ch['workspace_id'],u['id'],'member')
   was=is_channel_member(ch['id'],u['id']); add_channel_member(ch['id'],u['id']);
   if not was: emit_event(ch['id'],'member.joined',{'user_id':u['id']})
   return self.sendj({'channel':channel_to_json(ch)})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj({},404)
   if not get_member_role(w['id'],u['id']):return self.sendj({},403)
   h=data.get('handle'); ids=data.get('user_ids',[])
   if not validate_group_handle(h) or not isinstance(data.get('name'),str) or not isinstance(ids,list):return self.sendj({},400)
   for uid in ids:
    if get_member_role(w['id'],uid) is None:return self.sendj({},400)
   try:g=create_group(w['id'],u['id'],h,data['name'],ids)
   except sqlite3.IntegrityError:return self.sendj({},409)
   return self.sendj({'group':group_to_json(g)},201)
  m=re.match(r'/api/workspaces/([^/]+)/invitations',path)
  if m:
   w=get_workspace_by_slug(m.group(1)); 
   if not w:return self.sendj({},404)
   if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj({},403)
   target=data.get('invited_user_id') or (get_user_by_username(data.get('invited_username')) or {'id':None})['id']; x=create_invitation(w['id'],u['id'],data.get('expires_in_seconds'),data.get('max_uses',1),target); return self.sendj({'invitation':invitation_to_json(x)},201)
  m=re.match(r'/api/invitations/([^/]+)/accept',path)
  if m:
   x=get_invitation(m.group(1))
   if not is_invitation_usable(x):return self.sendj({},404)
   if x['invited_user_id'] and x['invited_user_id']!=u['id']:return self.sendj({},403)
   mem=redeem_invitation(x,u['id']); w=db().execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone(); return self.sendj({'member':member_to_json(mem),'workspace':workspace_to_json(w)})
  m=re.match(r'/api/channels/(\d+)/(leave|archive|unarchive)$',path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   if m.group(2)=='leave':
    was=is_channel_member(ch['id'],u['id']); remove_channel_member(ch['id'],u['id']);
    if was: emit_event(ch['id'],'member.left',{'user_id':u['id']})
    return self.sendj({'channel':channel_to_json(ch)})
   w=get_workspace_by_slug(db().execute('SELECT slug FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()['slug'])
   if w['owner_id']!=u['id']:return self.sendj({},403)
   desired=(m.group(2)=='archive')
   if bool(ch['is_archived'])!=desired:
    ch=update_channel(ch['id'],is_archived=desired)
    emit_event(ch['id'],'channel.archived' if desired else 'channel.unarchived',{'channel':channel_to_json(ch)})
   return self.sendj({'channel':channel_to_json(ch)})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   msg=get_message(int(m.group(1)))
   if not msg:return self.sendj({},404)
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   emoji=data.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj({},400)
   changed=add_reaction(msg['id'],u['id'],emoji); rs=reactions_for(msg['id'])
   if changed: emit_event(ch['id'],'reaction.added',{'message_id':msg['id'],'emoji':emoji,'user_id':u['id'],'reactions':rs})
   return self.sendj({'reactions':rs})
  m=re.match(r'/api/messages/(\d+)/pin$',path)
  if m:
   msg=get_message(int(m.group(1)))
   if not msg:return self.sendj({},404)
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   return self.sendj({'pin':pin_to_json(pin_message(msg['id'],u['id']))})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   body=data.get('body')
   if not isinstance(body,str) or not body:return self.sendj({},400)
   if body.startswith('/') and not body.startswith('//'):
    try:
     if body.startswith('/unarchive'): pass
     elif not require_writable(ch): return self.sendj({},423)
     if not can_read_channel(ch,u['id']):return self.sendj({},403)
     status,out=run_slash_command(ch,u,body); return self.sendj(out,status)
    except ValueError:return self.sendj({},400)
   if not require_writable(ch):return self.sendj({},423)
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   fids=data.get('file_ids') or []
   if not isinstance(fids,list):return self.sendj({},400)
   for fid in fids:
    f=get_file(fid) if isinstance(fid,int) else None
    if not f or f['uploader_id']!=u['id']:
     return self.sendj({},400)
    c0=db(); used=c0.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone(); c0.close()
    if used:return self.sendj({},400)
   x=create_message(ch['id'],u['id'],body,data.get('parent_id'),fids)
   if not x:return self.sendj({},400)
   return self.sendj({'message':message_to_json(x)},201)
  return self.sendj({},404)
 def do_PATCH(self):
  u=auth(self.headers.get('Authorization'))
  if not u:return self.sendj({},401)
  if self.path=='/api/users/me':
   d=self.body()
   if not validate_profile(d):return self.sendj({},400)
   x=update_profile(u['id'],d); frame={'type':'user.updated','user':user_to_json(x)}; broadcast_global(frame)
   return self.sendj({'user':user_to_json(x)})
  m=re.match(r'/api/workspaces/([^/]+)$',self.path)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj({},404)
   if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj({},403)
   d=self.body()
   if 'join_mode' in d and not validate_join_mode(d['join_mode']):return self.sendj({},400)
   return self.sendj({'workspace':workspace_to_json(update_workspace(w['id'],d.get('name'),d.get('join_mode')))})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   mid=int(m.group(1)); msg=get_message(mid)
   if not msg:
    c=db(); raw=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); c.close()
    if raw:return self.sendj({},409)
    return self.sendj({},404)
   if msg['author_id']!=u['id']:return self.sendj({},403)
   d=self.body()
   if not isinstance(d.get('body'),str) or not d.get('body'):return self.sendj({},400)
   msg=edit_message(msg,d['body']); emit_event(msg['channel_id'],'message.updated',{'message':message_to_json(msg)})
   return self.sendj({'message':message_to_json(msg)})
  m=re.match(r'/api/channels/(\d+)$',self.path)
  if m:
   ch=get_channel(int(m.group(1)))
   if not ch:return self.sendj({},404)
   w=db().execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if not (is_channel_member(ch['id'],u['id']) or w['owner_id']==u['id']):return self.sendj({},403)
   if not require_writable(ch):return self.sendj({},423)
   d=self.body()
   if 'topic' not in d or not validate_topic(d['topic']):return self.sendj({},400)
   ch=update_channel(ch['id'],topic=d['topic']); emit_event(ch['id'],'channel.updated',{'channel':channel_to_json(ch)}); return self.sendj({'channel':channel_to_json(ch)})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   w=get_workspace_by_slug(m.group(1)); g=get_group(w['id'],m.group(2)) if w else None
   if not w or not g:return self.sendj({},404)
   if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return self.sendj({},403)
   d=self.body()
   if 'name' in d and (not isinstance(d['name'],str) or not d['name']):return self.sendj({},400)
   if 'user_ids' in d and not isinstance(d['user_ids'],list):return self.sendj({},400)
   return self.sendj({'group':group_to_json(update_group(g,**{k:d[k] for k in ('name','user_ids') if k in d}))})
  m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)',self.path)
  if m:
   w=get_workspace_by_slug(m.group(1)); d=self.body()
   if not w:return self.sendj({},404)
   target=get_member_role(w['id'],int(m.group(2))); a=get_member_role(w['id'],u['id'])
   if target is None:return self.sendj({},404)
   if not validate_role(d.get('role')) or d['role']=='owner':return self.sendj({},400)
   if not can_change_role(a,target,d['role']):return self.sendj({},403)
   return self.sendj({'member':member_to_json(set_member_role(w['id'],int(m.group(2)),d['role']))})
  return self.sendj({},404)
 def do_DELETE(self):
  u=auth(self.headers.get('Authorization'))
  if not u:return self.sendj({},401)
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   w=get_workspace_by_slug(m.group(1)); g=get_group(w['id'],m.group(2)) if w else None
   if not g:return self.sendj({'deleted':True})
   if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return self.sendj({},403)
   delete_group(g); return self.sendj({'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',self.path)
  if m:
   msg=get_message(int(m.group(1)))
   if not msg:return self.sendj({},404)
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   from urllib.parse import unquote
   emoji=unquote(m.group(2))
   if not emoji:return self.sendj({},400)
   changed=remove_reaction(msg['id'],u['id'],emoji); rs=reactions_for(msg['id'])
   if changed: emit_event(ch['id'],'reaction.removed',{'message_id':msg['id'],'emoji':emoji,'user_id':u['id'],'reactions':rs})
   return self.sendj({'reactions':rs})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   mid=int(m.group(1)); msg=get_message(mid)
   c=db(); raw=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); c.close()
   if not raw:return self.sendj({},404)
   if raw['deleted']:return self.sendj({'deleted':True})
   ch=get_channel(raw['channel_id']); w=db().execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if raw['author_id']!=u['id'] and w['owner_id']!=u['id']:return self.sendj({},403)
   changed=delete_message(raw)
   if changed: emit_event(ch['id'],'message.deleted',{'message_id':mid})
   return self.sendj({'deleted':True})
  m=re.match(r'/api/messages/(\d+)/pin$',self.path)
  if m:
   msg=get_message(int(m.group(1)))
   if not msg:return self.sendj({},404)
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj({},403)
   unpin_message(msg['id']); return self.sendj({'unpinned':True})
  m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)',self.path)
  if m:
   w=get_workspace_by_slug(m.group(1)); x=get_invitation(m.group(2))
   if not w or not x:return self.sendj({},404)
   if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj({},403)
   c=db();c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(x['code'],));c.close();return self.sendj({'deleted':True})
  return self.sendj({},404)
HTML='''<!doctype html><html><head><title>Huddle</title><style>body{font-family:Arial;margin:0;background:#f7f7f7}button{padding:9px;border:0;color:white;background:#007a5a}input{padding:9px;margin:4px}.app{max-width:1000px;margin:40px auto;background:white;padding:30px}</style></head><body><div class=app><h1>Huddle</h1><form data-testid=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><input name=display_name placeholder=display name><button data-testid=auth-submit data-button-role=primary>Sign in</button></form><p id=out></p></div><script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),x=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(f))});document.querySelector('#out').textContent=await x.text()}</script></body></html>'''
def redis_listener():
 while True:
  try:
   z=socket.create_connection(('127.0.0.1',6379),timeout=2)
   z.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
   while z.recv(4096): pass
  except Exception: time.sleep(.5)
def event_pump():
 while True: time.sleep(.1)
if __name__=='__main__':
 init(); threading.Thread(target=redis_listener,daemon=True).start(); threading.Thread(target=event_pump,daemon=True).start(); port=8000+int(sys.argv[1]); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()

IRC_NICKS=set()
IRC_CLIENTS=[]
IRC_LOCK=threading.Lock()
IRC_HEAD={}
def irc_reply(c,code,nick,text):
    c.sendall((':huddle %s %s %s\r\n'%(code,nick or '*',text)).encode())
def irc_channel_name(w,ch):
    return '#'+w['slug']+'/'+ch['name']
def irc_find_channel(name):
    if not name.startswith('#') or '/' not in name[1:]: return None
    slug,cn=name[1:].split('/',1); w=get_workspace_by_slug(slug)
    if not w:return None
    c=db(); x=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],cn)).fetchone(); c.close()
    return (w,x) if x else None
def irc_register(c,nick):
    for code,text in [('001','Welcome to Huddle'),('002','Your host is Huddle'),('003','This server was created today'),('004','huddle huddle 1'),('005','CHANTYPES=#')]:
        irc_reply(c,code,nick,text)
    irc_reply(c,'422',nick,'MOTD File is missing')
def irc_send(c,text):
    try:c.sendall(text.encode())
    except:pass
def irc_pump():
    while True:
        try:
            c=db(); rows=c.execute("SELECT * FROM events WHERE type='message.created' ORDER BY rowid").fetchall(); c.close()
            for e in rows:
                ch=get_channel(e['channel_id'])
                if not ch: continue
                if ch['workspace_id'] is None: continue
                wc=db(); w=wc.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); wc.close()
                if not w: continue
                name=irc_channel_name(w,ch); payload=json.loads(e['payload']); m=payload.get('message',{})
                author=getuser(m.get('author_id')); nick=author['username'] if author else 'huddle'
                with IRC_LOCK: clients=list(IRC_CLIENTS)
                for item in clients:
                    if ch['id'] in item['channels']:
                        irc_send(item['sock'],':%s!%s@huddle PRIVMSG %s :%s\r\n'%(nick,nick,name,m.get('body','')))
                IRC_HEAD[ch['id']]=e['event_id']
        except: pass
        time.sleep(.1)
def irc_client(c):
    user=None; nick=None; password=None; registered=False; joined=set(); buf=b''
    item={'sock':c,'nick':None,'user':None,'channels':joined}
    with IRC_LOCK: IRC_CLIENTS.append(item)
    try:
        while True:
            x=c.recv(4096)
            if not x: break
            buf+=x
            while b'\r\n' in buf:
                raw,buf=buf.split(b'\r\n',1); line=raw.decode(errors='replace')
                if not line: continue
                a=line.split(' ',1); cmd=a[0].upper(); arg=a[1] if len(a)>1 else ''
                if cmd=='PASS':
                    password=arg.lstrip(':'); user=user_from_token(password)
                    if not user: irc_reply(c,'464',nick or '*','Password incorrect')
                elif cmd=='NICK':
                    nn=arg.lstrip(':').strip()
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',nn): continue
                    with IRC_LOCK: collision=any(z.lower()==nn.lower() for z in IRC_NICKS if z!=nick)
                    if collision: irc_reply(c,'433',nick or '*',nn+' Nickname is already in use')
                    else:
                        with IRC_LOCK:
                            if nick: IRC_NICKS.discard(nick)
                            nick=nn; IRC_NICKS.add(nick); item['nick']=nick
                        if user and not registered: registered=True; item['user']=user; irc_register(c,nick)
                elif cmd=='USER':
                    if user and nick and not registered: registered=True; item['user']=user; irc_register(c,nick)
                elif cmd=='PING':
                    text=arg[1:] if arg.startswith(':') else arg; irc_send(c,':huddle PONG huddle :%s\r\n'%text)
                elif cmd=='JOIN':
                    target=arg.split()[0] if arg else ''; found=irc_find_channel(target)
                    if not found: irc_reply(c,'403',nick or '*',target+' No such channel'); continue
                    w,ch=found
                    if ch['is_private'] or not can_read_channel(ch,user['id']): irc_reply(c,'403',nick,target+' No such channel'); continue
                    add_channel_member(ch['id'],user['id']); joined.add(ch['id'])
                    irc_send(c,':%s!%s@huddle JOIN %s\r\n'%(nick,user['username'],target))
                    names=' '.join(x['username'] for x in list_channel_members(ch['id']))
                    irc_reply(c,'353',nick,'= %s :%s'%(target,names)); irc_reply(c,'366',nick,target+' End of NAMES list')
                elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
                    target=arg.split()[0] if arg else ''
                    if target and target.startswith('#') and not irc_find_channel(target): irc_reply(c,'403',nick,target+' No such channel')
                    elif cmd=='TOPIC': irc_reply(c,'331',nick,target+' No topic is set')
                elif cmd=='PRIVMSG':
                    bits=arg.split(' ',1); target=bits[0] if bits else ''; body=bits[1].lstrip(':') if len(bits)>1 else ''
                    found=irc_find_channel(target)
                    if found and body and found[1]['id'] in joined:
                        create_message(found[1]['id'],user['id'],body)
                elif cmd=='QUIT': return
                elif cmd not in ('CAP',): irc_reply(c,'421',nick or '*',cmd+' Unknown command')
    finally:
        with IRC_LOCK:
            if nick: IRC_NICKS.discard(nick)
            if item in IRC_CLIENTS: IRC_CLIENTS.remove(item)
        try:c.close()
        except:pass
def irc_server():
    threading.Thread(target=irc_pump,daemon=True).start()
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen(100)
    while True:
        c,_=srv.accept(); threading.Thread(target=irc_client,args=(c,),daemon=True).start()
