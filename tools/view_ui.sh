#!/usr/bin/env bash
# Open the app an agent built in a run, so you can click through its web UI in your own browser.
# Kept outside milestone-2-specs/, so the agent never sees it. Changes nothing in the job folder.
#
# Usage (from the repo root):
#   tools/view_ui.sh tasks/slack-clone-course/jobs-practice/<timestamp>     # a job folder
#   tools/view_ui.sh <job folder> 8080                                       # use another local port
# Then open http://localhost:8000 (or your port). Press Ctrl-C here to stop; the app's data is thrown away.
#
# How it works: the app only listens on 127.0.0.1 inside its container, so a tiny forwarder inside the
# container passes your browser's traffic (including WebSockets) through to port 8000.
set -euo pipefail
SRC=${1:?usage: tools/view_ui.sh <job folder or app folder> [port]}
PORT=${2:-8000}
IMAGE=${VIEW_UI_IMAGE:-slack-clone-prewarm}

if [ -f "$SRC/start.sh" ]; then APP=$SRC; else APP=$(ls -d "$SRC"/*/artifacts/app 2>/dev/null | head -1); fi
[ -n "${APP:-}" ] && [ -f "$APP/start.sh" ] || { echo "No app with a start.sh found under $SRC" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "Docker image '$IMAGE' not found. Build it once with:" >&2
  echo "  docker build -t $IMAGE tasks/slack-clone-course/course-staff-files/final_task/environment" >&2; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cp -R "$APP" "$TMP/app"   # a copy, so the run's artifacts stay untouched
cat > "$TMP/fwd.py" <<'PY'
import socket, threading
def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 9000)); srv.listen(64)
while True:
    c, _ = srv.accept()
    try:
        u = socket.create_connection(("127.0.0.1", 8000), timeout=5); u.settimeout(None)
    except OSError:
        c.close(); continue
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
PY

echo "Serving $APP"
echo "Open http://localhost:$PORT in your browser (it may take a few seconds to start). Ctrl-C to stop."
TTY=(); [ -t 0 ] && TTY=(-it)
docker run --rm "${TTY[@]}" --init -p "127.0.0.1:$PORT:9000" -v "$TMP/app:/app" -v "$TMP/fwd.py:/fwd.py:ro" "$IMAGE" \
  bash -c 'cd /app && (/app/start.sh >/tmp/start.log 2>&1 &); sleep 3; exec python3 /fwd.py'
