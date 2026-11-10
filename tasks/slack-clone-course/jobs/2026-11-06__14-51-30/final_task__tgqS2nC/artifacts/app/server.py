import os,json,re,secrets,sqlite3,time,threading,socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse
DB='/var/lib/huddle/huddle.db'; PORT=int(os.getenv('PORT','8000')); NODE=PORT-8000
os.makedirs('/var/lib/huddle',exist_ok=True)
def db():
 c=sqlite3.connect(DB,timeout=2,isolation_level=None);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=2000');return c
def init():
 c=db();c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));''');c.close()
def now():return time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime())
def uj(u):return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def au(h):
 x=h.get('Authorization','');c=db();u=c.execute('select u.* from users u join tokens t on u.id=t.user_id where t.token=?',(x[7:] if x.startswith('Bearer ') else '',)).fetchone();c.close();return u
def cj(x):return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'] or '','is_archived':bool(x['is_archived'])}
def mj(x):
 c=db();u=c.execute('select * from users where id=?',(x['author_id'],)).fetchone();rs=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(x['id'],)).fetchall();rc=c.execute('select count(*) n from messages where parent_id=? and deleted=0',(x['id'],)).fetchone()['n'];c.close()
 return {'id':x['id'],'channel_id':x['channel_id'],'author_id':x['author_id'],'author':uj(u),'body':x['body'],'parent_id':x['parent_id'],'reply_count':rc,'created_at':x['created_at'],'edited_at':x['edited_at'],'files':[],'reactions':[{'emoji':r['emoji'],'count':r['n'],'user_ids':[int(i) for i in r['ids'].split(',')]} for r in rs],'mentions':[]}
try: HTML=open('/app/static/index.html').read()
except: HTML='<!doctype html><html><body>Huddle</body></html>'
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def out(self,n,x,ct='application/json'):
  b=x if isinstance(x,bytes) else json.dumps(x).encode();self.send_response(n);self.send_header('Content-Type',ct);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def body(self):return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')) or 0) or b'{}')
 def do_GET(self):
  p=urlparse(self.path).path
  if p=='/':return self.out(200,HTML.encode(),'text/html')
  if p=='/api/health':return self.out(200,{'status':'ok','node_id':NODE})
  u=au(self.headers)
  if not u:return self.out(401,{'error':'unauthorized'})
  c=db()
  if p=='/api/auth/me':return self.out(200,{'user':uj(u)})
  if p=='/api/workspaces':
   w=c.execute('select w.* from workspaces w join wm on w.id=wm.workspace_id where wm.user_id=?',(u['id'],)).fetchall();return self.out(200,{'workspaces':[dict(x) for x in w]})
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.out(404,{})
   cs=c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],)).fetchall();return self.out(200,{'workspace':dict(w),'channels':[cj(x) for x in cs]})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   x=c.execute('select * from channels where id=?',(m.group(1),)).fetchone()
   if not x:return self.out(404,{})
   if x['is_private'] and not c.execute('select 1 from cm where channel_id=? and user_id=?',(x['id'],u['id'])).fetchone():return self.out(403,{})
   q=c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(x['id'],)).fetchall();return self.out(200,{'messages':[mj(z) for z in q],'next_cursor':None})
  c.close();return self.out(404,{})
 def do_POST(self):
  p=urlparse(self.path).path;b=self.body()
  if p=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return self.out(400,{'error':'invalid registration'})
   c=db()
   try:c.execute('insert into users(username,password,display_name) values(?,?,?)',(b['username'],b['password'],b.get('display_name') or b['username']));uid=c.execute('select last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));u=c.execute('select * from users where id=?',(uid,)).fetchone();return self.out(201,{'user':uj(u),'token':t})
   except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
  if p=='/api/auth/login':
   c=db();u=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
   if not u:return self.out(401,{})
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,u['id']));return self.out(200,{'user':uj(u),'token':t})
  u=au(self.headers)
  if not u:return self.out(401,{})
  c=db()
  if p=='/api/workspaces':
   try:c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(b['slug'],b['name'],u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into wm values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));w=c.execute('select * from workspaces where id=?',(wid,)).fetchone();ch=c.execute('select * from channels where id=?',(cid,)).fetchone();return self.out(201,{'workspace':dict(w),'general_channel':cj(ch)})
   except sqlite3.IntegrityError:return self.out(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone();name=re.sub('[^a-z0-9-]+','-',b.get('name','').lower()).strip('-')
   try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cm values(?,?)',(cid,u['id']));return self.out(201,{'channel':cj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   except sqlite3.IntegrityError:return self.out(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',p)
  if m:
   x=c.execute('select * from channels where id=?',(m.group(1),)).fetchone()
   if not x:return self.out(404,{})
   c.execute('insert or ignore into cm values(?,?)',(x['id'],u['id']));c.execute('insert or ignore into wm values(?,?,?)',(x['workspace_id'],u['id'],'member'));return self.out(200,{'channel':cj(x)})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   if not b.get('body','').strip():return self.out(400,{})
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(m.group(1),u['id'],b['body'],b.get('parent_id'),now()));x=c.execute('select * from messages where id=?',(c.execute('select last_insert_rowid()').fetchone()[0],)).fetchone();return self.out(201,{'message':mj(x)})
  return self.out(404,{})
 def do_PATCH(self):
  u=au(self.headers);b=self.body()
  if not u:return self.out(401,{})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   c=db();x=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
   if not x:return self.out(404,{})
   if x['author_id']!=u['id']:return self.out(403,{})
   if not b.get('body','').strip():return self.out(400,{})
   c.execute('update messages set body=?,edited_at=? where id=?',(b['body'],now(),x['id']));return self.out(200,{'message':mj(c.execute('select * from messages where id=?',(x['id'],)).fetchone())})
  return self.out(404,{})
 def do_DELETE(self):
  u=au(self.headers);m=re.match(r'/api/messages/(\d+)$',self.path)
  if not u:return self.out(401,{})
  c=db();x=c.execute('select * from messages where id=?',(m.group(1),)).fetchone() if m else None
  if not x:return self.out(404,{})
  c.execute('update messages set deleted=1 where id=?',(x['id'],));return self.out(200,{'deleted':True})
init()
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
