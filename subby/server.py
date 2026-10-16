# -*- coding: utf-8 -*-
"""Helyi webszerver (csak 127.0.0.1):
  /            az OBS böngészőforrás felirata (ugyanaz a cím, mint eddig)
  /events      a felirat élőben (Server-Sent Events: nincs 100 ms-os lekérdezési késés)
  /state       a felirat állapota JSON-ban (régi lekérdezős oldalaknak)
  /app         a beállító varázsló és a vezérlőpult
  /player      a lejátszó: a capture kártya képe, rajta a felirat
  /api/...     a felület hívásai
Védelem: csak a saját címére érkező kérést szolgálja ki (Host-ellenőrzés a DNS-rebinding ellen), az
/api/ hívásokat pedig csak a saját oldalairól fogadja el (Origin + JSON tartalomtípus)."""

import http.server
import json
import os
import threading
import urllib.parse

from . import __version__

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
           ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
           ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}
MAX_BODY = 256 * 1024


def _read_web(name):
    with open(os.path.join(WEB_DIR, name), "rb") as f:
        return f.read()


class SubtitleServer:
    def __init__(self, sink, port, api=None):
        self.sink, self.port, self.api = sink, port, api
        self.httpd = None
        self.stopping = threading.Event()
        self.error = ""

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        sink, api, stopping, port = self.sink, self.api, self.stopping, self.port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origins = {f"http://{h}" for h in hosts}

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "rksubby/" + __version__

            def log_message(self, *args):
                pass

            # ------------------------------------------------------- segédek
            def _host_ok(self):
                return (self.headers.get("Host") or "").strip().lower() in hosts

            def _origin_ok(self):
                origin = self.headers.get("Origin")
                return origin is None or origin.strip().lower() in origins

            def _send(self, code, body, ctype, cache=False):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _file(self, name):
                try:
                    body = _read_web(name)
                except OSError:
                    self.send_error(404)
                    return
                self._send(200, body, _CTYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream"),
                           cache=name.endswith(".png"))

            # ------------------------------------------------------- GET
            def do_GET(self):
                if not self._host_ok():
                    self._send(403, b"forbidden", "text/plain")
                    return
                url = urllib.parse.urlsplit(self.path)
                path = url.path
                if path in ("/", "/index.html", "/sub.html"):
                    self._file("overlay.html")
                elif path in ("/state", "/sub.json"):
                    self._json(200, sink.state())
                elif path == "/events":
                    self._sse()
                elif path in ("/app", "/app/"):
                    self._file("app.html")
                elif path in ("/player", "/player/"):
                    self._file("player.html")
                elif path.startswith("/static/"):
                    name = os.path.basename(path[len("/static/"):])   # mappát nem lehet bejárni
                    if os.path.splitext(name)[1].lower() in _CTYPES:
                        self._file(name)
                    else:
                        self.send_error(404)
                elif path == "/favicon.ico":
                    self._file("logo-64.png")
                elif path == "/api/ping":
                    self._json(200, {"app": "rksubby", "version": __version__})
                elif path.startswith("/api/") and api is not None:
                    if not self._origin_ok():
                        self._send(403, b"forbidden", "text/plain")
                        return
                    query = dict(urllib.parse.parse_qsl(url.query))
                    try:
                        code, obj = api.get(path[5:], query)
                    except Exception as e:  # a felület kapjon értelmes hibát
                        code, obj = 500, {"error": f"belső hiba: {e}"}
                    self._json(code, obj)
                else:
                    self.send_error(404)

            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(b"retry: 1000\n\n")
                    st = sink.state()
                    while not stopping.is_set():
                        self.wfile.write(("data: " + json.dumps(st, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                        v = st["v"]
                        while not stopping.is_set():
                            st = sink.wait_state(v, 15.0)
                            if st["v"] != v:
                                break
                            self.wfile.write(b"event: ping\ndata: 1\n\n")   # életjel: az oldal tudja, hogy él a kapcsolat
                            self.wfile.flush()
                except OSError:
                    pass   # bezárták a forrást / leállt az OBS

            # ------------------------------------------------------- POST
            def do_POST(self):
                if not self._host_ok() or not self._origin_ok() or not self.path.startswith("/api/") or api is None:
                    self._send(403, b"forbidden", "text/plain")
                    return
                if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                    self._send(415, b"application/json kell", "text/plain")
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY:
                    self._send(413, b"too large", "text/plain")
                    return
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    self._json(400, {"error": "hibás kérés"})
                    return
                try:
                    code, obj = api.post(urllib.parse.urlsplit(self.path).path[5:], body)
                except Exception as e:
                    code, obj = 500, {"error": f"belső hiba: {e}"}
                self._json(code, obj)

        try:
            self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            self.error = str(e)
            return False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="webszerver").start()
        return True

    def stop(self):
        self.stopping.set()
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
