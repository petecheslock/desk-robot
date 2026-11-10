"""Eyes: a camera stream (the robot's, or the browser's while it stands in
for the robot), and a live-view page for the browser.

The robot sends one JPEG per binary frame (type byte 0x02). We keep only the
newest. The dashboard's browser can push its webcam's frames the same way
(robot offline only). A tiny HTTP server (standard library, its own thread)
serves:

  /          the dashboard page (brain/dashboard.html): what Rocky sees and
              hears, plus controls for his head, face, voice and sleep. The
              launcher (server/launcher.py, port 8766) serves the same page with
              settings and a Start button, and passes the live parts through
              to this server
  /stream    multipart MJPEG — the newest frame, pushed as it changes
  /frame     the newest frame as a plain JPEG
  /status    JSON: fps, frame age, chip temperature, last transcript, and
              whatever main.py adds through `state_provider`
  /api/<x>   POST, JSON body: a control action, handed to `command_handler`
              (main.py). Only answers requests that carry the X-Rocky-Console
              header, which a cross-site page can't add without a CORS
              preflight we never grant.

One shared CONSOLE_TOKEN gates all of it (the page is opened with ?token=,
which it then carries as a header); loopback-only binds may leave it unset.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

_ROTATE_CODES = {90: "ROTATE_90_CLOCKWISE", 180: "ROTATE_180", 270: "ROTATE_90_COUNTERCLOCKWISE"}


def rotate_jpeg(jpeg: bytes, deg: int, mirror: bool = False) -> bytes:
    """Turn a JPEG `deg` degrees clockwise (0 / 90 / 180 / 270), then mirror it left-right if asked. The camera sits
    on its side in the V2 head and the sensor can only flip, not turn, so frames are turned here. Returns the input
    unchanged for 0 without mirror, or on any error."""
    code = _ROTATE_CODES.get(int(deg) % 360)
    if (code is None and not mirror) or not jpeg:
        return jpeg
    import cv2           # only needed when rotating (the tracker imports it anyway)
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    if code is not None:
        img = cv2.rotate(img, getattr(cv2, code))
    if mirror:
        img = cv2.flip(img, 1)
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return out.tobytes() if ok else jpeg


class Eyes:
    def __init__(self) -> None:
        self.jpeg: bytes = b""          # newest raw frame from the robot
        self.frame_at = 0.0
        self.frame_seq = 0
        self.display: bytes = b""       # what the browser shows (annotated if a tracker runs)
        self.display_seq = 0
        self.has_annotator = False       # set by the tracker; then it publishes frames
        self.tracking_info: dict = {}
        self.frames = 0
        self._fps_window: list[float] = []
        self.temperature: float | None = None
        self.last_heard = ""
        self.last_said = ""
        # main.py plugs in: extra /status fields, and the handler for /api/<action>.
        self.state_provider: Callable[[], dict] | None = None
        self.command_handler: Callable[[str, dict], dict] | None = None
        self._cond = threading.Condition()

    # ── robot/browser → server ──────────────────────────────────────────────
    def push_frame(self, jpeg: bytes, source: str = "robot") -> None:
        # The robot's camera sits on its side in the V2 head (CAMERA_ROTATE). The browser's webcam
        # is upright already: its frames go through untouched.
        if source == "robot":
            jpeg = rotate_jpeg(jpeg, config.CAMERA_ROTATE, config.CAMERA_MIRROR)
        now = time.time()
        with self._cond:
            self.jpeg = jpeg
            self.frame_at = now
            self.frame_seq += 1
            self.frames += 1
            self._fps_window.append(now)
            self._fps_window = [t for t in self._fps_window if now - t < 2.0]
            if not self.has_annotator:
                self.display = jpeg
                self.display_seq = self.frame_seq
            self._cond.notify_all()

    def publish(self, jpeg: bytes) -> None:
        """A frame for the browser (the tracker's annotated copy)."""
        if not jpeg:
            return
        with self._cond:
            self.display = jpeg
            self.display_seq += 1
            self._cond.notify_all()

    def wait_for_display(self, seen_seq: int, timeout: float = 1.0) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.display_seq != seen_seq, timeout=timeout)
            return self.display_seq

    def latest(self, max_age: float = 2.0) -> bytes | None:
        """The newest frame if it's recent enough to be worth showing the model."""
        if self.jpeg and time.time() - self.frame_at <= max_age:
            return self.jpeg
        return None

    def fps(self) -> float:
        return len(self._fps_window) / 2.0

    def wait_for_new(self, seen_seq: int, timeout: float = 1.0) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.frame_seq != seen_seq, timeout=timeout)
            return self.frame_seq

    # ── browser ─────────────────────────────────────────────────────────────
    def serve(self, port: int, bind: str = "127.0.0.1") -> None:
        eyes = self
        token = config.CONSOLE_TOKEN
        try:
            loopback_only = ipaddress.ip_address(bind.split("%")[0]).is_loopback
        except ValueError:
            loopback_only = False
        if token and (len(token) < 16 or token == "change-me"):
            print(f"(refusing to serve the console: CONSOLE_TOKEN must be a random string of at least 16 "
                  f"characters, e.g. from `python3 -c \"import secrets; print(secrets.token_hex(16))\"`)")
            return
        if not token and not loopback_only:
            print(f"(refusing to serve the console on {bind}: set CONSOLE_TOKEN in server/.env (it gates the "
                  f"page, the camera and the controls), or bind loopback only for local development)")
            return
        page_path = Path(__file__).with_name("dashboard.html")
        public_host = config.PUBLIC_HOST

        def dashboard_bytes() -> bytes:
            # Read fresh each request so dashboard.html edits show up without restarting the brain.
            return (page_path.read_text(encoding="utf-8")
                    .replace("{name}", config.ROBOT_NAME)
                    .replace("{console_token}", json.dumps(token)).encode())

        class Handler(BaseHTTPRequestHandler):
            server_version = "desk-robot"  # don't advertise the Python version
            sys_version = ""

            def log_message(self, *args) -> None:  # keep the console quiet
                pass

            def _authorized(self, via_query: bool) -> bool:
                """One shared token gates everything (the tunnel is the only route in, and the token is the
                real gate; PUBLIC_HOST, when set, is a belt against DNS rebinding). The page itself is opened
                with ?token=; everything after that (the page holds it in memory) sends it as a header."""
                if public_host:
                    host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                    if host != public_host:
                        self._reply(403, "text/plain", b"forbidden")
                        return False
                if not token:
                    return True   # loopback-only bind, local development
                offered = self.headers.get("X-Rocky-Console-Token", "")
                if via_query and not offered:
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    offered = (q.get("token") or [""])[0]
                if not secrets.compare_digest(offered.encode(), token.encode()):
                    self._reply(403, "text/plain",
                                b"the console needs its token: open this page with ?token=<CONSOLE_TOKEN> "
                                b"from server/.env")
                    return False
                return True
            def do_POST(self) -> None:
                if not self._authorized(via_query=False):
                    return
                # Controls: a page on another site could still POST here
                # (browsers allow simple cross-site POSTs), so demand a custom
                # header — that turns it into a preflighted request, and we
                # never answer preflights.
                if self.headers.get("X-Rocky-Console") != "1":
                    self._reply(403, "application/json", b'{"error": "not the console"}')
                    return
                if self.path == "/api/browser_frame" and eyes.command_handler is not None:
                    # the dashboard's webcam standing in for the robot's camera: one JPEG per POST,
                    # a raw body rather than JSON
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        if not 0 < length <= 512 * 1024:
                            raise ValueError("bad content length")
                        jpeg = self.rfile.read(length)
                    except (ValueError, OSError) as e:
                        self._reply(400, "application/json", json.dumps({"error": f"bad request: {e}"}).encode())
                        return
                    try:
                        eyes.command_handler("browser_frame", {"jpeg": jpeg})
                    except ValueError as e:
                        self._reply(400, "application/json", json.dumps({"error": str(e)}).encode())
                        return
                    except Exception as e:
                        self._reply(500, "application/json", json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
                        return
                    self._reply(200, "application/json", b'{"ok": true}')
                    return
                if not self.path.startswith("/api/") or eyes.command_handler is None:
                    self._reply(404, "application/json", b'{"error": "no such control"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 <= length <= 64 * 1024:
                        raise ValueError("bad content length")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                except (ValueError, json.JSONDecodeError, RecursionError) as e:
                    self._reply(400, "application/json", json.dumps({"error": f"bad request: {e}"}).encode())
                    return
                action = self.path[len("/api/"):]
                try:
                    result = eyes.command_handler(action, payload)
                except ValueError as e:  # the control said no
                    self._reply(400, "application/json", json.dumps({"error": str(e)}).encode())
                    return
                except Exception as e:  # something broke inside the server
                    self._reply(500, "application/json", json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
                    return
                self._reply(200, "application/json", json.dumps({"ok": True, **(result or {})}).encode())

            def do_GET(self) -> None:
                if not self._authorized(via_query=True):
                    return
                if self.path == "/" or self.path.startswith("/?"):
                    self._reply(200, "text/html; charset=utf-8", dashboard_bytes())
                elif self.path == "/frame":
                    if eyes.jpeg:
                        self._reply(200, "image/jpeg", eyes.jpeg)
                    else:
                        self._reply(503, "text/plain", b"no frame yet")
                elif self.path == "/status":
                    st = {
                        "fps": round(eyes.fps(), 1),
                        "frame_age_s": round(time.time() - eyes.frame_at, 1) if eyes.frame_at else None,
                        "frames": eyes.frames,
                        "temperature_c": eyes.temperature,
                        "last_heard": eyes.last_heard,
                        "last_said": eyes.last_said,
                        **eyes.tracking_info,
                    }
                    if eyes.state_provider is not None:
                        try:
                            st.update(eyes.state_provider())
                        except Exception as e:  # never let a status bug kill the page
                            st["state_error"] = str(e)
                    self._reply(200, "application/json", json.dumps(st).encode())
                elif self.path == "/stream":
                    self._stream()
                else:
                    self._reply(404, "text/plain", b"not found")

            def _reply(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")  # robot-supplied bytes stay images
                self.send_header("X-Frame-Options", "DENY")                  # no framing (clickjacking)
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                seq = -1
                try:
                    while True:
                        seq = eyes.wait_for_display(seq, timeout=1.0)
                        frame = eyes.display
                        if not frame:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer((bind, port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()

