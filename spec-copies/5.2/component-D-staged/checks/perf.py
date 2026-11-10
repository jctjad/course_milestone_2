#!/usr/bin/env python3
"""Load test for check_D_4.sh (standard library only). Not run directly by the agent.

Usage: perf.py TOKEN CHANNEL_ID POSTS_PER_WRITER RATE_PER_WRITER SUBS_PER_NODE
One writer per node posts POSTS_PER_WRITER messages at RATE_PER_WRITER posts per second, all three at once,
while SUBS_PER_NODE WebSocket subscribers per node listen. Prints one line:
PERF posts_ok=A/B deliveries=C/D missing_pct=E median_ms=F p95_ms=G
"""
import json, os, sys, threading, time, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ws import Conn  # noqa: E402

PORTS = (8000, 8001, 8002)


def main(token, ch, per_writer, rate, subs_per_node):
    cid = json.loads(ch) if ch.lstrip("-").isdigit() else ch
    nonce = "perf%d" % (time.time() * 1000)
    conns = []
    for p in PORTS:
        for _ in range(subs_per_node):
            c = Conn(f"ws://127.0.0.1:{p}/api/ws?token={token}")
            c.send({"type": "subscribe", "channel_id": cid})
            end = time.time() + 3
            while time.time() < end:
                f = c.recv(end - time.time())
                if f and f.get("type") == "subscribed":
                    break
            else:
                print(f"PERF ERROR no subscribed frame on port {p}"); return
            conns.append(c)
    sent, codes, got = {}, [], [dict() for _ in conns]
    state = {"writers_done": None}

    def reader(i, c):
        while True:
            done = state["writers_done"]
            if done and time.time() > done + 2:
                return
            try:
                f = c.recv(0.2)
            except Exception:
                return
            if f and f.get("type") == "message.created":
                b = str((f.get("message") or {}).get("body", ""))
                if b.startswith(nonce):
                    got[i].setdefault(b, time.time())

    def writer(p):
        start = time.time()
        for i in range(per_writer):
            body = f"{nonce}-{p}-{i}"
            req = urllib.request.Request(f"http://127.0.0.1:{p}/api/channels/{ch}/messages",
                                         data=json.dumps({"body": body}).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            t = time.time()
            try:
                code = urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                code = e.code
            except Exception:
                code = 0
            sent[body] = (t, code)
            codes.append(code)
            time.sleep(max(0, start + (i + 1) / rate - time.time()))

    readers = [threading.Thread(target=reader, args=(i, c), daemon=True) for i, c in enumerate(conns)]
    writers = [threading.Thread(target=writer, args=(p,), daemon=True) for p in PORTS]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    state["writers_done"] = time.time()
    for t in readers:
        t.join(4)
    ok_bodies = [b for b, (_, code) in sent.items() if code == 201]
    lat, missing = [], 0
    for g in got:
        for b in ok_bodies:
            if b in g:
                lat.append((g[b] - sent[b][0]) * 1000)
            else:
                missing += 1
    expected = len(ok_bodies) * len(conns)
    lat.sort()
    med = lat[len(lat) // 2] if lat else -1
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else -1
    pct = 100.0 * missing / expected if expected else 100.0
    print(f"PERF posts_ok={len(ok_bodies)}/{len(sent)} deliveries={expected - missing}/{expected} "
          f"missing_pct={pct:.1f} median_ms={med:.0f} p95_ms={p95:.0f}")


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5]))
    except Exception as e:
        print(f"PERF ERROR {type(e).__name__}: {e}")
