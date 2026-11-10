import socket,threading,sqlite3,json,datetime,time
DB='/var/lib/huddle/huddle.db'; clients=[]; lock=threading.Lock(); sent=set()
def db():
 c=sqlite3.connect(DB,timeout=30); c.execute('PRAGMA busy_timeout=30000'); c.row_factory=sqlite3.Row; return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def message_json(c,r):
 u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':{k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')},'body':r['body'],'parent_id':r['parent_id'],'reply_count':0,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
def pump():
 last=0
 while True:
  try:
   c=db(); rows=c.execute('select e.*,m.author_id,m.id mid,m.body,m.created_at,m.parent_id from events e left join messages m on e.kind in ("message.created","message.reply") and json_extract(e.payload,"$.message.id")=m.id where e.id>? order by e.id',(last,)).fetchall()
   for e in rows:
    last=e['id']; d=json.loads(e['payload']); m=None
    if e['mid']:
     m=message_json(c,e); d['message']=m
    if e['kind'] in ('message.created','message.reply') and m:
     u=m['author']['username']; c2=c.execute('select w.slug,ch.name from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(e['channel_id'],)).fetchone(); name='#'+c2['slug']+'/'+c2['name']
     with lock:
      for s,nick,uid,joined in list(clients):
       if e['channel_id'] in joined and not (uid==m['author_id'] and s in sent):
        send(s,f":{u}!{u}@huddle PRIVMSG {name} :{m['body']}")
      sent.discard(next(iter(sent),None)) if False else None
   c.close()
  except: pass
  time.sleep(.1)
def client(s):
 uid=None; nick=None; joined={}; reader=s.makefile('r',newline='')
 try:
  while True:
   line=reader.readline()
   if not line:break
   a=line.strip().split(' ',2); cmd=a[0].upper()
   if cmd=='PASS':
    c=db(); r=c.execute('select user_id from tokens where token=?',(a[1] if len(a)>1 else '',)).fetchone(); uid=r[0] if r else None; c.close()
    if not uid:send(s,':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    n=a[1] if len(a)>1 else ''
    with lock:
     if any(x[1]==n for x in clients):send(s,f':huddle 433 * {n} :Nickname in use'); continue
    nick=n
   elif cmd=='USER' and uid and nick:
    with lock:clients.append((s,nick,uid,joined))
    for code,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server'),('004','huddle 1'),('005','CHANTYPES=#'),('422','MOTD File is missing')]:send(s,f':huddle {code} {nick} :{text}')
   elif cmd=='PING':send(s,'PONG '+(a[1] if len(a)>1 else ':ping'))
   elif cmd=='JOIN' and uid:
    name=a[1]; bits=name[1:].split('/')
    c=db(); x=c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(bits[0],bits[1] if len(bits)>1 else '')).fetchone(); c.close()
    if not x:send(s,f':huddle 403 {nick} {name} :No such channel')
    else:
     joined[x['id']]=x['id']; send(s,f':{nick}!{nick}@huddle JOIN {name}'); send(s,f':huddle 353 {nick} = {name} :{nick}'); send(s,f':huddle 366 {nick} {name} :End of NAMES')
   elif cmd=='PRIVMSG' and uid and len(a)>2:
    name=a[1]; bits=name[1:].split('/'); body=a[2].lstrip(':'); c=db(); x=c.execute('select ch.* from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(bits[0],bits[1] if len(bits)>1 else '')).fetchone()
    if x:
     ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'); c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(x['id'],uid,body,ts)); r=c.execute('select * from messages where id=last_insert_rowid()').fetchone(); m=message_json(c,r); n=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(x['id'],)).fetchone()['n']; c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(x['id'],n,'message.created',json.dumps({'message':m}))); c.commit(); c.close()
   elif cmd=='QUIT':break
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):pass
   else:send(s,f':huddle 421 {nick or "*"} {cmd} :Unknown command')
 except:pass
 finally:
  with lock:clients[:]=[x for x in clients if x[0]!=s]
  try:reader.close()
  except:pass
  s.close()
def run():
 threading.Thread(target=pump,daemon=True).start(); z=socket.socket(); z.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); z.bind(('0.0.0.0',6667)); z.listen()
 while True:threading.Thread(target=client,args=(z.accept()[0],),daemon=True).start()
if __name__=='__main__':run()
