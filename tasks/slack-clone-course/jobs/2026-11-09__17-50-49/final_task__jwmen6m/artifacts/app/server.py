import os, sys, re, json, uuid, sqlite3, hashlib, secrets, threading, base64, struct, socket, time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

DB='/var/lib/huddle/huddle.db'
LOCK=threading.RLock()
SUBS=[]
SUBLOCK=threading.RLock()
def notify_nodes(channel_id):
    try:
        x=socket.create_connection(('127.0.0.1',6379),timeout=.15)
        b=str(channel_id).encode(); x.sendall(b'*3\r\n$7\r\nPUBLISH\r\n$13\r\nhuddle:events\r\n$'+str(len(b)).encode()+b'\r\n'+b+b'\r\n'); x.close()
    except Exception: pass
def redis_listener():
    while True:
        try:
            x=socket.create_connection(('127.0.0.1',6379),timeout=1)
            x.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
            while x.recv(4096): pass
        except Exception: time.sleep(.5)
def start_redis_listener():
    threading.Thread(target=redis_listener,daemon=True).start()
class Conn(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        cur = super().execute(sql, parameters)
        self._last_rowid = cur.lastrowid
        return cur
    @property
    def lastrowid(self):
        return getattr(self, '_last_rowid', None)
    def __enter__(self):
        return self
    def __exit__(self, typ, val, tb):
        self.close()
        return False
def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB, timeout=10, isolation_level=None, factory=Conn); c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000')
    return c
def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
        CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
        CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
        CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
        CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
        CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,users TEXT,UNIQUE(workspace_id,handle));
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
        CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
        CREATE TABLE IF NOT EXISTS presence(conn_id TEXT PRIMARY KEY,user_id INTEGER,last_seen REAL);
        CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created_at TEXT);
        ''')
def userobj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def token_user(t):
    if not t:return None
    with db() as c:
        r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone()
    return r
def wsframe(data):
    if isinstance(data,str): data=data.encode()
    n=len(data)
    if n<126: h=bytes([0x81,n])
    elif n<65536: h=bytes([0x81,126])+struct.pack('!H',n)
    else: h=bytes([0x81,127])+struct.pack('!Q',n)
    return h+data
def broadcast(frame):
    dead=[]
    with SUBLOCK:
        for sock,cid in list(SUBS):
            if cid is None or cid==frame.get('channel_id'):
                try: sock.sendall(wsframe(json.dumps(frame,separators=(',',':'))))
                except Exception: dead.append((sock,cid))
        for x in dead:
            if x in SUBS: SUBS.remove(x)
def emit(cid,kind,payload):
    with LOCK:
        c=db()
        try:
            c.execute('BEGIN IMMEDIATE')
            x=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
            c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,x,kind,json.dumps(payload),now()))
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        notify_nodes(cid)
        return x
def channel(cid):
    with db() as c:return c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
def member(cid,uid):
    with db() as c:return c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone()
def msgobj(r):
    with db() as c:
        au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
        rs=c.execute('SELECT emoji,GROUP_CONCAT(user_id) ids,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(r['id'],)).fetchall()
        replies=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[file_json(x) for x in attached_files(r['id'])],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs],'mentions':[]}
def channel_obj(r):
    d=dict(r)
    for k in ('is_private','is_dm','is_archived'):
        if k in d: d[k]=bool(d[k])
    return d
def workspace_obj(r):
    return {k:r[k] for k in ('id','slug','name','owner_id','join_mode')}
def group_json(r):
    ids=json.loads(r['users'] or '[]')
    return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':ids}
def valid_group_handle(h):
    return bool(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',h or '')) and h not in ('here','channel')
def file_json(r):
    return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def file_row(fid):
    with db() as c:return c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
def attached_files(mid):
    with db() as c:return c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(mid,)).fetchall()
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a):pass
    def sendj(self,code,obj,headers=None):
        b=json.dumps(obj).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def auth(self):
        x=self.headers.get('Authorization','')
        return token_user(x[7:] if x.startswith('Bearer ') else '')
    def err(self,c,m):self.sendj(c,{'error':m})
    def do_GET(self):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
        if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
        if path=='/': return self.html()
        u=self.auth()
        if path=='/api/auth/me':
            return self.sendj(200,{'user':userobj(u)}) if u else self.err(401,'unauthorized')
        m=re.match(r'^/api/users/(\d+)$',path)
        if m:
            if not u:return self.err(401,'unauthorized')
            with db() as c:r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone()
            return self.sendj(200,{'user':userobj(r)}) if r else self.err(404,'not found')
        if path=='/api/workspaces':
            if not u:return self.err(401,'unauthorized')
            with db() as c:
                rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.id',(u['id'],)).fetchall()
            return self.sendj(200,{'workspaces':[workspace_obj(x) for x in rs]})
        m=re.match(r'^/api/workspaces/([^/]+)/members$',path)
        if m:
            if not u:return self.err(401,'unauthorized')
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                if not w:return self.err(404,'not found')
                rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=? ORDER BY user_id',(w['id'],)).fetchall()
            return self.sendj(200,{'members':[{'user_id':x['user_id'],'role':x['role']} for x in rs]})
        m=re.match(r'^/api/workspaces/([^/]+)/invitations$',path)
        if m:
            if not u:return self.err(401,'unauthorized')
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
                if not w:return self.err(404,'not found')
                if not role or role['role'] not in ('owner','admin'):return self.err(403,'forbidden')
                rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses AND (expires_at IS NULL OR CAST(expires_at AS REAL)>?)',(w['id'],time.time())).fetchall()
            return self.sendj(200,{'invitations':[dict(x) for x in rs]})
        m=re.match(r'^/api/workspaces/([^/]+)/groups$',path)
        if m:
            if not u:return self.err(401,'unauthorized')
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                if not w:return self.err(404,'not found')
                if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.err(403,'forbidden')
                rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall()
            return self.sendj(200,{'groups':[group_json(x) for x in rs]})
        m=re.match(r'^/api/workspaces/([^/]+)$',path)
        if m and u:
            with db() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return self.err(404,'not found')
            with db() as c:
                role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not role:return self.err(403,'forbidden')
                cs=c.execute('SELECT c.* FROM channels c WHERE c.workspace_id=? AND (c.is_private=0 OR EXISTS(SELECT 1 FROM channel_members cm WHERE cm.channel_id=c.id AND cm.user_id=?)) AND (c.is_archived=0 OR ?)',(w['id'],u['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
                states=[]
                for ch in cs:
                    rr=c.execute('SELECT COALESCE(last_read_event_id,0) n FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
                    last=rr['n'] if rr else 0
                    evs=c.execute("SELECT kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(ch['id'],last)).fetchall()
                    total=mentions=0
                    uname=c.execute('SELECT username FROM users WHERE id=?',(u['id'],)).fetchone()['username']
                    for ev in evs:
                        if ev['kind'] not in ('message.created','message.reply'): continue
                        mm=json.loads(ev['payload']).get('message',{})
                        if mm.get('author_id')!=u['id']:
                            total+=1
                            body=mm.get('body','')
                            if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(uname)+r'\b',body,re.I) or '@channel' in body: mentions+=1
                    states.append({'channel_id':ch['id'],'last_read_event_id':last,'unread_count':total,'mention_count':mentions})
            return self.sendj(200,{'workspace':workspace_obj(w),'channels':[channel_obj(x) for x in cs],'read_state':states})
        m=re.match(r'^/api/channels/(\d+)/members$',path)
        if m:
            if not u:return self.err(401,'unauthorized')
            cid=int(m.group(1))
            with db() as c:
                ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                if not ch:return self.err(404,'not found')
                if ch['is_private'] and not member(cid,u['id']):return self.err(403,'forbidden')
                rs=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall()
            return self.sendj(200,{'members':[userobj(x) for x in rs]})
        m=re.match(r'^/api/channels/(\d+)/read$',path)
        if m:
            if not u:return self.err(401,'unauthorized')
            cid=int(m.group(1))
            if not member(cid,u['id']):return self.err(403,'forbidden')
            with db() as c:
                x=c.execute('SELECT COALESCE(last_read_event_id,0) n FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
                last=x['n'] if x else 0
                evs=c.execute('SELECT kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
            unread=mentions=0
            with db() as c: me=c.execute('SELECT username FROM users WHERE id=?',(u['id'],)).fetchone()['username']
            for ev in evs:
                if ev['kind'] not in ('message.created','message.reply'): continue
                mm=json.loads(ev['payload']).get('message',{})
                if mm.get('author_id')!=u['id']:
                    unread+=1
                    body=mm.get('body','')
                    if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(me)+r'\b',body,re.I) or '@channel' in body: mentions+=1
            return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
        def pageargs():
            raw=q.get('limit',['50'])[0]
            try: lim=int(raw)
            except Exception: raise ValueError()
            if lim<1 or lim>200:return None,None
            cur=q.get('cursor',[None])[0]
            if cur is not None:
                try:
                    if not cur.startswith('c:'):raise ValueError()
                    pos=int(cur[2:])
                except Exception: raise ValueError()
            else: pos=None
            return lim,pos
        m=re.match(r'^/api/channels/(\d+)/messages$',path)
        if m and u:
            try: lim,pos=pageargs()
            except Exception:return self.err(400,'invalid pagination')
            if lim is None:return self.err(400,'invalid limit')
            cid=int(m.group(1))
            if not member(cid,u['id']):return self.err(403,'forbidden')
            sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL'
            args=[cid]
            if pos is not None:sql+=' AND id<?';args.append(pos)
            sql+=' ORDER BY id DESC LIMIT ?';args.append(lim+1)
            with db() as c:rs=c.execute(sql,args).fetchall()
            nxt='c:'+str(rs[lim-1]['id']) if len(rs)>lim else None
            rs=rs[:lim]
            return self.sendj(200,{'messages':[msgobj(x) for x in rs],'next_cursor':nxt})
        m=re.match(r'^/api/messages/(\d+)/replies$',path)
        if m and u:
            try: lim,pos=pageargs()
            except Exception:return self.err(400,'invalid pagination')
            if lim is None:return self.err(400,'invalid limit')
            with db() as c:
                parent=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
                if not parent:return self.err(404,'not found')
                if not member(parent['channel_id'],u['id']):return self.err(403,'forbidden')
                sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0';args=[m.group(1)]
                if pos is not None:sql+=' AND id>?';args.append(pos)
                sql+=' ORDER BY id LIMIT ?';args.append(lim+1)
                rs=c.execute(sql,args).fetchall()
            nxt='c:'+str(rs[lim-1]['id']) if len(rs)>lim else None
            return self.sendj(200,{'replies':[msgobj(x) for x in rs[:lim]],'next_cursor':nxt})
        if path=='/api/search' and not u:return self.err(401,'unauthorized')
        if path=='/api/search' and u:
            try: lim,pos=pageargs()
            except Exception:return self.err(400,'invalid pagination')
            if lim is None:return self.err(400,'invalid limit')
            text=q.get('q',[''])[0]; wsid=q.get('workspace_id',[None])[0]
            sql="SELECT DISTINCT m.* FROM messages m JOIN channels cc ON cc.id=m.channel_id JOIN members wm ON wm.workspace_id=cc.workspace_id AND wm.user_id=? WHERE m.deleted=0 AND m.body LIKE ? AND (cc.is_private=0 OR EXISTS(SELECT 1 FROM channel_members pm WHERE pm.channel_id=m.channel_id AND pm.user_id=?))";args=[u['id'],'%'+text+'%',u['id']]
            if wsid:sql+=' AND cc.workspace_id=?';args.append(wsid)
            if pos is not None:sql+=' AND m.id<?';args.append(pos)
            sql+=' ORDER BY m.id DESC LIMIT ?';args.append(lim+1)
            with db() as c:rs=c.execute(sql,args).fetchall()
            nxt='c:'+str(rs[lim-1]['id']) if len(rs)>lim else None
            return self.sendj(200,{'results':[msgobj(x) for x in rs[:lim]],'next_cursor':nxt})
        m=re.match(r'^/api/channels/(\d+)/pins$',path)
        if m and not u:return self.err(401,'unauthorized')
        if m and u:
            try: lim,pos=pageargs()
            except Exception:return self.err(400,'invalid pagination')
            if lim is None:return self.err(400,'invalid limit')
            cid=int(m.group(1))
            if not member(cid,u['id']):return self.err(403,'forbidden')
            sql='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?';args=[cid]
            if pos is not None:sql+=' AND p.message_id<?';args.append(pos)
            sql+=' ORDER BY p.pinned_at DESC LIMIT ?';args.append(lim+1)
            with db() as c:rs=c.execute(sql,args).fetchall()
            nxt='c:'+str(rs[lim-1]['message_id']) if len(rs)>lim else None
            return self.sendj(200,{'pins':[{'message':msgobj(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rs[:lim]],'next_cursor':nxt})
        m=re.match(r'^/api/files/(\d+)(/download)?$',path)
        if m:
            if not u:return self.err(401,'unauthorized')
            r=file_row(m.group(1))
            if not r:return self.err(404,'not found')
            if m.group(2):
                data=r['data'];self.send_response(200);self.send_header('Content-Type',r['content_type']);self.send_header('Content-Disposition','attachment; filename="%s"'%r['filename']);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            return self.sendj(200,{'file':file_json(r)})
        if path=='/api/ws':
            tok=q.get('token',[''])[0]
            if tok.startswith('Bearer '): tok=tok[7:]
            if not token_user(tok): return self.err(401,'unauthorized')
            key=self.headers.get('Sec-WebSocket-Key','')
            if not key:return self.err(400,'websocket required')
            import hashlib as _h
            accept=base64.b64encode(_h.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
            sock=self.connection; sock.settimeout(0.2); cid=None; sent=0; wsuid=token_user(tok)['id']
            with db() as c: gsent=c.execute('SELECT COALESCE(MAX(id),0) n FROM global_events').fetchone()['n']
            with SUBLOCK: SUBS.append((sock,cid,wsuid))
            conn_id=secrets.token_hex(12)
            with db() as pc: pc.execute('INSERT INTO presence VALUES(?,?,?)',(conn_id,wsuid,time.time()))
            try:
                while True:
                    if cid is not None:
                        with db() as c:
                            evs=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,sent)).fetchall()
                        for ev in evs:
                            fr={'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid}; fr.update(json.loads(ev['payload']))
                            sock.sendall(wsframe(json.dumps(fr,separators=(',',':')))); sent=ev['event_id']
                    with db() as c: gevs=c.execute('SELECT id,kind,payload FROM global_events WHERE id>? ORDER BY id',(gsent,)).fetchall()
                    for ev in gevs:
                        fr={'type':ev['kind']}; fr.update(json.loads(ev['payload']))
                        sock.sendall(wsframe(json.dumps(fr,separators=(',',':')))); gsent=ev['id']
                    try:
                        h=sock.recv(2)
                    except socket.timeout:
                        continue
                    if not h:break
                    ln=h[1]&127; masked=h[1]&128
                    if ln==126: ln=struct.unpack('!H',sock.recv(2))[0]
                    elif ln==127: ln=struct.unpack('!Q',sock.recv(8))[0]
                    mask=sock.recv(4) if masked else b''; data=sock.recv(ln)
                    if masked:data=bytes(data[i]^mask[i%4] for i in range(len(data)))
                    if h[0]&15==8:break
                    try:
                        z=json.loads(data.decode())
                        if z.get('type') in ('subscribe','resume'):
                            cid=int(z['channel_id'])
                            with SUBLOCK:
                                for i,x in enumerate(SUBS):
                                    if x[0] is sock: SUBS[i]=(sock,cid,wsuid)
                            with db() as c:
                                head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
                                if z.get('type')=='resume':
                                    since=int(z.get('since_event_id',0))
                                    replay=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
                                else:
                                    replay=[]
                            if z.get('type')=='resume':
                                for ev in replay:
                                    fr={'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid}; fr.update(json.loads(ev['payload']))
                                    sock.sendall(wsframe(json.dumps(fr,separators=(',',':'))))
                                sent=head
                                sock.sendall(wsframe(json.dumps({'type':'resumed','channel_id':cid,'head_event_id':head})))
                            else:
                                sent=head
                                sock.sendall(wsframe(json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':head})))
                    except Exception: pass
            finally:
                with SUBLOCK: SUBS[:]=[x for x in SUBS if x[0] is not sock]
                with db() as pc: pc.execute('DELETE FROM presence WHERE conn_id=?',(conn_id,))
            return
        return self.err(404,'not found')
    def do_POST(self):
        path=urlparse(self.path).path; u=self.auth()
        if path=='/api/auth/register':
            try:
                b=self.body(); un=b.get('username',''); pw=b.get('password','')
                if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: return self.err(400,'invalid')
                with db() as c:
                    c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); uid=c.lastrowid
                    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid))
                with db() as c:r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
                return self.sendj(201,{'user':userobj(r),'token':t})
            except sqlite3.IntegrityError:return self.err(409,'duplicate')
        if path=='/api/auth/login':
            b=self.body()
            with db() as c:r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
            if not r:return self.err(401,'invalid credentials')
            t=secrets.token_urlsafe(24)
            with db() as c:c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']))
            return self.sendj(200,{'user':userobj(r),'token':t})
        if not u:return self.err(401,'unauthorized')
        if path=='/api/dms':
            b=self.body(); other=b.get('user_id')
            if not other or int(other)==u['id']:return self.err(400,'invalid user')
            with db() as c:
                target=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
                if not target:return self.err(404,'not found')
                a,bid=sorted((u['id'],int(other))); ch=c.execute('SELECT id FROM channels WHERE is_dm=1 AND name=?',(f'dm-{a}-{bid}',)).fetchone()
                if not ch:
                    c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)',(f'dm-{a}-{bid}',1));cid=c.lastrowid
                    c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,bid))
                else:cid=ch['id']
            return self.sendj(200,{'channel_id':cid})
        if path=='/api/files':
            ctype=self.headers.get('Content-Type','')
            if 'multipart/form-data' not in ctype:return self.err(400,'file required')
            raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
            mm=re.search(r'boundary="?([^";]+)',ctype)
            if not mm:return self.err(400,'file required')
            boundary=('--'+mm.group(1)).encode()
            part=next((x for x in raw.split(boundary) if b'filename=' in x),None)
            if not part:return self.err(400,'file required')
            head,data=part.split(b'\r\n\r\n',1);data=data[:-2] if data.endswith(b'\r\n') else data
            fm=re.search(br'filename="([^"]*)"',head); cm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
            if not fm:return self.err(400,'file required')
            if len(data)>10485760:return self.err(413,'too large')
            fn=fm.group(1).decode(errors='replace'); ct=(cm.group(1).decode().strip() if cm else 'application/octet-stream'); t=now()
            with db() as c:
                c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,ct,len(data),t,data));fid=c.lastrowid
                r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
            return self.sendj(201,{'file':file_json(r)})
        if path=='/api/workspaces':
            b=self.body(); slug=b.get('slug','')
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'):return self.err(400,'invalid')
            try:
                with db() as c:
                    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],u['id']));wid=c.lastrowid
                    c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']))
                with db() as c:w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                return self.sendj(201,{'workspace':workspace_obj(w),'general_channel':channel_obj(ch)})
            except sqlite3.IntegrityError:return self.err(409,'duplicate')
        m=re.match(r'^/api/messages/(\d+)/pin$',path)
        if m:
            with db() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            if not member(r['channel_id'],u['id']):return self.err(403,'forbidden')
            t=now()
            with db() as c:c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],t))
            return self.sendj(200,{'pin':{'message_id':r['id'],'pinned_by':u['id'],'pinned_at':t}})
        m=re.match(r'^/api/channels/(\d+)/(archive|unarchive)$',path)
        if m:
            cid=int(m.group(1))
            with db() as c:
                ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch else None
            if not ch:return self.err(404,'not found')
            if w['owner_id']!=u['id']:return self.err(403,'forbidden')
            val=1 if m.group(2)=='archive' else 0
            with db() as c:
                was=ch['is_archived']
                c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            if was!=val: emit(cid,'channel.archived' if val else 'channel.unarchived',{'channel':channel_obj(ch)})
            return self.sendj(200,{'channel':channel_obj(ch)})
        m=re.match(r'^/api/workspaces/([^/]+)/invitations$',path)
        if m:
            b=self.body()
            with db() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return self.err(404,'not found')
            with db() as c:role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not role or role['role'] not in ('owner','admin'):return self.err(403,'forbidden')
            code=secrets.token_urlsafe(12); target=b.get('invited_user_id')
            if b.get('invited_username'):
                with db() as c:
                    tu=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone()
                if not tu:return self.err(404,'user not found')
                target=tu['id']
            exp=None
            if b.get('expires_in_seconds') is not None:
                exp=str(time.time()+int(b['expires_in_seconds']))
            maximum=1 if target is not None else int(b.get('max_uses',1))
            with db() as c:c.execute('INSERT INTO invitations(code,workspace_id,invited_user_id,expires_at,max_uses) VALUES(?,?,?,?,?)',(code,w['id'],target,exp,maximum))
            return self.sendj(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':maximum,'invited_user_id':target}})
        m=re.match(r'^/api/workspaces/([^/]+)/transfer_ownership$',path)
        if m:
            b=self.body()
            with db() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return self.err(404,'not found')
            if w['owner_id']!=u['id']:return self.err(403,'forbidden')
            target=b.get('new_owner_id')
            with db() as c:r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
            if not r or r['role']!='admin':return self.err(400,'target must be admin')
            with db() as c:
                c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']))
                c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
                c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target))
            return self.sendj(200,{'workspace':workspace_obj(dict(w)|{'owner_id':target})})
        m=re.match(r'^/api/invitations/([^/]+)/accept$',path)
        if m:
            code=m.group(1)
            with db() as c:inv=c.execute('SELECT * FROM invitations WHERE code=? AND uses<max_uses',(code,)).fetchone()
            if not inv:return self.err(404,'not found')
            if inv['expires_at'] and float(inv['expires_at'])<=time.time():return self.err(404,'expired')
            if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return self.err(403,'forbidden')
            with db() as c:
                c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,))
                c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'))
            return self.sendj(200,{'member':{'user_id':u['id'],'role':'member'}})
        m=re.match(r'^/api/workspaces/([^/]+)/groups$',path)
        if m:
            b=self.body();h=b.get('handle','')
            if not valid_group_handle(h) or not b.get('name'):return self.err(400,'invalid handle')
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                if not w:return self.err(404,'not found')
                if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.err(403,'forbidden')
                try:
                    c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,users) VALUES(?,?,?,?,?)',(w['id'],h,b['name'],u['id'],json.dumps(b.get('user_ids',[]))))
                    r=c.execute('SELECT * FROM groups_ WHERE id=?',(c.lastrowid,)).fetchone()
                except sqlite3.IntegrityError:return self.err(409,'duplicate')
            return self.sendj(201,{'group':group_json(r)})
        m=re.match(r'^/api/workspaces/([^/]+)/channels$',path)
        if m:
            b=self.body()
            name=b.get('name','')
            if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(b.get('topic',''))>250:return self.err(400,'invalid channel')
            with db() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return self.err(404,'not found')
            with db() as c: role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not role:return self.err(403,'forbidden')
            if role['role']=='guest' or (b.get('is_private') and role['role'] not in ('owner','admin')):return self.err(403,'forbidden')
            try:
                with db() as c:
                    c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b.get('name',''),int(bool(b.get('is_private'))),b.get('topic','')));cid=c.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']))
                with db() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                return self.sendj(201,{'channel':channel_obj(ch)})
            except sqlite3.IntegrityError:return self.err(409,'duplicate')
        m=re.match(r'^/api/channels/(\d+)/join$',path)
        if m:
            cid=int(m.group(1))
            with db() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            if not ch:return self.err(404,'not found')
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if role and role['role']=='guest':return self.err(403,'forbidden')
                if ch['is_private'] and (not role or not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()):return self.err(403,'forbidden')
                if not role and (w['join_mode']!='open' or ch['is_private']):return self.err(403,'forbidden')
                if not role:c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
                existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
                c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']))
            if not existed: emit(cid,'member.joined',{'user_id':u['id']})
            return self.sendj(200,{'channel':channel_obj(ch)})
        m=re.match(r'^/api/channels/(\d+)/leave$',path)
        if m:
            cid=int(m.group(1))
            with db() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            if not ch:return self.err(404,'not found')
            with db() as c:
                existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
                c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id']))
            if existed: emit(cid,'member.left',{'user_id':u['id']})
            return self.sendj(200,{'channel':channel_obj(ch)})
        m=re.match(r'^/api/channels/(\d+)/read$',path)
        if m:
            cid=int(m.group(1));b=self.body()
            if not member(cid,u['id']):return self.err(403,'forbidden')
            eid=int(b.get('last_read_event_id',0))
            with db() as c:
                old=c.execute('SELECT last_read_event_id FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
                eid=max(eid,old['last_read_event_id'] if old else 0)
                c.execute('INSERT OR REPLACE INTO readstate VALUES(?,?,?)',(u['id'],cid,eid))
            return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':eid,'unread_count':0,'mention_count':0}})
        m=re.match(r'^/api/channels/(\d+)/messages$',path)
        if m:
            cid=int(m.group(1));b=self.body(); body=b.get('body','')
            if not body:return self.err(400,'empty')
            with db() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            if not ch:return self.err(404,'not found')
            if not member(cid,u['id']):return self.err(403,'forbidden')
            if body.startswith('/') and not body.startswith('//'):
                cmd,_,arg=body[1:].partition(' '); arg=arg.strip()
                if cmd=='me':
                    if not arg:return self.err(400,'/me needs text')
                elif cmd=='shrug':
                    body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
                elif cmd=='topic':
                    if not arg or len(arg)>250:return self.err(400,'invalid topic')
                    with db() as c:
                        owner=c.execute('SELECT w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,)).fetchone()
                    if not member(cid,u['id']) and (not owner or owner['owner_id']!=u['id']):return self.err(403,'forbidden')
                    with db() as c:c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                    emit(cid,'channel.updated',{'channel':channel_obj(ch)})
                    return self.sendj(201,{'message':None,'channel':channel_obj(ch)})
                elif cmd=='invite':
                    if not arg.startswith('@') or len(arg)<2:return self.err(400,'username required')
                    if not member(cid,u['id']):return self.err(403,'forbidden')
                    with db() as c:
                        w=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone()
                        target=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
                        inw=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['workspace_id'],target['id'] if target else -1)).fetchone()
                    if not target:return self.err(404,'user not found')
                    if not inw:return self.err(403,'outside workspace')
                    with db() as c:c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,target['id']))
                    return self.sendj(201,{'message':None,'invited_user':userobj(target)})
                elif cmd in ('archive','unarchive'):
                    with db() as c:
                        owner=c.execute('SELECT w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,)).fetchone()
                    if not owner or owner['owner_id']!=u['id']:return self.err(403,'forbidden')
                    val=1 if cmd=='archive' else 0
                    with db() as c:c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                    emit(cid,'channel.archived' if val else 'channel.unarchived',{'channel':channel_obj(ch)})
                    return self.sendj(201,{'message':None,'channel':channel_obj(ch)})
                else:return self.err(400,'unknown command')
            if ch['is_archived']:return self.err(423,'archived')
            if b.get('parent_id'):
                with db() as c:
                    if not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(b['parent_id'],cid)).fetchone():return self.err(404,'parent')
            fids=b.get('file_ids') or []
            if not isinstance(fids,list):return self.err(400,'invalid files')
            with db() as c:
                for fid in fids:
                    if not c.execute('SELECT 1 FROM files f WHERE f.id=? AND f.uploader_id=? AND NOT EXISTS(SELECT 1 FROM message_files mf WHERE mf.file_id=f.id)',(fid,u['id'])).fetchone():return self.err(400,'invalid file')
            mentions=[]
            with db() as c:
                chmembers=c.execute('SELECT u.id,u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall()
                ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone()
                wmembers=c.execute('SELECT u.id,u.username FROM users u JOIN members wm ON wm.user_id=u.id WHERE wm.workspace_id=?',(ch['workspace_id'],)).fetchall()
            for mm in re.finditer(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
                handle=mm.group(1)
                if handle=='channel':
                    mentions.extend(x['id'] for x in chmembers if x['id']!=u['id'])
                else:
                    with db() as gc:
                        gg=gc.execute('SELECT users FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],handle.lower())).fetchone()
                    if gg:
                        ids={int(x) for x in json.loads(gg['users'] or '[]')}
                        mentions.extend(x['id'] for x in chmembers if x['id'] in ids and x['id']!=u['id'])
                    elif handle=='here':
                        with db() as pc: online={x['user_id'] for x in pc.execute('SELECT user_id FROM presence WHERE last_seen>?',(time.time()-1.5,)).fetchall()}
                        mentions.extend(x['id'] for x in chmembers if x['id'] in online and x['id']!=u['id'])
                    else:
                        mentions.extend(x['id'] for x in wmembers if x['username'].lower()==handle.lower() and x['id']!=u['id'])
            mentions=list(dict.fromkeys(mentions))
            with db() as c:
                c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),now()));mid=c.lastrowid
                for fid in fids:c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid))
                r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
            out=msgobj(r);out['mentions']=mentions
            emit(cid,'message.reply' if b.get('parent_id') else 'message.created',{'message':out})
            return self.sendj(201,{'message':out})
        m=re.match(r'^/api/messages/(\d+)/reactions$',path)
        if m:
            b=self.body();emoji=b.get('emoji','')
            if not emoji:return self.err(400,'emoji required')
            with db() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            if not member(r['channel_id'],u['id']):return self.err(403,'forbidden')
            with db() as c:
                before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji)).fetchone()
                c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?,?)',(r['id'],u['id'],emoji,now()))
            reactions=msgobj(r)['reactions']
            if not before: emit(r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':emoji,'user_id':u['id'],'reactions':reactions})
            return self.sendj(200,{'reactions':reactions})
        m=re.match(r'^/api/messages/(\d+)/pin$',path)
        if m:
            with db() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            with db() as c:c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],now()))
            return self.sendj(200,{'pin':{'message_id':r['id'],'pinned_by':u['id'],'pinned_at':now()}})
        return self.err(404,'not found')
    def do_PATCH(self):
        u=self.auth()
        if not u:return self.err(401,'unauthorized')
        p=urlparse(self.path).path;b=self.body()
        if p=='/api/users/me':
            allowed=('display_name','timezone','avatar_url','status_text','status_emoji')
            if 'timezone' in b:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(b['timezone'])
                except Exception:return self.err(400,'invalid timezone')
            for k in allowed:
                if k in b and (not isinstance(b[k],str) or len(b[k])>500 or (k=='display_name' and not b[k])):
                    return self.err(400,'invalid profile')
            sets=[k+'=?' for k in allowed if k in b]
            if sets:
                with db() as c:
                    c.execute('UPDATE users SET '+','.join(sets)+' WHERE id=?',[b[k] for k in allowed if k in b]+[u['id']])
                    u=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
            with db() as c:c.execute('INSERT INTO global_events(kind,payload,created_at) VALUES(?,?,?)',('user.updated',json.dumps({'user':userobj(u)}),now()))
            return self.sendj(200,{'user':userobj(u)})
        m=re.match(r'^/api/workspaces/([^/]+)$',p)
        if m:
            with db() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return self.err(404,'not found')
            with db() as c:r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not r or r['role'] not in ('owner','admin'):return self.err(403,'forbidden')
            if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.err(400,'invalid')
            vals=[]; args=[]
            for k in ('name','join_mode'):
                if k in b: vals.append(k+'=?');args.append(b[k])
            if vals:
                with db() as c:c.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',args+[w['id']]);w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone()
            return self.sendj(200,{'workspace':workspace_obj(w)})
        m=re.match(r'^/api/channels/(\d+)$',p)
        if m:
            cid=int(m.group(1))
            with db() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            if not ch:return self.err(404,'not found')
            if ch['is_archived']:return self.err(423,'archived')
            with db() as c:
                cw=c.execute('SELECT w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
            if not member(cid,u['id']) and (not cw or cw['owner_id']!=u['id']):return self.err(403,'forbidden')
            if 'topic' in b and len(b['topic'])>250:return self.err(400,'topic')
            changed='topic' in b and b.get('topic')!=ch['topic']
            with db() as c:c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',ch['topic']),cid));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            if changed: emit(cid,'channel.updated',{'channel':channel_obj(ch)})
            return self.sendj(200,{'channel':channel_obj(ch)})
        m=re.match(r'^/api/workspaces/([^/]+)/groups/([^/]+)$',p)
        if m:
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
            if not w or not g:return self.err(404,'not found')
            if u['id'] not in (g['creator_id'],w['owner_id']):return self.err(403,'forbidden')
            if 'name' in b and not b['name']:return self.err(400,'invalid')
            with db() as c:
                c.execute('UPDATE groups_ SET name=?,users=? WHERE id=?',(b.get('name',g['name']),json.dumps(b.get('user_ids',json.loads(g['users'] or '[]'))),g['id']))
                g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone()
            return self.sendj(200,{'group':group_json(g)})
        m=re.match(r'^/api/workspaces/([^/]+)/members/(\d+)$',p)
        if m:
            slug, target = m.group(1), int(m.group(2))
            role=b.get('role')
            if role not in ('owner','admin','member','guest'): return self.err(400,'invalid role')
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
                if not w:return self.err(404,'not found')
                actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                old=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
            if not old:return self.err(404,'not found')
            if old['role']=='owner' or target==u['id']:return self.err(403,'forbidden')
            if role=='owner':return self.err(400,'invalid role')
            if not actor or actor['role'] not in ('owner','admin'):return self.err(403,'forbidden')
            if actor['role']=='admin' and old['role']=='admin':return self.err(403,'forbidden')
            with db() as c:
                c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target))
            return self.sendj(200,{'member':{'user_id':target,'role':role}})
        m=re.match(r'^/api/messages/(\d+)/pin$',p)
        if m:
            with db() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            if not member(r['channel_id'],u['id']):return self.err(403,'forbidden')
            with db() as c:c.execute('DELETE FROM pins WHERE message_id=?',(r['id'],))
            return self.sendj(200,{'unpinned':True})
        m=re.match(r'^/api/messages/(\d+)$',p)
        if m:
            with db() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            if r['deleted']:return self.err(409,'deleted')
            if r['author_id']!=u['id']:return self.err(403,'forbidden')
            body=b.get('body')
            if not isinstance(body,str) or not body:return self.err(400,'empty')
            with db() as c:
                c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,now(),r['id']))
                r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone()
            out=msgobj(r); emit(r['channel_id'],'message.updated',{'message':out})
            return self.sendj(200,{'message':out})
        return self.err(404,'not found')
    def do_DELETE(self):
        u=self.auth()
        p=urlparse(self.path).path
        if not u:return self.err(401,'unauthorized')
        m=re.match(r'^/api/workspaces/([^/]+)/groups/([^/]+)$',p)
        if m:
            with db() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
            if not w or not g:return self.sendj(200,{'deleted':True})
            if u['id'] not in (g['creator_id'],w['owner_id']):return self.err(403,'forbidden')
            with db() as c:c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],))
            return self.sendj(200,{'deleted':True})
        m=re.match(r'^/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
        if m:
            with db() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return self.err(404,'not found')
            with db() as c:r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not r or r['role'] not in ('owner','admin'):return self.err(403,'forbidden')
            with db() as c:
                inv=c.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(m.group(2),w['id'])).fetchone()
                if not inv:return self.err(404,'not found')
                c.execute('DELETE FROM invitations WHERE code=?',(m.group(2),))
            return self.sendj(200,{'deleted':True})
        m=re.match(r'^/api/messages/(\d+)/reactions/(.*)$',p)
        if m:
            eid=unquote(m.group(2))
            with db() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            if not member(r['channel_id'],u['id']):return self.err(403,'forbidden')
            with db() as c:
                before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],eid)).fetchone()
                c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],eid))
            reactions=msgobj(r)['reactions']
            if before:emit(r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':eid,'user_id':u['id'],'reactions':reactions})
            return self.sendj(200,{'reactions':reactions})
        m=re.match(r'^/api/messages/(\d+)/pin$',p)
        if m:
            with db() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            if not member(r['channel_id'],u['id']):return self.err(403,'forbidden')
            with db() as c:c.execute('DELETE FROM pins WHERE message_id=?',(r['id'],))
            return self.sendj(200,{'unpinned':True})
        m=re.match(r'^/api/messages/(\d+)$',p)
        if m:
            with db() as c:
                r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(m.group(1),)).fetchone()
            if not r:return self.err(404,'not found')
            if r['author_id']!=u['id'] and r['owner_id']!=u['id']:return self.err(403,'forbidden')
            if not r['deleted']:
                with db() as c:c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],))
                emit(r['channel_id'],'message.deleted',{'message_id':r['id']})
            return self.sendj(200,{'deleted':True})
        return self.err(404,'not found')
    def html(self):
        try:
            b=open('/app/static/index.html','rb').read()
        except Exception:
            b=b'<h1>Huddle</h1>'
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)

IRC_NICKS=set(); IRC_CLIENTS=[]; IRC_LOCK=threading.RLock()
def irc_send(sock,line):
    try:sock.sendall((line+'\r\n').encode())
    except Exception:pass
def irc_channel_name(w,c): return '#%s/%s'%(w['slug'],c['name'])
def irc_find_channel(name):
    if not name.startswith('#') or '/' not in name[1:]: return None
    slug,cn=name[1:].split('/',1)
    with db() as c:return c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone()
def irc_pump():
    seen=0
    while True:
        try:
            with db() as c: evs=c.execute("SELECT rowid,channel_id,payload FROM events WHERE rowid>? AND kind='message.created' ORDER BY rowid",(seen,)).fetchall()
            for e in evs:
                seen=e['rowid']; m=json.loads(e['payload']).get('message',{})
                with db() as c:
                    ch=c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(e['channel_id'],)).fetchone()
                    au=c.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone()
                if not ch or not au:continue
                line=':%s!%s@huddle PRIVMSG %s :%s'%(au['username'],au['username'],irc_channel_name(ch,ch),m.get('body',''))
                with IRC_LOCK: clients=list(IRC_CLIENTS)
                for z in clients:
                    if z.get('channel_id')==e['channel_id'] and z.get('nick')!=m.get('irc_sender'):irc_send(z['sock'],line)
        except Exception:pass
        time.sleep(.1)
def irc_client(sock,addr):
    token=None; nick=None; user=None; uid=None; joined=set(); state={'sock':sock,'nick':None,'channel_id':None}
    with IRC_LOCK:IRC_CLIENTS.append(state)
    try:
        buf=b''
        while True:
            while b'\r\n' not in buf:
                x=sock.recv(4096)
                if not x:return
                buf+=x
            raw,buf=buf.split(b'\r\n',1); line=raw.decode(errors='replace'); parts=line.split(' ',1); cmd=parts[0].upper(); rest=parts[1] if len(parts)>1 else ''
            if cmd=='PASS':
                token=rest.lstrip(':').strip();uid=token_user(token)
                if not uid:irc_send(sock,':huddle 464 * :Password incorrect')
            elif cmd=='NICK':
                n=rest.strip().lstrip(':')
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',n):continue
                with IRC_LOCK:
                    if n.lower() in {x.lower() for x in IRC_NICKS}:irc_send(sock,':huddle 433 %s %s :Nickname is already in use'%(nick or '*',n));continue
                    if nick:IRC_NICKS.discard(nick)
                    nick=n;IRC_NICKS.add(n);state['nick']=nick
            elif cmd=='USER':user=rest.split(' ',1)[0] if rest else nick
            elif cmd=='PING':irc_send(sock,':huddle PONG huddle :'+rest.lstrip(':'))
            elif cmd=='QUIT':return
            elif cmd=='JOIN':
                cn=rest.split()[0] if rest else '';ch=irc_find_channel(cn)
                if not ch:irc_send(sock,':huddle 403 %s %s :No such channel'%(nick,cn))
                elif uid:
                    with db() as c:
                        c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],uid['id'],'member'));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],uid['id']))
                        ns=c.execute('SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall()
                    joined.add(ch['id']);state['channel_id']=ch['id'];irc_send(sock,':%s!%s@huddle JOIN %s'%(nick,uid['username'],cn));irc_send(sock,':huddle 353 %s = %s :%s'%(nick,cn,' '.join(x['username'] for x in ns)));irc_send(sock,':huddle 366 %s %s :End of NAMES list'%(nick,cn))
            elif cmd=='PRIVMSG' and ' :' in rest:
                cn,text=rest.split(' :',1);ch=irc_find_channel(cn.strip())
                if ch and uid and ch['id'] in joined:
                    with db() as c:
                        c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],uid['id'],text,now()));mid=c.lastrowid;r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                    emit(ch['id'],'message.created',{'message':msgobj(r),'irc_sender':nick})
            elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):pass
            elif cmd not in ('PASS','NICK','USER'):irc_send(sock,':huddle 421 %s %s :Unknown command'%(nick or '*',cmd))
            if uid and nick and user and not state.get('registered'):
                state['registered']=True
                for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created now'),('004','huddle huddle 1 0'),('005','CHANTYPES=# :are supported'),('422',':No MOTD')]:irc_send(sock,':huddle %s %s %s'%(code,nick,text))
    except Exception:pass
    finally:
        with IRC_LOCK:
            if nick:IRC_NICKS.discard(nick)
            if state in IRC_CLIENTS:IRC_CLIENTS.remove(state)
        try:sock.close()
        except Exception:pass
def irc_server():
    threading.Thread(target=irc_pump,daemon=True).start()
    z=socket.socket();z.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);z.bind(('0.0.0.0',6667));z.listen(100)
    while True:
        c,a=z.accept();threading.Thread(target=irc_client,args=(c,a),daemon=True).start()

if __name__=='__main__':
    init()
    if len(sys.argv)>1 and sys.argv[1]=='irc':
        irc_server()
    else:
        start_redis_listener(); port=int(sys.argv[1]) if len(sys.argv)>1 else int(os.environ.get('PORT','8000')); os.environ['NODE_ID']=str(port-8000); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
