import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,socket,threading,time
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; STATIC='/app/static/index.html'
lock=threading.RLock(); clients=[]; irc_clients=[]; irc_lock=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=5,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 os.makedirs('/var/lib/huddle',exist_ok=True)
 c=db(); c.execute('PRAGMA journal_mode=WAL'); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
'''); c.close()
def userj(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(h):
 t=(h.get('Authorization','') or '').replace('Bearer ',''); c=db(); r=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(t,)).fetchone(); c.close(); return r
def chan(c,i): return c.execute('select * from channels where id=?',(i,)).fetchone()
def channelj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def members(c,cid): return [x['user_id'] for x in c.execute('select user_id from cmembers where channel_id=?',(cid,))]
def msgj(c,r):
 u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 rs=[]
 for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
  rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[z['user_id'] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':rs,'mentions':[]}
def emit(cid,kind,payload):
 with lock:
  c=db(); n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0]; c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(cid,n,kind,json.dumps(payload))); c.commit(); c.close()
 frame={'type':kind,'event_id':n,'channel_id':cid}; frame.update(payload); return frame
def broadcast(cid,obj,skip=None):
 data=json.dumps(obj)
 with lock:
  for s,u,ch in list(clients):
   if ch==cid and s is not skip:
    try: ws_send(s,data)
    except: pass
 with irc_lock:
  for cl in list(irc_clients):
   if cid in cl.get('joined',set()) and cl is not skip:
    try:
     r=cl['channel']; m=obj.get('message',{}); nick=m.get('author',{}).get('username','huddle')
     cl['sock'].sendall((':{}!{}@host PRIVMSG {} :{}\r\n'.format(nick,nick,r,m.get('body',''))).encode())
    except: pass
def ws_send(s,x):
 b=x.encode(); n=len(b)
 if n<126: h=bytes([0x81,n])
 elif n<65536: h=struct.pack('!BBH',0x81,126,n)
 else: h=struct.pack('!BBQ',0x81,127,n)
 s.sendall(h+b)
def ws_read(s):
 h=s.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=struct.unpack('!H',s.recv(2))[0]
 elif n==127:n=struct.unpack('!Q',s.recv(8))[0]
 mask=s.recv(4); b=bytearray(s.recv(n)); return bytes(v^mask[i%4] for i,v in enumerate(b)).decode()
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,o):
  b=json.dumps(o).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def do_OPTIONS(self): self.sendj(200,{})
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  if self.path=='/':
   b=open(STATIC,'rb').read(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if self.path.startswith('/api/ws'): return self.websocket()
  p=self.path.split('?')[0]; a=p.strip('/').split('/'); u=auth(self.headers)
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':(int(sys.argv[1])-8000) if len(sys.argv)>1 and sys.argv[1].isdigit() else 0})
  if p=='/api/auth/me': return self.sendj(200,{'user':userj(u)}) if u else self.sendj(401,{})
  if not u:return self.sendj(401,{})
  c=db()
  if a[:2]==['api','workspaces'] and len(a)==2:
   ws=[dict(x) for x in c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],))]
   c.close(); return self.sendj(200,{'workspaces':ws})
  if a[:2]==['api','workspaces'] and len(a)==3:
   w=c.execute('select * from workspaces where slug=?',(a[2],)).fetchone()
   if not w:return self.sendj(404,{})
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
   cs=[channelj(x) for x in c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],))]
   return self.sendj(200,{'workspace':dict(w),'channels':cs})
  if len(a)>=4 and a[1]=='channels' and a[3]=='messages':
   ch=chan(c,int(a[2]))
   if not ch:return self.sendj(404,{})
   if ch['is_private'] and u['id'] not in members(c,ch['id']):return self.sendj(403,{})
   ms=[msgj(c,x) for x in c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(ch['id'],))]
   return self.sendj(200,{'messages':ms,'next_cursor':None})
  if len(a)>=4 and a[1]=='channels' and a[3]=='members':
   return self.sendj(200,{'members':[userj(c.execute('select * from users where id=?',(i,)).fetchone()) for i in members(c,int(a[2]))]})
  if len(a)>=4 and a[1]=='messages' and a[3]=='replies':
   r=c.execute('select * from messages where id=?',(a[2],)).fetchone()
   if not r:return self.sendj(404,{})
   return self.sendj(200,{'replies':[msgj(c,x) for x in c.execute('select * from messages where parent_id=? and deleted=0 order by id',(a[2],))],'next_cursor':None})
  c.close(); return self.sendj(404,{})
 def do_POST(self):
  p=self.path.split('?')[0]; a=p.strip('/').split('/'); u=auth(self.headers)
  if a[:3]==['api','auth','register']:
   c=None
   try:
    q=self.body(); un=q['username']; pw=q['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise ValueError()
    c=db(); c.execute('insert into users(username,password,display_name) values(?,?,?)',(un,pw,q.get('display_name') or un)); c.commit(); uid=c.execute('select last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); c.commit(); r=c.execute('select * from users where id=?',(uid,)).fetchone(); return self.sendj(201,{'user':userj(r),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   except:return self.sendj(400,{})
   finally:
    if c:
     c.close()
  if a[:3]==['api','auth','login']:
   c=db()
   try:
    q=self.body(); r=c.execute('select * from users where username=? and password=?',(q.get('username'),q.get('password'))).fetchone()
    if not r:return self.sendj(401,{})
    t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,r['id'])); c.commit(); return self.sendj(200,{'user':userj(r),'token':t})
   finally:c.close()
  if not u:return self.sendj(401,{})
  c=db()
  try:
   if a[:2]==['api','workspaces'] and len(a)==2:
    q=self.body(); c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(q['slug'],q['name'],u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,u['id'])); c.commit(); w=c.execute('select * from workspaces where id=?',(wid,)).fetchone(); return self.sendj(201,{'workspace':dict(w),'general_channel':channelj(chan(c,cid))})
   if len(a)>=4 and a[1]=='workspaces' and a[3]=='channels':
    w=c.execute('select * from workspaces where slug=?',(a[2],)).fetchone(); q=self.body(); c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],q['name'],int(q.get('is_private',False)),q.get('topic',''))); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,u['id'])); c.commit(); return self.sendj(201,{'channel':channelj(chan(c,cid))})
   if len(a)>=4 and a[1]=='channels' and a[3]=='join':
    ch=chan(c,int(a[2])); w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
    if ch['is_private']:return self.sendj(403,{})
    c.execute('insert or ignore into members values(?,?,?)',(w['id'],u['id'],'member')); c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],u['id'])); c.commit(); return self.sendj(200,{'channel':channelj(ch)})
   if len(a)>=4 and a[1]=='channels' and a[3] in ('archive','unarchive'):
    c.execute('update channels set is_archived=? where id=?',(a[3]=='archive',a[2])); c.commit(); return self.sendj(200,{'channel':channelj(chan(c,int(a[2])))})
   if len(a)>=4 and a[1]=='channels' and a[3]=='messages':
    ch=chan(c,int(a[2])); q=self.body(); body=q.get('body','')
    if not body:return self.sendj(400,{})
    if ch['is_archived']:return self.sendj(423,{})
    c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],u['id'],body,q.get('parent_id'),now())); mid=c.execute('select last_insert_rowid()').fetchone()[0]; c.commit(); m=msgj(c,c.execute('select * from messages where id=?',(mid,)).fetchone())
    names=set(re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body))
    m['mentions']=[x['id'] for x in c.execute('select id from users where username in (%s)' % (','.join('?'*len(names)) if names else "''"),tuple(names)) if x['id']!=u['id']]
    emit(ch['id'],'message.reply' if q.get('parent_id') else 'message.created',{'message':m}); return self.sendj(201,{'message':m})
   if len(a)>=4 and a[1]=='messages' and a[3]=='reactions':
    mid=int(a[2]); q=self.body(); c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],q['emoji'])); c.commit(); m=c.execute('select * from messages where id=?',(mid,)).fetchone(); return self.sendj(200,{'reactions':msgj(c,m)['reactions']})
  except sqlite3.IntegrityError:return self.sendj(409,{})
  except Exception:return self.sendj(400,{})
  finally:c.close()
 def do_PATCH(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{})
  a=self.path.split('?')[0].strip('/').split('/'); c=db()
  try:
   if len(a)>=3 and a[1]=='channels':
    ch=chan(c,int(a[2])); q=self.body()
    if not ch:return self.sendj(404,{})
    c.execute('update channels set topic=? where id=?',(q.get('topic',''),ch['id'])); c.commit()
    return self.sendj(200,{'channel':channelj(chan(c,ch['id']))})
   if len(a)>=3 and a[1]=='messages':
    r=c.execute('select * from messages where id=?',(a[2],)).fetchone()
    if not r:return self.sendj(404,{})
    if r['deleted']:return self.sendj(409,{})
    if r['author_id']!=u['id']:return self.sendj(403,{})
    q=self.body()
    if not q.get('body'):return self.sendj(400,{})
    edited=now(); c.execute('update messages set body=?,edited_at=? where id=?',(q['body'],edited,r['id'])); c.commit()
    m=msgj(c,c.execute('select * from messages where id=?',(r['id'],)).fetchone()); m['edited_at']=edited
    emit(r['channel_id'],'message.updated',{'message':m}); return self.sendj(200,{'message':m})
   return self.sendj(404,{})
  finally:c.close()
 def do_DELETE(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{})
  a=self.path.split('?')[0].strip('/').split('/'); c=db()
  try:
   if len(a)>=3 and a[1]=='messages':
    r=c.execute('select * from messages where id=?',(a[2],)).fetchone()
    if not r:return self.sendj(404,{})
    w=c.execute('select w.* from workspaces w join channels ch on ch.workspace_id=w.id where ch.id=?',(r['channel_id'],)).fetchone()
    if r['author_id']!=u['id'] and w['owner_id']!=u['id']:return self.sendj(403,{})
    if not r['deleted']:
     c.execute('update messages set deleted=1 where id=?',(r['id'],)); c.commit(); emit(r['channel_id'],'message.deleted',{'message_id':r['id']})
    return self.sendj(200,{'deleted':True})
   return self.sendj(404,{})
  finally:c.close()
 def websocket(self):
  u=auth({'Authorization':'Bearer '+(self.path.split('token=')[-1])})
  if not u:return self.connection.close()
  key=self.headers.get('Sec-WebSocket-Key'); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','WebSocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers(); s=self.connection
  try:
   while True:
    q=json.loads(ws_read(s) or '{}')
    if q.get('type') in ('subscribe','resume'):
     cid=int(q['channel_id']); c=db(); head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(cid,)).fetchone()[0]; c.close()
     with lock: clients.append((s,u['id'],cid))
     ws_send(s,json.dumps({'type':'subscribed' if q['type']=='subscribe' else 'resumed','channel_id':cid,'head_event_id':head}))
  except: pass
def event_pump():
 seen={}
 try:
  c=db()
  for r in c.execute('select channel_id,max(event_id) m from events group by channel_id'): seen[r['channel_id']]=r['m']
  c.close()
 except: pass
 while True:
  try:
   c=db()
   for cid,eid,kind,payload in c.execute('select channel_id,event_id,kind,payload from events order by id'):
    if eid>seen.get(cid,0):
     seen[cid]=eid
     frame={'type':kind,'event_id':eid,'channel_id':cid}; frame.update(json.loads(payload))
     with lock:
      for sock,uid,sub in list(clients):
       if sub==cid:
        try: ws_send(sock,json.dumps(frame))
        except: pass
     if kind in ('message.created','message.reply','message.updated'):
      body=json.loads(payload).get('message',{})
      with irc_lock:
       for cl in list(irc_clients):
        if cid in cl.get('joined',set()):
         try:
          name=cl.get('channel_names',{}).get(cid,'#channel')
          nick=body.get('author',{}).get('username','huddle')
          cl['sock'].sendall((':{}!{}@host PRIVMSG {} :{}\r\n'.format(nick,nick,name,body.get('body',''))).encode())
         except: pass
   c.close()
  except: pass
  time.sleep(.1)
def irc():
 srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen()
 while True:
  s,_=srv.accept(); threading.Thread(target=irc_client,args=(s,),daemon=True).start()
def irc_client(s):
 cl={'sock':s,'joined':set(),'nick':None,'uid':None}; irc_clients.append(cl); buf=b''
 try:
  while True:
   buf+=s.recv(4096)
   if not buf:break
   while b'\r\n' in buf:
    line,buf=buf.split(b'\r\n',1); z=line.decode(errors='ignore').split(' ',2); cmd=z[0].upper(); arg=z[1:] 
    if cmd=='PASS':
     c=db(); r=c.execute('select user_id from tokens where token=?',(arg[0] if arg else '',)).fetchone(); cl['uid']=r['user_id'] if r else None
     if not cl['uid']:s.sendall(b':huddle 464 * :Password incorrect\r\n')
    elif cmd=='NICK':
     if any(x.get('nick')==arg[0] for x in irc_clients if x is not cl):s.sendall((':huddle 433 * {} :Nickname in use\r\n'.format(arg[0])).encode())
     else:cl['nick']=arg[0]
    elif cmd=='USER' and cl['uid'] and cl['nick']:
     for n,t in [('001','Welcome'),('002','Your host'),('003','Created'),('004','huddle'),('005','CHANTYPES=#'),('422','No MOTD')]:s.sendall((':huddle {} {} :{}\r\n'.format(n,cl['nick'],t)).encode())
    elif cmd=='PING':s.sendall(('PONG :'+(arg[-1].lstrip(':') if arg else '')+'\r\n').encode())
    elif cmd=='QUIT':return
    elif cmd=='JOIN' and arg:
     name=arg[-1].lstrip(':'); m=re.match(r'#([^/]+)/(.+)',name); c=db(); r=c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(m.group(1),m.group(2))) .fetchone() if m else None
     if not r:s.sendall((':huddle 403 {} :No such channel\r\n'.format(cl['nick'])).encode()); continue
     cl['joined'].add(r['id']); cl['channel_names']=getattr(cl,'channel_names',{}) if False else cl.get('channel_names',{}); cl['channel_names'][r['id']]=name; prefix=':{}!{}@host JOIN {}\r\n'.format(cl['nick'],cl['nick'],name); s.sendall(prefix.encode()); s.sendall((':huddle 353 {} = {} :{}\r\n:huddle 366 {} {} :End\r\n'.format(cl['nick'],name,cl['nick'],cl['nick'],name)).encode())
    elif cmd=='PRIVMSG' and len(arg)>=2:
     name=arg[0]; body=' '.join(arg[1:]).lstrip(':'); m=re.match(r'#([^/]+)/(.+)',name); c=db(); r=c.execute('select ch.* from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(m.group(1),m.group(2))).fetchone() if m else None
     if r:
      c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(r['id'],cl['uid'],body,now())); mid=c.execute('select last_insert_rowid()').fetchone()[0]; c.commit(); emit(r['id'],'message.created',{'message':msgj(c,c.execute('select * from messages where id=?',(mid,)).fetchone())})
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): pass
    elif cmd: s.sendall((':huddle 421 {} :Unknown command\r\n'.format(cmd)).encode())
 finally:
  try:s.close()
  except:pass
if __name__=='__main__':
 init()
 if '--irc' in sys.argv:
  threading.Thread(target=event_pump,daemon=True).start()
  irc()
 else:
  threading.Thread(target=event_pump,daemon=True).start()
  ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
