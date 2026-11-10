#!/usr/bin/env python3
"""Minimal WebSocket client for the basics checks (standard library only).

Usage:
  ws.py reject URL                              -> prints "rejected" or "accepted"
  ws.py head URL CHANNEL_ID                      -> prints head_event_id from "subscribed"
  ws.py listen URL CHANNEL_ID COUNT SECONDS OUT  -> subscribes, writes every frame to OUT as one JSON
                                                   line, exits after COUNT event frames or SECONDS;
                                                   skips events at or below head_event_id (they
                                                   happened before the subscribe)
  ws.py resume URL CHANNEL_ID SINCE SECONDS      -> sends resume, prints frames until resumed/resume.gap
  ws.py watch URL SECONDS OUT                    -> connects without subscribing, writes {"_ws": "open"} and
                                                   then every frame to OUT as one JSON line, for SECONDS

Not run directly by the agent; the check scripts call it. Errors are printed as one line starting
with "ERROR" so the checks can report them.
"""
import base64, json, os, socket, struct, sys, time
from urllib.parse import urlparse


class Rejected(Exception):
    pass


class Conn:
    def __init__(self, url, timeout=3.0):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        path = u.path + ("?" + u.query if u.query else "")
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Rejected("closed during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101" not in status:
            raise Rejected(status)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout):
        """Returns the next text frame as a dict, or None on timeout. Control frames (ping, pong)
        never extend the wait: the deadline holds even if the server keeps sending pings."""
        end = time.time() + max(timeout, 0.01)
        try:
            while True:
                left = end - time.time()
                if left <= 0:
                    return None
                self.sock.settimeout(left)
                b0, b1 = self._read(2)
                op, n = b0 & 0x0F, b1 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                data = self._read(n)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if op == 0x8:
                    raise EOFError("server closed the connection")
                if op == 0x9:  # ping -> pong
                    self.sock.sendall(struct.pack("!BB", 0x8A, 0x80) + b"\0\0\0\0")
                    continue
                if op in (0x1, 0x0):
                    return json.loads(data.decode())
        except socket.timeout:
            return None


def main(argv):
    cmd, url = argv[1], argv[2]
    try:
        if cmd == "reject":
            try:
                Conn(url)
                print("accepted")
            except (Rejected, ConnectionError, EOFError, socket.timeout):
                print("rejected")
            return
        c = Conn(url)
        if cmd == "watch":
            secs, out = float(argv[3]), argv[4]
            with open(out, "w") as fh:
                fh.write(json.dumps({"_ws": "open"}) + "\n")
                fh.flush()
                deadline = time.time() + secs
                while time.time() < deadline:
                    f = c.recv(deadline - time.time())
                    if f is not None:
                        fh.write(json.dumps(f) + "\n")
                        fh.flush()
            return
        cid = json.loads(argv[3]) if argv[3].lstrip("-").isdigit() else argv[3]
        if cmd == "head":
            c.send({"type": "subscribe", "channel_id": cid})
            deadline = time.time() + 3
            while time.time() < deadline:
                f = c.recv(deadline - time.time())
                if f and f.get("type") == "subscribed":
                    print(f.get("head_event_id"))
                    return
            print("ERROR no subscribed frame within 3s")
        elif cmd == "listen":
            count, secs, out = int(argv[4]), float(argv[5]), argv[6]
            with open(out, "w") as fh:
                c.send({"type": "subscribe", "channel_id": cid})
                deadline, got, head = time.time() + secs, 0, None
                while time.time() < deadline and got < count:
                    f = c.recv(deadline - time.time())
                    if f is None:
                        continue
                    if f.get("type") == "subscribed" and head is None:
                        head = f.get("head_event_id")
                    elif isinstance(head, int) and isinstance(f.get("event_id"), int) and f["event_id"] <= head:
                        continue  # happened before the subscribe; already counted in head_event_id
                    fh.write(json.dumps(f) + "\n")
                    fh.flush()
                    if f.get("type") not in ("subscribed", "resumed", "resume.gap", "error"):
                        got += 1
        elif cmd == "resume":
            since, secs = int(argv[4]), float(argv[5])
            c.send({"type": "resume", "channel_id": cid, "since_event_id": since})
            deadline = time.time() + secs
            while time.time() < deadline:
                f = c.recv(deadline - time.time())
                if f is None:
                    continue
                print(json.dumps(f))
                if f.get("type") in ("resumed", "resume.gap"):
                    return
    except Exception as e:  # report, never hang
        print(f"ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(sys.argv)
