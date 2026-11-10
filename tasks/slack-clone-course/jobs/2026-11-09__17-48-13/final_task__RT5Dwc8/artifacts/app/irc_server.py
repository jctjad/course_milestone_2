import socket,threading,sqlite3,urllib.parse,time,os,json
DB='/var/lib/huddle/huddle.db'; clients=[]; lock=threading.Lock()
def db():
 c=sqlite3.connect(DB,timeout=10,isolation_level=None);c.row_factory=sqlite3.Row;return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def user(token):
 c=db();r=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(token,)).fetchone();c.close();return r
def channel(name):
 if not name.startswith('#') or '/' not in name:return None
 a,b=name[1:].split('/',1);c=db();r=c.execute('select ch.*,w.slug,w.owner_id from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(a,b)).fetchone();c.close();return r
def emit(c,cid,payload):
 n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0]
 c.execute('insert into events(channel_id,event_id,type,payload,created_at) values(?,?,?,?,?)',(cid,n,'message.created',payload,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))
def broadcast(cid,line,exclude=None):
 with lock:
  for x in list(clients):
   if x is not exclude or True:
    if x.get('cid')==cid:send(x['s'],line)
def relay():
 while True:
  time.sleep(.08)
  with lock: active=list(clients)
  for st in active:
   cid=st.get('cid')
   if not cid or not st.get('u'): continue
   c=db()
   rows=c.execute("select event_id,payload from events where channel_id=? and event_id>? and type='message.created' order by event_id",(cid,st.get('last',0))).fetchall()
   for r in rows:
    try:
     m=json.loads(r['payload']); body=m.get('body',''); nick=m.get('author',{}).get('username',str(m.get('author_id','user')))
     send(st['s'],':%s!%s@localhost PRIVMSG %s :%s'%(nick,nick,st.get('ircchan','#channel'),body))
    except: pass
    st['last']=r['event_id']
   c.close()
def handle(s,addr):
 st={'s':s,'cid':None,'nick':None,'u':None,'last':0};token=None
 with lock:clients.append(st)
 try:
  buf=''
  while True:
   z=s.recv(4096)
   if not z:break
   buf+=z.decode(errors='ignore')
   while '\n' in buf:
    line,buf=buf.split('\n',1); line=line.strip('\r ')
    if not line:continue
    q=line.split(); cmd=q[0].upper()
    if cmd=='PASS':token=q[1] if len(q)>1 else ''
    elif cmd=='NICK':
     nick=q[1] if len(q)>1 else ''
     with lock: collision=any(x.get('nick')==nick and x is not st for x in clients)
     if collision:send(s,':huddle 433 * '+nick+' :Nickname is already in use');continue
     st['nick']=nick
    elif cmd=='USER':
     if not user(token):send(s,':huddle 464 * :Password incorrect');continue
     st['u']=user(token); n=st['nick'] or st['u']['username']
     for code,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1'),('005','CHANTYPES=# :are supported')]:send(s,':huddle '+code+' '+n+' :'+text)
     send(s,':huddle 422 '+n+' :MOTD File is missing')
    elif cmd=='PING':send(s,':huddle PONG huddle '+(' '.join(q[1:]) if len(q)>1 else ''))
    elif cmd=='QUIT':return
    elif not st['u']: continue
    elif cmd=='JOIN' and len(q)>1:
     ch=channel(q[1])
     if not ch:send(s,':huddle 403 '+st['nick']+' '+q[1]+' :No such channel');continue
     st['cid']=ch['id']; st['ircchan']=q[1]; c=db(); st['last']=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(ch['id'],)).fetchone()[0]; c.execute('insert or ignore into cm values(?,?)',(ch['id'],st['u']['id']));c.commit();c.close()
     pref=st['nick']+'!'+st['u']['username']+'@localhost';send(s,':'+pref+' JOIN '+q[1]);send(s,':huddle 353 '+st['nick']+' = '+q[1]+' :'+st['nick']);send(s,':huddle 366 '+st['nick']+' '+q[1]+' :End of NAMES list')
    elif cmd=='PRIVMSG' and len(q)>2:
     ch=channel(q[1])
     if not ch:continue
     body=line.split(' :',1)[1] if ' :' in line else ' '.join(q[2:])
     c=db();c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch['id'],st['u']['id'],body,None,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())));mid=c.execute('select last_insert_rowid()').fetchone()[0];payload='{"id":%d,"channel_id":%d,"author_id":%d,"body":%s}'%(mid,ch['id'],st['u']['id'],__import__('json').dumps(body));emit(c,ch['id'],payload);c.close();broadcast(ch['id'],':'+st['nick']+'!'+st['u']['username']+'@localhost PRIVMSG '+q[1]+' :'+body)
    elif cmd=='JOIN':pass
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):pass
    else:send(s,':huddle 421 '+st['nick']+' '+cmd+' :Unknown command')
 finally:
  with lock:
   if st in clients:clients.remove(st)
  try:s.close()
  except:pass
def main():
 threading.Thread(target=relay,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  x,a=s.accept();threading.Thread(target=handle,args=(x,a),daemon=True).start()
if __name__=='__main__':main()
