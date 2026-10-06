#!/usr/bin/env python3
"""Cross-platform Andonstar Wi-Fi microscope client.

Works on Windows / macOS / Linux once your machine is on the scope's AP
(or can route to it). Joining Wi-Fi itself is left to the OS.

Protocol (Novatek-style):
  GET http://IP/?custom=1&cmd=N[&par=P]   control
  GET http://IP:8192/                    multipart MJPEG after preview-on
"""

from __future__ import annotations

import argparse
import re
import signal
import socket
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import requests

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

DEFAULT_IP = "192.168.1.254"
STREAM_PORT = 8192
DEFAULT_BOUNDARY = b"--arflebarfle"


class Scope:
    def __init__(self, ip: str = DEFAULT_IP, timeout: float = 5.0):
        self.ip = ip
        self.timeout = timeout
        self.base = f"http://{ip}"

    def cmd(self, code: int, par: int | str | None = None, **kwargs) -> str:
        params: dict = {"custom": 1, "cmd": code}
        if par is not None:
            params["par"] = par
        params.update(kwargs)
        r = requests.get(self.base + "/", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def preview(self, on: bool = True) -> None:
        # cmd 3001 par=1 starts live MJPEG on :8192 (observed on this unit)
        self.cmd(3001, 1 if on else 0)

    def status(self) -> dict[str, str]:
        text = self.cmd(3016)
        root = ET.fromstring(text)
        return {child.tag: (child.text or "") for child in root}

    def wifi_info(self) -> dict[str, str]:
        text = self.cmd(3029)
        root = ET.fromstring(text)
        return {child.tag: (child.text or "") for child in root}

    def config(self) -> list[tuple[str, str]]:
        text = self.cmd(3014)
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

    def snap_jpeg(self, path: Path, max_bytes: int = 400_000) -> Path:
        """Grab one JPEG from the MJPEG stream (starts preview if needed)."""
        self.preview(True)
        time.sleep(0.3)
        jpeg = next(iter_mjpeg_frames(self.ip, STREAM_PORT, max_bytes=max_bytes))
        path = Path(path)
        path.write_bytes(jpeg)
        return path


def iter_mjpeg_frames(
    host: str,
    port: int = STREAM_PORT,
    *,
    max_bytes: int | None = None,
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
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("stream closed")
                buf += chunk
                if max_bytes is not None and len(buf) > max_bytes * 2:
                    # still allow finding a frame, but bound memory
                    pass
            head, buf = buf.split(token, 1)
            return head, buf

        header, buf = recv_until(b"\r\n\r\n")
        m = re.search(rb"boundary=(\S+)", header, re.I)
        boundary = b"--" + m.group(1) if m else DEFAULT_BOUNDARY

        while True:
            _, buf = recv_until(boundary + b"\r\n")
            head, buf = recv_until(b"\r\n\r\n")
            m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
            if not m:
                continue
            size = int(m.group(1))
            while len(buf) < size:
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

    scope.preview(True)
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
            scope.preview(False)
        except Exception:
            pass


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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
