import socket, threading, sqlite3, re, json, time
DB='/var/lib/huddle/huddle.sqlite3'
NICKS=set(); CLIENTS={}; LOCK=threading.RLock()
def conn(): c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row; return c
def user_token(t):
 with conn() as c:return c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone()
def out(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def reply(s,code,nick,text):out(s,f':huddle {code} {nick or "*"} {text}')
def find(name):
 if not name.startswith('#') or '/' not in name[1:]:return None
 slug,ch=name[1:].split('/',1)
 with conn() as c:return c.execute('select ch.*,w.slug,w.name wname from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,ch)).fetchone()
def emit(c,ch,kind,payload):
 r=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(ch,)).fetchone(); eid=r['n']
 c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(ch,eid,kind,json.dumps(payload))); return eid
def post(uid,ch,text):
 with conn() as c:
  r=c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(ch,uid,text,time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime()))); mid=r.lastrowid
  m=c.execute('select * from messages where id=?',(mid,)).fetchone()
  u=c.execute('select * from users where id=?',(uid,)).fetchone()
  msg={'id':mid,'channel_id':ch,'author_id':uid,'author':{'id':u['id'],'username':u['username'],'display_name':u['display_name'],'timezone':u['timezone'],'avatar_url':u['avatar_url'],'status_text':u['status_text'],'status_emoji':u['status_emoji']},'body':text,'parent_id':None,'reply_count':0,'created_at':m['created_at'],'edited_at':None,'files':[],'reactions':[],'mentions':[]}
  emit(c,ch,'message.created',{'message':msg})
 return msg
def pump():
 seen={}
 while True:
  try:
   with conn() as c:
    rows=list(c.execute("select e.*,ch.name cname,w.slug from events e join channels ch on ch.id=e.channel_id join workspaces w on w.id=ch.workspace_id where e.kind='message.created' order by e.rowid"))
   for r in rows:
    key=(r['channel_id'],r['event_id'])
    if key in seen:continue
    msg=json.loads(r['payload']).get('message',{}); name=f"#{r['slug']}/{r['cname']}"; nick=msg.get('author',{}).get('username','user')
    with LOCK:
     clients=list(CLIENTS.get(r['channel_id'],set()))
     for cl in clients:
      out(cl[0],f":{nick}!{nick}@huddle PRIVMSG {name} :{msg.get('body','')}")
    if clients: seen[key]=1
  except:pass
  time.sleep(.1)
def client(s):
 token=None; nick=None; uid=None; joined={}
 buf=b''
 try:
  while True:
   d=s.recv(4096)
   if not d:break
   buf+=d
   while b'\n' in buf:
    raw,buf=buf.split(b'\n',1); line=raw.rstrip(b'\r').decode(errors='replace')
    if not line:continue
    z=line.split(' ',1); cmd=z[0].upper(); rest=z[1] if len(z)>1 else ''
    if cmd=='PASS':
     token=rest.strip(); u=user_token(token)
     if not u:reply(s,'464',nick,'Password incorrect')
     else:uid=u['id']
    elif cmd=='NICK':
     n=rest.strip().lstrip(':')
     with LOCK:
      if n in NICKS and n!=nick:reply(s,'433',nick,n+' Nickname is already in use')
      elif re.fullmatch(r'[A-Za-z0-9_-]{1,30}',n):
       if nick:NICKS.discard(nick)
       nick=n;NICKS.add(nick)
    elif cmd=='USER':
     if uid and nick:
      for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 oiwsz biklmnop'),('005','CHANTYPES=# PREFIX=(ov)@+')]:reply(s,code,nick,text)
      reply(s,'422',nick,'MOTD File is missing')
    elif cmd=='PING':out(s,':huddle PONG '+rest)
    elif cmd=='QUIT':return
    elif cmd=='JOIN':
     name=rest.split(',')[0].strip(); ch=find(name)
     if not ch or ch['is_private']:reply(s,'403',nick,name+' No such channel');continue
     with conn() as c:c.execute('insert or ignore into channel_members values(?,?)',(ch['id'],uid))
     joined[ch['id']]=name
     with LOCK:CLIENTS.setdefault(ch['id'],set()).add((s,nick,uid))
     out(s,f':{nick}!{nick}@huddle JOIN {name}')
     with conn() as c:
      names=[x['username'] for x in c.execute('select u.username from users u join channel_members m on m.user_id=u.id where m.channel_id=?',(ch['id'],))]
     reply(s,'353',nick,f'= {name} :{" ".join(names)}');reply(s,'366',nick,name+' End of NAMES list')
    elif cmd in ('NAMES','WHO','TOPIC','MODE'):
     name=rest.split()[0] if rest else next(iter(joined.values()),'')
     if cmd=='NAMES':reply(s,'353',nick,f'= {name} :{nick}');reply(s,'366',nick,name+' End of NAMES list')
    elif cmd=='LIST':reply(s,'323',nick,'End of LIST')
    elif cmd=='PRIVMSG':
     if ' :' not in rest:continue
     target,text=rest.split(' :',1); ch=find(target.strip())
     if ch and ch['id'] in joined:post(uid,ch['id'],text)
     elif ch:reply(s,'404',nick,target+' Cannot send to channel')
    elif cmd not in ('CAP','PONG'):reply(s,'421',nick or '*',cmd+' Unknown command')
 finally:
  with LOCK:
   if nick:NICKS.discard(nick)
   for cid in joined:CLIENTS.get(cid,set()).difference_update({x for x in CLIENTS.get(cid,set()) if x[0] is s})
  try:s.close()
  except:pass
def irc_server():
 threading.Thread(target=pump,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  c,a=s.accept();threading.Thread(target=client,args=(c,),daemon=True).start()
if __name__=='__main__':irc_server()
