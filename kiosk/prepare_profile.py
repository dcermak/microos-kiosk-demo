#!/usr/bin/env python3
"""Seed a dedicated Firefox profile with one native two-tab split view.

Uses Firefox's internal session format, reviewed against upstream September 2026.
Run only before Firefox starts. The profile belongs to the disposable container.
"""

import json
import os
from pathlib import Path
import struct
import sys
from urllib.parse import urlsplit


def validate_url(value):
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("URLs must not contain control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Each kiosk URL must be an absolute http(s) URL")
    return value


def session_state(left, right):
    return {
        "version": ["sessionrestore", 1],
        "selectedWindow": 1,
        "maxSplitViewId": 1,
        "session": {"state": "stopped"},
        "windows": [
            {
                "selected": 1,
                "tabs": [
                    {
                        "entries": [{"url": validate_url(url)}],
                        "index": 1,
                        "splitViewId": 1,
                    }
                    for url in (left, right)
                ],
                "splitViews": [{"id": 1, "numberOfTabs": 2}],
            }
        ],
    }


def mozlz4(data):
    """Mozilla header + size + valid LZ4 block containing only literals.

    This tiny session does not need compression. A literal-only LZ4 block avoids
    a Python lz4 package dependency while remaining readable by Firefox's LZ4
    decoder. See the LZ4 block format's last-sequence rule.
    """
    size = len(data)
    block = bytearray([min(size, 15) << 4])
    if size >= 15:
        extra = size - 15
        while extra >= 255:
            block.append(255)
            extra -= 255
        block.append(extra)
    block.extend(data)
    return b"mozLz40\0" + struct.pack("<I", size) + block


def prepare(profile, left, right):
    state = session_state(left, right)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    preferences = {
        "browser.tabs.splitView.enabled": True,
        "browser.startup.page": 3,
        "browser.sessionstore.resume_session_once": True,
        "browser.sessionstore.restore_on_demand": False,
        "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False,
        "browser.startup.homepage_override.mstone": "ignore",
        "browser.tabs.warnOnClose": False,
    }
    user_js = "// Managed by kiosk-start; regenerated before each launch.\n"
    user_js += "".join(
        f"user_pref({json.dumps(key)}, {json.dumps(value)});\n"
        for key, value in preferences.items()
    )
    # Firefox starts only after both writes succeed; no state needs to survive
    # removal of this container.
    (profile / "user.js").write_text(user_js, encoding="utf-8")
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    (profile / "sessionstore.jsonlz4").write_bytes(mozlz4(payload))


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError(
                "Usage: python3 -m kiosk.prepare_profile PROFILE_DIRECTORY"
            )
        prepare(
            Path(sys.argv[1]),
            os.environ["KIOSK_URL_LEFT"],
            os.environ["KIOSK_URL_RIGHT"],
        )
    except (ValueError, KeyError) as error:
        sys.exit(str(error))
