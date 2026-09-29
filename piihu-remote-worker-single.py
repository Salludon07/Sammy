#!/usr/bin/env python3
# PIIHU REMOTE WORKER - SINGLE FILE
# No extra Python packages are required.
# Upload this ONE file to GitHub and deploy it as a Render Web Service.

import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "10000"))
TOKEN = os.environ.get("PIIHU_REMOTE_TOKEN", "change-this-token")
ROOT = Path(os.environ.get("PIIHU_WORKDIR", "/tmp/piihu-remote-project"))
ROOT.mkdir(parents=True, exist_ok=True)

STATE = {
    "proc": None,
    "status": "IDLE",
    "output": "",
    "started_at": None,
    "lock": threading.RLock(),
}


def log(s):
    with STATE["lock"]:
        STATE["output"] = (STATE["output"] + str(s))[-200000:]


def auth(h):
    # For a quick first deployment, the default token works.
    # Change PIIHU_REMOTE_TOKEN in Render Environment Variables for security.
    return h.headers.get("X-PIIHU-TOKEN", "") == TOKEN


def stop_locked():
    p = STATE.get("proc")
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    STATE["proc"] = None


def run_project():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PIIHU_REMOTE_WORKER"] = "1"
    try:
        p = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        with STATE["lock"]:
            STATE.update(proc=p, status="RUNNING", started_at=time.time(), output="")
        log(f"[REMOTE] START PID={p.pid}\n")
        for line in iter(p.stdout.readline, ""):
            if not line:
                break
            log(line)
        rc = p.wait()
        with STATE["lock"]:
            STATE["status"] = "STOPPED" if rc == 0 else "CRASHED"
            STATE["proc"] = None
        log(f"[REMOTE] EXIT code={rc}\n")
    except Exception as e:
        with STATE["lock"]:
            STATE["status"] = "START FAILED"
            STATE["proc"] = None
        log(f"[REMOTE] ERROR {type(e).__name__}: {e}\n")


def deploy_zip(encoded):
    import io
    import zipfile
    raw = base64.b64decode(encoded or "")
    tmp = ROOT.parent / (ROOT.name + "-incoming")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        base = tmp.resolve()
        for info in z.infolist():
            target = (tmp / info.filename).resolve()
            if target != base and base not in target.parents:
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
    if not (tmp / "main.py").is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("Uploaded project does not contain main.py")
    with STATE["lock"]:
        stop_locked()
    old = ROOT.parent / (ROOT.name + "-old")
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if ROOT.exists():
        ROOT.rename(old)
    tmp.rename(ROOT)
    shutil.rmtree(old, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "PIIHU-Remote-Worker/1.0"

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path == "/health":
            with STATE["lock"]:
                p = STATE.get("proc")
                self._send(200, {
                    "ok": True,
                    "service": "PIIHU REMOTE WORKER",
                    "status": STATE["status"],
                    "pid": p.pid if p else None,
                    "project": (ROOT / "main.py").is_file(),
                })
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not auth(self):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 120 * 1024 * 1024:
            self._send(413, {"ok": False, "error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        if self.path == "/remote/api/deploy":
            try:
                deploy_zip(payload.get("zip_b64", ""))
                self._send(200, {"ok": True, "status": "DEPLOYED"})
            except Exception as e:
                self._send(400, {"ok": False, "error": str(e)})
            return

        if self.path == "/remote/api/start":
            with STATE["lock"]:
                p = STATE.get("proc")
                if p and p.poll() is None:
                    self._send(200, {"ok": True, "status": "RUNNING", "pid": p.pid})
                    return
                if not (ROOT / "main.py").is_file():
                    self._send(400, {"ok": False, "error": "main.py has not been deployed"})
                    return
            threading.Thread(target=run_project, daemon=True).start()
            self._send(200, {"ok": True, "status": "STARTING"})
            return

        if self.path == "/remote/api/stop":
            with STATE["lock"]:
                stop_locked()
                STATE["status"] = "STOPPED"
            self._send(200, {"ok": True, "status": "STOPPED"})
            return

        if self.path == "/remote/api/status":
            with STATE["lock"]:
                p = STATE.get("proc")
                self._send(200, {
                    "ok": True,
                    "status": STATE["status"],
                    "pid": p.pid if p else None,
                    "output": STATE["output"][-20000:],
                })
            return

        self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        # Keep Render logs useful without duplicating every HTTP request excessively.
        print("[HTTP] " + (fmt % args), flush=True)


def main():
    print("PIIHU REMOTE WORKER starting", flush=True)
    print(f"Listening on 0.0.0.0:{PORT}", flush=True)
    if TOKEN == "change-this-token":
        print("WARNING: using default token. Set PIIHU_REMOTE_TOKEN in Render Environment Variables.", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with STATE["lock"]:
            stop_locked()
        server.server_close()


if __name__ == "__main__":
    main()
