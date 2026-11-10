import os,sys,re,json,sqlite3,hashlib,secrets,threading,time,base64,struct,urllib.parse,datetime,subprocess,socket,select,email
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/var/lib/huddle/huddle.db'; NODE=int(os.environ.get('NODE_ID','0'))
db_lock=threading.RLock(); clients=[]; clients_lock=threading.RLock()
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=2,check_same_thread=False,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=1500'); c.execute('PRAGMA journal_mode=WAL'); return c
def init():
    with db_lock:
        c=db(); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,body TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS ws_presence(channel_id INTEGER,user_id INTEGER,token TEXT,PRIMARY KEY(channel_id,user_id,token));
CREATE TABLE IF NOT EXISTS reads(channel_id INTEGER,user_id INTEGER,last_read INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0,expires INTEGER,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER);
CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,UNIQUE(message_id,file_id));
'''); c.commit(); c.close()
init()
def out(h,status,obj=None,raw=None,ctype='application/json'):
    h.send_response(status); h.send_header('Content-Type',ctype); h.send_header('Access-Control-Allow-Origin','*'); h.send_header('Access-Control-Allow-Headers','Authorization,Content-Type'); h.send_header('Access-Control-Allow-Methods','GET,POST,PATCH,DELETE,OPTIONS'); h.end_headers()
    if raw is not None: h.wfile.write(raw)
    elif obj is not None: h.wfile.write(json.dumps(obj).encode())
def user(r):
    t=r.headers.get('Authorization','').replace('Bearer ','')
    c=db(); x=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone(); c.close(); return x
def uobj(x):
    return {k:x[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def fileobj(f):
    return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def chobj(x):
    return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'],'is_archived':bool(x['is_archived'])}
def wsobj(x): return {'id':x['id'],'slug':x['slug'],'name':x['name'],'owner_id':x['owner_id'],'join_mode':x['join_mode']}
def auth(h):
    x=user(h)
    if not x: out(h,401,{'error':'unauthorized'}); return None
    return x
def channel(cid):
    c=db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return x
def member(uid,cid):
    c=db(); x=c.execute('SELECT 1 FROM cmembers WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone(); c.close(); return bool(x)
def role(uid,wid):
    c=db(); x=c.execute('SELECT role FROM members WHERE user_id=? AND workspace_id=?',(uid,wid)).fetchone(); c.close(); return x['role'] if x else None
def msgobj(m):
    c=db(); a=c.execute('SELECT * FROM users WHERE id=?',(m['author_id'],)).fetchone(); rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(m['id'],)).fetchall(); fs=[fileobj(f) for f in c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(m['id'],)).fetchall()]
    ids=[]
    members={int(r['id']) for r in c.execute('SELECT u.id FROM users u JOIN cmembers cm ON cm.user_id=u.id WHERE cm.channel_id=?',(m['channel_id'],)).fetchall()}
    if re.search(r'(?<![\w@])@channel(?![\w@])',m['body'],re.I):
        ids.extend(sorted(members-{int(m['author_id'])}))
    names=re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)(?![\w@])',m['body'])
    for name in names:
        if name.lower()=='channel': continue
        z=c.execute('SELECT u.id FROM users u JOIN members wm ON wm.user_id=u.id JOIN channels cc ON cc.workspace_id=wm.workspace_id WHERE lower(u.username)=lower(?) AND cc.id=?',(name,m['channel_id'])).fetchone()
        if z and int(z['id'])!=int(m['author_id']): ids.append(int(z['id']))
    gm=re.findall(r'(?<![\\w@])@([A-Za-z0-9_-]+)(?![\\w@])',m['body'])
    wc=c.execute('SELECT workspace_id FROM channels WHERE id=?',(m['channel_id'],)).fetchone()['workspace_id']
    for handle in gm:
        if handle.lower() in ('channel','here'): continue
        gr=c.execute('SELECT id FROM groups_ WHERE workspace_id=? AND handle=?',(wc,handle)).fetchone()
        if gr:
            ids.extend(int(r['user_id']) for r in c.execute('SELECT gm.user_id FROM group_members gm JOIN cmembers cm ON cm.user_id=gm.user_id WHERE gm.group_id=? AND cm.channel_id=?',(gr['id'],m['channel_id'])).fetchall())
    if re.search(r'(?<![\w@])@here(?![\w@])',m['body'],re.I):
        pc=db()
        ids.extend(int(q['user_id']) for q in pc.execute('SELECT DISTINCT p.user_id FROM ws_presence p JOIN cmembers cm ON cm.user_id=p.user_id WHERE cm.channel_id=?',(m['channel_id'],)).fetchall())
        pc.close()
    ids=[i for i in ids if i!=int(m['author_id'])]
    ids=list(dict.fromkeys(ids))
    return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':uobj(a),'body':m['body'],'parent_id':m['parent_id'],'reply_count':sum(1 for _ in db().execute('SELECT id FROM messages WHERE parent_id=? AND deleted=0',(m['id'],))),'created_at':m['created_at'],'edited_at':m['edited_at'],'files':fs,'reactions':[{'emoji':r['emoji'],'count':r['n'],'user_ids':[int(v) for v in r['ids'].split(',')]} for r in rs],'mentions':ids}
def event(cid,kind,payload):
    with db_lock:
        c=db(); c.execute('INSERT INTO seq(channel_id,n) VALUES(?,1) ON CONFLICT(channel_id) DO UPDATE SET n=n+1',(cid,)); eid=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone()['n']; ev={'type':kind,'event_id':eid,'channel_id':cid}; ev.update(payload); c.execute('INSERT OR REPLACE INTO events VALUES(?,?,?)',(cid,eid,json.dumps(ev))); c.commit(); c.close()
    with clients_lock:
        for q in list(clients):
            try:
                if q[0]==cid: q[1].append(ev)
            except: pass
    return eid
def cursor_decode(v):
    if not v:return 0
    try:
        n=int(base64.urlsafe_b64decode(v.encode()+b'===').decode())
        if n<0:return None
        return n
    except:return None
def cursor_encode(n): return base64.urlsafe_b64encode(str(n).encode()).decode().rstrip('=')
def reaction_list(mid):
    c=db(); rows=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall(); c.close()
    return [{'emoji':r['emoji'],'count':r['n'],'user_ids':[int(v) for v in r['ids'].split(',')]} for r in rows]
def readobj(uid,cid):
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return None
    r=c.execute('SELECT last_read FROM reads WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone()
    last=int(r['last_read']) if r else 0
    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND event_id>? AND author_id!=? ORDER BY event_id',(cid,last,uid)).fetchall()
    unread=len(rows); mentions=0
    for m in rows:
        if uid in msgobj(m).get('mentions',[]): mentions+=1
    c.close()
    return {'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def parse_json(h):
    try: return json.loads(h.rfile.read(int(h.headers.get('Content-Length','0')) or 0) or b'{}')
    except: return {}
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_OPTIONS(self): out(self,204)
    def do_GET(self):
      p=urllib.parse.urlparse(self.path); path=p.path; q=urllib.parse.parse_qs(p.query)
      if path=='/api/health': return out(self,200,{'status':'ok','node_id':NODE})
      if path=='/': return out(self,200,HTML.encode(),ctype='text/html')
      if path=='/api/auth/me':
        x=auth(self); return x and out(self,200,{'user':uobj(x)})
      x=auth(self)
      if not x:return
      c=db()
      if path=='/api/workspaces':
        rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(x['id'],)).fetchall(); return out(self,200,{'workspaces':[wsobj(r) for r in rows]})
      im=re.match(r'^/api/workspaces/([^/]+)/invitations$',path)
      if im:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(im.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        if role(x['id'],w['id']) not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
        rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires IS NULL OR expires>?)',(w['id'],int(time.time()))).fetchall()
        return out(self,200,{'invitations':[dict(r) for r in rows]})
      
      m=re.match(r'^/api/workspaces/([^/]+)(?:/members)?$',path)
      if m:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        if path.endswith('/members'):
          return out(self,200,{'members':[dict(r) for r in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
        if not role(x['id'],w['id']):return out(self,403,{'error':'forbidden'})
        inc=q.get('include_archived',['0'])[0] in ('1','true')
        cs=[z for z in c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],inc)).fetchall() if (not z['is_private'] or member(x['id'],z['id']))]
        rs=[readobj(x['id'],r['id']) for r in cs]
        return out(self,200,{'workspace':wsobj(w),'channels':[chobj(z) for z in cs],'read_state':rs})
      rr=re.match(r'^/api/messages/(\d+)/replies$',path)
      if rr:
        parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(int(rr.group(1)),)).fetchone()
        if not parent:return out(self,404,{'error':'not found'})
        ch=c.execute('SELECT * FROM channels WHERE id=?',(parent['channel_id'],)).fetchone()
        if not ch or not member(x['id'],ch['id']):return out(self,403,{'error':'forbidden'})
        lim=q.get('limit',['50'])[0]
        try: lim=int(lim)
        except:return out(self,400,{'error':'limit'})
        if lim<1 or lim>200:return out(self,400,{'error':'limit'})
        boundary=cursor_decode(q.get('cursor',[''])[0])
        if boundary is None:return out(self,400,{'error':'cursor'})
        if boundary:
          rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id ASC LIMIT ?',(parent['id'],boundary,lim)).fetchall()
        else:
          rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC LIMIT ?',(parent['id'],lim)).fetchall()
        nxt=cursor_encode(rows[-1]['id']) if len(rows)==lim else None
        return out(self,200,{'replies':[msgobj(r) for r in rows],'next_cursor':nxt})
      m=re.match(r'^/api/channels/(\d+)(.*)$',path)
      if m:
        ch=c.execute('SELECT * FROM channels WHERE id=?',(int(m.group(1)),)).fetchone()
        if not ch:return out(self,404,{'error':'not found'})
        if m.group(2)=='/members': return out(self,200,{'members':[uobj(r) for r in c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],))]})
        rm=re.match(r'^/messages/(\d+)/replies$',m.group(2))
        if rm:
          parent=c.execute('SELECT * FROM messages WHERE id=? AND channel_id=? AND deleted=0',(int(rm.group(1)),ch['id'])).fetchone()
          if not parent:return out(self,404,{'error':'not found'})
          lim=q.get('limit',['50'])[0]
          try: lim=int(lim)
          except:return out(self,400,{'error':'limit'})
          if lim<1 or lim>200:return out(self,400,{'error':'limit'})
          boundary=cursor_decode(q.get('cursor',[''])[0])
          if boundary is None:return out(self,400,{'error':'cursor'})
          if boundary:
            rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id ASC LIMIT ?',(parent['id'],boundary,lim)).fetchall()
          else:
            rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC LIMIT ?',(parent['id'],lim)).fetchall()
          nxt=cursor_encode(rows[-1]['id']) if len(rows)==lim else None
          return out(self,200,{'replies':[msgobj(r) for r in rows],'next_cursor':nxt})
        if m.group(2)=='/pins':
          if ch['is_private'] and not member(x['id'],ch['id']): return out(self,403,{'error':'forbidden'})
          lim=q.get('limit',['50'])[0]
          try: lim=int(lim)
          except: return out(self,400,{'error':'limit'})
          if lim<1 or lim>200:return out(self,400,{'error':'limit'})
          boundary=cursor_decode(q.get('cursor',[''])[0])
          if boundary is None:return out(self,400,{'error':'cursor'})
          if boundary:
            ps=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND p.message_id<? ORDER BY p.message_id DESC LIMIT ?',(ch['id'],boundary,lim)).fetchall()
          else:
            ps=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.message_id DESC LIMIT ?',(ch['id'],lim)).fetchall()
          nxt=cursor_encode(ps[-1]['message_id']) if ps and c.execute('SELECT 1 FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND p.message_id<? LIMIT 1',(ch['id'],ps[-1]['message_id'])).fetchone() else None
          return out(self,200,{'pins':[{'message':msgobj(r),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in ps],'next_cursor':nxt})
        if m.group(2)=='/read':
          if ch['is_private'] and not member(x['id'],ch['id']): return out(self,403,{'error':'forbidden'})
          return out(self,200,{'read_state':readobj(x['id'],ch['id'])})
        if (ch['is_private'] or ch['is_dm']) and not member(x['id'],ch['id']): return out(self,403,{'error':'forbidden'})
        if m.group(2).startswith('/messages'):
          lim=q.get('limit',['50'])[0]
          try: lim=int(lim)
          except: return out(self,400,{'error':'limit'})
          if lim<1 or lim>200:return out(self,400,{'error':'limit'})
          boundary=cursor_decode(q.get('cursor',[''])[0])
          if boundary is None:return out(self,400,{'error':'cursor'})
          if boundary:
            rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(ch['id'],boundary,lim)).fetchall()
          else:
            rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(ch['id'],lim)).fetchall()
          nxt=cursor_encode(rows[-1]['id']) if len(rows)==lim else None
          return out(self,200,{'messages':[msgobj(r) for r in rows],'next_cursor':nxt})
      if path=='/api/search':
        term=q.get('q',[''])[0].lower(); lim=q.get('limit',['50'])[0]
        try: lim=int(lim)
        except:return out(self,400,{'error':'limit'})
        if lim<1 or lim>200:return out(self,400,{'error':'limit'})
        boundary=cursor_decode(q.get('cursor',[''])[0])
        if boundary is None:return out(self,400,{'error':'cursor'})
        sql='SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id JOIN members wm ON wm.workspace_id=c.workspace_id AND wm.user_id=? WHERE m.deleted=0 AND lower(m.body) LIKE ? AND (c.is_private=0 OR EXISTS (SELECT 1 FROM cmembers pcm WHERE pcm.channel_id=c.id AND pcm.user_id=?))'
        args=[x['id'],f'%{term}%',x['id']]
        wid=q.get('workspace_id',[None])[0]
        if wid is not None:
          try: wid=int(wid)
          except:return out(self,400,{'error':'workspace_id'})
          sql+=' AND c.workspace_id=?'; args.append(wid)
        if boundary:
          sql+=' AND m.id<?'; args.append(boundary)
        sql+=' ORDER BY m.id DESC LIMIT ?'; args.append(lim)
        rows=c.execute(sql,args).fetchall()
        more=False
        if rows:
          chk='SELECT 1 FROM messages m JOIN channels c ON c.id=m.channel_id JOIN members wm ON wm.workspace_id=c.workspace_id AND wm.user_id=? WHERE m.deleted=0 AND m.id<? AND lower(m.body) LIKE ? AND (c.is_private=0 OR EXISTS (SELECT 1 FROM cmembers pcm WHERE pcm.channel_id=c.id AND pcm.user_id=?))'
          ca=[x['id'],rows[-1]['id'],f'%{term}%',x['id']]
          if wid is not None: chk+=' AND c.workspace_id=?'; ca.append(wid)
          more=bool(c.execute(chk+' LIMIT 1',ca).fetchone())
        nxt=cursor_encode(rows[-1]['id']) if more else None
        return out(self,200,{'results':[msgobj(r) for r in rows],'next_cursor':nxt})
      fm=re.match(r'^/api/files/(\d+)(/download)?$',path)
      if fm:
        z=c.execute('SELECT * FROM files WHERE id=?',(int(fm.group(1)),)).fetchone()
        if not z:return out(self,404,{'error':'not found'})
        if fm.group(2):
          self.send_response(200); self.send_header('Content-Type',z['content_type']); self.send_header('Content-Disposition','attachment; filename="'+z['filename']+'"'); self.end_headers(); self.wfile.write(z['data']); return
        return out(self,200,{'file':fileobj(z)})
      gm=re.match(r'^/api/workspaces/([^/]+)/groups(?:/([^/]+))?$',path)
      if gm:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        if not role(x['id'],w['id']):return out(self,403,{'error':'forbidden'})
        rows=c.execute('SELECT * FROM groups_ WHERE workspace_id=?'+(' AND handle=?' if gm.group(2) else ''),((w['id'],gm.group(2)) if gm.group(2) else (w['id'],))).fetchall()
        gs=[]
        for g0 in rows:
          ids=[r['user_id'] for r in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g0['id'],))]
          gs.append({'id':g0['id'],'handle':g0['handle'],'name':g0['name'],'member_user_ids':ids})
        return out(self,200,{'groups':gs}) if not gm.group(2) else (out(self,200,{'group':gs[0]}) if gs else out(self,404,{'error':'not found'}))
      if path.startswith('/api/users/'):
        uid=x['id'] if path.endswith('/me') else int(path.rsplit('/',1)[1]); z=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        return out(self,200,{'user':uobj(z)}) if z else out(self,404,{'error':'not found'})
      return out(self,404,{'error':'not found'})
    def do_POST(self):
      p=urllib.parse.urlparse(self.path); path=p.path
      if path=='/api/auth/register':
        d=parse_json(self)
        if not re.fullmatch(r'[A-Za-z0-9_]+',str(d.get('username',''))) or len(d.get('password',''))<8:return out(self,400,{'error':'invalid'})
        try:
          with db_lock:
            c=db(); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],d['password'],d.get('display_name',d['username']))); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); z=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
          return out(self,201,{'user':uobj(z),'token':t})
        except sqlite3.IntegrityError:return out(self,409,{'error':'duplicate'})
      if path=='/api/auth/login':
        d=parse_json(self); c=db(); z=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
        if not z:return out(self,401,{'error':'invalid'})
        t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,z['id'])); c.commit(); return out(self,200,{'user':uobj(z),'token':t})
      x=auth(self)
      if not x:return
      d={} if path=='/api/files' else parse_json(self); c=db()
      gm=re.match(r'^/api/workspaces/([^/]+)/groups$',path)
      if gm:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        if not role(x['id'],w['id']):return out(self,403,{'error':'forbidden'})
        handle=str(d.get('handle',''))
        if not re.fullmatch(r'[a-z0-9_-]{2,32}',handle) or handle.lower()=='here':return out(self,400,{'error':'handle'})
        if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone():return out(self,409,{'error':'duplicate'})
        c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,d.get('name',''),x['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        for uid in d.get('user_ids',[]): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,int(uid)))
        c.commit(); return out(self,201,{'group':{'id':gid,'handle':handle,'name':d.get('name',''),'member_user_ids':[int(i) for i in d.get('user_ids',[])]}})
      if path=='/api/dms':
        target=int(d.get('user_id',0))
        if target==x['id']:return out(self,400,{'error':'self'})
        if not c.execute('SELECT id FROM users WHERE id=?',(target,)).fetchone():return out(self,404,{'error':'user'})
        ids=sorted([x['id'],target]); name='dm-'+str(ids[0])+'-'+str(ids[1])
        z=c.execute('SELECT * FROM channels WHERE name=? AND is_dm=1',(name,)).fetchone()
        if not z:
          c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,name)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
          c.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(cid,ids[0],cid,ids[1])); c.commit()
        else: cid=z['id']
        return out(self,200,{'channel_id':cid})
      if path=='/api/files':
        if not self.headers.get('Content-Type','').lower().startswith('multipart/form-data'):return out(self,400,{'error':'file'})
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        ct=self.headers.get('Content-Type'); boundary=ct.split('boundary=',1)[1].strip().strip('"').encode()
        parts=raw.split(b'--'+boundary)
        found=None
        for part in parts:
          if b'filename=' not in part: continue
          head,body=part.split(b'\r\n\r\n',1); body=body.rsplit(b'\r\n',1)[0]
          mm=re.search(br'filename="([^"]*)"',head); typ=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
          found=(mm.group(1).decode(errors='replace') if mm else 'file',typ.group(1).decode() if typ else 'application/octet-stream',body); break
        if not found:return out(self,400,{'error':'file'})
        fn,typ,data=found
        if len(data)>10485760:return out(self,413,{'error':'too large'})
        ts=now(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(x['id'],fn,typ,len(data),ts,data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit()
        return out(self,201,{'file':{'id':fid,'uploader_id':x['id'],'filename':fn,'content_type':typ,'size':len(data),'created_at':ts}})
      rx=re.match(r'^/api/messages/(\d+)/reactions$',path)
      if rx:
        z=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(int(rx.group(1)),)).fetchone()
        if not z:return out(self,404,{'error':'not found'})
        if not member(x['id'],z['channel_id']):return out(self,403,{'error':'forbidden'})
        emoji=str(d.get('emoji',''))
        if not emoji:return out(self,400,{'error':'emoji'})
        added=c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(z['id'],x['id'],emoji)).rowcount; c.commit()
        if added: event(z['channel_id'],'reaction.added',{'message_id':z['id'],'emoji':emoji,'user_id':x['id'],'reactions':reaction_list(z['id'])})
        return out(self,200,{'reactions':reaction_list(z['id'])})
      im=re.match(r'^/api/workspaces/([^/]+)/invitations$',path)
      if im:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(im.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        if role(x['id'],w['id']) not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
        target=d.get('invited_user_id')
        if not target and d.get('invited_username'):
          z=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone(); target=z['id'] if z else None
        code=secrets.token_urlsafe(16); mx=1 if target else int(d.get('max_uses',1)); exp=int(time.time())+int(d.get('expires_in_seconds',604800))
        c.execute('INSERT INTO invitations(code,workspace_id,invited_user_id,max_uses,expires) VALUES(?,?,?,?,?)',(code,w['id'],target,mx,exp)); c.commit()
        return out(self,201,{'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'max_uses':mx,'uses':0,'expires':exp}})
      am=re.match(r'^/api/invitations/([^/]+)/accept$',path)
      if am:
        inv=c.execute('SELECT * FROM invitations WHERE code=? AND revoked=0',(am.group(1),)).fetchone()
        if not inv or inv['uses']>=inv['max_uses'] or (inv['expires'] and inv['expires']<int(time.time())):return out(self,404,{'error':'not found'})
        if inv['invited_user_id'] and inv['invited_user_id']!=x['id']:return out(self,403,{'error':'targeted'})
        c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],x['id'],'member')); c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(inv['code'],)); c.commit()
        return out(self,200,{'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],)).fetchone())})
      
      pm=re.match(r'^/api/messages/(\d+)/pin$',path)
      if pm:
        z=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(int(pm.group(1)),)).fetchone()
        if not z:return out(self,404,{'error':'not found'})
        if not member(x['id'],z['channel_id']):return out(self,403,{'error':'forbidden'})
        if z['is_private'] and not member(x['id'],z['channel_id']):return out(self,403,{'error':'forbidden'})
        r=c.execute('SELECT * FROM pins WHERE message_id=?',(z['id'],)).fetchone()
        if not r:
          ts=now(); c.execute('INSERT INTO pins VALUES(?,?,?)',(z['id'],x['id'],ts)); c.commit(); r=c.execute('SELECT * FROM pins WHERE message_id=?',(z['id'],)).fetchone()
        return out(self,200,{'pin':{'message_id':r['message_id'],'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']}})
      tm=re.match(r'^/api/workspaces/([^/]+)/transfer_ownership$',path)
      if tm:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(tm.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        if x['id']!=w['owner_id']:return out(self,403,{'error':'forbidden'})
        target=int(d.get('new_owner_id',0)); r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
        if not r or r['role']!='admin':return out(self,400,{'error':'target must be admin'})
        c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],w['owner_id']))
        c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target))
        c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id'])); c.commit()
        return out(self,200,{'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
      if path=='/api/workspaces':
        if not re.fullmatch(r'[a-z0-9-]{2,32}',str(d.get('slug',''))):return out(self,400,{'error':'slug'})
        try:
          c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),x['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?,?)',(wid,x['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,x['id'])); c.commit(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); return out(self,201,{'workspace':wsobj(w),'general_channel':chobj(ch)})
        except sqlite3.IntegrityError:return out(self,409,{'error':'duplicate'})
      m=re.match(r'^/api/workspaces/([^/]+)/channels$',path)
      if m:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); r=role(x['id'],w['id']) if w else None
        if not w:return out(self,404,{'error':'not found'})
        if r not in ('owner','admin','member') or (d.get('is_private') and r not in ('owner','admin')):return out(self,403,{'error':'forbidden'})
        if not re.fullmatch(r'[a-z0-9-]{1,32}',str(d.get('name',''))) or len(d.get('topic',''))>250:return out(self,400,{'error':'invalid'})
        if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],d['name'])).fetchone():return out(self,409,{'error':'duplicate'})
        try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,x['id'])); c.commit(); return out(self,201,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
        except sqlite3.IntegrityError:return out(self,409,{'error':'duplicate'})
      m=re.match(r'^/api/channels/(\d+)/join$',path)
      if m:
        ch=c.execute('SELECT * FROM channels WHERE id=?',(int(m.group(1)),)).fetchone()
        if not ch:return out(self,404,{'error':'not found'})
        r=role(x['id'],ch['workspace_id'])
        wmode=c.execute('SELECT join_mode FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
        if r=='guest' or (ch['is_private'] and not member(x['id'],ch['id'])) or (not r and wmode and wmode['join_mode']=='invite_only'):return out(self,403,{'error':'forbidden'})
        if not r:c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],x['id'],'member'))
        existed=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],x['id'])).fetchone()
        c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],x['id'])); c.commit()
        if not existed: event(ch['id'],'member.joined',{'user_id':x['id']})
        return out(self,200,{'channel':chobj(ch)})
      am=re.match(r'^/api/channels/(\d+)/(leave|archive|unarchive)$',path)
      if am:
        ch=c.execute('SELECT * FROM channels WHERE id=?',(int(am.group(1)),)).fetchone()
        if not ch:return out(self,404,{'error':'not found'})
        act=am.group(2)
        if act=='leave':
          was=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],x['id'])).fetchone()
          c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],x['id'])); c.commit()
          if was: event(ch['id'],'member.left',{'user_id':x['id']})
          return out(self,200,{'channel':chobj(ch)})
        w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
        if x['id']!=w['owner_id']:return out(self,403,{'error':'forbidden'})
        c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if act=='archive' else 0,ch['id'])); c.commit()
        event(ch['id'],'channel.archived' if act=='archive' else 'channel.unarchived',{})
        return out(self,200,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
      rm=re.match(r'^/api/channels/(\d+)/read$',path)
      if rm:
        ch=c.execute('SELECT * FROM channels WHERE id=?',(int(rm.group(1)),)).fetchone()
        if not ch:return out(self,404,{'error':'not found'})
        if not member(x['id'],ch['id']):return out(self,403,{'error':'forbidden'})
        try: requested=int(d.get('last_read_event_id',0))
        except:return out(self,400,{'error':'cursor'})
        cur=c.execute('SELECT last_read FROM reads WHERE channel_id=? AND user_id=?',(ch['id'],x['id'])).fetchone()
        oldread=int(cur['last_read']) if cur else 0
        val=max(oldread,requested)
        updated=c.execute('UPDATE reads SET last_read=? WHERE channel_id=? AND user_id=?',(val,ch['id'],x['id'])).rowcount
        if not updated: c.execute('INSERT INTO reads(channel_id,user_id,last_read) VALUES(?,?,?)',(ch['id'],x['id'],val))
        c.commit()
        return out(self,200,{'read_state':readobj(x['id'],ch['id'])})
      m=re.match(r'^/api/channels/(\d+)/messages$',path)
      if m:
        ch=c.execute('SELECT * FROM channels WHERE id=?',(int(m.group(1)),)).fetchone()
        if not ch:return out(self,404,{'error':'not found'})
        if not member(x['id'],ch['id']):return out(self,403,{'error':'forbidden'})
        body=str(d.get('body',''))
        if ch['is_archived'] and not body.startswith('/unarchive'):return out(self,423,{'error':'archived'})
        if not body:return out(self,400,{'error':'body'})
        if body.startswith('/') and not body.startswith('//'):
          cmd,_,arg=body.partition(' '); arg=arg.strip()
          if cmd=='/me':
            if not arg:return out(self,400,{'error':'text'})
          elif cmd=='/shrug':
            body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
          elif cmd=='/topic':
            if not arg or len(arg)>250:return out(self,400,{'error':'topic'})
            if not member(x['id'],ch['id']):return out(self,403,{'error':'forbidden'})
            c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id'])); c.commit(); event(ch['id'],'channel.updated',{})
            return out(self,201,{'message':None,'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
          elif cmd in ('/archive','/unarchive'):
            rr=role(x['id'],ch['workspace_id'])
            w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            if x['id']!=w['owner_id']:return out(self,403,{'error':'forbidden'})
            archived=1 if cmd=='/archive' else 0
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,ch['id'])); c.commit()
            event(ch['id'],'channel.archived' if archived else 'channel.unarchived',{})
            return out(self,201,{'message':None,'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
          elif cmd=='/invite':
            if not member(x['id'],ch['id']):return out(self,403,{'error':'forbidden'})
            if not arg or not re.fullmatch(r'@[A-Za-z0-9_]+',arg):return out(self,400,{'error':'user'})
            uname=arg[1:]; target=c.execute('SELECT * FROM users WHERE username=?',(uname,)).fetchone()
            if not target:return out(self,404,{'error':'user'})
            if not role(target['id'],ch['workspace_id']):return out(self,403,{'error':'outside workspace'})
            c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],target['id'],'member')); c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],target['id'])); c.commit()
            return out(self,201,{'message':None,'invited_user':uobj(target)})
          else:return out(self,400,{'error':'unknown command'})

        parent=d.get('parent_id'); fileids=d.get('file_ids',[])
        for fid in fileids:
          fz=c.execute('SELECT * FROM files WHERE id=?',(int(fid),)).fetchone()
          if not fz or fz['uploader_id']!=x['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(int(fid),)).fetchone(): return out(self,400,{'error':'file'})
        c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],x['id'],body,parent,now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; [c.execute('INSERT INTO message_files VALUES(?,?)',(mid,int(fid))) for fid in fileids]; c.commit(); eid=event(ch['id'],'message.reply' if parent else 'message.created',{'message':msgobj(c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())}); c.execute('UPDATE messages SET event_id=? WHERE id=?',(eid,mid)); c.commit(); return out(self,201,{'message':msgobj(c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())})
      return out(self,404,{'error':'not found'})
    def do_PATCH(self):
      x=auth(self)
      if not x:return
      p=urllib.parse.urlparse(self.path); d=parse_json(self); c=db()
      mt=re.match(r'^/api/workspaces/([^/]+)/members/(\d+)$',p.path)
      if mt:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(mt.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        actor=role(x['id'],w['id']); target=int(mt.group(2)); old=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
        if not old:return out(self,404,{'error':'not found'})
        new=d.get('role')
        if new not in ('owner','admin','member','guest'):return out(self,400,{'error':'role'})
        if new=='owner':return out(self,400,{'error':'use transfer'})
        if target==w['owner_id'] or (target==x['id'] and old['role']=='owner'):return out(self,403,{'error':'forbidden'})
        if actor not in ('owner','admin') or (actor=='admin' and old['role'] in ('owner','admin')):return out(self,403,{'error':'forbidden'})
        c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],target)); c.commit()
        return out(self,200,{'member':{'user_id':target,'role':new}})
      wm=re.match(r'^/api/workspaces/([^/]+)$',p.path)
      if wm:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(wm.group(1),)).fetchone()
        if not w:return out(self,404,{'error':'not found'})
        if role(x['id'],w['id']) not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
        if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return out(self,400,{'error':'join_mode'})
        c.execute('UPDATE workspaces SET name=?,join_mode=? WHERE id=?',(d.get('name',w['name']),d.get('join_mode',w['join_mode']),w['id'])); c.commit()
        return out(self,200,{'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
      gm=re.match(r'^/api/workspaces/([^/]+)/groups/([^/]+)$',p.path)
      if gm:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone(); gr=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],gm.group(2))) .fetchone() if w else None
        if not w or not gr:return out(self,404,{'error':'not found'})
        if x['id']!=w['owner_id'] and x['id']!=gr['creator_id']:return out(self,403,{'error':'forbidden'})
        c.execute('UPDATE groups_ SET name=? WHERE id=?',(d.get('name',gr['name']),gr['id']))
        if 'user_ids' in d:
          c.execute('DELETE FROM group_members WHERE group_id=?',(gr['id'],))
          for uid in d['user_ids']: c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gr['id'],int(uid)))
        c.commit(); ids=[r['user_id'] for r in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gr['id'],))]
        return out(self,200,{'group':{'id':gr['id'],'handle':gr['handle'],'name':d.get('name',gr['name']),'member_user_ids':ids}})
      m=re.match(r'^/api/channels/(\d+)$',p.path)
      if m:
        ch=c.execute('SELECT * FROM channels WHERE id=?',(int(m.group(1)),)).fetchone()
        if not ch:return out(self,404,{'error':'not found'})
        if not member(x['id'],ch['id']) and role(x['id'],ch['workspace_id'])!='owner':return out(self,403,{'error':'forbidden'})
        if ch['is_archived']:return out(self,423,{'error':'archived'})
        if len(d.get('topic',''))>250:return out(self,400,{'error':'topic'})
        c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',ch['topic']),ch['id'])); c.commit(); event(ch['id'],'channel.updated',{})
        return out(self,200,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
      im=re.match(r'^/api/workspaces/([^/]+)/invitations/([^/]+)$',self.path)
      if im:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(im.group(1),)).fetchone(); inv=c.execute('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(im.group(2),w['id'] if w else 0)).fetchone()
        if not w or not inv:return out(self,404,{'error':'not found'})
        if role(x['id'],w['id']) not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
        c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(inv['code'],)); c.commit(); return out(self,200,{'deleted':True})
      
      m=re.match(r'^/api/messages/(\d+)$',p.path)
      if m:
        z=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
        if not z:return out(self,404,{'error':'not found'})
        if z['author_id']!=x['id']:return out(self,403,{'error':'forbidden'})
        c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d.get('body',''),now(),z['id'])); c.commit(); return out(self,200,{'message':msgobj(c.execute('SELECT * FROM messages WHERE id=?',(z['id'],)).fetchone())})
      return out(self,404,{'error':'not found'})
    def do_DELETE(self):
      x=auth(self)
      if not x:return
      rx=re.match(r'^/api/messages/(\d+)/reactions/([^/]+)$',self.path); c=db()
      if rx:
        z=c.execute('SELECT m.* FROM messages m WHERE m.id=? AND m.deleted=0',(int(rx.group(1)),)).fetchone()
        if not z:return out(self,404,{'error':'not found'})
        if not member(x['id'],z['channel_id']):return out(self,403,{'error':'forbidden'})
        emoji=urllib.parse.unquote(rx.group(2)); existed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(z['id'],x['id'],emoji)).fetchone()
        if existed:
          c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(z['id'],x['id'],emoji)); c.commit()
          event(z['channel_id'],'reaction.removed',{'message_id':z['id'],'emoji':emoji,'user_id':x['id'],'reactions':reaction_list(z['id'])})
        return out(self,200,{'reactions':reaction_list(z['id'])})
      pm=re.match(r'^/api/messages/(\d+)/pin$',self.path); c=db()
      if pm:
        z=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(int(pm.group(1)),)).fetchone()
        if not z:return out(self,404,{'error':'not found'})
        if not member(x['id'],z['channel_id']):return out(self,403,{'error':'forbidden'})
        c.execute('DELETE FROM pins WHERE message_id=?',(z['id'],)); c.commit(); return out(self,200,{'unpinned':True})
      im=re.match(r'^/api/workspaces/([^/]+)/invitations/([^/]+)$',self.path); c=db()
      if im:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(im.group(1),)).fetchone()
        inv=c.execute('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(im.group(2),w['id'] if w else 0)).fetchone()
        if not w or not inv:return out(self,404,{'error':'not found'})
        if role(x['id'],w['id']) not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
        c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(inv['code'],)); c.commit(); return out(self,200,{'deleted':True})
      gm=re.match(r'^/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
      if gm:
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone()
        gr=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],gm.group(2))).fetchone() if w else None
        if not w:return out(self,404,{'error':'not found'})
        if not gr:return out(self,200,{'deleted':True})
        if x['id']!=w['owner_id'] and x['id']!=gr['creator_id']:return out(self,403,{'error':'forbidden'})
        c.execute('DELETE FROM group_members WHERE group_id=?',(gr['id'],)); c.execute('DELETE FROM groups_ WHERE id=?',(gr['id'],)); c.commit()
        return out(self,200,{'deleted':True})
      m=re.match(r'^/api/messages/(\d+)$',self.path); c=db()
      if m:
        z=c.execute('SELECT * FROM messages WHERE id=?',(int(m.group(1)),)).fetchone()
        if not z:return out(self,404,{'error':'not found'})
        if z['author_id']!=x['id']:return out(self,403,{'error':'forbidden'})
        c.execute('UPDATE messages SET deleted=1 WHERE id=?',(z['id'],)); c.commit(); return out(self,200,{'deleted':True})
      return out(self,404,{'error':'not found'})
HTML='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>body{font-family:Arial;margin:0;background:#f8f8f8;color:#222}button{border:0;padding:10px 15px;border-radius:4px}.primary{background:#007a5a;color:#fff}.danger{background:#e01e5a;color:#fff}.secondary{background:#1264a3;color:#fff}#app{padding:30px}</style></head><body><div id="app"><form data-testid="auth-form" id="auth-form"><h1>Huddle</h1><input name="username" placeholder="username"><input name="password" type="password" placeholder="password"><button data-testid="auth-submit" data-button-role="primary">Sign up / Log in</button><div id="error"></div></form></div><script>const f=document.querySelector('form');f.onsubmit=async e=>{e.preventDefault();let d=Object.fromEntries(new FormData(f));let r=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(r.status==409)r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});let j=await r.json();if(!r.ok)return error.textContent=j.error||'Invalid input';localStorage.setItem('huddle.token',j.token);app.innerHTML='<h1 data-testid="workspace-header">Huddle</h1><div data-testid="channel-list"></div>'}</script></body></html>'''
def ws_frame(data):
    data=data if isinstance(data,bytes) else data.encode()
    n=len(data)
    if n<126:return b"\\x81"+bytes([n])+data
    if n<65536:return b"\\x81\\x7e"+struct.pack("!H",n)+data
    return b"\\x81\\x7f"+struct.pack("!Q",n)+data
def ws_loop(h):
    q=urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query); token=q.get("token",[""])[0]
    c=db(); z=c.execute("SELECT user_id FROM tokens WHERE token=?",(token,)).fetchone(); c.close()
    if not z:
        h.send_response(401); h.end_headers(); return
    key=h.headers.get("Sec-WebSocket-Key",""); accept=base64.b64encode(hashlib.sha1((key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    h.send_response(101); h.send_header("Upgrade","websocket"); h.send_header("Connection","Upgrade"); h.send_header("Sec-WebSocket-Accept",accept); h.end_headers()
    subs=[]; stop=threading.Event(); lock=threading.Lock()
    def send(obj):
        data=json.dumps(obj).encode(); n=len(data)
        if n<126: frame=bytes([0x81,n])+data
        elif n<65536: frame=bytes([0x81,126])+struct.pack("!H",n)+data
        else: frame=bytes([0x81,127])+struct.pack("!Q",n)+data
        with lock: h.wfile.write(frame); h.wfile.flush()
    def pump():
        while not stop.is_set():
            for sub in list(subs):
                cid,queue,last=sub
                c=db(); rows=c.execute("SELECT body,event_id FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,last)).fetchall(); c.close()
                for r in rows:
                    queue.append(json.loads(r["body"])); sub[2]=r["event_id"]
                while queue:
                    try: send(queue.pop(0))
                    except Exception: stop.set(); return
            time.sleep(.02)
    threading.Thread(target=pump,daemon=True).start()
    try:
      while not stop.is_set():
        first=h.rfile.read(2)
        if not first: break
        op=first[0]&15; ln=first[1]&127
        if op==8: break
        if ln==126: ln=struct.unpack("!H",h.rfile.read(2))[0]
        elif ln==127: ln=struct.unpack("!Q",h.rfile.read(8))[0]
        mask=h.rfile.read(4) if first[1]&128 else b""; data=h.rfile.read(ln)
        if mask: data=bytes(data[i]^mask[i%4] for i in range(ln))
        d=json.loads(data.decode()); cid=int(d.get("channel_id",0)); headrow=db().execute("SELECT n FROM seq WHERE channel_id=?",(cid,)).fetchone(); head=headrow["n"] if headrow else 0
        if d.get("type")=="subscribe":
            sub=[cid,[],head,z["user_id"]]; subs.append(sub); clients.append(sub); pc=db(); pc.execute("INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)",(cid,z["user_id"],token)); pc.commit(); pc.close(); send({"type":"subscribed","channel_id":cid,"head_event_id":head})
        elif d.get("type")=="resume":
            since=int(d.get("since_event_id",0)); sub=[cid,[],since,z["user_id"]]; subs.append(sub); clients.append(sub); pc=db(); pc.execute("INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)",(cid,z["user_id"],token)); pc.commit(); pc.close()
            c=db(); rows=c.execute("SELECT body,event_id FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall(); c.close()
            for r in rows: sub[1].append(json.loads(r["body"])); sub[2]=r["event_id"]
            while sub[1]: send(sub[1].pop(0))
            send({"type":"resumed","channel_id":cid,"head_event_id":head})
    except (ConnectionError,EOFError,OSError,json.JSONDecodeError): pass
    finally:
      stop.set()
      pc=db()
      for sub in subs:
        try: clients.remove(sub)
        except ValueError: pass
        pc.execute('DELETE FROM ws_presence WHERE channel_id=? AND user_id=? AND token=?',(sub[0],z['user_id'],token))
      pc.commit(); pc.close()
def run():
    srv=ThreadingHTTPServer(('127.0.0.1',8000+NODE),H)
    old=H.do_GET
    def get(self):
      if self.path.startswith('/api/ws') and self.headers.get('Upgrade','').lower()=='websocket': return ws_loop(self)
      return old(self)
    H.do_GET=get; srv.serve_forever()
if __name__=='__main__': run()
