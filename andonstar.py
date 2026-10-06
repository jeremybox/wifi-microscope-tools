#!/usr/bin/env python3
"""Cross-platform Andonstar Wi-Fi microscope client + local web UI.

Works on Windows / macOS / Linux once your machine is on the scope's AP
(or can route to it). Joining Wi-Fi itself is left to the OS.

Protocol (Novatek-style):
  GET http://IP/?custom=1&cmd=N[&par=P]   control
  GET http://IP:8192/                    multipart MJPEG after preview-on

Web UI:
  python andonstar.py serve [--port 23903] [--ip 192.168.1.254]
"""

from __future__ import annotations

import argparse
import re
import signal
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import requests

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

DEFAULT_IP = "192.168.1.254"
STREAM_PORT = 8192
DEFAULT_UI_PORT = 23903
DEFAULT_BOUNDARY = b"--arflebarfle"
WEB_DIR = Path(__file__).resolve().parent / "web"

# Safe UI option catalogs (AD409 / Novatek family). Live MJPEG stays ~640px.
PHOTO_RES = [
    (0, "24M"),
    (1, "20M"),
    (2, "16M"),
    (3, "12M"),
    (4, "10M"),
    (5, "8M"),
    (6, "2592×1944"),
    (7, "2048×1536"),
    (8, "1920×1080 (16:9)"),
    (9, "640×480"),
    (10, "1280×960"),
]

VIDEO_RES = [
    (0, "2880×2160 @24"),
    (1, "2560×1440 @30"),
    (5, "1920×1080 @60"),
    (6, "1920×1080 @30"),
    (8, "1280×720 @120"),
    (9, "1280×720 @60"),
    (10, "1280×720 @30"),
]

# Alternate video-res table seen on some firmwares (cmd 2024)
VIDEO_RES_ALT = [
    (0, "1920×1080 @60"),
    (1, "1280×738 @30"),
    (2, "864×480 @30"),
    (3, "640×480 @30"),
    (4, "320×240 @30"),
]

MODES = [
    (0, "Photo"),
    (1, "Video"),
    (2, "Playback (stops live stream)"),
]

FLIP = [
    (0, "Normal"),
    (1, "Flip horizontal"),
    (2, "Flip vertical"),
    (3, "Rotate 180°"),
]

EXPOSURE = [(i, str(i)) for i in range(0, 13)]  # 0 lightest … 12 darkest
BRIGHTNESS = [(i, str(i)) for i in range(0, 5)]
LOOP_RECORD = [(0, "Off"), (3, "3 min"), (4, "5 min"), (5, "10 min")]


def _xml_children(text: str) -> dict[str, str]:
    root = ET.fromstring(text)
    return {child.tag: (child.text or "") for child in root}


def _xml_cmd_status_pairs(text: str) -> list[tuple[str, str]]:
    root = ET.fromstring(text)
    out: list[tuple[str, str]] = []
    kids = list(root)
    i = 0
    while i + 1 < len(kids):
        if kids[i].tag == "Cmd" and kids[i + 1].tag == "Status":
            out.append((kids[i].text or "", kids[i + 1].text or ""))
            i += 2
        else:
            i += 1
    return out


class Scope:
    def __init__(self, ip: str = DEFAULT_IP, timeout: float = 2.5):
        self.ip = ip
        self.timeout = timeout
        self.base = f"http://{ip}"
        self.stream_url = f"http://{ip}:{STREAM_PORT}/"
        self.last_mode: int | None = None

    def cmd(self, code: int, par: int | str | None = None, **kwargs) -> str:
        params: dict[str, Any] = {"custom": 1, "cmd": code}
        if par is not None:
            params["par"] = par
        params.update(kwargs)
        r = requests.get(self.base + "/", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def cmd_parsed(self, code: int, par: int | str | None = None, **kwargs) -> dict[str, str]:
        return _xml_children(self.cmd(code, par, **kwargs))

    def set_mode(self, mode: int) -> dict[str, str]:
        """0=photo, 1=video, 2=playback (stops MJPEG)."""
        resp = self.cmd_parsed(3001, int(mode))
        self.last_mode = int(mode)
        return resp

    def preview(self, on: bool = True) -> None:
        # Video mode enables MJPEG; playback mode stops it.
        self.set_mode(1 if on else 2)

    def detect_mode(self) -> int | None:
        """Infer mode. Firmware has no reliable mode query; 3016 is a ping.

        Workaround (AD409 notes): cmd 2017 returns -22 if not in video mode,
        -13 if video but not recording, 0/1 if snapshot ok.
        """
        try:
            resp = self.cmd_parsed(2017)
            st = int(resp.get("Status", "0"))
        except Exception:
            return getattr(self, "last_mode", None)
        if st in (-13, 0, 1):
            self.last_mode = 1
            return 1
        # -22 or other → not video; trust last commanded mode if photo/playback
        last = getattr(self, "last_mode", None)
        if last in (0, 2):
            return last
        # default guess: photo (common idle)
        if last is None:
            self.last_mode = 0
            return 0
        return last

    def status(self) -> dict[str, str]:
        return self.cmd_parsed(3016)

    def wifi_info(self) -> dict[str, str]:
        return _xml_children(self.cmd(3029))

    def config(self) -> list[tuple[str, str]]:
        return _xml_cmd_status_pairs(self.cmd(3014))

    def config_map(self) -> dict[str, str]:
        return {k: v for k, v in self.config()}

    def disk_space(self) -> dict[str, str]:
        return self.cmd_parsed(3017)

    def device_info(self) -> dict[str, str]:
        try:
            return self.cmd_parsed(3012)
        except Exception:
            return {}

    def version_blob(self) -> str:
        try:
            return self.cmd(8004)
        except Exception as e:
            return str(e)

    def snap_photo(self) -> dict[str, str]:
        """Still capture to SD (photo mode + cmd 1001)."""
        self.set_mode(0)
        time.sleep(0.2)
        return self.cmd_parsed(1001)

    def record(self, on: bool) -> dict[str, str]:
        self.set_mode(1)
        time.sleep(0.2)
        return self.cmd_parsed(2001, 1 if on else 0)

    def snap_jpeg(self, path: Path, max_bytes: int = 400_000) -> Path:
        """Grab one JPEG from the MJPEG stream (starts preview if needed)."""
        self.set_mode(1)
        time.sleep(0.3)
        jpeg = next(iter_mjpeg_frames(self.ip, STREAM_PORT, max_bytes=max_bytes))
        path = Path(path)
        path.write_bytes(jpeg)
        return path

    def snapshot(self) -> dict[str, Any]:
        """Gather UI state; tolerate partial failures."""
        out: dict[str, Any] = {
            "ip": self.ip,
            "stream_url": self.stream_url,
            "ok": False,
            "error": None,
            "mode": None,
            "current_mode": None,
            "wifi": {},
            "config": {},
            "disk": {},
            "device": {},
            "options": {
                "modes": MODES,
                "photo_res": PHOTO_RES,
                "video_res": VIDEO_RES,
                "video_res_alt": VIDEO_RES_ALT,
                "flip": FLIP,
                "exposure": EXPOSURE,
                "brightness": BRIGHTNESS,
                "loop_record": LOOP_RECORD,
            },
        }
        try:
            out["mode"] = self.status()
            out["config"] = self.config_map()
            # Prefer last commanded mode; probe only when unknown.
            # (cmd 3016 Status is a ping, not the camera mode.)
            if self.last_mode is None:
                out["current_mode"] = self.detect_mode()
            else:
                out["current_mode"] = self.last_mode
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
            out["current_mode"] = getattr(self, "last_mode", None)
            return out
        try:
            out["wifi"] = self.wifi_info()
        except Exception as e:
            out["wifi"] = {"error": str(e)}
        try:
            out["disk"] = self.disk_space()
        except Exception as e:
            out["disk"] = {"error": str(e)}
        try:
            out["device"] = self.device_info()
        except Exception as e:
            out["device"] = {"error": str(e)}
        return out


def iter_mjpeg_frames(
    host: str,
    port: int = STREAM_PORT,
    *,
    max_bytes: int | None = None,
    stop_event: threading.Event | None = None,
):
    """Yield raw JPEG bytes from the multipart MJPEG stream."""
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"User-Agent: andonstar-py/1.0\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection((host, port), timeout=5)
    sock.settimeout(5)
    try:
        sock.sendall(req)
        buf = b""

        def recv_until(token: bytes) -> tuple[bytes, bytes]:
            nonlocal buf
            while token not in buf:
                if stop_event is not None and stop_event.is_set():
                    raise ConnectionError("stopped")
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("stream closed")
                buf += chunk
            head, buf = buf.split(token, 1)
            return head, buf

        header, buf = recv_until(b"\r\n\r\n")
        m = re.search(rb"boundary=(\S+)", header, re.I)
        boundary = b"--" + m.group(1) if m else DEFAULT_BOUNDARY

        while True:
            if stop_event is not None and stop_event.is_set():
                return
            _, buf = recv_until(boundary + b"\r\n")
            head, buf = recv_until(b"\r\n\r\n")
            m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
            if not m:
                continue
            size = int(m.group(1))
            while len(buf) < size:
                if stop_event is not None and stop_event.is_set():
                    return
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("stream closed mid-frame")
                buf += chunk
            jpeg, buf = buf[:size], buf[size:]
            if jpeg.startswith(b"\xff\xd8"):
                yield jpeg
            if max_bytes is not None:
                return
    finally:
        sock.close()


def view_live(scope: Scope, *, fullscreen: bool = False, save_dir: Path | None = None) -> None:
    if cv2 is None:
        raise SystemExit("opencv-python is required for live view: pip install opencv-python")

    scope.set_mode(1)
    win = "Andonstar"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    if fullscreen:
        cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    idx = 0
    if save_dir:
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

    print("Live view — Esc or Ctrl-C to quit")
    try:
        for jpeg in iter_mjpeg_frames(scope.ip, STREAM_PORT):
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            cv2.imshow(win, img)
            if save_dir:
                (save_dir / f"frame_{idx:06d}.jpg").write_bytes(jpeg)
                idx += 1
            if cv2.waitKey(1) & 0xFF == 27:
                break
    finally:
        cv2.destroyAllWindows()
        try:
            scope.set_mode(2)
        except Exception:
            pass


def create_app(scope: Scope):
    from flask import Flask, Response, jsonify, request, send_from_directory

    import wifi_link

    app = Flask(__name__, static_folder=None)
    state = {
        "stream_enabled": False,
        "last_jpeg": None,
        "proxy_clients": 0,
        "wifi_iface": None,  # preferred adapter name, e.g. "Wi-Fi 2"
        "lock": threading.Lock(),
    }

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name: str):
        return send_from_directory(WEB_DIR, name)

    @app.get("/api/state")
    def api_state():
        snap = scope.snapshot()
        with state["lock"]:
            snap["stream_enabled"] = state["stream_enabled"]
            snap["proxy_clients"] = state["proxy_clients"]
            snap["has_last_frame"] = state["last_jpeg"] is not None
            pref = state["wifi_iface"]
        try:
            snap["link"] = wifi_link.status(scope.ip, preferred_iface=pref)
        except Exception as e:
            snap["link"] = {"error": str(e)}
        return jsonify(snap)

    @app.get("/api/wifi/status")
    def api_wifi_status():
        with state["lock"]:
            pref = state["wifi_iface"]
        return jsonify({"ok": True, **wifi_link.status(scope.ip, preferred_iface=pref)})

    @app.get("/api/wifi/scan")
    def api_wifi_scan():
        iface = request.args.get("interface") or None
        with state["lock"]:
            if not iface:
                iface = state["wifi_iface"]
        try:
            nets = wifi_link.scan_networks(iface)
            andon = [n for n in nets if (n.get("ssid") or "").lower().startswith("andonstar")]
            return jsonify({"ok": True, "networks": nets, "andonstar": andon, "interface": iface})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/wifi/connect")
    def api_wifi_connect():
        body = request.get_json(force=True, silent=True) or {}
        ssid = (body.get("ssid") or "").strip()
        password = body.get("password") or "12345678"
        iface = (body.get("interface") or "").strip() or None
        if not ssid:
            return jsonify({"ok": False, "error": "ssid required"}), 400
        if iface:
            with state["lock"]:
                state["wifi_iface"] = iface
        try:
            result = wifi_link.connect(ssid, password, iface)
            # Prefer host route to scope IP after connect
            try:
                route = wifi_link.ensure_host_route(scope.ip, iface)
                result["route"] = route
            except Exception as e:
                result["route"] = {"ok": False, "error": str(e)}
            result["link"] = wifi_link.status(scope.ip, preferred_iface=iface)
            return jsonify(result)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/wifi/disconnect")
    def api_wifi_disconnect():
        body = request.get_json(force=True, silent=True) or {}
        iface = (body.get("interface") or "").strip() or None
        with state["lock"]:
            if not iface:
                iface = state["wifi_iface"]
        try:
            result = wifi_link.disconnect(iface)
            result["link"] = wifi_link.status(scope.ip, preferred_iface=iface)
            return jsonify(result)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/wifi/route")
    def api_wifi_route():
        body = request.get_json(force=True, silent=True) or {}
        iface = (body.get("interface") or "").strip() or None
        with state["lock"]:
            if not iface:
                iface = state["wifi_iface"]
        try:
            result = wifi_link.ensure_host_route(scope.ip, iface)
            result["link"] = wifi_link.status(scope.ip, preferred_iface=iface)
            return jsonify(result)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/wifi/prefer")
    def api_wifi_prefer():
        body = request.get_json(force=True, silent=True) or {}
        iface = (body.get("interface") or "").strip() or None
        with state["lock"]:
            state["wifi_iface"] = iface
        return jsonify({"ok": True, "interface": iface})

    @app.post("/api/preview")
    def api_preview():
        body = request.get_json(force=True, silent=True) or {}
        on = bool(body.get("on", True))
        if on:
            scope.set_mode(1)
            with state["lock"]:
                state["stream_enabled"] = True
        else:
            # Playback mode stops the scope MJPEG service
            scope.set_mode(2)
            with state["lock"]:
                state["stream_enabled"] = False
        return jsonify({
            "ok": True,
            "stream_enabled": on,
            "current_mode": scope.last_mode,
        })

    @app.post("/api/mode")
    def api_mode():
        body = request.get_json(force=True, silent=True) or {}
        mode = int(body["mode"])
        resp = scope.set_mode(mode)
        with state["lock"]:
            state["stream_enabled"] = mode in (0, 1)
        return jsonify({
            "ok": True,
            "response": resp,
            "stream_enabled": mode in (0, 1),
            "current_mode": mode,
        })

    @app.post("/api/set")
    def api_set():
        """Generic safe setter: {cmd, par}."""
        body = request.get_json(force=True, silent=True) or {}
        code = int(body["cmd"])
        # Block known-dangerous commands from the UI surface
        blocked = {3010, 3011, 3013, 2013, 2014, 3003, 3004, 3032, 3033}
        if code in blocked:
            return jsonify({"ok": False, "error": f"cmd {code} blocked (unsafe)"}), 400
        par = body.get("par", None)
        str_param = body.get("str", None)
        kwargs = {}
        if str_param is not None:
            kwargs["str"] = str_param
        text = scope.cmd(code, par, **kwargs)
        try:
            parsed = _xml_children(text)
        except Exception:
            parsed = {"raw": text}
        return jsonify({"ok": True, "response": parsed, "raw": text})

    @app.post("/api/snap")
    def api_snap():
        resp = scope.snap_photo()
        return jsonify({"ok": True, "response": resp})

    @app.post("/api/record")
    def api_record():
        body = request.get_json(force=True, silent=True) or {}
        resp = scope.record(bool(body.get("on", False)))
        with state["lock"]:
            state["stream_enabled"] = True
        return jsonify({"ok": True, "response": resp})

    @app.get("/api/frame.jpg")
    def api_frame():
        with state["lock"]:
            data = state["last_jpeg"]
        if not data:
            # one-shot grab if stream not piping
            try:
                scope.set_mode(1)
                time.sleep(0.2)
                data = next(iter_mjpeg_frames(scope.ip, STREAM_PORT, max_bytes=500_000))
                with state["lock"]:
                    state["last_jpeg"] = data
                    state["stream_enabled"] = True
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 503
        return Response(data, mimetype="image/jpeg")

    @app.get("/stream")
    def mjpeg_proxy():
        with state["lock"]:
            enabled = state["stream_enabled"]
        if not enabled:
            # auto-enable when a viewer connects
            try:
                scope.set_mode(1)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 503
            with state["lock"]:
                state["stream_enabled"] = True

        stop = threading.Event()
        boundary = b"frame"

        def generate():
            with state["lock"]:
                state["proxy_clients"] += 1
            try:
                for jpeg in iter_mjpeg_frames(scope.ip, STREAM_PORT, stop_event=stop):
                    with state["lock"]:
                        state["last_jpeg"] = jpeg
                    yield (
                        b"--" + boundary + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                        + jpeg + b"\r\n"
                    )
            except Exception:
                return
            finally:
                with state["lock"]:
                    state["proxy_clients"] = max(0, state["proxy_clients"] - 1)

        resp = Response(
            generate(),
            mimetype=f"multipart/x-mixed-replace; boundary={boundary.decode()}",
        )
        resp.call_on_close(stop.set)
        return resp

    return app


def run_server(scope: Scope, host: str = "127.0.0.1", port: int = DEFAULT_UI_PORT) -> None:
    try:
        from flask import Flask  # noqa: F401
    except ImportError as e:
        raise SystemExit("Flask required for web UI: pip install flask") from e

    if not WEB_DIR.is_dir():
        raise SystemExit(f"missing web UI assets at {WEB_DIR}")

    app = create_app(scope)
    print(f"Andonstar UI  http://{host}:{port}/")
    print(f"Scope         {scope.ip}  stream {scope.stream_url}")
    print("Ctrl-C to stop")
    app.run(host=host, port=port, threaded=True, use_reloader=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Andonstar Wi-Fi microscope (cross-platform)")
    p.add_argument("--ip", default=DEFAULT_IP, help=f"scope IP (default {DEFAULT_IP})")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("view", help="live MJPEG window (Esc to quit)")
    v.add_argument("--fullscreen", action="store_true")
    v.add_argument("--save", metavar="DIR", help="also write JPEGs to DIR")

    s = sub.add_parser("snap", help="save one JPEG from the live stream")
    s.add_argument("-o", "--output", default="sample_frame.jpg")

    sub.add_parser("status", help="query mode / wifi / config")

    c = sub.add_parser("cmd", help="raw API: --code N [--par P]")
    c.add_argument("--code", type=int, required=True)
    c.add_argument("--par", default=None)

    sub.add_parser("preview-on", help="open stream port without viewing")
    sub.add_parser("preview-off", help="request preview off")

    k = sub.add_parser(
        "keepalive",
        help="keep preview on for OBS/VLC (Ctrl-C to stop + preview-off)",
    )
    k.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="seconds between preview-on refresh (default 30)",
    )

    srv = sub.add_parser("serve", help=f"local web UI (default port {DEFAULT_UI_PORT})")
    srv.add_argument("--host", default="127.0.0.1", help="bind address")
    srv.add_argument("--port", type=int, default=DEFAULT_UI_PORT, help=f"default {DEFAULT_UI_PORT}")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scope = Scope(args.ip)

    if args.cmd == "view":
        save = Path(args.save) if args.save else None
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        view_live(scope, fullscreen=args.fullscreen, save_dir=save)
        return 0

    if args.cmd == "snap":
        path = scope.snap_jpeg(Path(args.output))
        print(f"wrote {path} ({path.stat().st_size} bytes)")
        return 0

    if args.cmd == "status":
        print("mode:", scope.status())
        try:
            print("wifi:", scope.wifi_info())
        except Exception as e:
            print("wifi: error", e)
        print("config:")
        for code, status in scope.config():
            print(f"  cmd {code}: {status}")
        return 0

    if args.cmd == "cmd":
        print(scope.cmd(args.code, args.par))
        return 0

    if args.cmd == "preview-on":
        scope.preview(True)
        print(f"preview on — stream at http://{scope.ip}:{STREAM_PORT}/")
        return 0

    if args.cmd == "preview-off":
        scope.preview(False)
        print("preview off")
        return 0

    if args.cmd == "keepalive":
        url = f"http://{scope.ip}:{STREAM_PORT}/"
        print(f"Keeping preview on for OBS/VLC")
        print(f"  Media Source input:  {url}")
        print(f"  Input Format:        mpjpeg")
        print("Ctrl-C to stop")
        try:
            while True:
                try:
                    scope.preview(True)
                except Exception as e:
                    print(f"preview refresh failed: {e}", file=sys.stderr)
                time.sleep(max(args.interval, 1.0))
        except KeyboardInterrupt:
            print("\nStopping…")
        finally:
            try:
                scope.preview(False)
            except Exception:
                pass
        return 0

    if args.cmd == "serve":
        run_server(scope, host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
