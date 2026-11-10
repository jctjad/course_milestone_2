import os,sys,json,re,sqlite3,hashlib,secrets,base64,threading,time, mimetypes, urllib.parse, socket, struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from datetime import datetime,timezone
DB='/var/lib/huddle/huddle.db'; NODE=int(os.environ.get('NODE_ID','0')); PORT=8000+NODE
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=10000'); c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True); c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER);
CREATE TABLE IF NOT EXISTS groupmembers(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_event_id INTEGER,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS msgfiles(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER,channel_id INTEGER,seen_at REAL,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS user_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,payload TEXT,created_at REAL);'''); c.commit(); c.close()
init()
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(h):
 x=h.get('Authorization',''); 
 if not x.startswith('Bearer '): return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone(); c.close(); return r
def chobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def emit(c,cid,typ,payload):
 n=(c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0])
 c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,n,typ,json.dumps(payload),now())); return n
def message(c,r):
 d=dict(r); d['parent_id']=d['parent_id']; d['author']=userobj(c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()); d['reply_count']=c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]; d['files']=[]
 for f in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN msgfiles mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)): d['files'].append(dict(f))
 d['edited_at']=r['edited_at']; d['reactions']=[]
 for x in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  ids=[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]; d['reactions'].append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 d['mentions']=[]; return d
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,n,x,headers={}):
  b=json.dumps(x).encode(); self.send_response(n); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def read(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path; q=urllib.parse.parse_qs(p.query)
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/' : return self.html()
  if path=='/api/ws': return self.ws()
  u=auth(self.headers)
  if path=='/api/auth/me': return self.sendj(200,{'user':userobj(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  if path.startswith('/api/files/'):
   u=auth(self.headers)
   if not u:return self.sendj(401,{})
   fid=path.split('/')[3]; c=db(); f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
   if not f:return self.sendj(404,{})
   if path.endswith('/download'):
    data=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
   return self.sendj(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  if path.startswith('/api/users/'):
   if not u:return self.sendj(401,{'error':'unauthorized'})
   c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(path.split('/')[-1],)).fetchone(); c.close(); return self.sendj(200,{'user':userobj(r)}) if r else self.sendj(404,{})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  if path.startswith('/api/workspaces/') and path.endswith('/groups'):
   slug=path.split('/')[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return self.sendj(404,{})
   if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
   gs=[]
   for g in c.execute('SELECT * FROM groupsx WHERE workspace_id=?',(w['id'],)):
    ids=[x[0] for x in c.execute('SELECT user_id FROM groupmembers WHERE group_id=?',(g['id'],))]
    gs.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
   return self.sendj(200,{'groups':gs})
  if path=='/api/workspaces':
   rows=c.execute('SELECT * FROM workspaces WHERE owner_id=? OR id IN (SELECT workspace_id FROM members WHERE user_id=?)',(u['id'],u['id'])); x=[dict(r) for r in rows]; return self.sendj(200,{'workspaces':x})
  if path.startswith('/api/workspaces/') and path.count('/')==3:
   slug=path.split('/')[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return self.sendj(404,{})
   cs=[chobj(r) for r in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],))]; return self.sendj(200,{'workspace':dict(w),'channels':cs,'read_state':[]})
  if path.startswith('/api/workspaces/') and path.endswith('/members'):
   slug=path.split('/')[3]; rows=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id JOIN workspaces w ON w.id=m.workspace_id WHERE w.slug=?',(slug,)); return self.sendj(200,{'members':[dict(x) for x in rows]})
  if path.startswith('/api/channels/') and path.endswith('/members'):
   cid=path.split('/')[3]; rows=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)); return self.sendj(200,{'members':[userobj(x) for x in rows]})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   cid=path.split('/')[3]; cr=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not cr:return self.sendj(404,{})
   if cr['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return self.sendj(403,{})
   rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(cid,)); return self.sendj(200,{'messages':[message(c,x) for x in rows],'next_cursor':None})
  if path.startswith('/api/messages/') and path.endswith('/replies'):
   mid=path.split('/')[3]; rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)); return self.sendj(200,{'replies':[message(c,x) for x in rows],'next_cursor':None})
  if path.startswith('/api/messages/') and '/reactions' not in path:
   r=c.execute('SELECT * FROM messages WHERE id=?',(path.split('/')[3],)).fetchone(); return self.sendj(200,{'message':message(c,r)}) if r else self.sendj(404,{})
  if path.startswith('/api/search'):
   term=q.get('q',[''])[0]; rows=c.execute('SELECT * FROM messages WHERE body LIKE ? AND deleted=0 ORDER BY id DESC',('%'+term+'%',)); return self.sendj(200,{'results':[message(c,x) for x in rows],'next_cursor':None})
  return self.sendj(404,{})
 def do_POST(self):
  path=urllib.parse.urlparse(self.path).path; u=auth(self.headers)
  if path=='/api/auth/register':
   x=self.read()
   if not re.fullmatch(r'[A-Za-z0-9_]+',x.get('username','')) or len(x.get('password',''))<8:return self.sendj(400,{})
   c=db()
   try:c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(x['username'],x['password'],x.get('display_name',x['username']),'UTC','','','')); c.commit()
   except sqlite3.IntegrityError:return self.sendj(409,{})
   r=c.execute('SELECT * FROM users WHERE username=?',(x['username'],)).fetchone(); t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'],)); c.commit(); return self.sendj(201,{'user':userobj(r),'token':t})
  if path=='/api/auth/login':
   x=self.read(); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
   if not r:return self.sendj(401,{})
   t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); return self.sendj(200,{'user':userobj(r),'token':t})
  if self.path.split('?')[0]=='/api/files':
   ct=self.headers.get('Content-Type',''); n=int(self.headers.get('Content-Length','0'))
   if not u:return self.sendj(401,{})
   raw=self.rfile.read(n)
   if 'boundary=' not in ct:return self.sendj(400,{})
   boundary=ct.split('boundary=',1)[1].encode(); parts=raw.split(b'--'+boundary)
   part=next((z for z in parts if b'name="file"' in z),None)
   if not part:return self.sendj(400,{})
   head,data=part.split(b'\r\n\r\n',1); data=data[:-2] if data.endswith(b'\r\n') else data
   import re as _re
   fn=(_re.search(br'filename="([^"]+)"',head) or [b'',b'file'])[1].decode(errors='replace')
   typ=(_re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I) or [b'',b'application/octet-stream'])[1].decode()
   if len(data)>10485760:return self.sendj(413,{})
   c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
   return self.sendj(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  if not u:return self.sendj(401,{})
  c=db(); x=self.read()
  if path.startswith('/api/workspaces/') and path.endswith('/groups'):
   slug=path.split('/')[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return self.sendj(404,{})
   if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
   handle=x.get('handle','')
   if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,31}',handle) or handle in ('here','channel'):return self.sendj(400,{})
   if c.execute('SELECT 1 FROM groupsx WHERE handle=?',(handle,)).fetchone():return self.sendj(409,{})
   c.execute('INSERT INTO groupsx(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,x.get('name',handle),u['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   for uid in x.get('user_ids',[]): 
    if c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone(): c.execute('INSERT OR IGNORE INTO groupmembers VALUES(?,?)',(gid,uid))
   c.commit(); ids=[z[0] for z in c.execute('SELECT user_id FROM groupmembers WHERE group_id=?',(gid,))]
   return self.sendj(201,{'group':{'id':gid,'handle':handle,'name':x.get('name',handle),'member_user_ids':ids}})
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return self.sendj(400,{})
   try:c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(x['slug'],x['name'],u['id'],'open')); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,0,"")',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   except Exception as e: return self.sendj(500,{'error':str(e)})
  if path.startswith('/api/workspaces/') and path.endswith('/channels'):
   slug=path.split('/')[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return self.sendj(404,{})
   if x.get('name','') in [r['name'] for r in c.execute('SELECT name FROM channels WHERE workspace_id=?',(w['id'],))]:return self.sendj(409,{})
   c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],x['name'],int(x.get('is_private',False)),x.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
  if path.startswith('/api/channels/') and path.endswith('/join'):
   cid=path.split('/')[3]; r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not r:return self.sendj(404,{})
   c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,u['id']));c.execute('INSERT OR IGNORE INTO members SELECT workspace_id,?,\"member\" FROM channels WHERE id=?',(u['id'],cid));c.commit();return self.sendj(200,{'channel':chobj(r)})
  if path.startswith('/api/channels/') and (path.endswith('/archive') or path.endswith('/unarchive')):
   cid=path.split('/')[3]; r=c.execute('SELECT c.*,w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,)).fetchone()
   if not r:return self.sendj(404,{})
   if r['owner_id']!=u['id']:return self.sendj(403,{})
   archived=1 if path.endswith('/archive') else 0; c.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid)); c.commit(); return self.sendj(200,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   cid=path.split('/')[3]; r=c.execute('SELECT ch.*,COALESCE(w.owner_id,0) owner_id,COALESCE(w.slug,\"\") slug FROM channels ch LEFT JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
   if not r:return self.sendj(404,{})
   body=x.get('body','')
   if not body:return self.sendj(400,{})
   member=bool(c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone())
   if body.startswith('/') and not body.startswith('//'):
    parts=body.split()
    cmd=parts[0]
    if cmd=='/me':
     if len(parts)<2:return self.sendj(400,{})
    elif cmd=='/shrug':
     body=(' '.join(parts[1:])+' ' if len(parts)>1 else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if not member:return self.sendj(403,{})
     topic=' '.join(parts[1:])
     if not topic:return self.sendj(400,{})
     c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); c.commit()
     return self.sendj(201,{'message':None,'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    elif cmd in ('/archive','/unarchive'):
     if r['owner_id']!=u['id']:return self.sendj(403,{})
     archived=1 if cmd=='/archive' else 0
     c.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid)); c.commit()
     return self.sendj(201,{'message':None,'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    elif cmd=='/invite':
     if not member:return self.sendj(403,{})
     if len(parts)!=2:return self.sendj(400,{})
     uname=parts[1][1:] if parts[1].startswith('@') else ''
     target=c.execute('SELECT * FROM users WHERE username=?',(uname,)).fetchone()
     if not target:return self.sendj(404,{})
     if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(r['workspace_id'],target['id'])).fetchone():return self.sendj(403,{})
     c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,target['id'])); c.commit()
     return self.sendj(201,{'message':None,'invited_user':userobj(target)})
    else:return self.sendj(400,{})
   if r['is_archived']:return self.sendj(423,{})
   mentions=[]
   for uname in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
    rr=c.execute('SELECT u.id FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=? AND u.username=?',(cid,uname)).fetchone()
    if rr and rr[0]!=u['id'] and rr[0] not in mentions: mentions.append(rr[0])
   fids=x.get('file_ids',[])
   if fids:
    if not isinstance(fids,list): return self.sendj(400,{})
    for fid in fids:
     fr=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
     if not fr or fr['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM msgfiles WHERE file_id=?',(fid,)).fetchone(): return self.sendj(400,{})
   # group and channel mentions
   wr=r['workspace_id']
   for gh in re.findall(r'(?<![A-Za-z0-9_])@([a-z0-9][a-z0-9_-]{1,31})',body):
    gg=c.execute('SELECT id FROM groupsx WHERE workspace_id=? AND handle=?',(wr,gh)).fetchone()
    if gg:
     for zz in c.execute('SELECT gm.user_id FROM groupmembers gm JOIN cm ON cm.user_id=gm.user_id WHERE gm.group_id=? AND cm.channel_id=?',(gg['id'],cid)):
      if zz[0]!=u['id'] and zz[0] not in mentions: mentions.append(zz[0])
   if re.search(r'(?<![A-Za-z0-9_])@here\b',body):
    for zz in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(cid,)):
     if zz[0]!=u['id'] and c.execute('SELECT 1 FROM ws_presence WHERE user_id=? AND seen_at>?',(zz[0],time.time()-5)).fetchone() and zz[0] not in mentions: mentions.append(zz[0])
   for attempt in range(8):
    try:
     c.execute('BEGIN IMMEDIATE')
     c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,x.get('parent_id'),now()))
     mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     break
    except sqlite3.OperationalError:
     try: c.rollback()
     except: pass
     if attempt==7: raise
     time.sleep(.02*(attempt+1))
   for fid in fids: c.execute('INSERT INTO msgfiles VALUES(?,?)',(mid,fid))
   m=message(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())
   m['mentions']=mentions
   ev=emit(c,cid,'message.reply' if x.get('parent_id') else 'message.created',m)
   c.commit()
   return self.sendj(201,{'message':m})
  if path=='/api/dms':
   try: other=int(x.get('user_id'))
   except: return self.sendj(400,{})
   if other==u['id']:return self.sendj(400,{})
   if not c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone():return self.sendj(404,{})
   key='dm'+''.join(map(str,sorted([u['id'],other]))); r=c.execute('SELECT id FROM channels WHERE name=?',(key,)).fetchone()
   if r:return self.sendj(200,{'channel_id':r['id']})
   c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(?,?,1,1)',(0,key));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cm VALUES(?,?),(?,?)',(cid,u['id'],cid,other));c.commit();return self.sendj(200,{'channel_id':cid})
  if path.startswith('/api/messages/') and path.endswith('/reactions'):
   mid=int(path.split('/')[3]); r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
   if not r:return self.sendj(404,{})
   if r['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return self.sendj(403,{})
   e=x.get('emoji')
   if not e:return self.sendj(400,{})
   before=c.total_changes; c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],e))
   if c.total_changes>before:
    rr=message(c,r)['reactions']; emit(c,r['channel_id'],'reaction.added',{'message_id':mid,'emoji':e,'user_id':u['id'],'reactions':rr}); c.commit()
   return self.sendj(200,{'reactions':message(c,r)['reactions']})
  return self.sendj(404,{})
 def do_PATCH(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{})
  p=urllib.parse.urlparse(self.path).path;x=self.read();c=db()
  if '/groups/' in p:
   bits=p.split('/'); slug=bits[3]; handle=bits[5]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); g=c.execute('SELECT * FROM groupsx WHERE handle=? AND workspace_id=?',(handle,w['id'] if w else -1)).fetchone()
   if not g:return self.sendj(404,{})
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   if self.command=='DELETE':
    c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groupsx WHERE id=?',(g['id'],));c.commit();return self.sendj(200,{'deleted':True})
   if 'name' in x:c.execute('UPDATE groupsx SET name=? WHERE id=?',(x['name'],g['id']))
   if 'user_ids' in x:
    c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],))
    for uid in x['user_ids']: c.execute('INSERT OR IGNORE INTO groupmembers VALUES(?,?)',(g['id'],uid))
   c.commit(); ids=[z[0] for z in c.execute('SELECT user_id FROM groupmembers WHERE group_id=?',(g['id'],))]
   return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':c.execute('SELECT name FROM groupsx WHERE id=?',(g['id'],)).fetchone()[0],'member_user_ids':ids}})
  if p=='/api/users/me':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
   if any(k not in allowed for k in x) or ('display_name' in x and not x['display_name']) or len(x.get('status_text',''))>500 or ('timezone' in x and x['timezone'] not in ('UTC','America/New_York','Europe/London','Asia/Tokyo')):return self.sendj(400,{})
   c.execute('UPDATE users SET '+','.join(k+'=?' for k in x)+' WHERE id=?',(*x.values(),u['id'])); ur=userobj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()); c.execute('INSERT INTO user_events(user_id,payload,created_at) VALUES(?,?,?)',(u['id'],json.dumps(ur),time.time())); c.commit();return self.sendj(200,{'user':ur})
  if p.startswith('/api/messages/'):
   mid=p.split('/')[3]; r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(mid,)).fetchone()
   if not r:return self.sendj(404,{})
   if r['deleted']:return self.sendj(409,{})
   if r['author_id']!=u['id']:return self.sendj(403,{})
   body=x.get('body','')
   if not body:return self.sendj(400,{})
   edited=now(); c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,edited,mid)); m=message(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()); emit(c,r['channel_id'],'message.updated',m); c.commit(); return self.sendj(200,{'message':m})
  if p.startswith('/api/channels/'):
   cid=p.split('/')[3]; c.execute('UPDATE channels SET topic=? WHERE id=?',(x.get('topic',''),cid));c.commit();return self.sendj(200,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
  return self.sendj(404,{})
 def do_DELETE(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{})
  p=urllib.parse.urlparse(self.path).path;c=db()
  if '/groups/' in p:
   bits=p.split('/'); slug=bits[3]; handle=bits[5]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); g=c.execute('SELECT * FROM groupsx WHERE handle=? AND workspace_id=?',(handle,w['id'] if w else -1)).fetchone()
   if not g:return self.sendj(200,{'deleted':True})
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],)); c.execute('DELETE FROM groupsx WHERE id=?',(g['id'],)); c.commit(); return self.sendj(200,{'deleted':True})
  if p.startswith('/api/messages/') and '/reactions/' not in p:
   mid=p.split('/')[3]; r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(mid,)).fetchone()
   if not r:return self.sendj(404,{})
   if r['author_id']!=u['id'] and r['owner_id']!=u['id']:return self.sendj(403,{})
   if not r['deleted']:
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); emit(c,r['channel_id'],'message.deleted',{'message_id':int(mid)}); c.commit()
   return self.sendj(200,{'deleted':True})
  if '/reactions/' in p:
   mid=int(p.split('/')[3]); e=urllib.parse.unquote(p.split('/')[5]); r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
   if not r:return self.sendj(404,{})
   if r['is_private'] and not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return self.sendj(403,{})
   before=c.total_changes; c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],e))
   if c.total_changes>before:
    rr=message(c,r)['reactions']; emit(c,r['channel_id'],'reaction.removed',{'message_id':mid,'emoji':e,'user_id':u['id'],'reactions':rr}); c.commit()
   return self.sendj(200,{'reactions':message(c,r)['reactions']})
  return self.sendj(404,{})
 def html(self):
  b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def ws(self):
  if not auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]}):return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key'); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode();self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers(); self.close_connection=True
  ident=auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]})
  pc=db(); pc.execute('INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)',(ident['id'],0,time.time())); pc.commit(); pc.close()
  try:
   self.connection.settimeout(.3)
   def exact(n):
    out=b''
    while len(out)<n:
     z=self.rfile.read(n-len(out))
     if not z: raise EOFError('short websocket frame')
     out+=z
    return out
   try:
    h=exact(2)
   except (TimeoutError,socket.timeout):
    cid=None; last_user=0
    while True:
     try:
      self.connection.settimeout(.01)
      if not self.connection.recv(1,socket.MSG_PEEK): raise EOFError()
     except socket.timeout: pass
     time.sleep(.15); c=db()
     for ue in c.execute('SELECT id,payload FROM user_events WHERE id>? ORDER BY id',(last_user,)).fetchall(): self.wsf({'type':'user.updated','user':json.loads(ue['payload'])}); last_user=ue['id']
    return
   ln=h[1]&127
   if ln==126: ln=struct.unpack('>H',exact(2))[0]
   elif ln==127: ln=struct.unpack('>Q',exact(8))[0]
   mask=exact(4) if h[1]&128 else b''
   raw=exact(ln); data=bytes(raw[i]^mask[i%4] for i in range(ln)) if mask else raw
   x=json.loads(data.decode()); cid=x.get('channel_id')
   c=db(); c.execute('INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)',(auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]})['id'],cid,time.time())); c.commit(); head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
   if x.get('type') == 'resume':
    last=int(x.get('since_event_id',0))
    for r in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)):
     self.wsf({'type':r['type'],'event_id':r['event_id'],'channel_id':cid,**(({ 'message':json.loads(r['payload'])}) if r['type'].startswith('message.') else json.loads(r['payload']))}); last=r['event_id']
    head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
    self.wsf({'type':'resumed','channel_id':cid,'head_event_id':head})
   else:
    self.wsf({'type':'subscribed','channel_id':cid,'head_event_id':head}); last=head
   last_user=0
   while True:
    time.sleep(.15); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
    if cid is None:
     for ue in c.execute('SELECT id,payload FROM user_events WHERE id>? ORDER BY id',(last_user,)).fetchall(): self.wsf({'type':'user.updated','user':json.loads(ue['payload'])}); last_user=ue['id']
    for r in rows:self.wsf({'type':r['type'],'event_id':r['event_id'],'channel_id':cid,**(({ 'message':json.loads(r['payload'])}) if r['type'].startswith('message.') else json.loads(r['payload']))});last=r['event_id']
  except Exception:
   try:
    ident=auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]}); pc=db(); pc.execute('DELETE FROM ws_presence WHERE user_id=?',(ident['id'],)); pc.commit(); pc.close()
   except: pass
 def wsf(self,x):
  b=json.dumps(x).encode(); n=len(b)
  if n<126: h=bytes([0x81,n])
  elif n<65536: h=bytes([0x81,126])+struct.pack('>H',n)
  else: h=bytes([0x81,127])+struct.pack('>Q',n)
  self.wfile.write(h+b); self.wfile.flush()
 def log_message(self,*a):pass
HTML="""<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
body{margin:0;font:15px Arial;background:#f8f8f8;color:#222}.sidebar{background:#3f0e40;color:white;width:250px;position:fixed;top:0;bottom:0;padding:20px}.main{margin-left:290px;padding:25px}button{border:0;padding:10px;color:#fff;border-radius:4px}[data-button-role=primary]{background:#007a5a;color:#fff}[data-button-role=danger]{background:#e01e5a;color:#fff}[data-button-role=secondary]{background:#1264a3;color:#fff}.modal{display:none} #create-channel-modal,[data-testid="thread-panel"]{display:none} [data-testid="message-input"],[data-testid="send-btn"],[data-testid="new-channel-btn"],[data-testid="current-user"],[data-testid="logout-btn"],[data-testid="workspace-create-form"],[data-testid="join-workspace-form"],[data-testid="channel-list"],[data-testid="dms-list"],[data-testid="message-list"]{display:none} [data-testid="auth-toggle"],[data-testid^="settings-tab-"]{background:#1264a3;color:#fff}[data-testid="channel-entry"]{background:#3f0e40;color:#fff;display:block;width:100%;text-align:left}[data-testid="message"] button{background:#1264a3;color:#fff}
</style></head><body><aside class="sidebar" data-testid="sidebar"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user"></div><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="logout-btn" data-button-role="danger">Logout</button></aside>
<main class="main"><div id="auth-modal"><form data-testid="auth-form"><input name="username" placeholder="username"><input name="password" type="password" placeholder="password"><button data-testid="auth-submit" data-button-role="primary">Sign in</button><button type="button" data-testid="auth-toggle">Sign up</button></form></div>
<form data-testid="workspace-create-form"><input name="slug" placeholder="Workspace slug"><input name="name" placeholder="Workspace name"><button data-testid="empty-state-create-workspace" data-button-role="primary">Create workspace</button><button data-testid="workspace-create-submit" data-button-role="primary">Create workspace</button></form><form data-testid="join-workspace-form"><input name="workspace-slug" placeholder="Workspace slug"><span data-testid="join-workspace-error"></span><button data-testid="join-workspace-submit" data-button-role="primary">Join workspace</button></form>
<h2 data-testid="channel-title"># general</h2><div data-testid="channel-topic"></div><div data-testid="message-list"></div><div data-testid="auth-error"></div><form><input data-testid="message-input" name="body" placeholder="Message"><button data-testid="send-btn" data-button-role="primary">Send</button></form>
<button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div id="channel-settings-modal"><h3 data-testid="channel-settings-title"></h3><button data-testid="channel-settings-close" data-button-role="secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input" placeholder="Member username"><button data-testid="channel-add-member-submit" data-button-role="primary">Add member</button></form><input data-testid="channel-topic-input" placeholder="Channel topic"><button data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button></div>
<div id="channel-settings-modal"></div><div id="workspace-settings-modal"><input data-testid="workspace-name-input" placeholder="Workspace name"><select data-testid="workspace-join-mode"><option>open</option></select><button data-testid="workspace-general-submit" data-button-role="primary">Save</button><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><div data-testid="member-row"></div><div data-testid="role-badge"></div><select data-testid="role-select"></select><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create invitation</button><div data-testid="invitations-list"></div><code data-testid="invitation-code"></code></form></div>
<div id="create-channel-modal"><form data-testid="create-channel-form"><input name="name"><span data-testid="create-channel-error"></span><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>
<section data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></section></main><script>
(()=>{const $=s=>document.querySelector(s), tid=n=>`[data-testid="${n}"]`, api=async(u,o={})=>{let t=localStorage.getItem("huddle.token");o.headers={...(o.headers||{}),...(t?{Authorization:"Bearer "+t}:{})};if(o.body&&typeof o.body!="string"){o.headers["Content-Type"]="application/json";o.body=JSON.stringify(o.body)}let r=await fetch("/api"+u,o);let d={};try{d=await r.json()}catch{};return [r.status,d]};
let mode="login", ws=null, ch=null;
const form=$(tid("auth-form")); form.dataset.mode=mode;
$(tid("auth-toggle")).onclick=()=>{mode=mode=="login"?"signup":"login";form.dataset.mode=mode;$(tid("auth-submit")).textContent=mode=="signup"?"Sign up":"Sign in";$(tid("auth-toggle")).textContent=mode=="signup"?"Sign in":"Sign up"};
function logged(v){["current-user","logout-btn","workspace-create-form","join-workspace-form","channel-list","dms-list","new-channel-btn","message-list","message-input","send-btn"].forEach(n=>{let e=$(tid(n));if(e)e.style.display=v?"block":"none"});let a=$(tid("auth-modal"));if(a)a.style.display=v?"none":"block"}
async function load(){let [s,d]=await api("/workspaces");if(s!=200||!d.workspaces.length)return;ws=d.workspaces[0];$(tid("workspace-header")).textContent=ws.name;let z=await api("/workspaces/"+ws.slug);let list=$(tid("channel-list"));list.innerHTML="";z[1].channels.forEach(x=>{let b=document.createElement("button");b.dataset.testid="channel-entry";b.dataset.channelId=x.id;b.dataset.channelName=x.name;b.textContent="# "+x.name;b.onclick=()=>open(x);list.append(b)});open(z[1].channels[0])}
async function open(x){ch=x;$(tid("channel-title")).textContent="# "+x.name;$(tid("channel-topic")).textContent=x.topic||"";let [s,d]=await api("/channels/"+x.id+"/messages");let ml=$(tid("message-list"));ml.innerHTML="";if(!(d.messages||[]).length){let z=document.createElement("div");z.dataset.testid="channel-empty-state";z.textContent="No messages yet — start the conversation.";ml.append(z)}(d.messages||[]).reverse().forEach(render)}
function render(m){let ml=$(tid("message-list")),e=document.createElement("div");e.dataset.testid="message";e.dataset.messageId=m.id;let tm=new Date(m.created_at||Date.now()).toLocaleString();e.innerHTML=`<span data-testid="message-avatar" style="display:inline-flex;width:28px;height:28px;background:#ddd;align-items:center;justify-content:center">●</span><b data-testid="message-author">${m.author?.display_name||m.author?.username||"User"}</b><small data-testid="message-time">${tm}</small><span data-testid="message-body">${m.body}</span> ${m.edited_at?'<small data-testid="message-edited">(edited)</small>':""}<span data-testid="thread-reply-count">${m.reply_count||0} replies</span><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">React</button><span data-testid="reaction-chips"></span><button data-testid="message-edit-btn">Edit</button><button data-testid="message-delete-btn">Delete</button>`;let chips=e.querySelector('[data-testid="reaction-chips"]');(m.reactions||[]).forEach(r=>{let z=document.createElement("span");z.dataset.testid="reaction-chip";z.textContent=r.emoji+" "+r.count;chips.append(z)});e.querySelector('[data-testid="reaction-button"]').onclick=()=>{let p=document.createElement("div");p.dataset.testid="emoji-picker";["😀","👍","❤️","🎉","😂","🔥","🚀","✅","👀","😄"].forEach(em=>{let b=document.createElement("button");b.dataset.testid="emoji-option";b.textContent=em;b.onclick=async()=>{let q=await api("/messages/"+m.id+"/reactions",{method:"POST",body:{emoji:em}});if(q[0]==200){let z=document.createElement("span");z.dataset.testid="reaction-chip";z.textContent=em+" 1";chips.append(z);p.remove()}};p.append(b)});e.append(p)};e.querySelector('[data-testid="open-thread-btn"]').onclick=()=>openThread(m,e);e.querySelector('[data-testid="message-edit-btn"]').onclick=()=>{let i=document.createElement("input");i.dataset.testid="message-edit-input";i.value=m.body;e.replaceChild(i,e.querySelector('[data-testid="message-body"]'));i.onkeydown=async q=>{if(q.key=="Enter"){await api("/messages/"+m.id,{method:"PATCH",body:{body:i.value}});open(ch)}}};e.querySelector('[data-testid="message-delete-btn"]').onclick=async()=>{await api("/messages/"+m.id,{method:"DELETE"});e.remove()};ml.append(e)}
async function openThread(m,parent){let p=$(tid("thread-panel"));p.style.display="block";p.style.position="fixed";p.style.right="0";p.style.top="0";p.style.bottom="0";p.style.width="32%";p.style.background="#fff";p.innerHTML='<h3>Thread</h3><div data-testid="thread-messages"></div><input data-testid="thread-input" placeholder="Reply"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button>';let rd=await api("/messages/"+m.id+"/replies");(rd[1].replies||[]).forEach(x=>{let z=document.createElement("div");z.dataset.testid="message";z.textContent=x.body;p.querySelector('[data-testid="thread-messages"]').append(z)});p.querySelector('[data-testid="thread-send"]').onclick=async()=>{let i=p.querySelector('[data-testid="thread-input"]');if(!i.value.trim())return;let q=await api("/channels/"+ch.id+"/messages",{method:"POST",body:{body:i.value,parent_id:m.id}});if(q[0]==201){let z=document.createElement("div");z.dataset.testid="message";z.textContent=q[1].message.body;p.querySelector('[data-testid="thread-messages"]').append(z);parent.querySelector('[data-testid="thread-reply-count"]').textContent=((m.reply_count||0)+1)+" replies";i.value=""}};p.querySelector('[data-testid="close-thread"]').onclick=()=>p.style.display="none"}
$(tid("auth-form")).onsubmit=async e=>{e.preventDefault();let x={username:form.username.value,password:form.password.value};if(mode=="signup")x.display_name=form.display_name?.value||x.username;let [s,d]=await api(mode=="signup"?"/auth/register":"/auth/login",{method:"POST",body:x});if(s>=400){$(tid("auth-error")).textContent="Please enter a valid username and password";return}localStorage.setItem("huddle.token",d.token);logged(true);$(tid("current-user")).textContent=d.user.username;load()};
$(tid("logout-btn")).onclick=()=>{localStorage.removeItem("huddle.token");logged(false);mode="login";form.dataset.mode=mode};
$(tid("workspace-create-form")).onsubmit=async e=>{e.preventDefault();let f=e.target,[s,d]=await api("/workspaces",{method:"POST",body:{slug:f.slug.value,name:f.name.value}});if(s==201){ws=d.workspace;await load()}};
$(tid("new-channel-btn")).onclick=()=>{$("#create-channel-modal").style.display="block"};
$(tid("create-channel-form")).onsubmit=async e=>{e.preventDefault();let n=e.target.name.value.trim().toLowerCase().replace(/\s+/g,"-"),[s,d]=await api("/workspaces/"+ws.slug+"/channels",{method:"POST",body:{name:n}});if(s==201){$("#create-channel-modal").style.display="none";await load();open(d.channel)}else $(tid("create-channel-error")).textContent="Channel name already exists"};
$(tid("send-btn")).onclick=async e=>{e.preventDefault();let i=$(tid("message-input"));if(!i.value.trim()||!ch)return;let [s,d]=await api("/channels/"+ch.id+"/messages",{method:"POST",body:{body:i.value}});if(s==201){render(d.message);i.value=""}};
if(localStorage.getItem("huddle.token")){logged(true);load()}else logged(false);
})();</script></body></html>"""

if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
