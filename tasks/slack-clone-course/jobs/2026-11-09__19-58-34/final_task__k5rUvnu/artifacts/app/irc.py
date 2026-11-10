#!/usr/bin/env python3
import socket,threading,sqlite3,json,time,os
DB='/var/lib/huddle/huddle.sqlite'
clients=[]; lock=threading.Lock()
def db():
 c=sqlite3.connect(DB,timeout=2);c.row_factory=sqlite3.Row;return c
def user_for_token(t):
 c=db()
 try:return c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone()
 finally:c.close()
def channel(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1);c=db()
 try:return c.execute('select ch.*,w.slug,w.name wname from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,cn)).fetchone()
 finally:c.close()
def ircname(ch):return '#'+ch['slug']+'/'+ch['name']
def send(c,line):
 try:c['sock'].sendall((line+'\r\n').encode())
 except OSError:pass
def event(cid,kind,payload):
 for _ in range(100):
  c=db()
  try:
   c.execute('begin immediate');n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0]
   c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(cid,n,kind,json.dumps(payload),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())));c.commit();c.close();return n
  except Exception:
   c.rollback();c.close();time.sleep(.01)
def message(uid,cid,body):
 c=db()
 try:
  ts=time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime())
  cur=c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(cid,uid,body,ts));mid=cur.lastrowid;c.commit()
 finally:c.close()
 event(cid,'message.created',{'id':mid,'channel_id':cid,'author_id':uid,'body':body,'parent_id':None,'reply_count':0,'created_at':ts,'edited_at':None,'files':[],'reactions':[],'mentions':[]})
def pump():
 seen={}
 while True:
  try:
   c=db(); rows=c.execute("select e.*,m.author_id,m.body,u.username from events e join messages m on m.id=json_extract(e.payload,'$.id') join users u on u.id=m.author_id where e.kind='message.created' order by e.id").fetchall();c.close()
   for e in rows:
    if e['id']<=seen.get(e['channel_id'],0):continue
    seen[e['channel_id']]=e['id'];ch=channel_by_id(e['channel_id'])
    if not ch:continue
    with lock: cs=list(clients)
    for x in cs:
     if e['channel_id'] in x['joined'] and x.get('last_irc_id')!=e['id']:
      send(x,':%s!%s@localhost PRIVMSG %s :%s'%(e['username'],e['username'],ircname(ch),e['body']))
  except Exception:pass
  time.sleep(.05)
def channel_by_id(cid):
 c=db()
 try:return c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(cid,)).fetchone()
 finally:c.close()
def client(sock,addr):
 st={'sock':sock,'joined':set(),'last_irc_id':None};token=None;u=None;nick=None;registered=False;buf=b''
 with lock:clients.append(st)
 try:
  while True:
   d=sock.recv(4096)
   if not d:break
   buf+=d
   while b'\r\n' in buf:
    raw,buf=buf.split(b'\r\n',1);line=raw.decode(errors='replace'); parts=line.split(' ',1);cmd=parts[0].upper();rest=parts[1] if len(parts)>1 else ''
    if cmd=='PASS':
     token=rest.strip();u=user_for_token(token)
     if not u:send(st,':huddle 464 * :Password incorrect')
    elif cmd=='NICK':
     nn=rest.strip().lstrip(':')
     with lock: collision=any(x is not st and x.get('nick')==nn for x in clients)
     if collision:send(st,':huddle 433 * '+nn+' :Nickname is already in use')
     else:nick=nn;st['nick']=nn
    elif cmd=='USER' and u and nick and not registered:
     registered=True
     for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created now'),('004','huddle 1.0 oi'),('005','NETWORK=Huddle')]:
      send(st,':huddle %s %s :%s'%(code,nick,text))
     send(st,':huddle 422 %s :MOTD File is missing'%nick)
    elif cmd=='PING':send(st,':huddle PONG '+(rest[1:] if rest.startswith(':') else rest))
    elif cmd=='QUIT':return
    elif cmd=='JOIN':
     nm=rest.split(',')[0].strip();ch=channel(nm)
     if not ch or ch['is_private']:send(st,':huddle 403 %s %s :No such channel'%(nick,nm));continue
     c=db();c.execute('insert or ignore into wm values(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('insert or ignore into cm values(?,?)',(ch['id'],u['id']));c.commit();c.close();st['joined'].add(ch['id'])
     send(st,':%s!%s@localhost JOIN %s'%(nick,u['username'],nm))
     c=db();names=[r['username'] for r in c.execute('select u.username from cm join users u on u.id=cm.user_id where cm.channel_id=?',(ch['id'],))];c.close()
     send(st,':huddle 353 %s = %s :%s'%(nick,nm,' '.join(names)));send(st,':huddle 366 %s %s :End of /NAMES list.'%(nick,nm))
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
     nm=rest.split()[0].lstrip(':') if rest else '';send(st,':huddle 324 %s %s +nt'%(nick,nm))
    elif cmd=='PRIVMSG':
     bits=rest.split(' ',1);nm=bits[0] if bits else '';body=bits[1][1:] if len(bits)>1 and bits[1].startswith(':') else (bits[1] if len(bits)>1 else '')
     ch=channel(nm)
     if ch and ch['id'] in st['joined']:message(u['id'],ch['id'],body);st['last_irc_id']=None
    elif cmd not in ('PONG','CAP'):send(st,':huddle 421 %s %s :Unknown command'%(nick or '*',cmd))
 finally:
  with lock:
   if st in clients:clients.remove(st)
  try:sock.close()
  except OSError:pass
def main():
 threading.Thread(target=pump,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  c,a=s.accept();threading.Thread(target=client,args=(c,a),daemon=True).start()
if __name__=='__main__':main()
