#!/usr/bin/env python3
import os,sys,re,json,secrets,hashlib,hmac,base64,datetime,time,threading,socket,select,sqlite3,urllib.parse,subprocess
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.sqlite'; DATA='/var/lib/huddle/files'
os.makedirs('/var/lib/huddle',exist_ok=True); os.makedirs(DATA,exist_ok=True)
SCHEMA='''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT '',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS evseq ON events(channel_id,event_id);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,path TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,users TEXT);
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,created_at TEXT);'''
def conn():
 c=sqlite3.connect(DB,timeout=1.5,check_same_thread=False); c.row_factory=sqlite3.Row; return c
with conn() as c: c.executescript(SCHEMA); c.commit()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def validate_username(x): return isinstance(x,str) and bool(re.fullmatch(r'[A-Za-z0-9_]+',x))
def validate_password(x): return isinstance(x,str) and len(x)>=8
def hash_password(x): return hashlib.sha256(x.encode()).hexdigest()
def verify_password(x,y): return hmac.compare_digest(hash_password(x),y)
def issue_token(uid):
 t=secrets.token_urlsafe(24)
 with conn() as c:c.execute('insert into tokens values(?,?)',(t,uid));c.commit()
 return t
def user_row(uid):
 with conn() as c:return c.execute('select * from users where id=?',(uid,)).fetchone()
def user_from_token(t):
 if not t:return None
 with conn() as c:r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone()
 return r
def user_to_json(u):
 return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def wsrow(slug):
 with conn() as c:return c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
def wsjson(w):return {k:w[k] for k in ('id','slug','name','owner_id','join_mode')}
def wsjson_by_id(i):
 with conn() as c:w=c.execute('select * from workspaces where id=?',(i,)).fetchone()
 return wsjson(w)
def role(w,u):
 with conn() as c:r=c.execute('select role from wm where workspace_id=? and user_id=?',(w,u)).fetchone()
 return r['role'] if r else None
def chan(cid):
 with conn() as c:return c.execute('select * from channels where id=?',(cid,)).fetchone()
def cj(x): return {**dict(x),'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'is_archived':bool(x['is_archived'])}
def cname(x): return isinstance(x,str) and bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',x)) and len(x)<=32
def valid_role(x): return x in ('owner','admin','member','guest')
def can_create(r,priv): return r in ('owner','admin') or (r=='member' and not priv)
def addcm(cid,uid):
 with conn() as c:c.execute('insert or ignore into cm values(?,?)',(cid,uid));c.commit()
def member(cid,uid):
 with conn() as c:return c.execute('select 1 from cm where channel_id=? and user_id=?',(cid,uid)).fetchone() is not None
def event(cid,kind,payload):
 for attempt in range(100):
  c=conn()
  try:
   c.execute('BEGIN IMMEDIATE')
   n=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(cid,)).fetchone()['n']
   c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(cid,n,kind,json.dumps(payload),now()))
   c.commit(); c.close(); return n
  except sqlite3.IntegrityError:
   c.rollback(); c.close(); time.sleep(.02)
  except sqlite3.OperationalError:
   c.rollback(); c.close(); time.sleep(.02)
  except Exception:
   c.rollback(); c.close(); raise
 raise sqlite3.OperationalError('event allocation busy')

def mentions_for(m):
 body=m['body']; out=[]
 ch=chan(m['channel_id'])
 if not ch:return out
 if re.search(r'(?<![A-Za-z0-9_])@here(?![A-Za-z0-9_])',body):
  out.extend(uid for uid in online_user_ids() if uid!=m['author_id'] and member(ch['id'],uid))
 if re.search(r'(?<![A-Za-z0-9_])@channel(?![A-Za-z0-9_])',body):
  with conn() as c: rows=c.execute('select user_id from cm where channel_id=? and user_id!=?',(ch['id'],m['author_id'])).fetchall()
  out.extend(r['user_id'] for r in rows)
 for handle in re.findall(r'(?<![A-Za-z0-9_])@([a-z0-9][a-z0-9_-]{0,31})',body):
  g=group_row(ch['workspace_id'],handle)
  if g:
   for uid in json.loads(g['users'] or '[]'):
    if uid!=m['author_id'] and member(ch['id'],uid):out.append(uid)
 for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
  if name in ('channel','here'):continue
  with conn() as c:
   r=c.execute('select u.id from users u join wm on wm.user_id=u.id where u.username=? and wm.workspace_id=?',(name,ch['workspace_id'])).fetchone()
  if r and r['id']!=m['author_id']:out.append(r['id'])
 return list(dict.fromkeys(out))
def valid_group_handle(h):
 return isinstance(h,str) and bool(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h)) and h not in ('here','channel')
def group_json(g):
 ids=json.loads(g['users'] or '[]')
 return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}
def group_row(wid,handle):
 with conn() as c:return c.execute('select * from groupsx where workspace_id=? and handle=?',(wid,handle)).fetchone()
def validate_timezone(tz):
 try: ZoneInfo(tz); return True
 except: return False
def validate_profile(fields):
 limits={'display_name':(str,1,80),'timezone':(str,1,200),'avatar_url':(str,0,2048),'status_text':(str,0,100),'status_emoji':(str,0,64)}
 for k,v in fields.items():
  if k not in limits: continue
  typ,lo,hi=limits[k]
  if not isinstance(v,typ) or not lo<=len(v)<=hi:return False
  if k=='timezone' and not validate_timezone(v):return False
 return True
def online_user_ids():
 with conn() as c:return [r['user_id'] for r in c.execute('select distinct user_id from presence where last_seen>?',(time.time()-1.5,)).fetchall()]
def reactions_for(mid):
 with conn() as c: rows=c.execute('select emoji,user_id from reactions where message_id=? order by rowid',(mid,)).fetchall()
 out=[]
 for r in rows:
  x=next((z for z in out if z['emoji']==r['emoji']),None)
  if not x:x={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(x)
  x['user_ids'].append(r['user_id']);x['count']=len(x['user_ids'])
 return out
def filejson(f):
 return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def files_for(mid):
 with conn() as c:return c.execute('select f.* from attachments a join files f on f.id=a.file_id where a.message_id=?',(mid,)).fetchall()
def file_row(fid):
 with conn() as c:return c.execute('select * from files where id=?',(fid,)).fetchone()
def dm_for(a,b):
 lo,hi=sorted((a,b))
 with conn() as c:
  return c.execute("select ch.id from channels ch join cm x on x.channel_id=ch.id and x.user_id=? join cm y on y.channel_id=ch.id and y.user_id=? where ch.is_dm=1 and x.user_id<>y.user_id",(lo,hi)).fetchone()
def msgjson(m):
 with conn() as c:
  rr=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(m['id'],)).fetchall()
  p=c.execute('select * from pins where message_id=?',(m['id'],)).fetchone()
 return {**dict(m),'author':user_to_json(user_row(m['author_id'])),'parent_id':m['parent_id'],'reply_count':m['reply_count'] if 'reply_count' in m.keys() else 0,'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[filejson(x) for x in files_for(m['id'])],'mentions':mentions_for(m),'reactions':reactions_for(m['id'])}
def getmsg(mid):
 with conn() as c:
  m=c.execute('select m.*, (select count(*) from messages q where q.parent_id=m.id and q.deleted=0) reply_count from messages m where m.id=? and m.deleted=0',(mid,)).fetchone()
 return m
def page(items,limit,cursor,reverse=False):
 start=0
 if cursor:
  try:start=int(base64.urlsafe_b64decode(cursor+'===').decode())
  except: raise ValueError()
 out=items[start:start+limit]; nxt=None if start+limit>=len(items) else base64.urlsafe_b64encode(str(start+limit).encode()).decode().rstrip('=')
 return out,nxt
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,status,obj,headers=None):
  b=json.dumps(obj,separators=(',',':')).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b))); 
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  x=self.headers.get('Authorization','');return user_from_token(x[7:] if x.startswith('Bearer ') else '')
 def err(self,s,m='error'):self.sendj(s,{m: 'invalid' if s==400 else 'forbidden'})
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path; q=urllib.parse.parse_qs(p.query)
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/':
   return self.html()
  if path=='/api/ws': return self.ws()
  u=self.auth()
  if path=='/api/auth/me':
   return self.sendj(200,{'user':user_to_json(u)}) if u else self.err(401)
   w=wsrow(path.split('/')[3])
   if not w:return self.err(404)
   if role(w['id'],u['id']) not in ('owner','admin'):return self.err(403)
   target=data.get('invited_user_id')
   if data.get('invited_username'):
    with conn() as c:x=c.execute('select id from users where username=?',(data['invited_username'],)).fetchone()
    target=x['id'] if x else None
   code=secrets.token_urlsafe(16); mx=1 if target else int(data.get('max_uses',1)); exp=int(time.time())+int(data.get('expires_in_seconds',604800))
   with conn() as c:c.execute('insert into invites(code,workspace_id,invited_user_id,expires,max_uses) values(?,?,?,?,?)',(code,w['id'],target,exp,mx));c.commit()
   return self.sendj(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'uses':0,'expires_at':datetime.datetime.fromtimestamp(exp,datetime.timezone.utc).isoformat().replace('+00:00','Z'),'invited_user_id':target}})
  if path.startswith('/api/invitations/') and path.endswith('/accept'):
   code=path.split('/')[3]
   with conn() as c:x=c.execute('select * from invites where code=?',(code,)).fetchone()
   if not x or x['uses']>=x['max_uses'] or x['expires']<int(time.time()):return self.err(404)
   if x['invited_user_id'] and x['invited_user_id']!=u['id']:return self.err(403)
   with conn() as c:
    c.execute('update invites set uses=uses+1 where code=?',(code,))
    c.execute('insert or ignore into wm values(?,?,?)',(x['workspace_id'],u['id'],'member'));c.commit()
   return self.sendj(200,{'workspace':wsjson_by_id(x['workspace_id']),'member':{'user_id':u['id'],'role':role(x['workspace_id'],u['id'])}})
  if path=='/api/workspaces':
   if not u:return self.err(401)
   with conn() as c: rows=c.execute('select w.* from workspaces w join wm on wm.workspace_id=w.id where wm.user_id=?',(u['id'],)).fetchall()
   return self.sendj(200,{'workspaces':[wsjson(x) for x in rows]})
  if path.startswith('/api/workspaces/') and path.endswith('/invitations'):
   w=wsrow(path.split('/')[3])
   if not w:return self.err(404)
   if not u:return self.err(401)
   if role(w['id'],u['id']) not in ('owner','admin'):return self.err(403)
   with conn() as c:ii=c.execute('select * from invites where workspace_id=? and uses<max_uses and expires>?',(w['id'],int(time.time()))).fetchall()
   return self.sendj(200,{'invitations':[{'code':x['code'],'workspace_id':x['workspace_id'],'max_uses':x['max_uses'],'uses':x['uses'],'expires_at':datetime.datetime.fromtimestamp(x['expires'],datetime.timezone.utc).isoformat().replace('+00:00','Z'),'invited_user_id':x['invited_user_id']} for x in ii]})
  if path.startswith('/api/workspaces/') and path.endswith('/groups'):
   w=wsrow(path.split('/')[3])
   if not w:return self.err(404)
   if not u:return self.err(401)
   if not role(w['id'],u['id']):return self.err(403)
   with conn() as c:gs=c.execute('select * from groupsx where workspace_id=?',(w['id'],)).fetchall()
   return self.sendj(200,{'groups':[group_json(g) for g in gs]})
  if path.startswith('/api/workspaces/'):
   a=path.split('/'); w=wsrow(a[3]) if len(a)>3 else None
   if not w:return self.err(404)
   if len(a)>4 and a[4]=='members':
    if not u:return self.err(401)
    with conn() as c: ms=c.execute('select u.*,wm.role from wm join users u on u.id=wm.user_id where wm.workspace_id=?',(w['id'],)).fetchall()
    return self.sendj(200,{'members':[{**user_to_json(x),'role':x['role'],'user_id':x['id']} for x in ms]})
   if not u:return self.err(401)
   if not role(w['id'],u['id']):return self.err(403)
   with conn() as c: cs=c.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
   cs=[x for x in cs if not x['is_private'] or member(x['id'],u['id'])]
   states=[]
   for x in cs:
    with conn() as c:r=c.execute('select last_read from reads where user_id=? and channel_id=?',(u['id'],x['id'])).fetchone()
    last=r['last_read'] if r else 0
    with conn() as c: es=c.execute("select e.event_id,m.author_id,m.body from events e join messages m on m.id=json_extract(e.payload,'$.id') where e.channel_id=? and e.event_id>?",(x['id'],last)).fetchall()
    states.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':sum(1 for z in es if z['author_id']!=u['id']),'mention_count':sum(1 for z in es if u['id'] in mentions_for({'channel_id':x['id'],'author_id':z['author_id'],'body':z['body']}) )})
   return self.sendj(200,{'workspace':wsjson(w),'channels':[cj(x) for x in cs],'read_state':states})
  if path.startswith('/api/channels/') and path.endswith('/read'):
   if not u:return self.err(401)
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   if not member(cid,u['id']):return self.err(403)
   with conn() as c:r=c.execute('select last_read from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone()
   last=r['last_read'] if r else 0
   with conn() as c:
    rows=c.execute("select e.event_id,m.author_id,m.body from events e join messages m on m.id=json_extract(e.payload,'$.id') where e.channel_id=? and e.event_id>? and e.kind in ('message.created','message.reply')",(cid,last)).fetchall()
   unread=sum(1 for r in rows if r['author_id']!=u['id'])
   mentions=sum(1 for r in rows if u['id'] in mentions_for({'channel_id':cid,'author_id':r['author_id'],'body':r['body']}))
   state={'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
   return self.sendj(200,{'read_state':state})
  if path.startswith('/api/channels/') and path.endswith('/pins'):
   if not u:return self.err(401)
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   if not member(cid,u['id']) and (ch['is_private'] or not role(ch['workspace_id'],u['id'])):return self.err(403)
   try:lim=int(q.get('limit',['50'])[0]);assert 1<=lim<=200
   except:return self.err(400)
   with conn() as c:ps=c.execute('select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=? order by p.pinned_at desc',(cid,)).fetchall()
   try:out,nxt=page(ps,lim,q.get('cursor',[None])[0])
   except:return self.err(400)
   return self.sendj(200,{'pins':[{'message':msgjson(x),'pinned_by':x['user_id'],'pinned_at':x['pinned_at']} for x in out],'next_cursor':nxt})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   if not u:return self.err(401)
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   if ch['is_dm']:
    if not member(cid,u['id']):return self.err(403)
   elif not role(ch['workspace_id'],u['id']) or (ch['is_private'] and not member(cid,u['id'])):return self.err(403)
   try:lim=int(q.get('limit',['50'])[0]);assert 1<=lim<=200
   except:return self.err(400)
   cur=q.get('cursor',[None])[0]
   try:boundary=int(base64.urlsafe_b64decode(cur+'===').decode()) if cur else None
   except:return self.err(400)
   with conn() as c:
    if boundary is None:ms=c.execute('select m.*, (select count(*) from messages x where x.parent_id=m.id and x.deleted=0) reply_count from messages m where m.channel_id=? and m.deleted=0 and m.parent_id is null order by m.id desc',(cid,)).fetchall()
    else:ms=c.execute('select m.*, (select count(*) from messages x where x.parent_id=m.id and x.deleted=0) reply_count from messages m where m.channel_id=? and m.id<? and m.deleted=0 and m.parent_id is null order by m.id desc',(cid,boundary)).fetchall()
   out=list(ms[:lim]);nxt=base64.urlsafe_b64encode(str(out[-1]['id']).encode()).decode().rstrip('=') if len(ms)>lim and out else None
   return self.sendj(200,{'messages':[msgjson(x) for x in out],'next_cursor':nxt})
  if path.startswith('/api/messages/') and path.endswith('/replies'):
   if not u:return self.err(401)
   pid=int(path.split('/')[3]);parent=getmsg(pid)
   if not parent:return self.err(404)
   ch=chan(parent['channel_id'])
   if ch['is_dm']:
    if not member(ch['id'],u['id']):return self.err(403)
   elif not role(ch['workspace_id'],u['id']) or (ch['is_private'] and not member(ch['id'],u['id'])):return self.err(403)
   try:lim=int(q.get('limit',['50'])[0]);assert 1<=lim<=200
   except:return self.err(400)
   with conn() as c:rs=c.execute('select m.*,0 reply_count from messages m where m.parent_id=? and m.deleted=0 order by m.id',(pid,)).fetchall()
   try:out,nxt=page(rs,lim,q.get('cursor',[None])[0])
   except:return self.err(400)
   return self.sendj(200,{'replies':[msgjson(x) for x in out],'next_cursor':nxt})
  if path.startswith('/api/channels/') and path.endswith('/members'):
   if not u:return self.err(401)
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   with conn() as c:ms=c.execute('select u.* from cm join users u on u.id=cm.user_id where cm.channel_id=?',(cid,)).fetchall()
   return self.sendj(200,{'members':[user_to_json(x) for x in ms]})
  if path.startswith('/api/files/'):
   if not u:return self.err(401)
   a=path.split('/');fid=int(a[3]);f=file_row(fid)
   if not f:return self.err(404)
   if len(a)>4 and a[4]=='download':
    try:b=open(f['path'],'rb').read()
    except:return self.err(404)
    self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
   return self.sendj(200,{'file':filejson(f)})
  if path.startswith('/api/search'):
   if not u:return self.err(401)
   text=q.get('q',[''])[0]; wid=q.get('workspace_id',[None])[0]
   try:lim=int(q.get('limit',['50'])[0]);assert 1<=lim<=200
   except:return self.err(400)
   cur=q.get('cursor',[None])[0]
   try:boundary=int(base64.urlsafe_b64decode(cur+'===').decode()) if cur else None
   except:return self.err(400)
   with conn() as c:
    sql="select m.*,0 reply_count from messages m join channels ch on ch.id=m.channel_id join wm on wm.workspace_id=ch.workspace_id where wm.user_id=? and lower(m.body) like lower(?) and m.deleted=0 and (ch.is_private=0 or exists(select 1 from cm where cm.channel_id=ch.id and cm.user_id=?))"
    args=[u['id'],'%'+text+'%',u['id']]
    if wid is not None:sql+=" and ch.workspace_id=?";args.append(int(wid))
    if boundary is not None:sql+=" and m.id<?";args.append(boundary)
    sql+=" order by m.id desc"
    ms=c.execute(sql,args).fetchall()
   out=list(ms[:lim]);nxt=base64.urlsafe_b64encode(str(out[-1]['id']).encode()).decode().rstrip('=') if len(ms)>lim and out else None
   return self.sendj(200,{'results':[msgjson(x) for x in out],'next_cursor':nxt})
  if path.startswith('/api/users/'):
   if not u:return self.err(401)
   try:x=user_row(int(path.split('/')[-1]))
   except:return self.err(404)
   return self.sendj(200,{'user':user_to_json(x)}) if x else self.err(404)
  return self.err(404)
 def do_POST(self):
  p=urllib.parse.urlparse(self.path);path=p.path;u=self.auth(); data={} if path=='/api/files' else self.body()
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   body=data.get('body')
   if isinstance(body,str) and body.startswith('/') and not body.startswith('//'):
    bits=body.split(' ',1);cmd=bits[0][1:];arg=bits[1].strip() if len(bits)>1 else ''
    if cmd=='me':
     if not arg:return self.err(400)
    elif cmd=='shrug':
     body=(arg+' ' if arg else '')+'¯'+chr(92)+'_(ツ)_/¯'
    elif cmd=='topic':
     if ch['is_archived']:return self.err(423)
     if not arg or len(arg)>250:return self.err(400)
     if not member(cid,u['id']) and role(ch['workspace_id'],u['id'])!='owner':return self.err(403)
     with conn() as c:c.execute('update channels set topic=? where id=?',(arg,cid));c.commit()
     event(cid,'channel.updated',cj(chan(cid)));return self.sendj(201,{'message':None,'channel':cj(chan(cid))})
    elif cmd=='invite':
     if not member(cid,u['id']):return self.err(403)
     if not arg.startswith('@') or len(arg)<2:return self.err(400)
     with conn() as c:target=c.execute('select * from users where username=?',(arg[1:],)).fetchone()
     if not target:return self.err(404)
     if not role(ch['workspace_id'],target['id']):return self.err(403)
     with conn() as c:c.execute('insert or ignore into cm values(?,?)',(cid,target['id']));c.commit()
     event(cid,'member.joined',{'user_id':target['id']});return self.sendj(201,{'message':None,'invited_user':user_to_json(target)})
    elif cmd in ('archive','unarchive'):
     if role(ch['workspace_id'],u['id'])!='owner':return self.err(403)
     val=1 if cmd=='archive' else 0
     with conn() as c:c.execute('update channels set is_archived=? where id=?',(val,cid));c.commit()
     event(cid,'channel.archived' if val else 'channel.unarchived',cj(chan(cid)))
     return self.sendj(201,{'message':None,'channel':cj(chan(cid))})
    else:return self.err(400)
   if not isinstance(body,str) or not body.strip():return self.err(400)
   data['body']=body
  if path.startswith('/api/channels/') and path.endswith(('/archive','/unarchive')):
   if not u:return self.err(401)
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   if role(ch['workspace_id'],u['id'])!='owner':return self.err(403)
   val=0 if path.endswith('unarchive') else 1
   with conn() as c:c.execute('update channels set is_archived=? where id=?',(val,cid));c.commit()
   event(cid,'channel.unarchived' if not val else 'channel.archived',cj(chan(cid)))
   return self.sendj(200,{'channel':cj(chan(cid))})
  if path.startswith('/api/workspaces/') and path.endswith('/transfer_ownership'):
   if not u:return self.err(401)
   w=wsrow(path.split('/')[3])
   if not w:return self.err(404)
   if u['id']!=w['owner_id']:return self.err(403)
   tid=data.get('new_owner_id')
   if role(w['id'],tid)!='admin':return self.err(400)
   with conn() as c:
    c.execute('update wm set role="admin" where workspace_id=? and user_id=?',(w['id'],u['id']))
    c.execute('update wm set role="owner" where workspace_id=? and user_id=?',(w['id'],tid))
    c.execute('update workspaces set owner_id=? where id=?',(tid,w['id']));c.commit()
   return self.sendj(200,{'workspace':wsjson(wsrow(w['slug']))})
  if path=='/api/auth/register':
   if not validate_username(data.get('username')) or not validate_password(data.get('password')):return self.err(400)
   try:
    with conn() as c:
     cur=c.execute('insert into users(username,password,display_name) values(?,?,?)',(data['username'],hash_password(data['password']),data.get('display_name') or data['username']));uid=cur.lastrowid;c.commit()
   except sqlite3.IntegrityError:return self.err(409)
   return self.sendj(201,{'user':user_to_json(user_row(uid)),'token':issue_token(uid)})
  if path=='/api/auth/login':
   with conn() as c:x=c.execute('select * from users where username=?',(data.get('username'),)).fetchone()
   if not x or not verify_password(data.get('password',''),x['password']):return self.err(401)
   return self.sendj(200,{'user':user_to_json(x),'token':issue_token(x['id'])})
  if path=='/api/auth/me':return self.sendj(200,{'user':user_to_json(u)}) if u else self.err(401)
  if not u:return self.err(401)
  if path.startswith('/api/workspaces/') and path.endswith('/groups'):
   w=wsrow(path.split('/')[3])
   if not w:return self.err(404)
   if not role(w['id'],u['id']):return self.err(403)
   h=data.get('handle'); name=data.get('name',''); ids=data.get('user_ids',[])
   if not valid_group_handle(h):return self.err(400)
   if group_row(w['id'],h):return self.err(409)
   with conn() as c:
    cur=c.execute('insert into groupsx(workspace_id,handle,name,creator_id,users) values(?,?,?,?,?)',(w['id'],h,name,u['id'],json.dumps(ids)));gid=cur.lastrowid;c.commit()
   return self.sendj(201,{'group':group_json(group_row(w['id'],h))})
  if path=='/api/dms':
   other=data.get('user_id')
   if other==u['id']:return self.err(400)
   if not user_row(other):return self.err(404)
   old=dm_for(u['id'],other)
   if old:return self.sendj(200,{'channel_id':old['id']})
   with conn() as c:
    cur=c.execute("insert into channels(workspace_id,name,is_private,is_dm) values(NULL,?,?,1)",('dm-%d-%d'%tuple(sorted((u['id'],other))),1));cid=cur.lastrowid
    c.execute('insert into cm values(?,?)',(cid,u['id']));c.execute('insert into cm values(?,?)',(cid,other));c.commit()
   return self.sendj(200,{'channel_id':cid})
  if path=='/api/files':
   raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
   if len(raw)>10485760+1048576:return self.err(413)
   ct=self.headers.get('Content-Type','');bm=re.search(r'boundary=([^;]+)',ct)
   if not bm:return self.err(400)
   boundary=('--'+bm.group(1).strip('"')).encode();blob=None;filename='upload';ctype='application/octet-stream'
   for part in raw.split(boundary):
    if b'name="file"' not in part:continue
    head,sep,val=part.partition(b'\r\n\r\n')
    if not sep:continue
    fm=re.search(br'filename="([^"]*)"',head);tm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
    filename=fm.group(1).decode(errors='replace') if fm else filename;ctype=tm.group(1).decode().strip() if tm else ctype;blob=val[:-2] if val.endswith(b'\r\n') else val
   if blob is None:return self.err(400)
   if len(blob)>10485760:return self.err(413)
   pathf=os.path.join(DATA,secrets.token_hex(16));open(pathf,'wb').write(blob);ts=now()
   with conn() as c:
    cur=c.execute('insert into files(uploader_id,filename,content_type,size,path,created_at) values(?,?,?,?,?,?)',(u['id'],filename,ctype,len(blob),pathf,ts));fid=cur.lastrowid;c.commit()
   return self.sendj(201,{'file':filejson(file_row(fid))})
  if path.startswith('/api/invitations/') and path.endswith('/accept'):
   code=path.split('/')[3]
   with conn() as c:inv=c.execute('select * from invites where code=?',(code,)).fetchone()
   if not inv or inv['uses']>=inv['max_uses'] or inv['expires']<int(time.time()):return self.err(404)
   if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return self.err(403)
   with conn() as c:
    c.execute('update invites set uses=uses+1 where code=?',(code,))
    c.execute('insert or ignore into wm values(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.commit()
   return self.sendj(200,{'workspace':wsjson_by_id(inv['workspace_id']),'member':{'user_id':u['id'],'role':role(inv['workspace_id'],u['id'])}})
  if path.startswith('/api/workspaces/') and path.endswith('/invitations'):
   w=wsrow(path.split('/')[3])
   if not w:return self.err(404)
   if role(w['id'],u['id']) not in ('owner','admin'):return self.err(403)
   target=data.get('invited_user_id')
   if data.get('invited_username'):
    with conn() as c:targetrow=c.execute('select id from users where username=?',(data['invited_username'],)).fetchone()
    if not targetrow:return self.err(404)
    target=targetrow['id']
   try:mx=int(data.get('max_uses',1))
   except:return self.err(400)
   if mx<1:return self.err(400)
   mx=1 if target else mx
   exp=int(time.time())+int(data.get('expires_in_seconds',604800))
   code=secrets.token_urlsafe(16)
   with conn() as c:c.execute('insert into invites(code,workspace_id,invited_user_id,expires,max_uses) values(?,?,?,?,?)',(code,w['id'],target,exp,mx));c.commit()
   return self.sendj(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'uses':0,'expires_at':datetime.datetime.fromtimestamp(exp,datetime.timezone.utc).isoformat().replace('+00:00','Z'),'invited_user_id':target}})
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',data.get('slug','')):return self.err(400)
   try:
    with conn() as c:
     cur=c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(data['slug'],data.get('name',''),u['id']));wid=cur.lastrowid
     c.execute('insert into wm values(?,?,?)',(wid,u['id'],'owner'));cur=c.execute("insert into channels(workspace_id,name) values(?, 'general')",(wid,));cid=cur.lastrowid;c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit()
   except sqlite3.IntegrityError:return self.err(409)
   return self.sendj(201,{'workspace':wsjson(wsrow(data['slug'])),'general_channel':cj(chan(cid))})
  if path.startswith('/api/workspaces/') and path.endswith('/channels'):
   w=wsrow(path.split('/')[3])
   if not w:return self.err(404)
   r=role(w['id'],u['id'])
   if not r or not can_create(r,bool(data.get('is_private',False))):return self.err(403)
   if not cname(data.get('name')) or len(data.get('topic',''))>250:return self.err(400)
   priv=bool(data.get('is_private',False));r=role(w['id'],u['id'])
   if priv and r not in ('owner','admin'):return self.err(403)
   with conn() as c:
    if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],data['name'])).fetchone():return self.err(409)
   try:
    with conn() as c:cur=c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],data['name'],int(priv),data.get('topic','')));cid=cur.lastrowid;c.execute('insert into cm values(?,?)',(cid,u['id']));c.commit()
   except sqlite3.IntegrityError:return self.err(409)
   return self.sendj(201,{'channel':cj(chan(cid))})
  if path.startswith('/api/channels/') and path.endswith('/join'):
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   already=member(cid,u['id']); wr=role(ch['workspace_id'],u['id'])
   if ch['is_private'] and not already:return self.err(403)
   if not already:
    if wr=='guest':return self.err(403)
    if wr is None:
     with conn() as c:w=c.execute('select join_mode from workspaces where id=?',(ch['workspace_id'],)).fetchone()
     if not w or w['join_mode']!='open':return self.err(403)
    with conn() as c:c.execute('insert or ignore into wm values(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('insert or ignore into cm values(?,?)',(cid,u['id']));c.commit()
    event(cid,'member.joined',{'user_id':u['id']})
   return self.sendj(200,{'channel':cj(ch)})
  if path.startswith('/api/channels/') and path.endswith('/leave'):
   cid=int(path.split('/')[3]); existed=member(cid,u['id'])
   with conn() as c:c.execute('delete from cm where channel_id=? and user_id=?',(cid,u['id']));c.commit()
   if existed:event(cid,'member.left',{'user_id':u['id']})
   return self.sendj(200,{'channel':cj(chan(cid))})
  if path.startswith('/api/channels/') and path.endswith('/read'):
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   if not member(cid,u['id']):return self.err(403)
   val=int(data.get('last_read_event_id',0))
   with conn() as c:
    old=c.execute('select last_read from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone()
    val=max(val,old['last_read'] if old else 0);c.execute('insert or replace into reads values(?,?,?)',(u['id'],cid,val));c.commit()
   return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':val,'unread_count':0,'mention_count':0}})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   cid=int(path.split('/')[3]);ch=chan(cid)
   if not ch:return self.err(404)
   if ch['is_archived']:return self.err(423)
   if not member(cid,u['id']):return self.err(403)
   body=data.get('body')
   if not isinstance(body,str) or not body.strip():return self.err(400)
   parent=data.get('parent_id')
   if parent and not getmsg(parent):return self.err(404)
   fids=data.get('file_ids') or []
   if not isinstance(fids,list):return self.err(400)
   with conn() as c:
    for fid in fids:
     f=c.execute('select * from files where id=?',(fid,)).fetchone()
     if not f or f['uploader_id']!=u['id'] or c.execute('select 1 from attachments where file_id=?',(fid,)).fetchone():return self.err(400)
   with conn() as c:
    cur=c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(cid,u['id'],body,parent,now()));mid=cur.lastrowid
    for fid in fids:c.execute('insert into attachments values(?,?)',(mid,fid))
    c.commit()
   m=getmsg(mid); ev=event(cid,'message.reply' if parent else 'message.created',msgjson(m)); return self.sendj(201,{'message':msgjson(m)})
  if path.startswith('/api/messages/') and path.endswith('/reactions'):
   mid=int(path.split('/')[3]);m=getmsg(mid)
   if not m:return self.err(404)
   ch=chan(m['channel_id'])
   if not member(ch['id'],u['id']):return self.err(403)
   emoji=data.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.err(400)
   with conn() as c:
    before=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)).fetchone()
    c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],emoji));c.commit()
   rs=reactions_for(mid)
   if not before:event(ch['id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':rs})
   return self.sendj(200,{'reactions':rs})
  if path.startswith('/api/messages/') and path.endswith('/pin'):
   mid=int(path.split('/')[3]);m=getmsg(mid)
   if not m:return self.err(404)
   ch=chan(m['channel_id'])
   if not member(ch['id'],u['id']):return self.err(403)
   with conn() as c:c.execute('insert or ignore into pins values(?,?,?)',(mid,u['id'],now()));c.commit()
   return self.sendj(200,{'pin':{'message_id':mid,'pinned_by':u['id'],'pinned_at':now()}})
  return self.err(404)
 def do_PATCH(self):
  u=self.auth();data=self.body()
  if not u:return self.err(401)
  if self.path.startswith('/api/workspaces/') and '/groups/' in self.path:
   a=self.path.split('/');w=wsrow(a[3]);h=a[5]
   if not w:return self.err(404)
   g=group_row(w['id'],h)
   if not g:return self.err(404)
   if role(w['id'],u['id'])!= 'owner' and g['creator_id']!=u['id']:return self.err(403)
   with conn() as c:
    if 'name' in data:c.execute('update groupsx set name=? where id=?',(data['name'],g['id']))
    if 'user_ids' in data:c.execute('update groupsx set users=? where id=?',(json.dumps(data['user_ids']),g['id']))
    c.commit()
   return self.sendj(200,{'group':group_json(group_row(w['id'],h))})
  if self.path.startswith('/api/workspaces/') and '/members/' in self.path:
   a=self.path.split('/'); w=wsrow(a[3])
   if not w:return self.err(404)
   try: tid=int(a[5])
   except:return self.err(404)
   tr=role(w['id'],tid); ar=role(w['id'],u['id']); nr=data.get('role')
   if tr is None:return self.err(404)
   if not valid_role(nr) or nr=='owner':return self.err(400)
   if tid==w['owner_id'] or ar not in ('owner','admin') or (ar=='admin' and tr=='admin'):return self.err(403)
   with conn() as c:c.execute('update wm set role=? where workspace_id=? and user_id=?',(nr,w['id'],tid));c.commit()
   return self.sendj(200,{'member':{'user_id':tid,'role':nr}})
  if self.path.startswith('/api/users/me'):
   if not u:return self.err(401)
   fields={k:data[k] for k in ('display_name','timezone','avatar_url','status_text','status_emoji') if k in data}
   if not validate_profile(fields):return self.err(400)
   with conn() as c:
    for k,v in fields.items(): c.execute('update users set '+k+'=? where id=?',(v,u['id']))
    c.commit()
   updated=user_to_json(user_row(u['id']))
   with conn() as c:
    c.execute('insert into global_events(payload,created_at) values(?,?)',(json.dumps({'type':'user.updated','user':updated}),now()))
    c.commit()
   return self.sendj(200,{'user':updated})

  if not u:return self.err(401)
  p=self.path.split('/')
  if self.path.startswith('/api/workspaces/') and '/members/' not in self.path:
   w=wsrow(self.path.split('/')[3])
   if not w:return self.err(404)
   if role(w['id'],u['id']) not in ('owner','admin'):return self.err(403)
   if 'join_mode' in data and data['join_mode'] not in ('open','invite_only'):return self.err(400)
   with conn() as c:
    if 'name' in data:c.execute('update workspaces set name=? where id=?',(data['name'],w['id']))
    if 'join_mode' in data:c.execute('update workspaces set join_mode=? where id=?',(data['join_mode'],w['id']))
    c.commit()
   return self.sendj(200,{'workspace':wsjson(wsrow(w['slug']))})
  if self.path.startswith('/api/channels/'):
   cid=int(p[3]);ch=chan(cid)
   if not ch:return self.err(404)
   if ch['is_archived']:return self.err(423)
   if data.get('topic') is None or len(data['topic'])>250:return self.err(400)
   if not member(cid,u['id']) and role(ch['workspace_id'],u['id'])!='owner':return self.err(403)
   with conn() as c:c.execute('update channels set topic=? where id=?',(data['topic'],cid));c.commit()
   event(cid,'channel.updated',cj(chan(cid)));return self.sendj(200,{'channel':cj(chan(cid))})
  if self.path.startswith('/api/messages/'):
   mid=int(p[3])
   with conn() as c:m0=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not m0:return self.err(404)
   if m0['deleted']:return self.err(409)
   if m0['author_id']!=u['id']:return self.err(403)
   body=data.get('body')
   if not isinstance(body,str) or not body.strip():return self.err(400)
   edited=now()
   with conn() as c:c.execute('update messages set body=?,edited_at=? where id=?',(body,edited,mid));c.commit()
   m=getmsg(mid);event(m['channel_id'],'message.updated',{'message':msgjson(m)})
   return self.sendj(200,{'message':msgjson(m)})
  return self.err(404)
 def do_DELETE(self):
  u=self.auth()
  if not u:return self.err(401)
  if self.path.startswith('/api/workspaces/') and '/invitations/' in self.path:
   a=self.path.split('/');w=wsrow(a[3]);code=a[5]
   if not w:return self.err(404)
   if role(w['id'],u['id']) not in ('owner','admin'):return self.err(403)
   with conn() as c:x=c.execute('select * from invites where code=? and workspace_id=?',(code,w['id'])).fetchone()
   if not x:return self.err(404)
   with conn() as c:c.execute('update invites set uses=max_uses where code=?',(code,));c.commit()
   return self.sendj(200,{'deleted':True})
  if self.path.startswith('/api/workspaces/') and '/groups/' in self.path:
   a=self.path.split('/');w=wsrow(a[3]);h=a[5]
   if not w:return self.err(404)
   g=group_row(w['id'],h)
   if not g:return self.sendj(200,{'deleted':True})
   if role(w['id'],u['id'])!='owner' and g['creator_id']!=u['id']:return self.err(403)
   with conn() as c:c.execute('delete from groupsx where id=?',(g['id'],));c.commit()
   return self.sendj(200,{'deleted':True})
  if '/reactions/' in self.path:
   a=self.path.split('/');mid=int(a[3]);emoji=urllib.parse.unquote(a[5]);m=getmsg(mid)
   if not m:return self.err(404)
   ch=chan(m['channel_id'])
   if not member(ch['id'],u['id']):return self.err(403)
   if not emoji:return self.err(400)
   with conn() as c:
    before=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)).fetchone()
    c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji));c.commit()
   rs=reactions_for(mid)
   if before:event(ch['id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id'],'reactions':rs})
   return self.sendj(200,{'reactions':rs})
  if self.path.startswith('/api/messages/') and self.path.endswith('/pin'):
   with conn() as c:c.execute('delete from pins where message_id=?',(int(self.path.split('/')[3]),));c.commit()
   return self.sendj(200,{'unpinned':True})
  if self.path.startswith('/api/messages/'):
   mid=int(self.path.split('/')[3])
   with conn() as c:m=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not m:return self.err(404)
   ch=chan(m['channel_id']); ar=role(ch['workspace_id'],u['id'])
   if m['author_id']!=u['id'] and ar!='owner':return self.err(403)
   if not m['deleted']:
    with conn() as c:c.execute('update messages set deleted=1 where id=?',(mid,));c.commit()
    with conn() as c:c.execute('delete from reactions where message_id=?',(mid,));c.commit()
    event(m['channel_id'],'message.deleted',{'message_id':mid})
   return self.sendj(200,{'deleted':True})
  return self.err(404)
 def do_PUT(self): self.do_PATCH()
 def html(self):
  with open('/app/static/index.html','rb') as f:b=f.read()
  self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def ws(self):
  t=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
  wsuser=user_from_token(t)
  if not wsuser:return self.send_error(401)
  conn_id=secrets.token_urlsafe(12)
  with conn() as pc:
   pc.execute('insert or replace into presence values(?,?,?)',(conn_id,wsuser['id'],time.time()))
   global_last=pc.execute('select coalesce(max(id),0) n from global_events').fetchone()['n']; pc.commit()
  key=self.headers.get('Sec-WebSocket-Key','');accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  self.connection.setblocking(False);last={}
  try:
   while True:
    with conn() as pc: pc.execute('update presence set last_seen=? where conn_id=?',(time.time(),conn_id)); pc.commit()
    try:
     with conn() as gc:
      ge=gc.execute('select id,payload from global_events where id>? order by id',(global_last,)).fetchall()
     for g in ge:
      self.wswrite(json.loads(g['payload']));global_last=g['id']
    except Exception:
     pass
    ready,_,_=select.select([self.connection],[],[],.1)
    if ready:
     h=self.connection.recv(2)
     if not h:break
     while len(h)<2:h+=self.connection.recv(2-len(h))
     op=h[0]&15;n=h[1]&127
     if n==126:
      x=b''
      while len(x)<2:x+=self.connection.recv(2-len(x))
      n=int.from_bytes(x,'big')
     elif n==127:
      x=b''
      while len(x)<8:x+=self.connection.recv(8-len(x))
      n=int.from_bytes(x,'big')
     mask=b''
     if h[1]&128:
      while len(mask)<4:mask+=self.connection.recv(4-len(mask))
     d=b''
     while len(d)<n:d+=self.connection.recv(n-len(d))
     if mask:d=bytes(x^mask[i%4] for i,x in enumerate(d))
     if op==8:break
     if op==9:self.connection.sendall(bytes([138,len(d)])+d);continue
     if op==1:
      o=json.loads(d);cid=o.get('channel_id')
      if o.get('type') in ('subscribe','resume'):
       since=int(o.get('since_event_id',0));last[cid]=since
       with conn() as c:es=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall()
       if o['type']=='subscribe':
        head=es[-1]['event_id'] if es else since
        last[cid]=head
        self.wswrite({'type':'subscribed','channel_id':cid,'head_event_id':head})
       else:
        for e in es:self.wswrite(event_frame(e));last[cid]=e['event_id']
        self.wswrite({'type':'resumed','channel_id':cid,'head_event_id':es[-1]['event_id'] if es else since})
    for cid,since in list(last.items()):
     with conn() as c:es=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall()
     for e in es:
      self.wswrite(event_frame(e));last[cid]=e['event_id']
  except Exception:pass
 def wswrite(self,o):
  d=json.dumps(o,separators=(',',':')).encode()
  n=len(d)
  if n<126: h=bytes([129,n])
  elif n<65536: h=bytes([129,126])+n.to_bytes(2,'big')
  else: h=bytes([129,127])+n.to_bytes(8,'big')
  try:self.connection.sendall(h+d)
  except (BrokenPipeError,ConnectionResetError,TimeoutError):raise

def event_frame(e):
 payload=json.loads(e['payload'])
 out={'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id']}
 if e['kind'] in ('message.created','message.reply','message.updated','message.deleted'):
  out['message']=payload
 else: out.update(payload)
 return out
def redis_listener():
 while True:
  try:
   z=socket.create_connection(('127.0.0.1',6379),1)
   req=b'*2'+bytes([13,10])+b'$9'+bytes([13,10])+b'SUBSCRIBE'+bytes([13,10])+b'$13'+bytes([13,10])+b'huddle:events'+bytes([13,10])
   z.sendall(req); z.settimeout(2)
   while True:
    if not z.recv(4096): raise OSError()
  except Exception:
   time.sleep(.5)
threading.Thread(target=redis_listener,daemon=True).start()
NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;color:#222}button[data-button-role=primary]{background:#007a5a;color:#fff}button[data-button-role=danger]{background:#e01e5a;color:#fff}button[data-button-role=secondary]{background:#1264a3;color:#fff}.app{display:flex;height:100vh}.side{width:250px;background:#3f0f52;color:white;padding:20px}.main{flex:1;padding:25px}.thread{width:300px;padding:20px;border-left:1px solid #ddd}.hidden{display:none}input,button{padding:9px;margin:4px}</style></head><body><div id=app></div><script>
const A='/api';let token=localStorage.getItem('huddle.token'),user,cur,channels=[];
async function api(p,o={}){o.headers={...(o.headers||{}),...(token?{Authorization:'Bearer '+token}:{})};if(o.body&&typeof o.body!='string'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(o.body)}let r=await fetch(A+p,o);return [r.status,await r.json().catch(()=>({}))]}
function render(){if(!token){app.innerHTML='<div id=auth-modal><form data-testid=auth-form><h1>Huddle</h1><input name=username placeholder=username><input name=password type=password placeholder=password><input name=display_name placeholder=display name><button data-testid=auth-submit data-button-role=primary>Sign in / Sign up</button><button type=button data-testid=auth-toggle>Register</button><div id=auth-error></div></form></div>';document.querySelector('form').onsubmit=async e=>{e.preventDefault();let d=Object.fromEntries(new FormData(e.target));let [s,x]=await api('/auth/register',{method:'POST',body:d});if(s!=201)[s,x]=await api('/auth/login',{method:'POST',body:d});if(s<300){token=x.token;localStorage.setItem('huddle.token',token);render()}else document.querySelector('#auth-error').textContent='Invalid credentials or registration';};return}
app.innerHTML='<div class=app><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user>'+user?.display_name+'</div><button data-testid=logout-btn data-button-role=danger>Logout</button><button data-testid=new-channel-btn data-button-role=secondary>New channel</button><div data-testid=channel-list id=channels></div></aside><main class=main><h2 data-testid=channel-title># '+(cur?.name||'general')+'</h2><div data-testid=channel-topic>'+ (cur?.topic||'')+'</div><div data-testid=message-list id=msgs></div><form id=send><input data-testid=message-input name=body><button data-testid=send-btn data-button-role=primary>Send</button></form></main><aside class=thread hidden data-testid=thread-panel><button data-testid=close-thread data-button-role=secondary>Close</button><div id=threadmsgs></div><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Reply</button></aside></div>';load()}
async function load(){let [,m]=await api('/auth/me');user=m.user;let [,w]=await api('/workspaces');if(!w.workspaces.length){let [,x]=await api('/workspaces',{method:'POST',body:{slug:'my-workspace',name:'My Workspace'}});cur=x.general_channel;channels=[cur]}else{let [,x]=await api('/workspaces/'+w.workspaces[0].slug);channels=x.channels;cur=channels[0]}channelsEl();messages();document.querySelector('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token');token=null;render()};document.querySelector('#send').onsubmit=async e=>{e.preventDefault();let d=Object.fromEntries(new FormData(e.target));if(d.body.trim()){await api('/channels/'+cur.id+'/messages',{method:'POST',body:d});e.target.reset();messages()}}}
function channelsEl(){channelsEl=()=>{};document.querySelector('#channels').innerHTML=channels.map(c=>`<div data-testid=channel-entry data-channel-id="${c.id}" data-channel-name="${c.name}" onclick="cur=channels.find(x=>x.id==${c.id});messages()">#${c.name}</div>`).join('')}
async function messages(){let [,x]=await api('/channels/'+cur.id+'/messages');document.querySelector('#msgs').innerHTML=x.messages.map(m=>`<div data-testid=message><b>${m.author.display_name}</b> <span data-testid=message-body>${m.body}</span> <button data-testid=open-thread-btn onclick="openThread(${m.id})"> ${m.reply_count||0} replies</button><button data-testid=reaction-button onclick="react(${m.id})">😀</button></div>`).join('')||'<div data-testid=empty-state>No messages yet — start the conversation!</div>'}
async function react(id){await api('/messages/'+id+'/reactions',{method:'POST',body:{emoji:'😀'}});messages()}async function openThread(id){document.querySelector('[data-testid=thread-panel]').hidden=false;let [,x]=await api('/messages/'+id+'/replies');document.querySelector('#threadmsgs').innerHTML=x.replies.map(r=>'<p>'+r.body+'</p>').join('')}
render();
</script></body></html>'''
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',8000+NODE),H).serve_forever()
