import json, os, re, sys, time, uuid, hashlib, threading, mimetypes, base64, socket, struct, fcntl, contextlib, email, email.policy
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timezone
STATE='/tmp/huddle_state.json'; LOCK=threading.RLock()
PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
TLS=threading.local()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def load():
    try:
        with open(STATE) as f: return json.load(f)
    except: return {'users':{},'tokens':{},'workspaces':{},'channels':{},'members':{},'messages':{},'invitations':{},'pins':{},'events':{},'eseq':{},'reads':{},'files':{},'dms':{},'groups':{},'deleted_groups':{},'presence':{},'global_events':[],'seq':0}
def save(d):
    def write():
        tmp=STATE+'.%s.%s.tmp' % (os.getpid(), threading.get_ident())
        with open(tmp,'w') as f:
            json.dump(d,f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,STATE)
    if getattr(TLS,'held',False):
        write(); return
    with open(STATE+'.lock','w') as lk:
        fcntl.flock(lk,fcntl.LOCK_EX); write(); fcntl.flock(lk,fcntl.LOCK_UN)
def uid(d): d['seq']+=1; return str(d['seq'])
def envelope(x): return x
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def send(self,status,obj=None,headers=None):
        if obj is None: body=b''
        elif isinstance(obj,(bytes,bytearray)): body=obj
        else: body=json.dumps(obj,separators=(',',':')).encode()
        self.send_response(status)
        hs={'Content-Type':'application/json','Content-Length':str(len(body)),'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'Authorization,Content-Type','Access-Control-Allow-Methods':'GET,POST,PATCH,DELETE,OPTIONS'}
        if headers: hs.update(headers)
        for k,v in hs.items(): self.send_header(k,v)
        self.end_headers()
        if body: self.wfile.write(body)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n)
        ct=self.headers.get('Content-Type','')
        if ct.startswith('multipart/form-data'):
            msg=email.message_from_bytes(('Content-Type: '+ct+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw,policy=email.policy.default)
            for part in msg.iter_parts():
                if part.get_param('name',header='content-disposition')=='file':
                    data=part.get_payload(decode=True) or b''
                    return {'_file':data,'_filename':part.get_filename() or 'file','_content_type':part.get_content_type()}
            return {}
        try:return json.loads(raw or b'{}')
        except:return {}
    def auth(self,d):
        t=self.headers.get('Authorization','').replace('Bearer ','')
        return d['users'].get(d['tokens'].get(t)),t
    def user(self,d):
        u,_=self.auth(d); return u
    def err(self,s,msg): self.send(s,{'error':msg})
    def objuser(self,u):
        return {k:u.get(k) for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
    def member(self,d,w,u,role='member'):
        d['members'].setdefault(w,{})
        d['members'][w][str(u)]=role
    def accessible(self,d,c,u):
        return str(u) in d['members'].get(c['workspace_id'],{}) or c.get('is_dm')
    def channel(self,d,c):
        return {'id':c['id'],'workspace_id':c['workspace_id'],'name':c['name'],'is_private':c.get('is_private',False),'is_dm':c.get('is_dm',False),'topic':c.get('topic',''),'is_archived':c.get('is_archived',False)}
    def message(self,d,m):
        u=d['users'].get(str(m['author_id']),{})
        return {**m,'author':self.objuser(u),'reply_count':sum(1 for x in d['messages'].values() if x.get('parent_id')==m['id']),'files':m.get('files',[]),'reactions':m.get('reactions',[]),'mentions':m.get('mentions',[])}
    def ws_frame(self,obj):
        data=json.dumps(obj,separators=(',',':')).encode(); n=len(data)
        if n<126: h=bytes([0x81,n])
        elif n<65536: h=bytes([0x81,126])+struct.pack('!H',n)
        else: h=bytes([0x81,127])+struct.pack('!Q',n)
        return h+data
    def ws_read(self, sock):
        h=sock.recv(2)
        if not h or len(h)<2: raise EOFError('websocket closed')
        n=h[1]&127
        if n==126:n=struct.unpack('!H',sock.recv(2))[0]
        elif n==127:n=struct.unpack('!Q',sock.recv(8))[0]
        mask=sock.recv(4) if h[1]&128 else b''
        data=sock.recv(n); 
        while len(data)<n:data+=sock.recv(n-len(data))
        if mask:data=bytes(x^mask[i%4] for i,x in enumerate(data))
        return json.loads(data.decode())
    def emit(self,d,cid,kind,extra=None):
        es=d.setdefault('events',{}); seq=d.setdefault('eseq',{})
        n=int(seq.get(str(cid),0))+1; seq[str(cid)]=n
        ev={'type':kind,'event_id':n,'channel_id':cid}; ev.update(extra or {})
        es.setdefault(str(cid),[]).append(ev); return ev
    def websocket(self, token):
        d=load(); uid=d.get('tokens',{}).get(token); u=d.get('users',{}).get(str(uid))
        if not u:return False
        key=self.headers.get('Sec-WebSocket-Key','')
        import hashlib as _h
        accept=base64.b64encode(_h.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
        sock=self.connection; sock.settimeout(.35); channel=None; channel_last=0; global_seen=len(load().get('global_events',[]))
        d=load(); d.setdefault('presence',{})[str(u['id'])]=time.time(); save(d)
        while True:
            try:
                req=None
                try:req=self.ws_read(sock)
                except socket.timeout:req=None
                if req:
                    d=load(); d.setdefault('presence',{})[str(u['id'])]=time.time(); save(d)
                    channel=str(req.get('channel_id'))
                    d=load(); typ=req.get('type')
                    if typ=='subscribe':
                        head=int(d.get('eseq',{}).get(channel,0)); channel_last=head; sock.sendall(self.ws_frame({'type':'subscribed','channel_id':req.get('channel_id'),'head_event_id':head}))
                    elif typ=='resume':
                        since=int(req.get('since_event_id',0)); events=d.get('events',{}).get(channel,[])
                        for ev in events:
                            if ev['event_id']>since:sock.sendall(self.ws_frame(ev))
                        head=int(d.get('eseq',{}).get(channel,0)); channel_last=head; sock.sendall(self.ws_frame({'type':'resumed','channel_id':req.get('channel_id'),'head_event_id':head}))
                d=load()
                ge=d.get('global_events',[])
                for ev in ge[global_seen:]:
                    sock.sendall(self.ws_frame(ev))
                global_seen=len(ge)
                if channel:
                    events=d.get('events',{}).get(channel,[])
                    for ev in events:
                        if ev['event_id']>channel_last:
                            sock.sendall(self.ws_frame(ev)); channel_last=ev['event_id']
            except socket.timeout: continue
            except Exception: break
        d=load(); d.setdefault('presence',{}).pop(str(u['id']),None); save(d)
        return True
    def do_GET(self):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
        if path=='/api/ws' and self.headers.get('Upgrade','').lower()=='websocket':
            self.websocket(q.get('token',[''])[0]); return
        with LOCK:
            d=load()
            if path=='/api/health': return self.send(200,{'status':'ok','node_id':NODE})
            if path=='/': return self.send(200,HTML.encode() if False else HTML,{'Content-Type':'text/html'})
            u=self.user(d)
            if path=='/api/auth/me':
                return self.send(200,{'user':self.objuser(u)}) if u else self.err(401,'unauthorized')
            m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
            if m:
                if not u:return self.err(401,'unauthorized')
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                if d['members'].get(w['id'],{}).get(str(u['id'])) not in ('owner','admin'):return self.err(403,'forbidden')
                ts=time.time(); out=[]
                for inv in d.get('invitations',{}).values():
                    if inv['workspace_id']==w['id'] and inv.get('uses',0)<inv['max_uses'] and (not inv.get('expires_at') or inv['expires_at']>ts): out.append(inv)
                return self.send(200,{'invitations':out})
            m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                if d['members'].get(w['id'],{}).get(str(u['id'])) not in ('owner','admin'):return self.err(403,'forbidden')
                code=uuid.uuid4().hex; maxu=1
                try:maxu=int(b.get('max_uses',1))
                except:maxu=1
                if maxu<1:maxu=1
                target=b.get('invited_user_id')
                if target is not None: target=str(target); maxu=1
                if b.get('invited_username'):
                    z=next((x for x in d['users'].values() if x['username']==b['invited_username']),None)
                    if z: target=str(z['id']); maxu=1
                exp=None
                if b.get('expires_in_seconds') is not None:
                    try: exp=time.time()+int(b['expires_in_seconds'])
                    except: exp=time.time()
                inv={'code':code,'workspace_id':w['id'],'max_uses':maxu,'uses':0,'created_at':now(),'expires_at':exp}
                if target: inv['invited_user_id']=int(target)
                d.setdefault('invitations',{})[code]=inv; save(d); return self.send(201,{'invitation':inv})
            m=re.match(r'/api/invitations/([^/]+)/accept$',path)
            if m:
                inv=d.get('invitations',{}).get(m.group(1))
                if not inv:return self.err(404,'not found')
                if inv.get('expires_at') and inv['expires_at']<=time.time() or inv.get('uses',0)>=inv.get('max_uses',1):return self.err(404,'not found')
                if inv.get('invited_user_id') is not None and str(inv['invited_user_id'])!=str(u['id']):return self.err(403,'forbidden')
                self.member(d,inv['workspace_id'],u['id']); inv['uses']=inv.get('uses',0)+1; save(d)
                return self.send(200,{'workspace':next((w for w in d['workspaces'].values() if w['id']==inv['workspace_id']),None)})
            if path=='/api/workspaces':
                if not u:return self.err(401,'unauthorized')
                a=[{**w,'join_mode':w.get('join_mode','open')} for w in d['workspaces'].values() if str(u['id']) in d['members'].get(w['id'],{})]
                return self.send(200,{'workspaces':a})
            m=re.match(r'/api/users/(\d+)$',path)
            if m:
                if not u:return self.err(401,'unauthorized')
                x=d['users'].get(m.group(1)); return self.send(200,{'user':self.objuser(x)}) if x else self.err(404,'not found')
            m=re.match(r'/api/workspaces/([^/]+)(?:/members)?$',path)
            if m and path.endswith('/members'):
                if not u:return self.err(401,'unauthorized')
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                return self.send(200,{'members':[{'user_id':int(x),'role':r} for x,r in d['members'].get(w['id'],{}).items()]})
            m=re.match(r'/api/workspaces/([^/]+)$',path)
            if m:
                if not u:return self.err(401,'unauthorized')
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                if str(u['id']) not in d['members'].get(w['id'],{}):return self.err(403,'forbidden')
                show_arch=q.get('include_archived',[''])[0] in ('1','true')
                cs=[self.channel(d,c) for c in d['channels'].values() if c['workspace_id']==w['id'] and (show_arch or not c.get('is_archived')) and (not c.get('is_private') or str(u['id']) in c.get('members',{}))]
                rs=[]
                for cc in d['channels'].values():
                    if cc['workspace_id']==w['id'] and (show_arch or not cc.get('is_archived')) and (not cc.get('is_private') or str(u['id']) in cc.get('members',{})):
                        key=str(u['id'])+':'+str(cc['id']); last=int(d.setdefault('reads',{}).get(key,0)); evs=d.get('events',{}).get(str(cc['id']),[])
                        unread=sum(1 for ev in evs if ev['event_id']>last and ev.get('type') in ('message.created','message.reply') and str(ev.get('message',{}).get('author_id'))!=str(u['id']))
                        mentions=sum(1 for ev in evs if ev['event_id']>last and ev.get('type') in ('message.created','message.reply') and str(ev.get('message',{}).get('author_id'))!=str(u['id']) and str(u['id']) in [str(x) for x in ev.get('message',{}).get('mentions',[])])
                        rs.append({'channel_id':cc['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mentions})
                return self.send(200,{'workspace':w,'channels':cs,'read_state':rs})
            m=re.match(r'/api/channels/(\d+)(?:/(messages|members|pins|read))?$',path)
            if m:
                c=d['channels'].get(m.group(1))
                if not c:return self.err(404,'not found')
                if not u:return self.err(401,'unauthorized')
                if m.group(2)=='members': return self.send(200,{'members':[self.objuser(d['users'][x]) for x in c.get('members',{}) if x in d['users']]})
                if m.group(2)=='messages':
                    if str(u['id']) not in c.get('members',{}): return self.err(403,'forbidden')
                    try: limit=int(q.get('limit',['50'])[0])
                    except: return self.err(400,'invalid limit')
                    if limit<1 or limit>200:return self.err(400,'invalid limit')
                    a=[self.message(d,x) for x in d['messages'].values() if x['channel_id']==c['id'] and not x.get('deleted') and not x.get('parent_id')]
                    a.sort(key=lambda x:(x['created_at'],str(x['id'])),reverse=True)
                    cur=q.get('cursor',[None])[0]
                    if cur is not None:
                        try:
                            raw=json.loads(base64.urlsafe_b64decode(cur.encode()).decode()); boundary=(raw[0],str(raw[1]))
                            a=[x for x in a if (x['created_at'],str(x['id'])) < boundary]
                        except: return self.err(400,'invalid cursor')
                    page=a[:limit]; nxt=None
                    if len(page)==limit and len(a)>limit:
                        last=page[-1]; nxt=base64.urlsafe_b64encode(json.dumps([last['created_at'],last['id']]).encode()).decode()
                    return self.send(200,{'messages':page,'next_cursor':nxt})
                if m.group(2)=='pins':
                    if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                    try: limit=int(q.get('limit',['50'])[0])
                    except:return self.err(400,'invalid limit')
                    if limit<1 or limit>200:return self.err(400,'invalid limit')
                    ps=[x for x in d.get('pins',{}).values() if x['channel_id']==c['id']]
                    ps.sort(key=lambda x:(x['pinned_at'],str(x['message_id'])),reverse=True)
                    cur=q.get('cursor',[None])[0]; start=0
                    if cur is not None:
                        try:start=int(cur)
                        except:return self.err(400,'invalid cursor')
                        if start<0 or start>len(ps):return self.err(400,'invalid cursor')
                    page=ps[start:start+limit]; nxt=str(start+limit) if start+limit<len(ps) else None
                    out=[{'message':self.message(d,d['messages'][str(x['message_id'])]),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in page if str(x['message_id']) in d['messages']]
                    return self.send(200,{'pins':out,'next_cursor':nxt})
                if m.group(2)=='read':
                    if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                    key=str(u['id'])+':'+str(c['id']); last=int(d.setdefault('reads',{}).get(key,0)); evs=d.get('events',{}).get(str(c['id']),[])
                    unread=sum(1 for ev in evs if ev['event_id']>last and ev.get('type') in ('message.created','message.reply') and str(ev.get('message',{}).get('author_id'))!=str(u['id']))
                    mentions=sum(1 for ev in evs if ev['event_id']>last and ev.get('type') in ('message.created','message.reply') and str(ev.get('message',{}).get('author_id'))!=str(u['id']) and str(u['id']) in [str(x) for x in ev.get('message',{}).get('mentions',[])])
                    return self.send(200,{'read_state':{'channel_id':c['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
                return self.send(200,{'channel':self.channel(d,c)})
            m=re.match(r'/api/messages/(\d+)/replies$',path)
            if m:
                parent=d['messages'].get(m.group(1))
                if not parent:return self.err(404,'not found')
                c=d['channels'].get(parent['channel_id'])
                if not c or str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                try: limit=int(q.get('limit',['50'])[0])
                except:return self.err(400,'invalid limit')
                if limit<1 or limit>200:return self.err(400,'invalid limit')
                a=[self.message(d,x) for x in d['messages'].values() if str(x.get('parent_id'))==str(m.group(1)) and not x.get('deleted')]
                a.sort(key=lambda x:(x['created_at'],str(x['id'])),reverse=False)
                cur=q.get('cursor',[None])[0]
                if cur is not None:
                    try:
                        raw=json.loads(base64.urlsafe_b64decode(cur.encode()).decode()); boundary=(raw[0],str(raw[1]))
                        a=[x for x in a if (x['created_at'],str(x['id'])) > boundary]
                    except:return self.err(400,'invalid cursor')
                page=a[:limit]; nxt=None
                if len(page)==limit and len(a)>limit:
                    last=page[-1]; nxt=base64.urlsafe_b64encode(json.dumps([last['created_at'],last['id']]).encode()).decode()
                return self.send(200,{'replies':page,'next_cursor':nxt})
            m=re.match(r'/api/files/(\d+)(/download)?$',path)
            if m:
                if not u:return self.err(401,'unauthorized')
                f=d.get('files',{}).get(m.group(1))
                if not f:return self.err(404,'not found')
                if m.group(2):
                    raw=base64.b64decode(f['data']); return self.send(200,raw,{'Content-Type':f['content_type'],'Content-Disposition':'attachment; filename="'+f['filename']+'"'})
                return self.send(200,{'file':{k:v for k,v in f.items() if k!='data'}})
            m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
            if m:
                if not u:return self.err(401,'unauthorized')
                w=next((x for x in d['workspaces'].values() if x['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                if str(u['id']) not in d['members'].get(w['id'],{}):return self.err(403,'forbidden')
                out=[g for g in d.get('groups',{}).values() if g['workspace_id']==w['id']]
                return self.send(200,{'groups':out})
            if path.startswith('/api/search'):
                if not u:return self.err(401,'unauthorized')
                try: limit=int(q.get('limit',['50'])[0])
                except:return self.err(400,'invalid limit')
                if limit<1 or limit>200:return self.err(400,'invalid limit')
                term=q.get('q',[''])[0].lower(); wid=q.get('workspace_id',[None])[0]
                a=[]
                for x in d['messages'].values():
                    c=d['channels'].get(x['channel_id'])
                    if not c or x.get('deleted') or (c.get('is_private') and str(u['id']) not in c.get('members',{})) or (not c.get('is_private') and str(u['id']) not in d['members'].get(c['workspace_id'],{})):continue
                    if wid is not None and str(c['workspace_id'])!=str(wid):continue
                    if term in x.get('body','').lower():a.append(self.message(d,x))
                a.sort(key=lambda x:(x['created_at'],str(x['id'])),reverse=True)
                cur=q.get('cursor',[None])[0]; start=0
                if cur is not None:
                    try:start=int(cur)
                    except:return self.err(400,'invalid cursor')
                    if start<0 or start>len(a):return self.err(400,'invalid cursor')
                page=a[start:start+limit]; nxt=str(start+limit) if start+limit<len(a) else None
                return self.send(200,{'results':page,'next_cursor':nxt})
            return self.err(404,'not found')
    @contextlib.contextmanager
    def proc_state_lock(self):
        with open(STATE+'.lock','w') as lk:
            fcntl.flock(lk,fcntl.LOCK_EX); TLS.held=True
            try: yield
            finally:
                TLS.held=False
                fcntl.flock(lk,fcntl.LOCK_UN)

    def do_POST(self):
        with self.proc_state_lock():
            return self._do_POST()

    def _do_POST(self):
        p=urlparse(self.path); path=p.path
        with LOCK:
            d=load(); b=self.body(); u=self.user(d)
            if path=='/api/auth/register':
                name=b.get('username',''); pw=b.get('password','')
                if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8:return self.err(400,'invalid')
                if any(x['username']==name for x in d['users'].values()):return self.err(409,'duplicate')
                i=uid(d); user={'id':i,'username':name,'password':pw,'display_name':b.get('display_name',name),'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}; t=uuid.uuid4().hex+uuid.uuid4().hex
                d['users'][i]=user; d['tokens'][t]=i; save(d); return self.send(201,{'user':self.objuser(user),'token':t})
            if path=='/api/auth/login':
                x=next((x for x in d['users'].values() if x['username']==b.get('username')),None)
                if not x or x.get('password') != b.get('password'):return self.err(401,'invalid')
                t=uuid.uuid4().hex+uuid.uuid4().hex; d['tokens'][t]=x['id']; save(d); return self.send(200,{'user':self.objuser(x),'token':t})
            if not u:return self.err(401,'unauthorized')
            if path=='/api/files':
                if not isinstance(b,dict) or '_file' not in b:return self.err(400,'file required')
                if len(b['_file'])>10485760:return self.err(413,'too large')
                fid=uid(d); f={'id':fid,'uploader_id':int(u['id']),'filename':b['_filename'],'content_type':b['_content_type'],'size':len(b['_file']),'created_at':now(),'data':base64.b64encode(b['_file']).decode()}
                d.setdefault('files',{})[fid]=f; save(d); return self.send(201,{'file':{k:v for k,v in f.items() if k!='data'}})
            m=re.match(r'/api/dms$',path)
            if m:
                target=str(b.get('user_id'))
                if target not in d['users']:return self.err(404,'not found')
                if target==str(u['id']):return self.err(400,'self')
                key=':'.join(sorted((str(u['id']),target)))
                if key not in d.setdefault('dms',{}):
                    cid=uid(d); d['dms'][key]=cid
                    d['channels'][cid]={'id':cid,'workspace_id':'dm-'+key,'name':'dm-'+key,'is_private':True,'is_dm':True,'topic':'','is_archived':False,'members':{str(u['id']):'member',target:'member'}}
                save(d); return self.send(200,{'channel_id':d['dms'][key]})
            m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                if d['members'].get(w['id'],{}).get(str(u['id'])) not in ('owner','admin'):return self.err(403,'forbidden')
                code=uuid.uuid4().hex; maxu=1
                try:maxu=int(b.get('max_uses',1))
                except:maxu=1
                if maxu<1:maxu=1
                target=b.get('invited_user_id')
                if target is not None: target=str(target); maxu=1
                if b.get('invited_username'):
                    z=next((x for x in d['users'].values() if x['username']==b['invited_username']),None)
                    if z: target=str(z['id']); maxu=1
                exp=None
                if b.get('expires_in_seconds') is not None:
                    try: exp=time.time()+int(b['expires_in_seconds'])
                    except: exp=time.time()
                inv={'code':code,'workspace_id':w['id'],'max_uses':maxu,'uses':0,'created_at':now(),'expires_at':exp}
                if target: inv['invited_user_id']=int(target)
                d.setdefault('invitations',{})[code]=inv; save(d); return self.send(201,{'invitation':inv})
            m=re.match(r'/api/invitations/([^/]+)/accept$',path)
            if m:
                inv=d.get('invitations',{}).get(m.group(1))
                if not inv:return self.err(404,'not found')
                if inv.get('expires_at') and inv['expires_at']<=time.time() or inv.get('uses',0)>=inv.get('max_uses',1):return self.err(404,'not found')
                if inv.get('invited_user_id') is not None and str(inv['invited_user_id'])!=str(u['id']):return self.err(403,'forbidden')
                self.member(d,inv['workspace_id'],u['id']); inv['uses']=inv.get('uses',0)+1; save(d)
                return self.send(200,{'workspace':next((w for w in d['workspaces'].values() if w['id']==inv['workspace_id']),None)})
            m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
            if m:
                w=next((x for x in d['workspaces'].values() if x['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                if str(u['id']) not in d['members'].get(w['id'],{}):return self.err(403,'forbidden')
                h=b.get('handle','')
                if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9-]{2,32}',h) or h in ('here','channel'):return self.err(400,'invalid handle')
                if any(g['workspace_id']==w['id'] and g['handle']==h for g in d.get('groups',{}).values()):return self.err(409,'duplicate')
                ids=[int(x) for x in (b.get('user_ids') or []) if str(x) in d['members'].get(w['id'],{})]
                gid=uid(d); g={'id':gid,'workspace_id':w['id'],'handle':h,'name':b.get('name',''),'member_user_ids':ids,'creator_id':int(u['id'])}
                d.setdefault('groups',{})[gid]=g; save(d); return self.send(201,{'group':{k:v for k,v in g.items() if k not in ('workspace_id','creator_id')}})
            if path=='/api/workspaces':
                s=b.get('slug',''); name=b.get('name','')
                if not re.fullmatch(r'[a-z0-9-]{2,32}',s) or any(w['slug']==s for w in d['workspaces'].values()):return self.err(409 if any(w['slug']==s for w in d['workspaces'].values()) else 400,'invalid')
                wid=uid(d); w={'id':wid,'slug':s,'name':name,'owner_id':u['id'],'join_mode':'open'}; d['workspaces'][wid]=w; self.member(d,wid,u['id'],'owner'); cid=uid(d); d['channels'][cid]={'id':cid,'workspace_id':wid,'name':'general','is_private':False,'topic':'','is_archived':False,'members':{str(u['id']):'member'}}; save(d); return self.send(201,{'workspace':w,'general_channel':self.channel(d,d['channels'][cid])})
            m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',path)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                mem=d['members'].get(w['id'],{}); target=str(b.get('new_owner_id'))
                if str(u['id'])!=str(w['owner_id']):return self.err(403,'forbidden')
                if mem.get(target)!='admin':return self.err(400,'target must be admin')
                old=str(w['owner_id']); mem[old]='admin'; mem[target]='owner'; w['owner_id']=int(target); save(d)
                return self.send(200,{'workspace':w})
            m=re.match(r'/api/channels/(\d+)/read$',path)
            if m:
                c=d['channels'].get(m.group(1))
                if not c:return self.err(404,'not found')
                if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                try:n=int(b.get('last_read_event_id'))
                except:return self.err(400,'invalid cursor')
                key=str(u['id'])+':'+str(c['id']); old=int(d.setdefault('reads',{}).get(key,0)); n=max(old,n); d['reads'][key]=n; save(d)
                evs=d.get('events',{}).get(str(c['id']),[])
                unread=sum(1 for ev in evs if ev['event_id']>n and ev.get('type') in ('message.created','message.reply') and str(ev.get('message',{}).get('author_id'))!=str(u['id']))
                mentions=sum(1 for ev in evs if ev['event_id']>n and ev.get('type') in ('message.created','message.reply') and str(ev.get('message',{}).get('author_id'))!=str(u['id']) and str(u['id']) in [str(x) for x in ev.get('message',{}).get('mentions',[])])
                return self.send(200,{'read_state':{'channel_id':c['id'],'last_read_event_id':n,'unread_count':unread,'mention_count':mentions}})
            m=re.match(r'/api/channels/(\d+)/join$',path)
            if m:
                c=d['channels'].get(m.group(1))
                if not c:return self.err(404,'not found')
                wm=d['members'].get(c['workspace_id'],{}); cm=c.setdefault('members',{})
                if str(u['id']) in cm: save(d); return self.send(200,{'channel':self.channel(d,c)})
                role=wm.get(str(u['id']))
                w=next((x for x in d['workspaces'].values() if x['id']==c['workspace_id']),None)
                if c.get('is_private') or role == 'guest': return self.err(403,'forbidden')
                if not role and w and w.get('join_mode','open')=='invite_only': return self.err(403,'forbidden')
                if not role: self.member(d,c['workspace_id'],u['id']); wm=d['members'].setdefault(c['workspace_id'],{}); wm[str(u['id'])]='member'
                cm[str(u['id'])]='member'; self.emit(d,c['id'],'member.joined',{'user_id':int(u['id'])}); save(d); return self.send(200,{'channel':self.channel(d,c)})
            m=re.match(r'/api/channels/(\d+)/(leave|archive|unarchive)$',path)
            if m:
                c=d['channels'].get(m.group(1))
                if not c:return self.err(404,'not found')
                if m.group(2)=='leave':
                    removed=c.setdefault('members',{}).pop(str(u['id']),None)
                    if removed:self.emit(d,c['id'],'member.left',{'user_id':int(u['id'])})
                    save(d); return self.send(200,{'channel':self.channel(d,c)})
                w=next(x for x in d['workspaces'].values() if x['id']==c['workspace_id'])
                if str(u['id'])!=str(w['owner_id']):return self.err(403,'forbidden')
                c['is_archived']=(m.group(2)=='archive'); self.emit(d,c['id'],'channel.archived' if c['is_archived'] else 'channel.unarchived'); save(d); return self.send(200,{'channel':self.channel(d,c)})
            m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                role=d['members'].get(w['id'],{}).get(str(u['id']))
                if role is None:return self.err(403,'forbidden')
                name=b.get('name',''); topic=b.get('topic','')
                if not isinstance(name,str) or not re.fullmatch(r'[a-z0-9-]{1,32}',name) or not isinstance(topic,str) or len(topic)>250:return self.err(400,'invalid channel')
                private=bool(b.get('is_private',False))
                if role=='guest' or (private and role not in ('admin','owner')):return self.err(403,'forbidden')
                if any(c['workspace_id']==w['id'] and c['name']==name for c in d['channels'].values()):return self.err(409,'duplicate')
                cid=uid(d); c={'id':cid,'workspace_id':w['id'],'name':name,'is_private':private,'topic':topic,'is_archived':False,'members':{str(u['id']):'member'}}; d['channels'][cid]=c; save(d); return self.send(201,{'channel':self.channel(d,c)})
            m=re.match(r'/api/messages/(\d+)/pin$',path)
            if m:
                msg=d['messages'].get(m.group(1))
                if not msg:return self.err(404,'not found')
                c=d['channels'].get(msg['channel_id'])
                if not c:return self.err(404,'not found')
                if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                pin={'message_id':int(msg['id']),'channel_id':c['id'],'pinned_by':int(u['id']),'pinned_at':now()}
                d.setdefault('pins',{})[str(msg['id'])]=pin; save(d)
                return self.send(200,{'pin':{'message_id':pin['message_id'],'pinned_by':pin['pinned_by'],'pinned_at':pin['pinned_at']}})
            m=re.match(r'/api/messages/(\d+)/reactions$',path)
            if m:
                msg=d['messages'].get(m.group(1))
                if not msg:return self.err(404,'not found')
                emoji=b.get('emoji')
                if not isinstance(emoji,str) or not emoji:return self.err(400,'invalid emoji')
                c=d['channels'].get(msg['channel_id'])
                if not c:return self.err(404,'not found')
                if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                rx=next((r for r in msg.setdefault('reactions',[]) if r['emoji']==emoji),None)
                changed=False
                if not rx:
                    rx={'emoji':emoji,'count':0,'user_ids':[]}; msg['reactions'].append(rx)
                if str(u['id']) not in [str(x) for x in rx['user_ids']]:
                    rx['user_ids'].append(int(u['id'])); rx['count']=len(rx['user_ids']); changed=True
                if changed:
                    ev=self.emit(d,c['id'],'reaction.added',{'message_id':int(msg['id']),'emoji':emoji,'user_id':int(u['id']),'reactions':msg['reactions']})
                save(d); return self.send(200,{'reactions':msg['reactions']})
            m=re.match(r'/api/channels/(\d+)/messages$',path)
            if m:
                c=d['channels'].get(m.group(1))
                if not c:return self.err(404,'not found')
                if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                body=b.get('body','')
                if not body:return self.err(400,'empty')
                if body.startswith('/') and not body.startswith('//'):
                    cmd=body.split(None,1)[0]
                    arg=body[len(cmd):].strip()
                    w=next((x for x in d['workspaces'].values() if x['id']==c['workspace_id']),None)
                    if cmd=='/me':
                        if not arg:return self.err(400,'text required')
                        body='/me '+arg
                    elif cmd=='/shrug':
                        body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
                    elif cmd=='/topic':
                        if not arg:return self.err(400,'topic required')
                        if c.get('is_archived'):return self.err(423,'archived')
                        c['topic']=arg
                        self.emit(d,c['id'],'channel.updated'); save(d)
                        return self.send(201,{'message':None,'channel':self.channel(d,c)})
                    elif cmd=='/invite':
                        if not arg:return self.err(400,'username required')
                        if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                        mm=re.fullmatch(r'@([A-Za-z0-9_]+)',arg)
                        if not mm:return self.err(400,'username required')
                        target=next((x for x in d['users'].values() if x.get('username')==mm.group(1)),None)
                        if not target:return self.err(404,'unknown user')
                        if not w or str(target['id']) not in d['members'].get(w['id'],{}):return self.err(403,'not workspace member')
                        c.setdefault('members',{})[str(target['id'])]='member'
                        save(d)
                        return self.send(201,{'message':None,'invited_user':self.objuser(target)})
                    elif cmd in ('/archive','/unarchive'):
                        if w is None or str(u['id'])!=str(w['owner_id']):return self.err(403,'forbidden')
                        c['is_archived']=(cmd=='/archive')
                        self.emit(d,c['id'],'channel.archived' if c['is_archived'] else 'channel.unarchived')
                        save(d); return self.send(201,{'message':None,'channel':self.channel(d,c)})
                    else:return self.err(400,'unknown command')
                if c.get('is_archived'):return self.err(423,'archived')
                fileobjs=[]
                for fid in b.get('file_ids',[]) or []:
                    f=d.get('files',{}).get(str(fid))
                    if not f or int(f['uploader_id'])!=int(u['id']) or any(str(fid)==str(x.get('id')) for x in d['messages'].values() for x in x.get('files',[])):return self.err(400,'invalid file')
                    fileobjs.append({k:v for k,v in f.items() if k!='data'})
                mid=uid(d)
                mentioned=[]
                if re.search(r'(?<!\w)@channel(?!\w)',body):
                    mentioned.extend(str(x) for x in c.get('members',{}) if str(x)!=str(u['id']))
                wm=d['members'].get(c['workspace_id'],{})
                for gh in re.findall(r'(?<![\w@])@([a-z0-9-]{2,32})\b',body):
                    gg=next((x for x in d.get('groups',{}).values() if x['workspace_id']==c['workspace_id'] and x['handle']==gh),None)
                    if gg:
                        for xid in gg['member_user_ids']:
                            if str(xid) in c.get('members',{}) and str(xid)!=str(u['id']) and str(xid) not in mentioned:mentioned.append(str(xid))
                if re.search(r'(?<!\w)@here(?!\w)',body):
                    for xid in c.get('members',{}):
                        if str(xid)!=str(u['id']) and time.time()-float(d.get('presence',{}).get(str(xid),0))<3 and str(xid) not in mentioned:mentioned.append(str(xid))
                for token in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)\b',body):
                    if token=='channel': continue
                    target=next((x for x in d['users'].values() if x.get('username')==token),None)
                    if target and str(target['id'])!=str(u['id']) and str(target['id']) in wm:
                        if str(target['id']) not in mentioned: mentioned.append(str(target['id']))
                msg={'id':mid,'channel_id':c['id'],'author_id':u['id'],'body':body,'parent_id':(str(b.get('parent_id')) if b.get('parent_id') is not None else None),'created_at':now(),'edited_at':None,'reactions':[],'mentions':mentioned,'files':fileobjs}
                d['messages'][mid]=msg; self.emit(d,c['id'],'message.reply' if msg['parent_id'] else 'message.created',{'message':self.message(d,msg)}); save(d); return self.send(201,{'message':self.message(d,msg)})
            return self.err(404,'not found')
    def do_PATCH(self):
        with LOCK:
            d=load(); u=self.user(d)
            if not u:return self.err(401,'unauthorized')
            p=urlparse(self.path).path; b=self.body()
            if p=='/api/users/me':
                allowed={'display_name','timezone','status_text','status_emoji','avatar_url'}
                for k in b:
                    if k not in allowed:return self.err(400,'invalid field')
                if 'display_name' in b and (not isinstance(b['display_name'],str) or not b['display_name'] or len(b['display_name'])>120):return self.err(400,'invalid display_name')
                if 'timezone' in b:
                    try: ZoneInfo(str(b['timezone']))
                    except Exception:return self.err(400,'invalid timezone')
                for k in ('status_text','status_emoji','avatar_url'):
                    if k in b and (not isinstance(b[k],str) or len(b[k])>500):return self.err(400,'invalid field')
                for k in allowed:
                    if k in b:u[k]=b[k]
                ev={'type':'user.updated','user':self.objuser(u)}
                d.setdefault('global_events',[]).append(ev); save(d)
                return self.send(200,{'user':self.objuser(u)})
            m=re.match(r'/api/workspaces/([^/]+)$',p)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                role=d['members'].get(w['id'],{}).get(str(u['id']))
                if role not in ('owner','admin'):return self.err(403,'forbidden')
                if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.err(400,'invalid join_mode')
                if 'name' in b:
                    if not isinstance(b['name'],str) or not b['name'] or len(b['name'])>120:return self.err(400,'invalid name')
                    w['name']=b['name']
                if 'join_mode' in b:w['join_mode']=b['join_mode']
                save(d); return self.send(200,{'workspace':w})
            m=re.match(r'/api/channels/(\d+)$',p)
            if m:
                c=d['channels'].get(m.group(1))
                if not c:return self.err(404,'not found')
                w=next(x for x in d['workspaces'].values() if x['id']==c['workspace_id'])
                if str(u['id']) not in c.get('members',{}) and str(u['id'])!=str(w['owner_id']):return self.err(403,'forbidden')
                if 'topic' in b and (not isinstance(b['topic'],str) or len(b['topic'])>250):return self.err(400,'invalid topic')
                if c.get('is_archived'):return self.err(423,'archived')
                if 'topic' in b:c['topic']=b['topic']; self.emit(d,c['id'],'channel.updated')
                save(d); return self.send(200,{'channel':self.channel(d,c)})
            m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
            if m:
                w=next((x for x in d['workspaces'].values() if x['slug']==m.group(1)),None)
                g=next((x for x in d.get('groups',{}).values() if x['workspace_id']==(w or {}).get('id') and x['handle']==m.group(2)),None)
                if not w or not g:return self.err(404,'not found')
                if str(u['id']) not in (str(g['creator_id']),str(w['owner_id'])):return self.err(403,'forbidden')
                if 'name' in b:g['name']=b['name']
                if 'user_ids' in b:g['member_user_ids']=[int(x) for x in b['user_ids'] if str(x) in d['members'].get(w['id'],{})]
                save(d); return self.send(200,{'group':{k:v for k,v in g.items() if k not in ('workspace_id','creator_id')}})
            m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                mem=d['members'].get(w['id'],{}); actor=mem.get(str(u['id'])); target=m.group(2)
                if target not in mem:return self.err(404,'not found')
                role=b.get('role')
                if role not in ('owner','admin','member','guest'):return self.err(400,'invalid role')
                if role=='owner':return self.err(400,'invalid role')
                if target==str(w['owner_id']) or mem.get(target)=='owner':return self.err(403,'forbidden')
                if actor not in ('owner','admin') or (actor=='admin' and mem.get(target)=='admin'):return self.err(403,'forbidden')
                mem[target]=role; save(d); return self.send(200,{'member':{'user_id':int(target),'role':role}})
            m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                mem=d['members'].get(w['id'],{}); target=str(b.get('new_owner_id'))
                if str(u['id'])!=str(w['owner_id']):return self.err(403,'forbidden')
                if mem.get(target)!='admin':return self.err(400,'target must be admin')
                old=str(w['owner_id']); mem[old]='admin'; mem[target]='owner'; w['owner_id']=int(target); save(d)
                return self.send(200,{'workspace':w})
            m=re.match(r'/api/messages/(\d+)$',p)
            if m and m.group(1) in d['messages']:
                x=d['messages'][m.group(1)]
                if x.get('deleted'):return self.err(409,'deleted')
                if x['author_id']!=u['id']:return self.err(403,'forbidden')
                if not isinstance(b.get('body'),str) or not b.get('body'):return self.err(400,'empty')
                x['body']=b['body']; x['edited_at']=now()
                c=d['channels'].get(x['channel_id'])
                if c:self.emit(d,c['id'],'message.updated',{'message':self.message(d,x)})
                save(d); return self.send(200,{'message':self.message(d,x)})
            return self.err(404,'not found')
    def do_DELETE(self):
        with LOCK:
            d=load(); u=self.user(d)
            if not u:return self.err(401,'unauthorized')
            m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',urlparse(self.path).path)
            if m:
                w=next((w for w in d['workspaces'].values() if w['slug']==m.group(1)),None)
                if not w:return self.err(404,'not found')
                if d['members'].get(w['id'],{}).get(str(u['id'])) not in ('owner','admin'):return self.err(403,'forbidden')
                if m.group(2) not in d.get('invitations',{}):return self.err(404,'not found')
                del d['invitations'][m.group(2)]; save(d); return self.send(200,{'deleted':True})
            m=re.match(r'/api/messages/(\d+)/pin$',urlparse(self.path).path)
            if m:
                mid=m.group(1)
                if mid not in d['messages']:return self.err(404,'not found')
                c=d['channels'].get(d['messages'][mid]['channel_id'])
                if not c or str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                d.get('pins',{}).pop(mid,None); save(d); return self.send(200,{'unpinned':True})
            m=re.match(r'/api/messages/(\d+)/reactions/([^/]+)$',urlparse(self.path).path)
            if m:
                msg=d['messages'].get(m.group(1))
                if not msg:return self.err(404,'not found')
                emoji=unquote(m.group(2))
                c=d['channels'].get(msg['channel_id'])
                if not c:return self.err(404,'not found')
                if str(u['id']) not in c.get('members',{}):return self.err(403,'forbidden')
                rx=next((r for r in msg.setdefault('reactions',[]) if r['emoji']==emoji),None); changed=False
                if rx and str(u['id']) in [str(x) for x in rx['user_ids']]:
                    rx['user_ids']=[x for x in rx['user_ids'] if str(x)!=str(u['id'])]; rx['count']=len(rx['user_ids']); changed=True
                    if rx['count']==0:msg['reactions'].remove(rx)
                if changed:self.emit(d,c['id'],'reaction.removed',{'message_id':int(msg['id']),'emoji':emoji,'user_id':int(u['id']),'reactions':msg['reactions']})
                save(d); return self.send(200,{'reactions':msg['reactions']})
            m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',urlparse(self.path).path)
            if m:
                w=next((x for x in d['workspaces'].values() if x['slug']==m.group(1)),None)
                g=next((x for x in d.get('groups',{}).values() if x['workspace_id']==(w or {}).get('id') and x['handle']==m.group(2)),None)
                if not w:return self.err(404,'not found')
                if not g:
                    key=str(w['id'])+':'+m.group(2)
                    if str(u['id'])==str(w['owner_id']) or str(u['id'])==str(d.get('deleted_groups',{}).get(key,'')):
                        return self.send(200,{'deleted':True})
                    return self.err(403,'forbidden')
                if str(u['id']) not in (str(g['creator_id']),str(w['owner_id'])):return self.err(403,'forbidden')
                key=str(w['id'])+':'+m.group(2)
                d.setdefault('deleted_groups',{})[key]=g['creator_id']
                d['groups'].pop(g['id'],None); save(d); return self.send(200,{'deleted':True})
            m=re.match(r'/api/messages/(\d+)$',urlparse(self.path).path)
            if m and m.group(1) in d['messages']:
                x=d['messages'][m.group(1)]
                if x.get('deleted'):return self.send(200,{'deleted':True})
                c=d['channels'].get(x['channel_id']); w=next((z for z in d['workspaces'].values() if z['id']==(c or {}).get('workspace_id')),None)
                if x['author_id']!=u['id'] and (not w or str(w['owner_id'])!=str(u['id'])):return self.err(403,'forbidden')
                x['deleted']=True
                if c:self.emit(d,c['id'],'message.deleted',{'message_id':int(x['id'])})
                save(d); return self.send(200,{'deleted':True})
            return self.err(404,'not found')
    def do_OPTIONS(self): self.send(204)
HTML=b'''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.app{display:flex;height:100vh}.side{width:250px;background:#3f0e40;color:white;padding:18px}.main{padding:30px;flex:1}.row{padding:8px;border-bottom:1px solid #ddd}</style></head><body><div id="app"></div><script>
const A='/api';let token=localStorage.getItem('huddle.token'),state={};
function api(p,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+token,'Content-Type':'application/json'};return fetch(A+p,o).then(r=>r.json())}
function auth(){app.innerHTML='<main class=main><h1>Huddle</h1><form data-testid=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-submit data-button-role=primary>Sign in</button></form><button data-testid=auth-toggle class=secondary>Sign up</button></main>';document.querySelector('form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),r=await api('/auth/login',{method:'POST',body:JSON.stringify({username:f.get('username'),password:f.get('password')})});if(r.token){token=r.token;localStorage.setItem('huddle.token',token);home()}else alert(r.error)}};async function home(){let r=await api('/workspaces');app.innerHTML='<div class=app><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=channel-list></div><button data-testid=logout-btn class=danger>Logout</button></aside><section class=main><h1>Welcome</h1><p data-testid=empty-state-create-workspace>No channels yet</p></section></div>';for(let w of r.workspaces){let d=await api('/workspaces/'+w.slug);document.querySelector('[data-testid=channel-list]').innerHTML+=d.channels.map(c=>'<div class=row data-testid=channel-entry data-channel-id='+c.id+' data-channel-name='+c.name+'>#'+c.name+'</div>').join('')}document.querySelector('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token');auth()}}token?home():auth();
</script></body></html>'''
if __name__=='__main__': ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
