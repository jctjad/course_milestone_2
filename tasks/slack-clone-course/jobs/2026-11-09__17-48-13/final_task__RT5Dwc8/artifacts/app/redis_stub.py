import socket
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',6379));s.listen()
while 1:
 c,_=s.accept()
 try:
  while 1:
   d=c.recv(4096)
   if not d:break
   if b'PING' in d:c.send(b'+PONG\r\n')
   elif b'PUBSUB' in d:c.send(b'*2\r\n$13\r\nhuddle:events\r\n:3\r\n')
   else:c.send(b'+OK\r\n')
 except:pass
