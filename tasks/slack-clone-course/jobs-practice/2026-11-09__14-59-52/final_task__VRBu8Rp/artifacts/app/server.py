#!/usr/bin/env python3
import os,sys,re,json,sqlite3,hashlib,secrets,datetime,urllib.parse,threading,socket,base64,struct,time,fcntl,contextlib
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.sqlite3'
NODE=int(sys.argv[sys.argv.index('--node-id')+1]) if '--node-id' in sys.argv else 0
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
@contextlib.contextmanager
def write_lock():
 os.makedirs('/var/lib/huddle',exist_ok=True)
 f=open('/var/lib/huddle/write.lock','w')
 try:
  fcntl.flock(f,fcntl.LOCK_EX)
  yield
 finally:
  fcntl.flock(f,fcntl.LOCK_UN); f.close()
def db():
 c=sqlite3.connect(DB,timeout=5); c.execute('PRAGMA busy_timeout=5000'); c.row_factory=sqlite3.Row; return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True)
 c=db(); c.executescript('''
 PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,invited_user_id INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0,expires_at TEXT,revoked INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER);
 CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,created_at TEXT);
 CREATE TABLE IF NOT EXISTS channel_events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
 ''');
 try:c.execute('ALTER TABLE invitations ADD COLUMN created_by INTEGER')
 except sqlite3.OperationalError:pass
 try:c.execute('ALTER TABLE invitations ADD COLUMN revoked INTEGER DEFAULT 0')
 except sqlite3.OperationalError:pass
 try:c.execute('ALTER TABLE files ADD COLUMN data BLOB')
 except sqlite3.OperationalError:pass
 try:c.execute('CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE)')
 except sqlite3.OperationalError:pass
 c.commit(); c.close()
def notify_nodes(channel_id):
 try:
  sock=socket.create_connection(('127.0.0.1',6379),timeout=0.15)
  msg=str(channel_id).encode();parts=[b'PUBLISH',b'huddle:events',msg]
  req=b'*3\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in parts)
  sock.sendall(req);sock.close()
 except Exception:pass
def redis_listener():
 while True:
  sock=None
  try:
   sock=socket.create_connection(('127.0.0.1',6379),timeout=2)
   sock.settimeout(None)
   def cmd(parts):
    return b'*'+str(len(parts)).encode()+b'\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in parts)
   sock.sendall(cmd([b'SUBSCRIBE',b'huddle:events']))
   while True:
    data=sock.recv(4096)
    if not data:break
  except Exception:time.sleep(0.5)
  finally:
   try:sock.close()
   except:pass
def event_pump():
 while True:time.sleep(0.1)
def emit_event(channel_id,kind,payload):
 with write_lock():
  c=db()
  eid=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM channel_events WHERE channel_id=?',(channel_id,)).fetchone()[0]
  frame={'type':kind,'event_id':eid,'channel_id':channel_id}
  frame.update(payload)
  c.execute('INSERT INTO channel_events(channel_id,event_id,type,payload,created_at) VALUES(?,?,?,?,?)',(channel_id,eid,kind,json.dumps(frame),now()))
  c.commit();c.close();notify_nodes(channel_id)
  if kind=='message.created':
   try:
    ch=get_channel(channel_id);w=get_workspace_by_id(ch['workspace_id']);name=irc_channel_name(w,ch);msg=payload.get('message',{});author=msg.get('author',{});nick=author.get('username','user')
    with IRC_LOCK: clients=list(IRC_CLIENTS)
    for cli in clients:
     if channel_id in cli.get('joined',{}).values():
      irc_send(cli,':%s!%s@huddle PRIVMSG %s :%s'%(nick,nick,name,msg.get('body','')))
   except Exception:pass
  return frame
def events_since(channel_id,since_event_id):
 c=db();rows=c.execute('SELECT payload FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel_id,since_event_id)).fetchall();c.close();return [json.loads(x['payload']) for x in rows]
def head_event_id(channel_id):
 c=db();r=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(channel_id,)).fetchone();c.close();return r['n']
def get_last_read(user_id,channel_id):
 c=db();r=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(user_id,channel_id)).fetchone();c.close();return int(r['last_read_event_id']) if r else 0
def set_last_read(user_id,channel_id,event_id):
 c=db();cur=get_last_read(user_id,channel_id)
 value=max(cur,int(event_id))
 c.execute('INSERT INTO reads(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(user_id,channel_id,value));c.commit();c.close();return value
def read_state_json(user_id,channel_id):
 last=get_last_read(user_id,channel_id); unread=mentions=0
 for ev in events_since(channel_id,last):
  if ev.get('type') not in ('message.created','message.reply'):continue
  msg=ev.get('message') or {}
  if int(msg.get('author_id',0))==int(user_id):continue
  unread+=1
  if int(user_id) in [int(x) for x in msg.get('mentions',[])]:mentions+=1
 return {'channel_id':int(channel_id),'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def validate_timezone(tz):
 try:
  from zoneinfo import ZoneInfo
  ZoneInfo(tz);return isinstance(tz,str)
 except Exception:return False
def validate_profile(fields):
 allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
 for k,v in fields.items():
  if k not in allowed:continue
  if k=='display_name' and (not isinstance(v,str) or not 1<=len(v)<=80):raise ValueError('display_name')
  if k=='timezone' and (not isinstance(v,str) or not validate_timezone(v)):raise ValueError('timezone')
  if k=='avatar_url' and (not isinstance(v,str) or len(v)>2048):raise ValueError('avatar_url')
  if k=='status_text' and (not isinstance(v,str) or len(v)>100):raise ValueError('status_text')
  if k=='status_emoji' and (not isinstance(v,str) or len(v)>64):raise ValueError('status_emoji')
def update_profile(user_id,fields):
 validate_profile(fields);allowed={'display_name','timezone','avatar_url','status_text','status_emoji'};c=db()
 for k,v in fields.items():
  if k in allowed:c.execute('UPDATE users SET '+k+'=? WHERE id=?',(v,user_id))
 c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone();c.close();return r
def broadcast_global(frame):
 c=db();c.execute('INSERT INTO global_events(payload,created_at) VALUES(?,?)',(json.dumps(frame),now()));c.commit();c.close()
def validate_username(x): return isinstance(x,str) and bool(re.fullmatch(r'[A-Za-z0-9_]+',x))
def validate_password(x): return isinstance(x,str) and len(x)>=8
def hash_password(x): return hashlib.pbkdf2_hmac('sha256',x.encode(),b'huddle',12000).hex()
def verify_password(x,h): return secrets.compare_digest(hash_password(x),h)
def issue_token(uid):
 with write_lock():
  for attempt in range(12):
   c=None
   try:
    t=secrets.token_urlsafe(32); c=db(); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); c.close(); return t
   except sqlite3.OperationalError as e:
    if c:
     try:c.rollback(); c.close()
     except:pass
    if 'locked' not in str(e): raise
    time.sleep(0.05*(attempt+1))
 raise sqlite3.OperationalError('database busy')
def user_from_token(t):
 if not t:return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def user_to_json(u):
 return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def create_user(username,password,display_name=None):
 with write_lock():
  for attempt in range(12):
   c=None
   try:
    c=db(); cur=c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(username,hash_password(password),display_name or username,'UTC','','','')); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(cur.lastrowid,)).fetchone(); c.close(); return r
   except sqlite3.IntegrityError:
    if c:
     try:c.rollback(); c.close()
     except:pass
    raise
   except sqlite3.OperationalError as e:
    if c:
     try:c.rollback(); c.close()
     except:pass
    if 'locked' not in str(e): raise
    time.sleep(0.05*(attempt+1))
 raise sqlite3.OperationalError('database busy')
def get_user_by_username(x):
 c=db()
 try:return c.execute('SELECT * FROM users WHERE username=?',(x,)).fetchone()
 finally:c.close()
def get_workspace_by_slug(x):
 c=db();r=c.execute('SELECT * FROM workspaces WHERE slug=?',(x,)).fetchone();c.close();return r
def workspace_to_json(w): return dict(w)
def get_member_role(w,u):
 c=db();r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone();c.close();return r['role'] if r else None
def add_workspace_member(w,u,role):
 c=db();c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w,u,role));c.commit();r=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone();c.close();return r
def member_to_json(m): return dict(m)
def create_channel(w,name,is_private=False,topic=''):
 c=db();cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w,name,int(is_private),topic));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cur.lastrowid,)).fetchone();c.close();return r
def get_channel(i):
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone();c.close();return r
def validate_role(role): return role in ('owner','admin','member','guest')
def is_admin_or_owner(role): return role in ('owner','admin')
def can_create_channel(role,is_private): return role in ('owner','admin') or (role=='member' and not is_private)
def can_change_role(actor_role,target_role,new_role):
 return validate_role(new_role) and new_role!='owner' and target_role!='owner' and actor_role in ('owner','admin') and not (actor_role=='admin' and target_role=='admin')
def set_member_role(workspace_id,user_id,role):
 c=db();c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,workspace_id,user_id));c.commit();r=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(workspace_id,user_id)).fetchone();c.close();return r
def transfer_ownership(workspace_id,new_owner_id):
 c=db();old=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(workspace_id,)).fetchone()['owner_id'];c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',workspace_id,old));c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',workspace_id,new_owner_id));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(new_owner_id,workspace_id));c.commit();r=c.execute('SELECT * FROM workspaces WHERE id=?',(workspace_id,)).fetchone();c.close();return r
def channel_to_json(channel): return chjson(channel)
def validate_channel_name(name): return isinstance(name,str) and bool(re.fullmatch(r'[a-z0-9]+(-[a-z0-9]+)*',name)) and len(name)<=32
def validate_topic(topic): return isinstance(topic,str) and len(topic)<=250
def add_channel_member(channel_id,user_id):
 c=db();c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(channel_id,user_id));c.commit();c.close()
def remove_channel_member(channel_id,user_id):
 c=db();c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(channel_id,user_id));c.commit();c.close()
def is_channel_member(channel_id,user_id):
 c=db();r=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(channel_id,user_id)).fetchone();c.close();return bool(r)
def list_channel_members(channel_id):
 c=db();r=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(channel_id,)).fetchall();c.close();return r
def update_channel(channel_id,**fields):
 allowed={k:v for k,v in fields.items() if k in ('topic','is_archived')}
 if not allowed:return get_channel(channel_id)
 c=db()
 for k,v in allowed.items():c.execute('UPDATE channels SET '+k+'=? WHERE id=?',(v,channel_id))
 c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone();c.close();return r
def visible_channels(workspace_id,user_id,include_archived=False):
 c=db();q='SELECT c.* FROM channels c WHERE c.workspace_id=? AND (c.is_private=0 OR EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=c.id AND cm.user_id=?))'
 args=[workspace_id,user_id]
 if not include_archived:q+=' AND c.is_archived=0'
 q+=' ORDER BY c.id';r=c.execute(q,args).fetchall();c.close();return r
def require_writable(channel):
 if channel and channel['is_archived']: raise PermissionError('archived')
def can_read_channel(channel,user_id):
 if not channel:return False
 if channel['is_dm']:return is_channel_member(channel['id'],user_id)
 w=get_workspace_by_id(channel['workspace_id'])
 return bool(get_member_role(w['id'],user_id)) and (not channel['is_private'] or is_channel_member(channel['id'],user_id))
def validate_group_handle(handle):
 return isinstance(handle,str) and handle not in ('here','channel') and bool(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',handle))
def create_group(workspace_id,creator_id,handle,name,user_ids):
 c=db()
 try:
  cur=c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(workspace_id,handle,name,creator_id));gid=cur.lastrowid
  for uid in user_ids:c.execute('INSERT INTO group_members VALUES(?,?)',(gid,int(uid)))
  c.commit();r=c.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone();c.close();return r
 except sqlite3.IntegrityError:
  c.rollback();c.close();raise
def get_group(workspace_id,handle):
 c=db();r=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(workspace_id,handle)).fetchone();c.close();return r
def group_to_json(group):
 c=db();ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY rowid',(group['id'],)).fetchall()];c.close()
 return {'id':group['id'],'handle':group['handle'],'name':group['name'],'member_user_ids':ids}
def update_group(group,**fields):
 c=db()
 if 'name' in fields:c.execute('UPDATE groups_ SET name=? WHERE id=?',(fields['name'],group['id']))
 if 'user_ids' in fields:
  c.execute('DELETE FROM group_members WHERE group_id=?',(group['id'],))
  for uid in fields['user_ids']:c.execute('INSERT INTO group_members VALUES(?,?)',(group['id'],int(uid)))
 c.commit();r=c.execute('SELECT * FROM groups_ WHERE id=?',(group['id'],)).fetchone();c.close();return r
def delete_group(group):
 c=db();c.execute('DELETE FROM group_members WHERE group_id=?',(group['id'],));c.execute('DELETE FROM groups_ WHERE id=?',(group['id'],));c.commit();c.close()
def online_user_ids():
 c=db();r=c.execute('SELECT DISTINCT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,)).fetchall();c.close();return {x['user_id'] for x in r}
def extract_mentions(body,channel,author_id):
 ids=[];members=list_channel_members(channel['id']);member_ids={int(x['id']) for x in members};w=get_workspace_by_id(channel['workspace_id']) if channel['workspace_id'] else None
 for name in re.findall(r'(?<![A-Za-z0-9_.])@([A-Za-z0-9_-]+)',body or ''):
  if name=='channel':
   ids += [x for x in member_ids if x!=int(author_id) and x not in ids]
   continue
  if name=='here':
   ids += [x for x in member_ids if x!=int(author_id) and x in online_user_ids() and x not in ids]
   continue
  g=get_group(w['id'],name) if w else None
  if g:
   c=db();gids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()];c.close()
   ids += [x for x in gids if x in member_ids and x!=int(author_id) and x not in ids];continue
  x=get_user_by_username(name)
  if x and w and get_member_role(w['id'],x['id']) and int(x['id'])!=int(author_id) and int(x['id']) not in ids:ids.append(int(x['id']))
 return ids
def create_message(channel_id,author_id,body,parent_id=None):
 c=db(); created=now()
 if parent_id:
  pr=c.execute('SELECT channel_id FROM messages WHERE id=? AND deleted=0',(parent_id,)).fetchone()
  if not pr or pr['channel_id']!=channel_id: c.close(); raise ValueError('parent')
 eid=(c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM messages WHERE channel_id=?',(channel_id,)).fetchone()[0])
 cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(channel_id,author_id,body,parent_id,created,eid))
 c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone();c.close();return r
def get_message(message_id):
 c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(message_id,)).fetchone();c.close();return r
def message_to_json(message):
 if not message:return None
 c=db();u=c.execute('SELECT * FROM users WHERE id=?',(message['author_id'],)).fetchone()
 replies=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(message['id'],)).fetchone()['n'];c.close()
 d=dict(message);d.pop('event_id',None);d.pop('deleted',None);d['author']=user_to_json(u);d['reply_count']=replies;c2=db();fs=c2.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(d['id'],)).fetchall();c2.close();d['files']=[file_to_json(f) for f in fs];d['reactions']=reactions_for(d['id']);d['mentions']=extract_mentions(d.get('body',''),get_channel(d['channel_id']),d['author_id']);return d
def parse_limit(raw):
 if raw is None or raw=='':return 50
 try:n=int(raw)
 except:raise ValueError('limit')
 if n<1 or n>200:raise ValueError('limit')
 return n
def encode_cursor(position):
 return base64.urlsafe_b64encode(json.dumps({'p':int(position),'v':1},separators=(',',':')).encode()).decode().rstrip('=')
def decode_cursor(cursor):
 if not cursor: return None
 try:
  x=json.loads(base64.urlsafe_b64decode(cursor+'='*((4-len(cursor)%4)%4)))
  if x.get('v')!=1:return None
  return int(x['p'])
 except: return None
def page(items,limit,cursor):
 start=int(cursor or 0);out=items[start:start+limit];n=encode_cursor(start+limit) if start+limit<len(items) else None;return out,n
def create_invitation(workspace_id,created_by,expires_in_seconds=None,max_uses=1,invited_user_id=None):
 if invited_user_id is not None:max_uses=1
 code=secrets.token_urlsafe(18); exp=None
 if expires_in_seconds is not None: exp=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=int(expires_in_seconds))).isoformat()
 c=db();c.execute('INSERT INTO invitations(code,workspace_id,created_by,invited_user_id,max_uses,uses,expires_at,revoked) VALUES(?,?,?,?,?,?,?,0)',(code,workspace_id,created_by,invited_user_id,max_uses,0,exp));c.commit();r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone();c.close();return r
def get_invitation(code):
 c=db();r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone();c.close();return r
def is_invitation_usable(invitation):
 if not invitation or invitation['revoked'] or invitation['uses']>=invitation['max_uses']:return False
 return not invitation['expires_at'] or invitation['expires_at']>now()
def list_active_invitations(workspace_id):
 c=db();rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)',(workspace_id,now())).fetchall();c.close();return rs
def redeem_invitation(invitation,user_id):
 c=db();c.execute('UPDATE invitations SET uses=uses+1 WHERE code=? AND uses<max_uses',(invitation['code'],));c.commit();c.close();return add_workspace_member(invitation['workspace_id'],user_id,'member')
def revoke_invitation(invitation):
 c=db();c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(invitation['code'],));c.commit();c.close()
def invitation_to_json(invitation):
 return {k:invitation[k] for k in ('code','workspace_id','max_uses','uses','expires_at','invited_user_id')}
def validate_join_mode(join_mode): return join_mode in ('open','invite_only')
def update_workspace(workspace_id,name=None,join_mode=None):
 c=db();c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(name,join_mode,workspace_id));c.commit();r=c.execute('SELECT * FROM workspaces WHERE id=?',(workspace_id,)).fetchone();c.close();return r
def ensure_workspace_member(workspace,user_id):
 role=get_member_role(workspace['id'],user_id)
 if role:return role
 if workspace['join_mode']=='open':
  add_workspace_member(workspace['id'],user_id,'member');return 'member'
 return None
def chjson(r): 
 d=dict(r); d['is_private']=bool(d['is_private']);d['is_dm']=bool(d['is_dm']);d['is_archived']=bool(d['is_archived']);return d
def auth(h):
 a=h.headers.get('Authorization',''); return user_from_token(a[7:] if a.startswith('Bearer ') else None)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):
  try:
   with open('/tmp/huddle-debug.log','a') as f:f.write(str(a)+'\\n')
  except:pass
 def sendj(self,status,obj,headers=None):
  b=json.dumps(obj,separators=(',',':')).encode();self.close_connection=True;self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Connection','close');self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)))
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def readj(self):
  if hasattr(self,'_cached_body'): return self._cached_body
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def route(self):
  return urllib.parse.urlparse(self.path)
 def ws(self):
  t=urllib.parse.parse_qs(self.route().query).get('token',[''])[0]
  if t.startswith('Bearer '):t=t[7:]
  user=user_from_token(t)
  if not user:return self.sendj(401,{'error':'unauthorized'})
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.sendj(400,{'error':'websocket required'})
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  conn_id=secrets.token_urlsafe(16);pc=db();pc.execute('INSERT OR REPLACE INTO presence(conn_id,user_id,last_seen) VALUES(?,?,?)',(conn_id,user['id'],time.time()));pc.commit();pc.close()
  self.connection.settimeout(0.1);subscriptions={};global_seen=0
  def recv(n):
   out=b''
   while len(out)<n:
    out+=self.connection.recv(n-len(out))
   return out
  def send(obj):
   data=json.dumps(obj,separators=(',',':')).encode();n=len(data)
   if n<126:hdr=bytes([129,n])
   elif n<65536:hdr=bytes([129,126])+struct.pack('>H',n)
   else:hdr=bytes([129,127])+struct.pack('>Q',n)
   self.connection.sendall(hdr+data)
  while True:
   try:
    cglob=db();grow=cglob.execute('SELECT id,payload FROM global_events WHERE id>? ORDER BY id',(global_seen,)).fetchall();cglob.close()
    for ge in grow:send(json.loads(ge['payload']));global_seen=ge['id']
    for cid,last in list(subscriptions.items()):
     for ev in events_since(cid,last):send(ev);subscriptions[cid]=ev['event_id']
    h=recv(2)
    op=h[0]&15;masked=bool(h[1]&128);ln=h[1]&127
    if ln==126:ln=struct.unpack('>H',recv(2))[0]
    elif ln==127:ln=struct.unpack('>Q',recv(8))[0]
    mask=recv(4) if masked else b'';data=bytearray(recv(ln))
    if masked:
     for i in range(ln):data[i]^=mask[i%4]
    if op==8:break
    if op!=1:continue
    msg=json.loads(bytes(data));cid=int(msg.get('channel_id',0));ch=get_channel(cid)
    if not ch or not can_read_channel(ch,user['id']):continue
    if msg.get('type')=='subscribe':
     subscriptions[cid]=head_event_id(cid);send({'type':'subscribed','channel_id':cid,'head_event_id':subscriptions[cid]})
    elif msg.get('type')=='resume':
     since=int(msg.get('since_event_id',0));c=db();ear=c.execute('SELECT MIN(event_id) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n'];c.close()
     if ear is not None and since<ear-1:send({'type':'resume.gap','channel_id':cid,'earliest_event_id':ear})
     else:
      for ev in events_since(cid,since):send(ev)
      subscriptions[cid]=head_event_id(cid);send({'type':'resumed','channel_id':cid,'head_event_id':subscriptions[cid]})
    elif msg.get('type')=='ping':send({'type':'pong'})
   except socket.timeout:continue
   except (OSError,EOFError,ValueError):break
  try:self.connection.settimeout(None)
  except:pass
  try:
   pc=db();pc.execute('DELETE FROM presence WHERE conn_id=?',(conn_id,));pc.commit();pc.close()
  except:pass
 def handle_one_request(self):
  try:
   return super().handle_one_request()
  except Exception:
   with open('/tmp/huddle-debug.log','a') as f: traceback.print_exc(file=f)
   try:self.close_connection=True
   except:pass

 def do_GET(self):
  p=self.route().path
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if p=='/':
   data=open('/app/static/index.html','rb').read();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
  if p=='/api/ws': return self.ws()
  u=auth(self)
  if p=='/api/auth/me': return self.sendj(200,{'user':user_to_json(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  m=re.fullmatch(r'/api/users/(\d+)',p)
  if m:
   x=get_user_by_id(m.group(1))
   if not x:return self.sendj(404,{'error':'not found'})
   return self.sendj(200,{'user':user_to_json(x)})
  if p=='/api/workspaces':
   c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return self.sendj(200,{'workspaces':[workspace_to_json(x) for x in rows]})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   if not get_member_role(w['id'],u['id']):return self.sendj(403,{'error':'forbidden'})
   c=db();gs=c.execute('SELECT * FROM groups_ WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall();c.close();return self.sendj(200,{'groups':[group_to_json(x) for x in gs]})
  m=re.fullmatch(r'/api/workspaces/([^/]+)(?:/members)?',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   if p.endswith('/members'):
    c=db();rs=c.execute('SELECT * FROM members WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return self.sendj(200,{'members':[member_to_json(x) for x in rs]})
   if not get_member_role(w['id'],u['id']):return self.sendj(403,{'error':'forbidden'})
   inc=urllib.parse.parse_qs(self.route().query).get('include_archived',['0'])[0] in ('1','true')
   chs=visible_channels(w['id'],u['id'],inc);return self.sendj(200,{'workspace':workspace_to_json(w),'channels':[chjson(x) for x in chs],'read_state':[read_state_json(u['id'],x['id']) for x in chs]})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj(403,{'error':'forbidden'})
   return self.sendj(200,{'invitations':[invitation_to_json(x) for x in list_active_invitations(w['id'])]})
  m=re.fullmatch(r'/api/messages/(\d+)/replies',p)
  if m:
   parent=get_message(m.group(1))
   if not parent:return self.sendj(404,{'error':'not found'})
   ch=get_channel(parent['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   q=urllib.parse.parse_qs(self.route().query)
   try:lim=parse_limit(q.get('limit',[None])[0])
   except ValueError:return self.sendj(400,{'error':'invalid limit'})
   cur=decode_cursor(q.get('cursor',[None])[0]) if q.get('cursor') else 0
   if q.get('cursor') and cur is None:return self.sendj(400,{'error':'invalid cursor'})
   boundary=cur or 0
   c=db()
   if boundary: rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id ASC LIMIT ?',(parent['id'],boundary,lim+1)).fetchall()
   else: rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC LIMIT ?',(parent['id'],lim+1)).fetchall()
   c.close();arr=list(rs);more=len(arr)>lim;part=arr[:lim];nxt=encode_cursor(part[-1]['id']) if more and part else None
   return self.sendj(200,{'replies':[message_to_json(x) for x in part],'next_cursor':nxt})
  m=re.fullmatch(r'/api/files/(\d+)(/download)?',p)
  if m:
   f=get_file(m.group(1))
   if not f:return self.sendj(404,{'error':'not found'})
   if m.group(2):
    b=bytes(f['data'] or b'');self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
   return self.sendj(200,{'file':file_to_json(f)})
  if p=='/api/search':
   q=urllib.parse.parse_qs(self.route().query);term=q.get('q',[''])[0]
   try:lim=parse_limit(q.get('limit',[None])[0])
   except ValueError:return self.sendj(400,{'error':'invalid limit'})
   cur=decode_cursor(q.get('cursor',[None])[0]) if q.get('cursor') else 0
   if q.get('cursor') and cur is None:return self.sendj(400,{'error':'invalid cursor'})
   arr=search_messages(u['id'],term,q.get('workspace_id',[None])[0]);part,nxt=page(arr,lim,cur)
   return self.sendj(200,{'results':[message_to_json(x) for x in part],'next_cursor':nxt})
  m=re.fullmatch(r'/api/channels/(\d+)/pins',p)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   q=urllib.parse.parse_qs(self.route().query)
   try:lim=parse_limit(q.get('limit',[None])[0])
   except ValueError:return self.sendj(400,{'error':'invalid limit'})
   cur=decode_cursor(q.get('cursor',[None])[0]) if q.get('cursor') else 0
   if q.get('cursor') and cur is None:return self.sendj(400,{'error':'invalid cursor'})
   arr=list(list_pins(ch['id']));part,nxt=page(arr,lim,cur)
   out=[]
   for pin in part:
    m=get_message(pin['message_id']);out.append({'message':message_to_json(m),'pinned_by':pin['pinned_by'],'pinned_at':pin['pinned_at']})
   return self.sendj(200,{'pins':out,'next_cursor':nxt})
  m=re.fullmatch(r'/api/channels/(\d+)/read',p)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   return self.sendj(200,{'read_state':read_state_json(u['id'],ch['id'])})
  m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   q=urllib.parse.parse_qs(self.route().query)
   try:lim=parse_limit(q.get('limit',[None])[0])
   except ValueError:return self.sendj(400,{'error':'invalid limit'})
   cur=decode_cursor(q.get('cursor',[None])[0]) if q.get('cursor') else 0
   if q.get('cursor') and cur is None:return self.sendj(400,{'error':'invalid cursor'})
   boundary=cur
   c=db()
   if boundary: rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(ch['id'],boundary,lim+1)).fetchall()
   else: rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT ?',(ch['id'],lim+1)).fetchall()
   c.close();arr=list(rs);more=len(arr)>lim;part=arr[:lim];nxt=encode_cursor(part[-1]['id']) if more and part else None
   return self.sendj(200,{'messages':[message_to_json(x) for x in part],'next_cursor':nxt})
  m=re.fullmatch(r'/api/channels/(\d+)/members',p)
  if m:
   if not get_channel(m.group(1)):return self.sendj(404,{'error':'not found'})
   return self.sendj(200,{'members':[user_to_json(x) for x in list_channel_members(m.group(1))]})
  return self.sendj(404,{'error':'not found'})
 def do_PATCH(self):
  p=self.route().path;u=auth(self)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
  if m:
   w=get_workspace_by_slug(m.group(1));g=get_group(w['id'],m.group(2)) if w else None
   if not w or not g:return self.sendj(404,{'error':'not found'})
   if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return self.sendj(403,{'error':'forbidden'})
   d=self.readj();g=update_group(g,**{k:d[k] for k in ('name','user_ids') if k in d});return self.sendj(200,{'group':group_to_json(g)})
  if p=='/api/users/me':
   try:d=self.readj();x=update_profile(u['id'],d)
   except ValueError:return self.sendj(400,{'error':'invalid profile'})
   frame={'type':'user.updated','user':user_to_json(x)};broadcast_global(frame)
   return self.sendj(200,{'user':user_to_json(x)})
  m=re.fullmatch(r'/api/messages/(\d+)',p)
  if m:
   msg=get_message(m.group(1))
   if not msg:
    c=db();raw=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone();c.close()
    if not raw:return self.sendj(404,{'error':'not found'})
    msg=raw
   ch=get_channel(msg['channel_id']);d=self.readj()
   if msg['deleted']:return self.sendj(409,{'error':'deleted'})
   if int(msg['author_id'])!=int(u['id']):return self.sendj(403,{'error':'forbidden'})
   body=d.get('body')
   if not isinstance(body,str) or not body:return self.sendj(400,{'error':'empty'})
   msg=edit_message(msg,body);emit_event(ch['id'],'message.updated',{'message':message_to_json(msg)})
   return self.sendj(200,{'message':message_to_json(msg)})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   actor=get_member_role(w['id'],u['id']);target=get_member_role(w['id'],int(m.group(2)));d=self.readj();new=d.get('role')
   if not target:return self.sendj(404,{'error':'not found'})
   if not validate_role(new) or new=='owner':return self.sendj(400,{'error':'invalid role'})
   if not can_change_role(actor,target,new):return self.sendj(403,{'error':'forbidden'})
   return self.sendj(200,{'member':member_to_json(set_member_role(w['id'],int(m.group(2)),new))})
  m=re.fullmatch(r'/api/channels/(\d+)',p)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   w=get_workspace_by_id(ch['workspace_id']);d=self.readj();role=get_member_role(w['id'],u['id'])
   if ch['is_archived']:return self.sendj(423,{'error':'archived'})
   if not is_channel_member(ch['id'],u['id']) and not (w['owner_id']==u['id']):return self.sendj(403,{'error':'forbidden'})
   if 'topic' not in d or not validate_topic(d.get('topic')):return self.sendj(400,{'error':'invalid topic'})
   ch=update_channel(ch['id'],topic=d['topic']);emit_event(ch['id'],'channel.updated',{'channel':chjson(ch)});return self.sendj(200,{'channel':chjson(ch)})
  m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj(403,{'error':'forbidden'})
   d=self.readj()
   if 'join_mode' in d and not validate_join_mode(d.get('join_mode')):return self.sendj(400,{'error':'invalid join_mode'})
   return self.sendj(200,{'workspace':workspace_to_json(update_workspace(w['id'],d.get('name'),d.get('join_mode')))})
  return self.sendj(404,{'error':'not found'})
 def do_DELETE(self):
  p=self.route().path;u=auth(self)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  gm=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
  if gm:
   w=get_workspace_by_slug(gm.group(1));g=get_group(w['id'],gm.group(2)) if w else None
   if not w or not g:return self.sendj(200,{'deleted':True}) if w else self.sendj(404,{'error':'not found'})
   if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return self.sendj(403,{'error':'forbidden'})
   delete_group(g);return self.sendj(200,{'deleted':True})
  mmsg=re.fullmatch(r'/api/messages/(\d+)',p)
  if mmsg:
   raw=get_message(mmsg.group(1))
   if not raw:
    c=db();raw=c.execute('SELECT * FROM messages WHERE id=?',(mmsg.group(1),)).fetchone();c.close()
    if not raw:return self.sendj(404,{'error':'not found'})
   ch=get_channel(raw['channel_id']);w=get_workspace_by_id(ch['workspace_id'])
   if int(raw['author_id'])!=int(u['id']) and int(w['owner_id'])!=int(u['id']):return self.sendj(403,{'error':'forbidden'})
   changed=delete_message(raw)
   if changed:emit_event(ch['id'],'message.deleted',{'message_id':raw['id']})
   return self.sendj(200,{'deleted':True})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations/([^/]+)',p)
  rm=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',p)
  if rm:
   msg=get_message(rm.group(1))
   if not msg:return self.sendj(404,{'error':'not found'})
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   emoji=urllib.parse.unquote(rm.group(2))
   if not emoji:return self.sendj(400,{'error':'invalid emoji'})
   changed=remove_reaction(msg['id'],u['id'],emoji);rs=reactions_for(msg['id'])
   if changed:emit_event(ch['id'],'reaction.removed',{'message_id':msg['id'],'emoji':emoji,'user_id':u['id'],'reactions':rs})
   return self.sendj(200,{'reactions':rs})
  if not m:
   pm=re.fullmatch(r'/api/messages/(\d+)/pin',p)
   if pm:
    msg=get_message(pm.group(1))
    if not msg:return self.sendj(404,{'error':'not found'})
    if not can_read_channel(get_channel(msg['channel_id']),u['id']):return self.sendj(403,{'error':'forbidden'})
    unpin_message(msg['id']);return self.sendj(200,{'unpinned':True})
   return self.sendj(404,{'error':'not found'})
  w=get_workspace_by_slug(m.group(1));inv=get_invitation(m.group(2))
  if not w or not inv or inv['workspace_id']!=w['id']:return self.sendj(404,{'error':'not found'})
  if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj(403,{'error':'forbidden'})
  revoke_invitation(inv);return self.sendj(200,{'deleted':True})
 def do_POST(self):
  n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n) if n else b''
  ismultipart=self.headers.get('Content-Type','').startswith('multipart/form-data')
  if ismultipart:self._raw_body=raw;self._cached_body={}
  else:
   try:self._cached_body=json.loads(raw or b'{}')
   except Exception:self._cached_body={}
  p=self.route().path; u=auth(self)
  m=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   w=get_workspace_by_id(ch['workspace_id']); action=m.group(2)
   if action=='leave':
    changed=is_channel_member(ch['id'],u['id']);remove_channel_member(ch['id'],u['id'])
    if changed:emit_event(ch['id'],'member.left',{'user_id':u['id']})
    return self.sendj(200,{'channel':chjson(ch)})
   if w['owner_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
   ch=update_channel(ch['id'],is_archived=1 if action=='archive' else 0)
   emit_event(ch['id'],'channel.archived' if action=='archive' else 'channel.unarchived',{'channel':chjson(ch)})
   return self.sendj(200,{'channel':chjson(ch)})
  if p=='/api/auth/register':
   try:
    d=self.readj()
    if not validate_username(d.get('username')) or not validate_password(d.get('password')):return self.sendj(400,{'error':'invalid'})
    x=create_user(d['username'],d['password'],d.get('display_name'));return self.sendj(201,{'user':user_to_json(x),'token':issue_token(x['id'])})
   except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
  if p=='/api/auth/login':
   d=self.readj();x=get_user_by_username(d.get('username',''))
   if not x or not verify_password(d.get('password',''),x['password']):return self.sendj(401,{'error':'invalid'})
   return self.sendj(200,{'user':user_to_json(x),'token':issue_token(x['id'])})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if p=='/api/dms':
   d=self.readj()
   try:target=int(d.get('user_id'))
   except:return self.sendj(400,{'error':'invalid'})
   if not get_user_by_id(target):return self.sendj(404,{'error':'not found'})
   if target==u['id']:return self.sendj(400,{'error':'self'})
   return self.sendj(200,{'channel_id':get_or_create_dm(u['id'],target)['id']})
  if p=='/api/files':
   ct=self.headers.get('Content-Type','');m=re.search(r'boundary=([^;]+)',ct)
   if not m:return self.sendj(400,{'error':'file required'})
   boundary=('--'+m.group(1).strip('"')).encode();part=next((x for x in self._raw_body.split(boundary) if b'filename=' in x),None)
   if not part:return self.sendj(400,{'error':'file required'})
   head,data=part.split(b'\r\n\r\n',1);data=data.rsplit(b'\r\n',1)[0];fm=re.search(br'filename="([^"]*)"',head);tm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
   if not fm:return self.sendj(400,{'error':'file required'})
   if len(data)>10485760:return self.sendj(413,{'error':'too large'})
   f=save_file(u['id'],fm.group(1).decode('utf8','replace'),(tm.group(1).decode() if tm else 'application/octet-stream'),data);return self.sendj(201,{'file':file_to_json(f)})
  m=re.fullmatch(r'/api/invitations/([^/]+)/accept',p)
  if m:
   inv=get_invitation(m.group(1))
   if not is_invitation_usable(inv):return self.sendj(404,{'error':'not found'})
   if inv['invited_user_id'] is not None and inv['invited_user_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
   member=redeem_invitation(inv,u['id']);w=get_workspace_by_id(inv['workspace_id'])
   return self.sendj(200,{'workspace':workspace_to_json(w),'member':member_to_json(member)})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   if not is_admin_or_owner(get_member_role(w['id'],u['id'])):return self.sendj(403,{'error':'forbidden'})
   d=self.readj();target=d.get('invited_user_id')
   if target is None and d.get('invited_username'):
    x=get_user_by_username(d['invited_username'])
    if not x:return self.sendj(400,{'error':'unknown user'})
    target=x['id']
   try:mx=max(1,int(d.get('max_uses',1)))
   except:mx=1
   inv=create_invitation(w['id'],u['id'],d.get('expires_in_seconds'),mx,target)
   return self.sendj(201,{'invitation':invitation_to_json(inv)})
  if p in ('/api/auth/register','/api/auth/login'): 
   try:
    with open('/tmp/auth-debug.log','a') as f:f.write(p+' '+repr(self._cached_body)+'\\n')
   except:pass
  if not u:return self.sendj(401,{'error':'unauthorized'})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   if not get_member_role(w['id'],u['id']):return self.sendj(403,{'error':'forbidden'})
   d=self.readj();h=d.get('handle');ids=d.get('user_ids') or []
   if not validate_group_handle(h) or not isinstance(d.get('name'),str):return self.sendj(400,{'error':'invalid handle'})
   try:g=create_group(w['id'],u['id'],h,d['name'],ids)
   except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
   return self.sendj(201,{'group':group_to_json(g)})
  if p=='/api/workspaces':
   d=self.readj()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.sendj(400,{'error':'invalid slug'})
   c=None
   try:
    with write_lock():
     c=db();cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=cur.lastrowid;c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return self.sendj(201,{'workspace':workspace_to_json(w),'general_channel':chjson(ch)})
   except sqlite3.IntegrityError:
    if c:
     try:c.rollback()
     except:pass
    return self.sendj(409,{'error':'duplicate'})
   finally:
    if c:
     try:c.close()
     except:pass
  m=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   if get_member_role(w['id'],u['id'])!='owner':return self.sendj(403,{'error':'forbidden'})
   target=self.readj().get('new_owner_id')
   if get_member_role(w['id'],int(target or 0))!='admin':return self.sendj(400,{'error':'target must be admin'})
   return self.sendj(200,{'workspace':workspace_to_json(transfer_ownership(w['id'],int(target)))})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
  if m:
   w=get_workspace_by_slug(m.group(1))
   if not w:return self.sendj(404,{'error':'not found'})
   role=get_member_role(w['id'],u['id']);d=self.readj();private=bool(d.get('is_private',False));name=d.get('name','');topic=d.get('topic','')
   if not validate_channel_name(name) or not validate_topic(topic):return self.sendj(400,{'error':'invalid channel'})
   if not role or not can_create_channel(role,private):return self.sendj(403,{'error':'forbidden'})
   c0=db(); exists=c0.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone(); c0.close()
   if exists:return self.sendj(409,{'error':'duplicate'})
   c=db()
   try:
    cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),topic));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'channel':chjson(r)})
   except sqlite3.IntegrityError:
    c.rollback();c.close();return self.sendj(409,{'error':'duplicate'})
  m=re.fullmatch(r'/api/channels/(\d+)/join',p)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   w=get_workspace_by_id(ch['workspace_id']); role=get_member_role(w['id'],u['id'])
   c=db();already=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone();c.close()
   if ch['is_private'] and not already:return self.sendj(403,{'error':'forbidden'})
   role=ensure_workspace_member(w,u['id'])
   if role is None:return self.sendj(403,{'error':'forbidden'})
   if role=='guest':return self.sendj(403,{'error':'forbidden'})
   changed=not is_channel_member(ch['id'],u['id']);add_channel_member(ch['id'],u['id'])
   if changed:emit_event(ch['id'],'member.joined',{'user_id':u['id']})
   return self.sendj(200,{'channel':chjson(ch)})
  m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
  if m:
   msg=get_message(m.group(1))
   if not msg:return self.sendj(404,{'error':'not found'})
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   return self.sendj(200,{'pin':pin_to_json(pin_message(msg['id'],u['id']))})
  m=re.fullmatch(r'/api/channels/(\d+)/read',p)
  if m:
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   try:event_id=int(self.readj().get('last_read_event_id'))
   except:return self.sendj(400,{'error':'invalid event id'})
   set_last_read(u['id'],ch['id'],event_id)
   return self.sendj(200,{'read_state':read_state_json(u['id'],ch['id'])})
  m=re.fullmatch(r'/api/messages/(\d+)/reactions',p)
  if m:
   msg=get_message(m.group(1))
   if not msg:return self.sendj(404,{'error':'not found'})
   ch=get_channel(msg['channel_id'])
   if not can_read_channel(ch,u['id']):return self.sendj(403,{'error':'forbidden'})
   emoji=self.readj().get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{'error':'invalid emoji'})
   changed=add_reaction(msg['id'],u['id'],emoji);rs=reactions_for(msg['id'])
   if changed:emit_event(ch['id'],'reaction.added',{'message_id':msg['id'],'emoji':emoji,'user_id':u['id'],'reactions':rs})
   return self.sendj(200,{'reactions':rs})
  m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
  if m:
   d=self.readj();body=d.get('body','')
   if not isinstance(body,str) or not body:return self.sendj(400,{'error':'empty'})
   ch=get_channel(m.group(1))
   if not ch:return self.sendj(404,{'error':'not found'})
   if ch['is_archived'] and body.strip()!='/unarchive':return self.sendj(423,{'error':'archived'})
   if not can_read_channel(ch,u['id']) or not is_channel_member(ch['id'],u['id']):return self.sendj(403,{'error':'forbidden'})
   if body.startswith('/') and not body.startswith('//'):
    try:status,result=run_slash_command(ch,u,body);return self.sendj(status,result)
    except ValueError as e:return self.sendj(400,{'error':str(e)})
   file_ids=d.get('file_ids') or []
   try:validate_file_ids(u['id'],file_ids)
   except ValueError:return self.sendj(400,{'error':'invalid attachment'})
   try:r=create_message(ch['id'],u['id'],body,d.get('parent_id'));attach_files(r['id'],u['id'],file_ids)
   except ValueError:return self.sendj(400,{'error':'invalid attachment'})
   kind='message.reply' if d.get('parent_id') else 'message.created'
   emit_event(ch['id'],kind,{'message':message_to_json(r)})
   return self.sendj(201,{'message':message_to_json(r)})
  return self.sendj(404,{'error':'not found'})
def get_user_by_id(i):
 c=db();r=c.execute('SELECT * FROM users WHERE id=?',(i,)).fetchone();c.close();return r
def get_workspace_by_id(i):
 c=db();r=c.execute('SELECT * FROM workspaces WHERE id=?',(i,)).fetchone();c.close();return r
def msgjson(r): return message_to_json(r)
def add_reaction(message_id,user_id,emoji):
 c=db();before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(message_id,user_id,emoji)).fetchone()
 if not before:c.execute('INSERT INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(message_id,user_id,emoji));c.commit()
 c.close();return not bool(before)
def remove_reaction(message_id,user_id,emoji):
 c=db();before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(message_id,user_id,emoji)).fetchone()
 if before:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(message_id,user_id,emoji));c.commit()
 c.close();return bool(before)
def edit_message(message,body):
 c=db();ts=now();c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=? AND deleted=0',(body,ts,message['id']));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone();c.close();return r
def delete_message(message):
 c=db();changed=not bool(message['deleted']);c.execute('UPDATE messages SET deleted=1 WHERE id=?',(message['id'],));c.commit();c.close();return changed
def reactions_for(message_id):
 c=db();rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(message_id,)).fetchall();c.close()
 out=[]
 for r in rows:
  x=next((z for z in out if z['emoji']==r['emoji']),None)
  if not x:x={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(x)
  x['user_ids'].append(r['user_id']);x['count']=len(x['user_ids'])
 return out
def get_or_create_dm(user_a,user_b):
 if int(user_a)==int(user_b):raise ValueError('self')
 lo,hi=sorted((int(user_a),int(user_b)));c=db()
 r=c.execute("SELECT c.* FROM channels c JOIN channel_members a ON a.channel_id=c.id JOIN channel_members b ON b.channel_id=c.id WHERE c.is_dm=1 AND a.user_id=? AND b.user_id=?",(lo,hi)).fetchone()
 if not r:
  cur=c.execute("INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)",(f"dm-{lo}-{hi}",1));cid=cur.lastrowid;c.execute("INSERT INTO channel_members VALUES(?,?),(?,?)",(cid,lo,cid,hi));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 c.close();return r
def save_file(uploader_id,filename,content_type,data):
 c=db();cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(uploader_id,filename,content_type,len(data),now(),sqlite3.Binary(data)));c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(cur.lastrowid,)).fetchone();c.close();return r
def get_file(file_id):
 c=db();r=c.execute('SELECT * FROM files WHERE id=?',(file_id,)).fetchone();c.close();return r
def file_to_json(f):return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def run_slash_command(channel,user,body):
 parts=body.split(' ',1);cmd=parts[0][1:];arg=parts[1].strip() if len(parts)>1 else ''
 w=get_workspace_by_id(channel['workspace_id']) if channel['workspace_id'] else None
 if cmd=='me':
  if not arg: raise ValueError('argument')
  return 201,{'message':message_to_json(create_message(channel['id'],user['id'],body))}
 if cmd=='shrug':
  text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  return 201,{'message':message_to_json(create_message(channel['id'],user['id'],text))}
 if cmd=='topic':
  if not arg or not validate_topic(arg): return 400,{'error':'invalid topic'}
  if not is_channel_member(channel['id'],user['id']) and (not w or w['owner_id']!=user['id']):return 403,{'error':'forbidden'}
  ch=update_channel(channel['id'],topic=arg);emit_event(ch['id'],'channel.updated',{'channel':chjson(ch)})
  return 201,{'message':None,'channel':chjson(ch)}
 if cmd=='invite':
  if not is_channel_member(channel['id'],user['id']):return 403,{'error':'forbidden'}
  if not arg.startswith('@') or not arg[1:]:return 400,{'error':'username required'}
  x=get_user_by_username(arg[1:])
  if not x:return 404,{'error':'not found'}
  if w is None or not get_member_role(w['id'],x['id']):return 403,{'error':'forbidden'}
  add_channel_member(channel['id'],x['id']);return 201,{'message':None,'invited_user':user_to_json(x)}
 if cmd in ('archive','unarchive'):
  if not w or w['owner_id']!=user['id']:return 403,{'error':'forbidden'}
  if cmd=='archive' and channel['is_archived']:return 201,{'message':None,'channel':chjson(channel)}
  ch=update_channel(channel['id'],is_archived=1 if cmd=='archive' else 0)
  emit_event(ch['id'],'channel.archived' if cmd=='archive' else 'channel.unarchived',{'channel':chjson(ch)})
  return 201,{'message':None,'channel':chjson(ch)}
 raise ValueError('unknown command')
def validate_file_ids(author_id,file_ids):
 c=db()
 try:
  for fid in file_ids:
   f=c.execute('SELECT id FROM files WHERE id=? AND uploader_id=?',(fid,author_id)).fetchone()
   used=c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone()
   if not f or used: raise ValueError('file')
 finally:c.close()
def attach_files(message_id,author_id,file_ids):
 c=db()
 for fid in file_ids:
  f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=?',(fid,author_id)).fetchone()
  used=c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone()
  if not f or used:c.close();raise ValueError('file')
 for fid in file_ids:c.execute('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(message_id,fid))
 c.commit();c.close()
def search_messages(user_id,q,workspace_id=None):
 c=db();sql='SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE ?';args=['%'+str(q).lower()+'%']
 if workspace_id is not None:sql+=' AND ch.workspace_id=?';args.append(workspace_id)
 sql+=' ORDER BY m.id DESC';rows=c.execute(sql,args).fetchall();c.close()
 return [r for r in rows if can_read_channel(get_channel(r['channel_id']),user_id)]
def pin_message(message_id,user_id):
 c=db();r=c.execute('SELECT * FROM pins WHERE message_id=?',(message_id,)).fetchone()
 if not r:
  c.execute('INSERT INTO pins(message_id,pinned_by,pinned_at) VALUES(?,?,?)',(message_id,user_id,now()));c.commit();r=c.execute('SELECT * FROM pins WHERE message_id=?',(message_id,)).fetchone()
 c.close();return r
def unpin_message(message_id):
 c=db();c.execute('DELETE FROM pins WHERE message_id=?',(message_id,));c.commit();c.close()
def list_pins(channel_id):
 c=db();r=c.execute('SELECT p.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0 ORDER BY p.pinned_at DESC,p.message_id DESC',(channel_id,)).fetchall();c.close();return r
def pin_to_json(pin): return {'message_id':pin['message_id'],'pinned_by':pin['pinned_by'],'pinned_at':pin['pinned_at']}
IRC_NICKS=set();IRC_CLIENTS=[];IRC_LOCK=threading.Lock()
def irc_reply(sock,code,nick,text):
 try:sock.sendall((':huddle '+str(code)+' '+(nick or '*')+' '+text+'\r\n').encode())
 except Exception:pass
def irc_channel_name(workspace,channel): return '#'+workspace['slug']+'/'+channel['name']
def irc_find_channel(name):
 if not name.startswith('#') or '/' not in name[1:]:return None
 slug,cname=name[1:].split('/',1);w=get_workspace_by_slug(slug)
 if not w:return None
 c=db();r=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],cname)).fetchone();c.close();return r
def irc_send(c,line):
 try:c['sock'].sendall((line+'\r\n').encode());return True
 except Exception:return False
def irc_register(c):
 if c.get('registered') or not(c.get('user') and c.get('nick') and c.get('user_cmd')):return
 c['registered']=True
 for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','Server created'),('004','huddle huddle 1.0 oiwsz biklmnop'),('005','CHANTYPES=# PREFIX=(ov)@+ :are supported'),('422',':No MOTD')]:irc_reply(c['sock'],code,c['nick'],text)
def irc_pump():
 seen={}
 while True:
  try:
   with IRC_LOCK: clients=list(IRC_CLIENTS)
   for c in clients:
    for chname,cid in list(c.get('joined',{}).items()):
     last=seen.get((id(c),cid),0)
     for ev in events_since(cid,last):
      seen[(id(c),cid)]=ev['event_id']
      if ev['type']!='message.created':continue
      msg=ev.get('message',{});author=msg.get('author',{});nick=author.get('username','user')
      irc_send(c,':%s!%s@huddle PRIVMSG %s :%s'%(nick,nick,chname,msg.get('body','')))
  except Exception:pass
  time.sleep(.1)
def irc_client(sock,addr):
 c={'sock':sock,'nick':None,'user':None,'user_cmd':False,'registered':False,'pass_ok':False,'joined':{}}
 with IRC_LOCK:IRC_CLIENTS.append(c)
 try:
  sock.settimeout(300);buf=b''
  while True:
   data=sock.recv(4096)
   if not data:break
   buf+=data
   while b'\n' in buf:
    raw,buf=buf.split(b'\n',1);line=raw.rstrip(b'\r').decode('utf8','replace')
    if not line:continue
    parts=line.split(' ',2);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else '';trail=parts[2] if len(parts)>2 else ''
    if cmd=='PASS':
     u=user_from_token(arg)
     if not u:irc_reply(sock,'464',c.get('nick'),'Password incorrect')
     else:c['user']=u;c['pass_ok']=True
    elif cmd=='NICK':
     nick=arg[:30]
     if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',nick):irc_reply(sock,'432',c.get('nick') or '*','Erroneous nickname');continue
     with IRC_LOCK:
      if nick in IRC_NICKS and nick!=c.get('nick'):irc_reply(sock,'433',c.get('nick') or '*',nick+' Nickname is already in use');continue
      if c.get('nick'):IRC_NICKS.discard(c['nick'])
      IRC_NICKS.add(nick)
     c['nick']=nick;irc_register(c)
    elif cmd=='USER':
     if c['pass_ok']:c['user_cmd']=True;irc_register(c)
    elif cmd=='PING':sock.sendall(('PONG :'+(arg[1:] if arg.startswith(':') else arg)+'\r\n').encode())
    elif cmd=='JOIN':
     name=arg;ch=irc_find_channel(name)
     if not ch or ch['is_private']:irc_reply(sock,'403',c.get('nick') or '*',name+' No such channel');continue
     w=get_workspace_by_id(ch['workspace_id']);add_workspace_member(w['id'],c['user']['id'],'member');add_channel_member(ch['id'],c['user']['id']);c['joined'][name]=ch['id']
     irc_send(c,':%s!%s@huddle JOIN %s'%(c['nick'],c['user']['username'],name))
     cm=list_channel_members(ch['id']);names=' '.join(x['username'] for x in cm)
     irc_reply(sock,'353',c['nick'],'= %s :%s'%(name,names));irc_reply(sock,'366',c['nick'],name+' End of /NAMES list.')
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
     name=arg;ch=irc_find_channel(name) if name else None
     if cmd=='NAMES' and ch:
      names=' '.join(x['username'] for x in list_channel_members(ch['id']));irc_reply(sock,'353',c['nick'],'= %s :%s'%(name,names));irc_reply(sock,'366',c['nick'],name+' End of /NAMES list.')
     elif cmd=='WHO' and ch:irc_reply(sock,'315',c['nick'],name+' End of /WHO list.')
     elif cmd=='LIST':irc_reply(sock,'323',c['nick'],'End of /LIST')
     elif cmd=='TOPIC' and ch:irc_reply(sock,'331',c['nick'],name+' No topic is set')
    elif cmd=='PRIVMSG':
     ch=irc_find_channel(arg);body=trail[1:] if trail.startswith(':') else trail
     if ch and body:
      r=create_message(ch['id'],c['user']['id'],body);emit_event(ch['id'],'message.created',{'message':message_to_json(r)})
    elif cmd=='QUIT':
     try:sock.shutdown(socket.SHUT_RDWR)
     except:pass
     break
    elif cmd not in ('PONG','CAP'):irc_reply(sock,'421',c.get('nick') or '*',cmd+' Unknown command')
 finally:
  with IRC_LOCK:
   if c.get('nick'):IRC_NICKS.discard(c['nick'])
   if c in IRC_CLIENTS:IRC_CLIENTS.remove(c)
  try:sock.close()
  except:pass
def irc_server():
 threading.Thread(target=irc_pump,daemon=True).start()
 srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(('0.0.0.0',6667));srv.listen(100)
 while True:
  sock,addr=srv.accept();threading.Thread(target=irc_client,args=(sock,addr),daemon=True).start()
def main():
 init()
 if '--irc' in sys.argv:return irc_server()
 threading.Thread(target=redis_listener,daemon=True).start();threading.Thread(target=event_pump,daemon=True).start();ThreadingHTTPServer(('127.0.0.1',8000+NODE),H).serve_forever()
if __name__=='__main__':main()
