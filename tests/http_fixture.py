#!/usr/bin/env python3
"""Loopback pages and assertions for the real Cage/Firefox smoke test."""

import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import stat
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ADDRESS = "http://127.0.0.1:8000"


class FixtureHandler(BaseHTTPRequestHandler):
    loaded = set()

    def do_GET(self):
        if self.path in ("/left", "/right"):
            side = self.path[1:]
            color = "#cceeff" if side == "left" else "#ffeecc"
            body = (
                f"<!doctype html><html><head><title>{side}</title></head>"
                f'<body style="background:{color}"><h1>{side}</h1>'
                f'<script>window.addEventListener("load", () => '
                f'fetch("/loaded/{side}"));</script></body></html>'
            ).encode()
            content_type = "text/html; charset=utf-8"
        elif self.path in ("/loaded/left", "/loaded/right"):
            self.loaded.add(self.path.rsplit("/", 1)[1])
            body, content_type = b"OK", "text/plain"
        elif self.path == "/status":
            body, content_type = (
                json.dumps(sorted(self.loaded)).encode(),
                "application/json",
            )
        elif self.path == "/ready":
            body, content_type = b"OK", "text/plain"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def wait_for(mode):
    # The fixture stays on container loopback even on hosts with proxy settings.
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            path = "/ready" if mode == "ready" else "/status"
            with opener.open(ADDRESS + path, timeout=1) as response:
                body = response.read()
            if mode == "ready" or json.loads(body) == ["left", "right"]:
                return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for fixture {mode}")


def check_runtime():
    programs = set()
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            program = path.read_bytes().split(b"\0", 1)[0].decode()
        except (OSError, UnicodeError):
            continue
        programs.add(Path(program).name)
    for program in ("dbus-run-session", "dbus-daemon", "cage"):
        if program not in programs:
            raise RuntimeError(f"Missing process {program}; found {sorted(programs)}")
    if not programs.intersection({"firefox", "firefox-bin"}):
        raise RuntimeError(f"Firefox is not running; found {sorted(programs)}")
    for path in (
        Path(os.environ["HOME"]) / "profile",
        Path(os.environ["XDG_RUNTIME_DIR"]),
    ):
        info = path.stat()
        if info.st_uid != 1000 or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError(f"Expected UID 1000 and mode 0700 on {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("serve", "ready", "loaded", "check"):
        sys.exit(f"Usage: {sys.argv[0]} serve|ready|loaded|check")
    if sys.argv[1] == "serve":
        HTTPServer(("127.0.0.1", 8000), FixtureHandler).serve_forever()
    elif sys.argv[1] == "check":
        check_runtime()
    else:
        wait_for(sys.argv[1])
