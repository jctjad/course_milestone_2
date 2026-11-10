#!/usr/bin/env python3
"""Minimal IRC test client for the component-E checks (standard library only).

Usage (the gateway is 127.0.0.1:6667):
  irc.py register TOKEN NICK              -> numerics received after PASS/NICK/USER, e.g. "001 002 003 004 005 422"
  irc.py badpass NICK                     -> numerics received after a made-up PASS, e.g. "464"
  irc.py collision TOKEN1 TOKEN2 NICK     -> second client takes the same nick: "<numerics> alive|closed"
  irc.py misc TOKEN NICK                  -> unknown command, PING, QUIT: "unknown=<numerics> ping=<PONG|none> quit=<clean|ERROR|open>"
  irc.py join TOKEN NICK CHANNEL          -> what JOIN returned, e.g. "JOIN 353 366" or "403"
  irc.py cmds TOKEN NICK CHANNEL          -> NAMES/WHO/LIST/TOPIC/MODE after JOIN: "421s=<count>"
  irc.py listen TOKEN NICK CHANNEL SECONDS OUT  -> joins, writes "joined" then every PRIVMSG line to OUT
  irc.py say TOKEN NICK CHANNEL TEXT      -> joins, sends PRIVMSG, quits: "sent" or what went wrong

Not run directly by the agent; the check scripts call it. Errors print one line starting with "ERROR".
"""
import socket, sys, time

HOST, PORT = "127.0.0.1", 6667


class Irc:
    def __init__(self, timeout=3.0):
        self.s = socket.create_connection((HOST, PORT), timeout=timeout)
        self.buf = b""
        self.closed = False

    def send(self, line):
        self.s.sendall((line + "\r\n").encode())

    def lines(self, seconds, stop=None):
        """Yields lines for up to `seconds`; stops after a line for which stop(line) is true."""
        end = time.time() + seconds
        while time.time() < end and not self.closed:
            while b"\r\n" in self.buf or b"\n" in self.buf:
                sep = b"\r\n" if b"\r\n" in self.buf else b"\n"
                raw, self.buf = self.buf.split(sep, 1)
                line = raw.decode(errors="replace")
                yield line
                if stop and stop(line):
                    return
            self.s.settimeout(max(end - time.time(), 0.01))
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                return
            except OSError:
                self.closed = True
                return
            if not chunk:
                self.closed = True
                return
            self.buf += chunk

    def register(self, token, nick):
        if token:
            self.send(f"PASS {token}")
        self.send(f"NICK {nick}")
        self.send(f"USER {nick} 0 * :{nick}")
        return [numeric(l) for l in self.lines(3, stop=lambda l: numeric(l) in ("422", "376", "464"))
                if numeric(l)]

    def join(self, chan):
        self.send(f"JOIN {chan}")
        got = []
        for l in self.lines(3, stop=lambda l: numeric(l) in ("366", "403")):
            n = numeric(l)
            parts = l.split()
            if len(parts) > 1 and parts[1].upper() == "JOIN":
                got.append("JOIN")
            elif n in ("353", "366", "403", "332", "331"):
                got.append(n)
        return got


def numeric(line):
    parts = line.split()
    if parts and parts[0].startswith(":"):
        parts = parts[1:]
    return parts[0] if parts and parts[0].isdigit() and len(parts[0]) == 3 else ""


def main(argv):
    cmd = argv[1]
    if cmd == "register":
        c = Irc(); print(" ".join(c.register(argv[2], argv[3])))
    elif cmd == "badpass":
        c = Irc(); print(" ".join(c.register("not-a-real-token-000000", argv[2])) or "none")
    elif cmd == "collision":
        a = Irc(); a.register(argv[2], argv[4])
        b = Irc(); got = b.register(argv[3], argv[4])
        b.send("PING :still-here")
        alive = any("PONG" in l and "still-here" in l for l in b.lines(2, stop=lambda l: "PONG" in l))
        print(" ".join(got) + (" alive" if alive and not b.closed else " closed"))
    elif cmd == "misc":
        c = Irc(); c.register(argv[2], argv[3])
        c.send("FROBNICATE now")
        unknown = [numeric(l) for l in c.lines(2, stop=lambda l: numeric(l) == "421") if numeric(l)]
        c.send("PING :abc123")
        pong = any("PONG" in l and "abc123" in l for l in c.lines(2, stop=lambda l: "PONG" in l))
        c.send("QUIT :bye")
        after = list(c.lines(2))
        quit_state = "ERROR" if any(l.startswith("ERROR") or numeric(l) for l in after) else ("clean" if c.closed else "open")
        print(f"unknown={' '.join(unknown) or 'none'} ping={'PONG' if pong else 'none'} quit={quit_state}")
    elif cmd == "join":
        c = Irc(); c.register(argv[2], argv[3]); print(" ".join(c.join(argv[4])) or "none")
    elif cmd == "cmds":
        c = Irc(); c.register(argv[2], argv[3]); c.join(argv[4])
        for line in (f"NAMES {argv[4]}", f"WHO {argv[4]}", "LIST", f"TOPIC {argv[4]}", f"MODE {argv[4]}"):
            c.send(line)
        n421 = sum(1 for l in c.lines(2) if numeric(l) == "421")
        print(f"421s={n421}")
    elif cmd == "listen":
        token, nick, chan, secs, out = argv[2], argv[3], argv[4], float(argv[5]), argv[6]
        c = Irc(); c.register(token, nick); joined = c.join(chan)
        with open(out, "w") as fh:
            fh.write(("joined" if "366" in joined else "not-joined " + " ".join(joined)) + "\n"); fh.flush()
            for l in c.lines(secs):
                if " PRIVMSG " in l:
                    fh.write(l + "\n"); fh.flush()
    elif cmd == "say":
        c = Irc(); c.register(argv[2], argv[3]); joined = c.join(argv[4])
        if "366" not in joined:
            print("not-joined " + " ".join(joined)); return
        c.send(f"PRIVMSG {argv[4]} :{argv[5]}"); time.sleep(0.5); c.send("QUIT :done"); list(c.lines(1))
        print("sent")


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception as e:  # report, never hang
        print(f"ERROR {type(e).__name__}: {e}")
