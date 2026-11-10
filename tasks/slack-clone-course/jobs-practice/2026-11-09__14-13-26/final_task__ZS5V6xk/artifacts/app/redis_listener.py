import socket,time
while True:
 try:
  s=socket.create_connection(('127.0.0.1',6379),2)
  s.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle:events\r\n')
  while s.recv(4096): pass
 except Exception:
  try:s.close()
  except Exception:pass
  time.sleep(1)
