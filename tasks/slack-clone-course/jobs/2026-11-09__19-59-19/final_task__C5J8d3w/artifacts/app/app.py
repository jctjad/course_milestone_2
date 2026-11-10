import sys, os, json, re, sqlite3, hashlib, secrets, base64, struct, socket, threading, urllib.parse, select, time
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
DB='/var/lib/huddle/huddle.sqlite3'
NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
PORT=8000+NODE
os.makedirs('/var/lib/huddle',exist_ok=True)
SCHEMA='''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,reply_count INTEGER DEFAULT 0,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0,expires_at TEXT,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,handle TEXT,name TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,last_seen REAL);
CREATE TABLE IF NOT EXISTS global_updates(id INTEGER PRIMARY KEY,payload TEXT);
'''
def db():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row; return c
with db() as c:
 c.executescript(SCHEMA)
 try:
  c.execute('select 1 from events limit 1')
  info=list(c.execute('pragma table_info(events)'))
  pk=[x[1] for x in info if x[5]]
  if pk != ['channel_id','event_id']:
   c.execute('alter table events rename to events_old')
   c.execute('create table events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,primary key(channel_id,event_id))')
   c.execute('insert or ignore into events select channel_id,event_id,kind,payload from events_old')
   c.execute('drop table events_old')
 except Exception: pass
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def q1(c,s,p=()): return c.execute(s,p).fetchone()
def validate_username(x): return isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_]+',x or '') is not None
def validate_password(x): return isinstance(x,str) and len(x)>=8
def hash_password(x): return hashlib.pbkdf2_hmac('sha256',x.encode(),b'huddle',120000).hex()
def verify_password(x,h): return secrets.compare_digest(hash_password(x),h)
def ujson(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def wjson(r): return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def cjson(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def role(c,w,u):
 r=q1(c,'select role from members where workspace_id=? and user_id=?',(w,u)); return r['role'] if r else None
def auth(h):
 x=h.headers.get('Authorization','')
 if not x.startswith('Bearer '): return None
 with db() as c:
  r=q1(c,'select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(x[7:],)); return r
def send(h,code,obj,typ='application/json'):
 b=json.dumps(obj,separators=(',',':')).encode(); h.send_response(code); h.send_header('Content-Type',typ); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)
def err(h,n,s): send(h,n,{'error':s})
def body(h):
 n=int(h.headers.get('Content-Length','0')); return json.loads(h.rfile.read(n) or b'{}')
def event(c,ch,kind,payload):
 for attempt in range(20):
  try:
   c.execute('BEGIN IMMEDIATE')
   r=q1(c,'select coalesce(max(event_id),0)+1 n from events where channel_id=?',(ch,))
   eid=r['n']
   c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(ch,eid,kind,json.dumps(payload)))
   c.commit()
   return eid
  except (sqlite3.IntegrityError,sqlite3.OperationalError):
   try:c.rollback()
   except:pass
   if attempt==19: raise
   time.sleep(0.003)
def extract_mentions(c,channel_id,author_id,text):
 out=set()
 ch=q1(c,'select workspace_id from channels where id=?',(channel_id,))
 if not ch:return []
 members={x['user_id'] for x in c.execute('select user_id from members where workspace_id=?',(ch['workspace_id'],))}
 if '@channel' in text:
  out.update(x['user_id'] for x in c.execute('select user_id from channel_members where channel_id=?',(channel_id,)) if x['user_id']!=author_id)
 for gm in re.finditer(r'(?<![A-Za-z0-9_.])@([a-z0-9][a-z0-9_-]*)',text.lower()):
  name=gm.group(1)
  if name in ('channel','here'): 
   if name=='here': out.update(x['user_id'] for x in c.execute('select cm.user_id from channel_members cm join ws_presence p on p.user_id=cm.user_id where cm.channel_id=? and cm.user_id!=?',(channel_id,author_id)))
   continue
  g=q1(c,'select g.id from groups g join channels ch on ch.workspace_id=g.workspace_id where g.handle=? and ch.id=?',(name,channel_id))
  if g: out.update(x['user_id'] for x in c.execute('select user_id from group_members where group_id=? and user_id in (select user_id from channel_members where channel_id=?) and user_id!=?',(g['id'],channel_id,author_id)))
 for m in re.finditer(r'(?<![A-Za-z0-9_.])@([A-Za-z0-9_]+)',text):
  name=m.group(1)
  if name.lower()=='channel': continue
  x=q1(c,'select id from users where username=?',(name,))
  if x and x['id'] in members and x['id']!=author_id: out.add(x['id'])
 return sorted(out)
def reactions(c,mid):
 out=[]
 for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji order by emoji',(mid,)):
  ids=[r['user_id'] for r in c.execute('select user_id from reactions where message_id=? and emoji=? order by user_id',(mid,x['emoji']))]
  out.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 return out
def groupjson(c,g):
 return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('select user_id from group_members where group_id=? order by user_id',(g['id'],))]}
def filejson(r):
 return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def msgjson(c,r):
 u=q1(c,'select * from users where id=?',(r['author_id'],))
 rx=[]
 for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
  ids=[z['user_id'] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]; rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':ujson(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':r['reply_count'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':rx,'mentions':[x['user_id'] for x in c.execute('select user_id from message_mentions where message_id=? order by user_id',(r['id'],))],'files':[filejson(x) for x in c.execute('select f.* from files f join message_files mf on mf.file_id=f.id where mf.message_id=?',(r['id'],))]}
def validate_profile(fields):
 for k,v in fields.items():
  if k=='display_name' and (not isinstance(v,str) or not 1<=len(v)<=80): raise ValueError()
  if k=='timezone':
   if not isinstance(v,str):
    raise ValueError()
   try: ZoneInfo(v)
   except: raise ValueError()
  if k=='status_text' and (not isinstance(v,str) or len(v)>100): raise ValueError()
  if k=='status_emoji' and (not isinstance(v,str) or len(v)>64): raise ValueError()
  if k=='avatar_url' and (not isinstance(v,str) or len(v)>2048): raise ValueError()
def read_state(c,user_id,channel_id):
 r=q1(c,'select last_read_event_id from reads where user_id=? and channel_id=?',(user_id,channel_id))
 last=int(r['last_read_event_id']) if r else 0
 unread=mentions=0
 for e in c.execute('select kind,payload from events where channel_id=? and event_id>? order by event_id',(channel_id,last)):
  if e['kind'] not in ('message.created','message.reply'): continue
  try: m=json.loads(e['payload']).get('message',{})
  except: continue
  if m.get('author_id')==user_id: continue
  unread+=1
  if user_id in m.get('mentions',[]): mentions+=1
 return {'channel_id':int(channel_id),'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def page(vals,limit,cur):
 start=int(cur) if cur else 0
 out=vals[start:start+limit]; nxt=str(start+limit) if start+limit<len(vals) else None
 return out,nxt
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def parsed_path(self): return urllib.parse.urlparse(self.path)
 def route(self):
  p=urllib.parse.urlparse(self.path); return p.path,urllib.parse.parse_qs(p.query)
 def do_GET(self):
  p,qs=self.route()
  if p=='/api/health': return send(self,200,{'status':'ok','node_id':NODE})
  if p=='/': return self.ui()
  if p=='/api/ws': return self.ws()
  u=auth(self)
  if p=='/api/auth/me':
   if not u:return err(self,401,'unauthorized')
   return send(self,200,{'user':ujson(u)})
  if not u:return err(self,401,'unauthorized')
  with db() as c:
   m=re.fullmatch(r'/api/files/(\d+)/download',p)
   if m:
    f=q1(c,'select * from files where id=?',(m.group(1),))
    if not f:return err(self,404,'not found')
    data=f['data'];self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
   m=re.fullmatch(r'/api/files/(\d+)',p)
   if m:
    f=q1(c,'select * from files where id=?',(m.group(1),));return send(self,200,{'file':filejson(f)}) if f else err(self,404,'not found')
   m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if not role(c,w['id'],u['id']):return err(self,403,'forbidden')
    return send(self,200,{'groups':[groupjson(c,g) for g in c.execute('select * from groups where workspace_id=?',(w['id'],))]})
   if p=='/api/workspaces':
    xs=[] 
    for w in c.execute('select * from workspaces where id in (select workspace_id from members where user_id=?)',(u['id'],)): xs.append(wjson(w))
    return send(self,200,{'workspaces':xs})
   m=re.fullmatch(r'/api/workspaces/([^/]+)(?:/members|/invitations)?',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if p.endswith('/members'):
     if not role(c,w['id'],u['id']): return err(self,403,'forbidden')
     return send(self,200,{'members':[dict(x) for x in c.execute('select user_id,role from members where workspace_id=?',(w['id'],))]})
    if p.endswith('/invitations'):
     if role(c,w['id'],u['id']) not in ('owner','admin'):return err(self,403,'forbidden')
     xs=[dict(x) for x in c.execute('select * from invitations where workspace_id=? and revoked=0 and uses<max_uses and (expires_at is null or expires_at>?)',(w['id'],now()))]
     return send(self,200,{'invitations':xs})
    if not role(c,w['id'],u['id']):return err(self,403,'forbidden')
    cs=[cjson(x) for x in c.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'],1 if qs.get('include_archived',['0'])[0] in ('1','true') else 0)) if not x['is_private'] or q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(x['id'],u['id']))]
    return send(self,200,{'workspace':wjson(w),'channels':cs,'read_state':[read_state(c,u['id'],x['id']) for x in c.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'],1 if qs.get('include_archived',['0'])[0] in ('1','true') else 0)) if not x['is_private'] or q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(x['id'],u['id']))]})
   m=re.fullmatch(r'/api/channels/(\d+)/read',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    if ch['is_private'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])):return err(self,403,'forbidden')
    return send(self,200,{'read_state':read_state(c,u['id'],ch['id'])})
   m=re.fullmatch(r'/api/channels/(\d+)/members',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    return send(self,200,{'members':[ujson(x) for x in c.execute('select u.* from users u join channel_members m on m.user_id=u.id where m.channel_id=?',(ch['id'],))]})
   m=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    w=q1(c,'select * from workspaces where id=?',(ch['workspace_id'],)); rr=role(c,w['id'],u['id'])
    action=m.group(2)
    if action=='leave':
     removed=c.execute('delete from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).rowcount
     if removed:event(c,ch['id'],'member.left',{'user_id':u['id']})
     return send(self,200,{'channel':cjson(ch)})
    if rr!='owner':return err(self,403,'forbidden')
    c.execute('update channels set is_archived=? where id=?',(1 if action=='archive' else 0,ch['id'])); updated=q1(c,'select * from channels where id=?',(ch['id'],)); event(c,ch['id'],'channel.archived' if action=='archive' else 'channel.unarchived',{'channel':cjson(updated)})
    return send(self,200,{'channel':cjson(updated)})
   m=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    w=q1(c,'select * from workspaces where id=?',(ch['workspace_id'],))
    action=m.group(2)
    if action=='leave':
     removed=c.execute('delete from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).rowcount
     if removed:event(c,ch['id'],'member.left',{'user_id':u['id']})
     return send(self,200,{'channel':cjson(ch)})
    if u['id']!=w['owner_id']:return err(self,403,'forbidden')
    c.execute('update channels set is_archived=? where id=?',(1 if action=='archive' else 0,ch['id']))
    updated=q1(c,'select * from channels where id=?',(ch['id'],))
    event(c,ch['id'],'channel.archived' if action=='archive' else 'channel.unarchived',{'channel':cjson(updated)})
    return send(self,200,{'channel':cjson(updated)})
   m=re.fullmatch(r'/api/messages/(\d+)/replies',p)
   if m:
    parent=q1(c,'select * from messages where id=?',(m.group(1),))
    if not parent:return err(self,404,'not found')
    try: lim=int(qs.get('limit',['50'])[0]); assert 1<=lim<=200
    except:return err(self,400,'invalid paging')
    cur=qs.get('cursor',[None])[0]
    try: boundary=int(cur) if cur else 0
    except: return err(self,400,'invalid cursor')
    rows=list(c.execute('select * from messages where parent_id=? and deleted=0 and id>? order by id asc limit ?',(parent['id'],boundary,lim+1)))
    more=len(rows)>lim; rows=rows[:lim]; nxt=str(rows[-1]['id']) if more and rows else None
    return send(self,200,{'replies':[msgjson(c,x) for x in rows],'next_cursor':nxt})
   if p=='/api/search':
    term=qs.get('q',[''])[0]
    try: lim=int(qs.get('limit',['50'])[0]); assert 1<=lim<=200
    except: return err(self,400,'invalid paging')
    cur=qs.get('cursor',[None])[0]
    try: boundary=int(cur) if cur else 9223372036854775807
    except: return err(self,400,'invalid cursor')
    wid=qs.get('workspace_id',[None])[0]
    sql='select m.* from messages m join channels ch on ch.id=m.channel_id join members wm on wm.workspace_id=ch.workspace_id and wm.user_id=? where m.deleted=0 and m.parent_id is null and lower(m.body) like lower(?) and m.id<? and (ch.is_private=0 or exists(select 1 from channel_members cm where cm.channel_id=ch.id and cm.user_id=?))'
    args=[u['id'],'%'+term+'%',boundary,u['id']]
    if wid: sql+=' and ch.workspace_id=?'; args.append(wid)
    sql+=' order by m.id desc limit ?'; args.append(lim+1)
    rows=list(c.execute(sql,args)); more=len(rows)>lim; rows=rows[:lim]
    return send(self,200,{'results':[msgjson(c,x) for x in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
   m=re.fullmatch(r'/api/channels/(\d+)/pins',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    if ch['is_private'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])):return err(self,403,'forbidden')
    try: lim=int(qs.get('limit',['50'])[0]); assert 1<=lim<=200
    except:return err(self,400,'invalid paging')
    cur=qs.get('cursor',[None])[0]
    try: boundary=int(cur) if cur else 9223372036854775807
    except:return err(self,400,'invalid cursor')
    rows=list(c.execute('select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=? and p.message_id<? order by p.pinned_at desc,p.message_id desc limit ?',(ch['id'],boundary,lim+1)))
    more=len(rows)>lim; rows=rows[:lim]
    pins=[{'message':msgjson(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows]
    return send(self,200,{'pins':pins,'next_cursor':str(rows[-1]['message_id']) if more and rows else None})
   m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    if ch['is_private'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])):return err(self,403,'forbidden')
    try: lim=int(qs.get('limit',['50'])[0]); cur=qs.get('cursor',[None])[0]; assert 1<=lim<=200; start=int(cur) if cur else 0
    except:return err(self,400,'invalid paging')
    boundary=int(cur) if cur else 9223372036854775807; rows=list(c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null and id<? order by id desc limit ?',(ch['id'],boundary,lim+1))); more=len(rows)>lim; rows=rows[:lim]; out=rows; nxt=str(rows[-1]['id']) if more and rows else None
    return send(self,200,{'messages':[msgjson(c,x) for x in out],'next_cursor':nxt})
   m=re.fullmatch(r'/api/users/(\d+)',p)
   if m:
    x=q1(c,'select * from users where id=?',(m.group(1),)); return send(self,200,{'user':ujson(x)}) if x else err(self,404,'not found')
  return err(self,404,'not found')
 def do_POST(self):
  p,qs=self.route()
  if p=='/api/auth/register':
   try:d=body(self); assert validate_username(d.get('username')) and validate_password(d.get('password')) and 'email' not in d
   except:return err(self,400,'invalid registration')
   with db() as c:
    try:
     cur=c.cursor(); cur.execute('insert into users(username,password,display_name) values(?,?,?)',(d['username'],hash_password(d['password']),d.get('display_name') or d['username'])); uid=cur.lastrowid
    except sqlite3.IntegrityError:return err(self,409,'duplicate')
    t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); u=q1(c,'select * from users where id=?',(uid,)); return send(self,201,{'user':ujson(u),'token':t})
  if p=='/api/auth/login':
   d=body(self)
   with db() as c:
    u=q1(c,'select * from users where username=?',(d.get('username'),))
    if not u or not verify_password(d.get('password',''),u['password']):return err(self,401,'invalid credentials')
    t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,u['id'],));return send(self,200,{'user':ujson(u),'token':t})
  u=auth(self)
  if not u:return err(self,401,'unauthorized')
  if p=='/api/files':
   n=int(self.headers.get('Content-Length','0'))
   if n>12*1024*1024:return err(self,413,'too large')
   raw=self.rfile.read(n); ct=self.headers.get('Content-Type','')
   m=re.search(r'boundary=([^;]+)',ct)
   if not m:return err(self,400,'file required')
   boundary=('--'+m.group(1)).encode(); part=next((x for x in raw.split(boundary) if b'name="file"' in x),None)
   if not part:return err(self,400,'file required')
   head,data=part.split(b'\r\n\r\n',1); data=data.rsplit(b'\r\n',1)[0]
   fm=re.search(br'filename="([^"]*)"',head); tm=re.search(br'Content-Type:\s*([^\r\n]+)',head)
   if not fm:return err(self,400,'file required')
   fn=fm.group(1).decode(errors='replace'); typ=(tm.group(1).decode().strip() if tm else 'application/octet-stream')
   if len(data)>10485760:return err(self,413,'too large')
   with db() as c:
    cur=c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data)); f=q1(c,'select * from files where id=?',(cur.lastrowid,)); return send(self,201,{'file':filejson(f)})
  d=body(self)
  with db() as c:
   m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if not role(c,w['id'],u['id']):return err(self,403,'forbidden')
    h=d.get('handle','')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h) or h in ('here','channel'):return err(self,400,'invalid handle')
    try:
     cur=c.execute('insert into groups(workspace_id,creator_id,handle,name) values(?,?,?,?)',(w['id'],u['id'],h,d.get('name','')));gid=cur.lastrowid
    except sqlite3.IntegrityError:return err(self,409,'duplicate')
    for uid in d.get('user_ids',[]):
     if q1(c,'select 1 from members where workspace_id=? and user_id=?',(w['id'],uid)):c.execute('insert or ignore into group_members values(?,?)',(gid,uid))
    return send(self,201,{'group':groupjson(c,q1(c,'select * from groups where id=?',(gid,)))})
   if p=='/api/dms':
    target=q1(c,'select id from users where id=?',(d.get('user_id'),))
    if not target:return err(self,404,'not found')
    if int(target['id'])==u['id']:return err(self,400,'self dm')
    a,b=sorted((u['id'],target['id'])); name=f'dm-{a}-{b}'
    ch=q1(c,'select * from channels where is_dm=1 and name=?',(name,))
    if not ch:
     cur=c.execute('insert into channels(workspace_id,name,is_private,is_dm) values(NULL,?,?,1)',(name,1)); cid=cur.lastrowid
     c.execute('insert into channel_members values(?,?), (?,?)',(cid,a,cid,b))
    else: cid=ch['id']
    return send(self,200,{'channel_id':cid})
   m=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if role(c,w['id'],u['id'])!='owner':return err(self,403,'forbidden')
    target=q1(c,'select * from members where workspace_id=? and user_id=?',(w['id'],d.get('new_owner_id')))
    if not target or target['role']!='admin':return err(self,400,'target must be admin')
    c.execute('update members set role=case when user_id=? then "owner" when user_id=? then "admin" else role end where workspace_id=?',(d['new_owner_id'],u['id'],w['id']))
    c.execute('update workspaces set owner_id=? where id=?',(d['new_owner_id'],w['id']))
    return send(self,200,{'workspace':wjson(q1(c,'select * from workspaces where id=?',(w['id'],)))})
   m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    r=role(c,w['id'],u['id']); private=bool(d.get('is_private',False))
    if r not in ('owner','admin') and (r!='member' or private):return err(self,403,'forbidden')
    name=d.get('name','')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32:return err(self,400,'invalid name')
    if not isinstance(d.get('topic',''),str) or len(d.get('topic',''))>250:return err(self,400,'topic')
    if q1(c,'select 1 from channels where workspace_id=? and name=?',(w['id'],name)):return err(self,409,'duplicate')
    cur=c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,private,d.get('topic',''))); ch=q1(c,'select * from channels where id=?',(cur.lastrowid,));c.execute('insert into channel_members values(?,?)',(ch['id'],u['id']));return send(self,201,{'channel':cjson(ch)})
   m=re.fullmatch(r'/api/channels/(\d+)/join',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    if q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])):return send(self,200,{'channel':cjson(ch)})
    w=q1(c,'select * from workspaces where id=?',(ch['workspace_id'],)); r=role(c,w['id'],u['id'])
    if ch['is_private']:return err(self,403,'private channel')
    if not r:
     if w['join_mode']!='open':return err(self,403,'invite required')
     c.execute('insert into members values(?,?,?)',(w['id'],u['id'],'member')); r='member'
    if r=='guest':return err(self,403,'forbidden')
    c.execute('insert into channel_members values(?,?)',(ch['id'],u['id']));event(c,ch['id'],'member.joined',{'user_id':u['id']});return send(self,200,{'channel':cjson(ch)})
   m=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    w=q1(c,'select * from workspaces where id=?',(ch['workspace_id'],)); action=m.group(2)
    if action=='leave':
     removed=c.execute('delete from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).rowcount
     if removed:event(c,ch['id'],'member.left',{'user_id':u['id']})
     return send(self,200,{'channel':cjson(ch)})
    if u['id']!=w['owner_id']:return err(self,403,'forbidden')
    c.execute('update channels set is_archived=? where id=?',(1 if action=='archive' else 0,ch['id']))
    updated=q1(c,'select * from channels where id=?',(ch['id'],))
    event(c,ch['id'],'channel.archived' if action=='archive' else 'channel.unarchived',{'channel':cjson(updated)})
    return send(self,200,{'channel':cjson(updated)})
   m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    if ch['is_archived'] and d.get('body')!='/unarchive':return err(self,423,'archived')
    if not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])):return err(self,403,'forbidden')
    text=d.get('body')
    if not isinstance(text,str) or not text.strip():return err(self,400,'empty body')
    if text.startswith('/') and not text.startswith('//'):
     bits=text.split(' ',1); cmd=bits[0]; arg=bits[1].strip() if len(bits)>1 else ''
     if cmd=='/me':
      if not arg:return err(self,400,'argument required')
      text='/me '+arg
     elif cmd=='/shrug':
      text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
     elif cmd in ('/topic','/invite','/archive','/unarchive'):
      if not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])) and u['id']!=q1(c,'select owner_id from workspaces where id=?',(ch['workspace_id'],))['owner_id']: return err(self,403,'forbidden')
      if cmd=='/topic':
       if not arg or len(arg)>250:return err(self,400,'invalid topic')
       c.execute('update channels set topic=? where id=?',(arg,ch['id'])); updated=q1(c,'select * from channels where id=?',(ch['id'],));event(c,ch['id'],'channel.updated',{'channel':cjson(updated)});return send(self,201,{'message':None,'channel':cjson(updated)})
      if cmd=='/invite':
       if not arg.startswith('@') or len(arg)<2:return err(self,400,'username required')
       target=q1(c,'select * from users where username=?',(arg[1:],))
       if not target:return err(self,404,'unknown user')
       if not q1(c,'select 1 from members where workspace_id=? and user_id=?',(ch['workspace_id'],target['id'])):return err(self,403,'not workspace member')
       c.execute('insert or ignore into channel_members values(?,?)',(ch['id'],target['id']));event(c,ch['id'],'member.joined',{'user_id':target['id']});return send(self,201,{'message':None,'invited_user':ujson(target)})
      if cmd in ('/archive','/unarchive'):
       w=q1(c,'select owner_id from workspaces where id=?',(ch['workspace_id'],))
       if u['id']!=w['owner_id']:return err(self,403,'owner only')
       val=1 if cmd=='/archive' else 0;c.execute('update channels set is_archived=? where id=?',(val,ch['id']));updated=q1(c,'select * from channels where id=?',(ch['id'],));event(c,ch['id'],'channel.archived' if val else 'channel.unarchived',{'channel':cjson(updated)});return send(self,201,{'message':None,'channel':cjson(updated)})
     else:return err(self,400,'unknown command')
    fids=d.get('file_ids') or []
    if not isinstance(fids,list):return err(self,400,'invalid files')
    for fid in fids:
     f=q1(c,'select * from files where id=?',(fid,))
     if not f or f['uploader_id']!=u['id'] or q1(c,'select 1 from message_files where file_id=?',(fid,)):return err(self,400,'invalid file')
    parent=d.get('parent_id'); created=now();cur=c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],u['id'],text,parent,created)); mid=cur.lastrowid
    for fid in fids:c.execute('insert into message_files values(?,?)',(mid,fid))
    c.execute('insert or ignore into message_mentions select ?,? where 0',(mid,u['id']))
    for mention_id in extract_mentions(c,ch['id'],u['id'],text): c.execute('insert or ignore into message_mentions values(?,?)',(mid,mention_id))
    if parent:c.execute('update messages set reply_count=reply_count+1 where id=?',(parent,))
    r=q1(c,'select * from messages where id=?',(mid,)); event(c,ch['id'],'message.reply' if parent else 'message.created',{'message':msgjson(c,r)});return send(self,201,{'message':msgjson(c,r)})
   m=re.fullmatch(r'/api/messages/(\d+)/reactions',p)
   if m:
    msg=q1(c,'select m.*,ch.is_private,ch.id channel_id from messages m join channels ch on ch.id=m.channel_id where m.id=? and m.deleted=0',(m.group(1),))
    if not msg:return err(self,404,'not found')
    if msg['is_private'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(msg['channel_id'],u['id'])):return err(self,403,'forbidden')
    emoji=d.get('emoji')
    if not isinstance(emoji,str) or not emoji:return err(self,400,'emoji required')
    existed=q1(c,'select 1 from reactions where message_id=? and user_id=? and emoji=?',(msg['id'],u['id'],emoji))
    if not existed:
     c.execute('insert into reactions values(?,?,?)',(msg['id'],u['id'],emoji))
     event(c,msg['channel_id'],'reaction.added',{'message_id':msg['id'],'emoji':emoji,'user_id':u['id'],'reactions':reactions(c,msg['id'])})
    return send(self,200,{'reactions':reactions(c,msg['id'])})
   m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
   if m:
    msg=q1(c,'select m.*,ch.is_private,ch.workspace_id from messages m join channels ch on ch.id=m.channel_id where m.id=? and m.deleted=0',(m.group(1),))
    if not msg:return err(self,404,'not found')
    if msg['is_private'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(msg['channel_id'],u['id'])):return err(self,403,'forbidden')
    oldpin=q1(c,'select * from pins where message_id=?',(msg['id'],))
    if not oldpin:
     c.execute('insert into pins values(?,?,?)',(msg['id'],u['id'],now())); oldpin=q1(c,'select * from pins where message_id=?',(msg['id'],))
    return send(self,200,{'pin':{'message_id':oldpin['message_id'],'pinned_by':oldpin['pinned_by'],'pinned_at':oldpin['pinned_at']}})
   m=re.fullmatch(r'/api/channels/(\d+)/read',p)
   if m:
    ch=q1(c,'select * from channels where id=?',(m.group(1),))
    if not ch:return err(self,404,'not found')
    if ch['is_private'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])):return err(self,403,'forbidden')
    c.execute('insert into reads values(?,?,?) on conflict(user_id,channel_id) do update set last_read_event_id=max(last_read_event_id,excluded.last_read_event_id)',(u['id'],ch['id'],int(d.get('last_read_event_id',0))))
    return send(self,200,{'read_state':read_state(c,u['id'],ch['id'])})
   m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if role(c,w['id'],u['id']) not in ('owner','admin'):return err(self,403,'forbidden')
    target=d.get('invited_user_id');
    if not target and d.get('invited_username'):
     tu=q1(c,'select id from users where username=?',(d['invited_username'],)); target=tu['id'] if tu else None
    code=secrets.token_urlsafe(18); exp=(datetime.now(timezone.utc)+timedelta(seconds=int(d.get('expires_in_seconds',604800)))).isoformat().replace('+00:00','Z'); mx=1 if target else int(d.get('max_uses',1));c.execute('insert into invitations values(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],mx,0,exp,target));return send(self,201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'uses':0,'expires_at':exp,'invited_user_id':target}})
   m=re.fullmatch(r'/api/invitations/([^/]+)/accept',p)
   if m:
    x=q1(c,'select * from invitations where code=?',(m.group(1),))
    if not x or x['revoked'] or x['uses']>=x['max_uses'] or (x['expires_at'] and x['expires_at']<now()):return err(self,404,'invalid invitation')
    if x['invited_user_id'] and x['invited_user_id']!=u['id']:return err(self,403,'targeted')
    c.execute('update invitations set uses=uses+1 where code=?',(x['code'],));c.execute('insert or ignore into members values(?,?,?)',(x['workspace_id'],u['id'],'member'));w=q1(c,'select * from workspaces where id=?',(x['workspace_id'],));return send(self,200,{'workspace':wjson(w),'member':{'user_id':u['id'],'role':'member'}})
   m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
   if m and d:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if w and role(c,w['id'],u['id']) in ('owner','admin'):
     if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return err(self,400,'invalid mode')
     c.execute('update workspaces set name=coalesce(?,name),join_mode=coalesce(?,join_mode) where id=?',(d.get('name'),d.get('join_mode'),w['id']));return send(self,200,{'workspace':wjson(q1(c,'select * from workspaces where id=?',(w['id'],)))})
   if p.startswith('/api/workspaces') and p.count('/')==3:
    slug=p.split('/')[3]; slug=slug or ''; w=q1(c,'select * from workspaces where slug=?',(slug,))
    if not w:return err(self,404,'not found')
  if p=='/api/workspaces':
   slug=d.get('slug','');name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return err(self,400,'invalid slug')
   try:
    cur=c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,name,u['id'])); wid=cur.lastrowid;c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));chcur=c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));ch=q1(c,'select * from channels where id=?',(chcur.lastrowid,));c.execute('insert into channel_members values(?,?)',(ch['id'],u['id']));return send(self,201,{'workspace':wjson(q1(c,'select * from workspaces where id=?',(wid,))),'general_channel':cjson(ch)})
   except sqlite3.IntegrityError:return err(self,409,'duplicate')
  return err(self,404,'not found')
 def do_PATCH(self):
  p,qs=self.route();u=auth(self)
  if not u:return err(self,401,'unauthorized')
  d=body(self)
  with db() as c:
   m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),));g=q1(c,'select * from groups where workspace_id=? and handle=?',(w['id'],m.group(2))) if w else None
    if not w:return err(self,404,'not found')
    if not g:return err(self,404,'not found')
    if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return err(self,403,'forbidden')
    if 'name' in d:c.execute('update groups set name=? where id=?',(d['name'],g['id']))
    if 'user_ids' in d:
     c.execute('delete from group_members where group_id=?',(g['id'],))
     for uid in d['user_ids']:
      if q1(c,'select 1 from members where workspace_id=? and user_id=?',(w['id'],uid)):c.execute('insert or ignore into group_members values(?,?)',(g['id'],uid))
    return send(self,200,{'group':groupjson(c,q1(c,'select * from groups where id=?',(g['id'],)))})
   m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if role(c,w['id'],u['id']) not in ('owner','admin'):return err(self,403,'forbidden')
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return err(self,400,'invalid mode')
    c.execute('update workspaces set name=coalesce(?,name),join_mode=coalesce(?,join_mode) where id=?',(d.get('name'),d.get('join_mode'),w['id']))
    return send(self,200,{'workspace':wjson(q1(c,'select * from workspaces where id=?',(w['id'],)))})
   m=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    target=q1(c,'select * from members where workspace_id=? and user_id=?',(w['id'],m.group(2)))
    if not target:return err(self,404,'not found')
    new=d.get('role')
    if new not in ('owner','admin','member','guest') or new=='owner':return err(self,400,'invalid role')
    actor=role(c,w['id'],u['id'])
    if actor not in ('owner','admin') or target['role']=='owner' or (actor=='admin' and target['role']=='admin'):return err(self,403,'forbidden')
    c.execute('update members set role=? where workspace_id=? and user_id=?',(new,w['id'],m.group(2)))
    return send(self,200,{'member':{'user_id':int(m.group(2)),'role':new}})
   m=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if role(c,w['id'],u['id'])!='owner':return err(self,403,'forbidden')
    target=q1(c,'select * from members where workspace_id=? and user_id=?',(w['id'],d.get('new_owner_id')))
    if not target or target['role']!='admin':return err(self,400,'target must be admin')
    c.execute('update members set role=case when user_id=? then "owner" when user_id=? then "admin" else role end where workspace_id=?',(d['new_owner_id'],u['id'],w['id']))
    c.execute('update workspaces set owner_id=? where id=?',(d['new_owner_id'],w['id']))
    return send(self,200,{'workspace':wjson(q1(c,'select * from workspaces where id=?',(w['id'],)))})
   m=re.fullmatch(r'/api/messages/(\d+)',p)
   if m:
    x=q1(c,'select * from messages where id=?',(m.group(1),))
    if not x:return err(self,404,'not found')
    if x['deleted']:return err(self,409,'deleted')
    if x['author_id']!=u['id']:return err(self,403,'forbidden')
    if not isinstance(d.get('body'),str) or not d['body'].strip():return err(self,400,'empty body')
    c.execute('update messages set body=?,edited_at=? where id=?',(d['body'],now(),x['id']))
    updated=q1(c,'select * from messages where id=?',(x['id'],))
    event(c,x['channel_id'],'message.updated',{'message':msgjson(c,updated)})
    return send(self,200,{'message':msgjson(c,updated)})
   m=re.fullmatch(r'/api/channels/(\d+)',p)
   if m:
    x=q1(c,'select * from channels where id=?',(m.group(1),))
    if not x:return err(self,404,'not found')
    if x['is_archived']:return err(self,423,'archived')
    w=q1(c,'select * from workspaces where id=?',(x['workspace_id'],))
    if u['id']!=w['owner_id'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(x['id'],u['id'])):return err(self,403,'forbidden')
    if not isinstance(d.get('topic',''),str) or len(d.get('topic',''))>250:return err(self,400,'topic')
    c.execute('update channels set topic=? where id=?',(d.get('topic',''),x['id'])); updated=q1(c,'select * from channels where id=?',(x['id'],)); event(c,x['id'],'channel.updated',{'channel':cjson(updated)});return send(self,200,{'channel':cjson(updated)})
   m=re.fullmatch(r'/api/users/me',p)
   if m:
    allowed={k:d[k] for k in ('display_name','timezone','avatar_url','status_text','status_emoji') if k in d}
    try: validate_profile(allowed)
    except ValueError: return err(self,400,'invalid profile')
    if allowed:
     c.execute('update users set '+','.join(k+'=?' for k in allowed)+' where id=?',tuple(allowed[k] for k in allowed)+(u['id'],))
     updated=ujson(q1(c,'select * from users where id=?',(u['id'],)))
     c.execute('insert into global_updates(payload) values(?)',(json.dumps({'type':'user.updated','user':updated}),))
    return send(self,200,{'user':ujson(q1(c,'select * from users where id=?',(u['id'],)))})
  return err(self,404,'not found')
 def do_DELETE(self):
  p,qs=self.route();u=auth(self)
  if not u:return err(self,401,'unauthorized')
  with db() as c:
   m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations/([^/]+)',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),))
    if not w:return err(self,404,'not found')
    if role(c,w['id'],u['id']) not in ('owner','admin'):return err(self,403,'forbidden')
    x=q1(c,'select * from invitations where code=? and workspace_id=?',(m.group(2),w['id']))
    if not x:return err(self,404,'not found')
    c.execute('update invitations set revoked=1 where code=?',(m.group(2),))
    return send(self,200,{'deleted':True})
   m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
   if m:
    w=q1(c,'select * from workspaces where slug=?',(m.group(1),));g=q1(c,'select * from groups where workspace_id=? and handle=?',(w['id'],m.group(2))) if w else None
    if not w:return err(self,404,'not found')
    if g and u['id'] not in (g['creator_id'],w['owner_id']):return err(self,403,'forbidden')
    if g:c.execute('delete from groups where id=?',(g['id'],))
    return send(self,200,{'deleted':True})
   m=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',p)
   if m:
    emoji=urllib.parse.unquote(m.group(2))
    msg=q1(c,'select m.*,ch.is_private,ch.id channel_id from messages m join channels ch on ch.id=m.channel_id where m.id=? and m.deleted=0',(m.group(1),))
    if not msg:return err(self,404,'not found')
    if msg['is_private'] and not q1(c,'select 1 from channel_members where channel_id=? and user_id=?',(msg['channel_id'],u['id'])):return err(self,403,'forbidden')
    existed=q1(c,'select 1 from reactions where message_id=? and user_id=? and emoji=?',(msg['id'],u['id'],emoji))
    if existed:
     c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(msg['id'],u['id'],emoji))
     event(c,msg['channel_id'],'reaction.removed',{'message_id':msg['id'],'emoji':emoji,'user_id':u['id'],'reactions':reactions(c,msg['id'])})
    return send(self,200,{'reactions':reactions(c,msg['id'])})
   m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
   if m:c.execute('delete from pins where message_id=?',(m.group(1),));return send(self,200,{'unpinned':True})
   m=re.fullmatch(r'/api/messages/(\d+)',p)
   if m:
    x=q1(c,'select * from messages where id=?',(m.group(1),))
    if not x:return err(self,404,'not found')
    if not x['deleted']:
     ch=q1(c,'select workspace_id from channels where id=?',(x['channel_id'],))
     w=q1(c,'select owner_id from workspaces where id=?',(ch['workspace_id'],))
     if x['author_id']!=u['id'] and w['owner_id']!=u['id']:return err(self,403,'forbidden')
     c.execute('update messages set deleted=1 where id=?',(x['id'],))
     event(c,x['channel_id'],'message.deleted',{'message_id':x['id']})
    return send(self,200,{'deleted':True})
  return err(self,404,'not found')
 def ws(self):
  parsed=urllib.parse.urlparse(self.path); token=urllib.parse.parse_qs(parsed.query).get('token',[''])[0]
  with db() as c:
   u=q1(c,'select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(token,))
  if not u:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send_error(400)
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers();self.connection.settimeout(None)
  with db() as c:c.execute('insert or replace into ws_presence(user_id,last_seen) values(?,?)',(u['id'],time.time()))
  subs={}; sent={}
  with db() as c:gseen=q1(c,'select coalesce(max(id),0) n from global_updates')['n']
  def frame(obj):
   data=json.dumps(obj,separators=(',',':')).encode(); n=len(data)
   if n<126:return bytes([129,n])+data
   if n<65536:return bytes([129,126])+struct.pack('>H',n)+data
   return bytes([129,127])+struct.pack('>Q',n)+data
  try:
   while True:
    try:
     if not select.select([self.connection],[],[],0.1)[0]: raise socket.timeout()
     h=self.rfile.read(2)
     if not h:break
     ln=h[1]&127
     if ln==126:ln=struct.unpack('>H',self.rfile.read(2))[0]
     elif ln==127:ln=struct.unpack('>Q',self.rfile.read(8))[0]
     mask=self.rfile.read(4); data=bytes(x^mask[i%4] for i,x in enumerate(self.rfile.read(ln))); obj=json.loads(data)
     typ=obj.get('type'); ch=int(obj.get('channel_id',0))
     if typ=='subscribe':
      with db() as c: head=q1(c,'select coalesce(max(event_id),0) n from events where channel_id=?',(ch,))['n']
      subs[ch]=head;sent[ch]=head;self.wfile.write(frame({'type':'subscribed','channel_id':ch,'head_event_id':head}))
     elif typ=='resume':
      since=int(obj.get('since_event_id',0))
      with db() as c:
       rows=list(c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,since,)))
       head=q1(c,'select coalesce(max(event_id),0) n from events where channel_id=?',(ch,))['n']
      for r in rows:
       payload=json.loads(r['payload']); payload.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':ch}); self.wfile.write(frame(payload))
      self.wfile.write(frame({'type':'resumed','channel_id':ch,'head_event_id':head}));subs[ch]=head;sent[ch]=head
    except socket.timeout: pass
    with db() as c:
     grows=list(c.execute('select id,payload from global_updates where id>? order by id',(gseen,)))
    for gr in grows:
     self.wfile.write(frame(json.loads(gr['payload'])));gseen=gr['id']
    with db() as c:
     for ch,last in list(sent.items()):
      rows=list(c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,last,)))
      for r in rows:
       payload=json.loads(r['payload']);payload.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':ch});self.wfile.write(frame(payload));sent[ch]=r['event_id']
  except Exception: pass
  finally:
   try:
    with db() as c:c.execute('delete from ws_presence where user_id=?',(u['id'],))
   except: pass
 def ui(self):
  with open('/app/static/index.html','rb') as f:b=f.read()
  self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
