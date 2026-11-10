#!/usr/bin/env python3
import sys, os, re, json, sqlite3, secrets, hashlib, base64, socket, threading, time, struct
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB='/var/lib/huddle/huddle.db'
NODE=int(sys.argv[1])-8000
clients=[]
clients_lock=threading.Lock()
wake=threading.Event()

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB, timeout=2, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=2000')
    return c
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,event_seq INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS workspace_members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
    CREATE INDEX IF NOT EXISTS events_chan ON events(channel_id,event_id);
    ''')
    c.commit(); c.close()
def user_json(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def user_from_token(t):
    if not t:return None
    c=db()
    try:
        r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone()
        return r
    finally:c.close()
def get_user_by_username(n):
    c=db()
    try:return c.execute('SELECT * FROM users WHERE username=?',(n,)).fetchone()
    finally:c.close()
def get_workspace_by_slug(s):
    c=db()
    try:return c.execute('SELECT * FROM workspaces WHERE slug=?',(s,)).fetchone()
    finally:c.close()
def get_member_role(w,u):
    c=db()
    try:
        r=c.execute('SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone()
        return r['role'] if r else None
    finally:c.close()
def get_channel(i):
    c=db()
    try:return c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone()
    finally:c.close()
def channel_json(r):
    return { 'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def create_channel(w,n,p=False):
    c=db()
    try:
        x=c.execute('INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,?)',(w,n,int(p)))
        c.commit(); return get_channel(x.lastrowid)
    finally:c.close()
def add_channel_member(ch,u):
    c=db()
    try:c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch,u));c.commit()
    finally:c.close()
def is_channel_member(ch,u):
    c=db()
    try:return bool(c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u)).fetchone())
    finally:c.close()
def list_channel_members(ch):
    c=db()
    try:return c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(ch,)).fetchall()
    finally:c.close()
def validate_topic(x): return isinstance(x,str) and len(x)<=250
def update_channel(i,**fields):
    c=db()
    try:
        sets=[]; vals=[]
        for k,v in fields.items():
            if k in ('topic','is_archived'): sets.append(k+'=?');vals.append(int(v) if k=='is_archived' else v)
        if sets:c.execute('UPDATE channels SET '+','.join(sets)+' WHERE id=?',(*vals,i));c.commit()
        return get_channel(i)
    finally:c.close()
def require_writable(ch):
    if ch and ch['is_archived']: raise ValueError('archived')
def can_read_channel(ch,u):
    return bool(ch) and (not ch['is_private'] and get_member_role(ch['workspace_id'],u) or is_channel_member(ch['id'],u))
def extract_mentions(body,ch,author):
    found=[]
    if re.search(r'(?<!\w)@channel(?!\w)',body): found += [r['id'] for r in list_channel_members(ch['id']) if r['id']!=author]
    for n in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
        if n=='channel':continue
        r=get_user_by_username(n)
        if r and r['id']!=author and get_member_role(ch['workspace_id'],r['id']) and r['id'] not in found: found.append(r['id'])
    return found
def message_json(r):
    c=db()
    try:
        a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
        return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_json(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':extract_mentions(r['body'],get_channel(r['channel_id']),r['author_id'])}
    finally:c.close()
def get_message(i):
    c=db()
    try:return c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(i,)).fetchone()
    finally:c.close()
def emit_event(ch,kind,payload):
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch,)).fetchone()
        eid=r['n']; c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(ch,eid,kind,json.dumps(payload,separators=(',',':'))));c.commit()
    finally:c.close()
    notify_nodes(ch); wake.set()
    return {'type':kind,'event_id':eid,'channel_id':ch,**payload}
def notify_nodes(ch):
    try:
        s=socket.create_connection(('127.0.0.1',6379),timeout=.2)
        s.sendall(b'*2\r\n$7\r\nPUBLISH\r\n$13\r\nhuddle:events\r\n$'+str(len(str(ch))).encode()+b'\r\n'+str(ch).encode()+b'\r\n');s.settimeout(.2);s.recv(128);s.close()
    except Exception: pass
def redis_listener():
    while True:
        try:
            s=socket.create_connection(('127.0.0.1',6379),timeout=.2);s.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
            while True:
                s.settimeout(1)
                data=s.recv(4096)
                if not data:
                    raise ConnectionError('redis closed')
                wake.set()
        except Exception: time.sleep(.5)
def frame(obj):
    b=json.dumps(obj,separators=(',',':')).encode(); return b'\x81'+(bytes([len(b)]) if len(b)<126 else b'\x81')+b if len(b)<126 else b'\x81\x7e'+struct.pack('!H',len(b))+b
def ws_send(s,o):
    try:s.sendall(frame(o));return True
    except:return False
def event_pump():
    while True:
        c=db()
        try: ev=c.execute('SELECT * FROM events ORDER BY id').fetchall()
        finally:c.close()
        with clients_lock:
            for cl in list(clients):
                s,subs,last=cl
                for e in ev:
                    if e['id']<=last or e['channel_id'] not in subs: continue
                    p=json.loads(e['payload']); ok=ws_send(s,{'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**p})
                    if not ok:
                        try:clients.remove(cl)
                        except:pass
                        break
                    cl[2]=e['id']; last=e['id']
        wake.wait(.1);wake.clear()
def token(h):
    a=h.get('Authorization','')
    return a[7:] if a.startswith('Bearer ') else ''
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a):pass
    def out(self,status,obj=None,headers=None):
        b=b'' if obj is None else json.dumps(obj,separators=(',',':')).encode()
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)))
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers()
        if b:self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or b'{}')
    def auth(self):
        u=user_from_token(token(self.headers))
        if not u:self.out(401,{'error':'unauthorized'});return None
        return u
    def do_GET(self):
        p=urlparse(self.path); q=parse_qs(p.query); parts=p.path.strip('/').split('/')
        if p.path=='/api/health':return self.out(200,{'status':'ok','node_id':NODE})
        if parts[:3]==['api','auth','me']:
            u=self.auth();return u and self.out(200,{'user':user_json(u)})
        if len(parts)>=3 and parts[:2]==['api','workspaces']:
            u=self.auth()
            if not u:return
            w=get_workspace_by_slug(parts[2])
            if not w:return self.out(404,{})
            if not get_member_role(w['id'],u['id']):return self.out(403,{})
            c=db()
            try: cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 AND (is_private=0 OR id IN (SELECT channel_id FROM channel_members WHERE user_id=?))',(w['id'],u['id'])).fetchall()
            finally:c.close()
            return self.out(200,{'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':'open'},'channels':[channel_json(x) for x in cs]})
        if len(parts)>=4 and parts[:2]==['api','channels'] and parts[3]=='members':
            u=self.auth();ch=get_channel(int(parts[2])) if parts[2].isdigit() else None
            if not u:return
            if not ch:return self.out(404,{})
            if not can_read_channel(ch,u['id']):return self.out(403,{})
            return self.out(200,{'members':[user_json(x) for x in list_channel_members(ch['id'])]})
        if len(parts)>=4 and parts[:3]==['api','channels',parts[2]] and parts[3]=='messages':
            u=self.auth(); ch=get_channel(int(parts[2])) if parts[2].isdigit() else None
            if not u:return
            if not ch:return self.out(404,{})
            if not can_read_channel(ch,u['id']):return self.out(403,{})
            c=db()
            try: rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(ch['id'],)).fetchall()
            finally:c.close()
            return self.out(200,{'messages':[message_json(x) for x in rows],'next_cursor':None})
        if p.path.startswith('/api/ws'): return self.websocket()
        self.out(404,{})
    def do_POST(self):
        p=urlparse(self.path); parts=p.path.strip('/').split('/')
        if p.path=='/api/auth/register':
            try:d=self.body(); n=d['username'];pw=d['password']
            except:return self.out(400,{})
            if not re.fullmatch(r'[A-Za-z0-9_]+',n) or len(pw)<8:return self.out(400,{})
            c=db()
            try:
                try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(n,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name',n)));c.commit()
                except sqlite3.IntegrityError:return self.out(409,{})
                uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            finally:c.close()
            return self.out(201,{'user':user_json(u),'token':t})
        if p.path=='/api/auth/login':
            d=self.body();u=get_user_by_username(d.get('username',''))
            if not u or u['password']!=hashlib.sha256(d.get('password','').encode()).hexdigest():return self.out(401,{})
            t=secrets.token_urlsafe(24);c=db()
            try:c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.commit()
            finally:c.close()
            return self.out(200,{'user':user_json(u),'token':t})
        u=self.auth()
        if not u:return
        d=self.body()
        if parts[:2]==['api','workspaces'] and len(parts)==2:
            if get_workspace_by_slug(d.get('slug','')):return self.out(409,{})
            if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.out(400,{})
            c=db()
            try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id']));wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO workspace_members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();w=get_workspace_by_slug(d['slug']);ch=get_channel(cid)
            finally:c.close()
            return self.out(201,{'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':'open'},'general_channel':channel_json(ch)})
        if len(parts)==4 and parts[0:2]==['api','workspaces'] and parts[3]=='channels':
            w=get_workspace_by_slug(parts[2]);role=get_member_role(w['id'],u['id']) if w else None
            if not w:return self.out(404,{})
            if role!='owner' or not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.out(400,{})
            c=db()
            try:
                if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],d['name'])).fetchone():return self.out(409,{})
            finally:c.close()
            ch=create_channel(w['id'],d['name'],bool(d.get('is_private')))
            add_channel_member(ch['id'],u['id']);return self.out(201,{'channel':channel_json(ch)})
        if len(parts)>=4 and parts[:2]==['api','channels'] and parts[3]=='join':
            ch=get_channel(int(parts[2]))
            if not ch:return self.out(404,{})
            if ch['is_private']:return self.out(403,{})
            add_channel_member(ch['id'],u['id'])
            if not get_member_role(ch['workspace_id'],u['id']):
                c=db()
                try:c.execute('INSERT OR IGNORE INTO workspace_members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.commit()
                finally:c.close()
            return self.out(200,{'channel':channel_json(ch)})
        if len(parts)>=4 and parts[:2]==['api','channels'] and parts[3] in ('archive','unarchive'):
            ch=get_channel(int(parts[2]))
            if not ch or get_member_role(ch['workspace_id'],u['id'])!='owner':return self.out(403,{})
            ch=update_channel(ch['id'],is_archived=parts[3]=='archive');return self.out(200,{'channel':channel_json(ch)})
        if len(parts)>=4 and parts[:2]==['api','channels'] and parts[3]=='messages':
            ch=get_channel(int(parts[2]))
            if not ch:return self.out(404,{})
            if not can_read_channel(ch,u['id']):return self.out(403,{})
            try:require_writable(ch)
            except:return self.out(423,{})
            if not isinstance(d.get('body'),str) or not d['body'].strip():return self.out(400,{})
            c=db()
            try:c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],u['id'],d['body'],now()));c.commit();m=get_message(c.execute('SELECT last_insert_rowid() x').fetchone()['x'])
            finally:c.close()
            mj=message_json(m); emit_event(ch['id'],'message.created',{'message':mj});return self.out(201,{'message':mj})
        self.out(404,{})
    def do_PATCH(self):
        u=self.auth()
        if not u:return
        parts=self.path.strip('/').split('/');d=self.body()
        if len(parts)==4 and parts[0:2]==['api','channels'] and parts[3]=='':pass
        if len(parts)==3 and parts[:2]==['api','channels']:
            ch=get_channel(int(parts[2]))
            if not ch:return self.out(404,{})
            if not validate_topic(d.get('topic')):return self.out(400,{})
            if not (is_channel_member(ch['id'],u['id']) or get_member_role(ch['workspace_id'],u['id'])=='owner'):return self.out(403,{})
            return self.out(200,{'channel':channel_json(update_channel(ch['id'],topic=d['topic']))})
        self.out(404,{})
    def websocket(self):
        if not user_from_token(parse_qs(urlparse(self.path).query).get('token',[''])[0]):
            self.send_error(401);return
        key=self.headers.get('Sec-WebSocket-Key')
        if not key:self.send_error(400);return
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
        s=self.connection; cl=[s,set(),0]
        with clients_lock:clients.append(cl)
        try:
            while True:
                h=s.recv(2)
                if not h:break
                n=h[1]&127; masked=h[1]&128
                if n==126:n=struct.unpack('!H',s.recv(2))[0]
                mask=s.recv(4) if masked else b'';data=s.recv(n)
                if masked:data=bytes(x^mask[i%4] for i,x in enumerate(data))
                try:o=json.loads(data);typ=o.get('type');ch=int(o.get('channel_id'))
                except:continue
                if typ=='subscribe':
                    cl[1].add(ch);c=db()
                    try:head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n'];cl[2]=c.execute('SELECT COALESCE(MAX(id),0) n FROM events').fetchone()['n']
                    finally:c.close()
                    ws_send(s,{'type':'subscribed','channel_id':ch,'head_event_id':head})
                elif typ=='resume':
                    since=int(o.get('since_event_id',0));c=db()
                    try:rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
                    finally:c.close()
                    for e in rows:ws_send(s,{'type':e['kind'],'event_id':e['event_id'],'channel_id':ch,**json.loads(e['payload'])})
                    cl[1].add(ch);cl[2]=max([cl[2]]+[e['id'] for e in rows]); head=rows[-1]['event_id'] if rows else since
                    ws_send(s,{'type':'resumed','channel_id':ch,'head_event_id':head})
        finally:
            with clients_lock:
                if cl in clients:clients.remove(cl)
init()
threading.Thread(target=event_pump,daemon=True).start()
threading.Thread(target=redis_listener,daemon=True).start()
ThreadingHTTPServer(('127.0.0.1',8000+NODE),H).serve_forever()
