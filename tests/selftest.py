#!/usr/bin/env python3
"""Source checks using temporary files, without container builds or graphics hardware."""

import ctypes
import ctypes.util
import io
import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from kiosk import configure
from kiosk import prepare_profile as helper
from tests import http_fixture as fixture

ROOT = Path(__file__).resolve().parents[1]
QUADLET_GENERATOR = Path("/usr/lib/systemd/system-generators/podman-system-generator")


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "config"
        self.keyfile = self.directory / "key.pub"
        self.key = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDjrzJUjw4ZEhwAwC19T5Gm3"
            "GwVUgvDdEE0ytxUwSCMd test"
        )
        self.keyfile.write_text(f"# Deployment key\n\n{self.key}\n")
        self.argv = [
            "kiosk.configure",
            "--image",
            "registry.example.org/kiosk:test",
            "--left",
            "https://example.org/left?q=one&two=3",
            "--right",
            "https://example.org/right",
            "--ssh-key",
            str(self.keyfile),
            "--output",
            str(self.output),
        ]

    def generate(self, answers):
        with (
            patch.object(sys, "argv", self.argv),
            patch.object(configure.getpass, "getpass", side_effect=answers),
        ):
            configure.main()

    def hash_with_same_salt(self, password, password_hash):
        salt = password_hash.split("$")[2]
        return (
            subprocess.run(
                ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
                input=password.encode("utf-8") + b"\n",
                capture_output=True,
                check=True,
            )
            .stdout.decode("ascii")
            .strip()
        )

    def test_generate_configuration_with_entered_password(self):
        password = "  café recovery password  "
        self.argv.extend(["--extra-package", "curl", "--ssh-key", str(self.keyfile)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch.object(configure.subprocess, "run", wraps=subprocess.run) as run,
        ):
            self.generate([password, password])

        args, kwargs = run.call_args
        self.assertEqual(args[0], ["openssl", "passwd", "-6", "-stdin"])
        self.assertNotIn("env", kwargs)
        self.assertNotIn(password, str(dict(os.environ)))
        self.assertEqual(kwargs["input"], password.encode("utf-8") + b"\n")

        config = self.output / "combustion"
        hashfile = config / "root-password.hash"
        password_hash = hashfile.read_text().strip()
        self.assertEqual(
            password_hash, self.hash_with_same_salt(password, password_hash)
        )
        self.assertEqual(hashfile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (config / "authorized_keys").read_text(), (self.key + "\n") * 2
        )
        self.assertEqual((config / "extra-packages.txt").read_text(), "curl\n")
        self.assertEqual(
            (config / "kiosk.env").read_text(),
            "KIOSK_URL_LEFT=https://example.org/left?q=one&two=3\n"
            "KIOSK_URL_RIGHT=https://example.org/right\n",
        )
        quadlet = (config / "kiosk.container").read_text()
        self.assertIn("Image=registry.example.org/kiosk:test\n", quadlet)
        self.assertIn("DropCapability=all", quadlet.splitlines())
        self.assertIn("AddCapability=SYS_CHROOT", quadlet.splitlines())
        ssh = (config / "units" / "00-kiosk-ssh.conf").read_text()
        self.assertIn("PasswordAuthentication no\n", ssh)
        self.assertIn("AuthenticationMethods publickey\n", ssh)
        for path in config.rglob("*"):
            if path.is_file():
                self.assertNotIn(password.encode("utf-8"), path.read_bytes(), str(path))
        self.assertNotIn(password, stdout.getvalue() + stderr.getvalue())

    def test_default_output_is_relative_to_repository(self):
        args = configure.parse_args(self.argv[1 : self.argv.index("--output")])
        self.assertEqual(configure.ROOT, ROOT)
        self.assertEqual(args.output, ROOT / "build" / "nuc-config")

    def test_module_cli_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "kiosk.configure", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ssh-key", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_cli_requires_ssh_key(self):
        key_index = self.argv.index("--ssh-key")
        args = self.argv[1:key_index] + self.argv[key_index + 2 :]
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            configure.parse_args(args)
        self.assertEqual(error.exception.code, 2)

    def test_invalid_keys_do_not_prompt_or_create_output(self):
        for contents in ("# Comment\n\n", "not-a-key", "ssh-ed25519 !!!"):
            with self.subTest(contents=contents):
                self.keyfile.write_text(contents)
                with (
                    patch.object(sys, "argv", self.argv),
                    patch.object(configure.getpass, "getpass") as prompt,
                    self.assertRaises(ValueError),
                ):
                    configure.main()
                prompt.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_existing_output_is_untouched_without_prompting(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing configuration")
        with (
            patch.object(sys, "argv", self.argv),
            patch.object(configure.getpass, "getpass") as prompt,
            self.assertRaisesRegex(ValueError, "Output already exists"),
        ):
            configure.main()
        prompt.assert_not_called()
        self.assertEqual(marker.read_text(), "existing configuration")

    def test_invalid_deployment_values_do_not_prompt_or_create_output(self):
        for option, value in (
            ("--image", "unqualified"),
            ("--right", "https://example.org/\nINJECT=value"),
            ("--extra-package", "invalid package"),
        ):
            with self.subTest(option=option):
                with (
                    patch.object(sys, "argv", self.argv + [option, value]),
                    patch.object(configure.getpass, "getpass") as prompt,
                    redirect_stderr(io.StringIO()),
                    self.assertRaises((ValueError, SystemExit)),
                ):
                    configure.main()
                prompt.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_invalid_passwords_do_not_hash_or_create_output(self):
        cases = [
            ("", "", "cannot be empty"),
            ("secret", "different", "do not match"),
            ("line\nbreak", "line\nbreak", "line breaks"),
            ("line\rbreak", "line\rbreak", "line breaks"),
            ("nul\0byte", "nul\0byte", "NUL"),
            ("a" * 257, "a" * 257, "256 UTF-8 bytes"),
            ("é" * 129, "é" * 129, "256 UTF-8 bytes"),
        ]
        for password, confirmation, message in cases:
            with self.subTest(message=message):
                with (
                    patch.object(configure.subprocess, "run") as run,
                    self.assertRaisesRegex(ValueError, message),
                ):
                    self.generate([password, confirmation])
                run.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_password_byte_limit_preserves_last_character(self):
        for password in ("a" * 255 + "b", "é" * 127 + "à"):
            with self.subTest(password=password):
                with patch.object(configure.getpass, "getpass", return_value=password):
                    password_hash = configure.prompt_root_password_hash()
                self.assertEqual(
                    password_hash, self.hash_with_same_salt(password, password_hash)
                )
                self.assertNotEqual(
                    password_hash,
                    self.hash_with_same_salt(password[:-1], password_hash),
                )

    def test_cli_without_terminal_fails_without_reading_password(self):
        result = subprocess.run(
            [sys.executable, "-m", "kiosk.configure", *self.argv[1:]],
            cwd=ROOT,
            input="piped secret\npiped secret\n",
            text=True,
            capture_output=True,
            start_new_session=True,
            check=False,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hidden password input", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("piped secret", result.stdout + result.stderr)
        self.assertFalse(self.output.exists())


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = ctypes.util.find_library("lz4")
        if not library:
            raise RuntimeError(
                "Install liblz4 (liblz4-1 on Ubuntu) for the decoder test"
            )
        cls.lz4 = ctypes.CDLL(library)
        cls.lz4.LZ4_decompress_safe.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        cls.lz4.LZ4_decompress_safe.restype = ctypes.c_int

    def decode(self, encoded):
        self.assertEqual(encoded[:8], b"mozLz40\0")
        (size,) = struct.unpack("<I", encoded[8:12])
        result = ctypes.create_string_buffer(max(size, 1))
        length = self.lz4.LZ4_decompress_safe(
            encoded[12:], result, len(encoded) - 12, size
        )
        self.assertEqual(
            length, size, "Independent liblz4 decoder rejected the session"
        )
        return result.raw[:size]

    def test_lz4_literal_length_boundaries(self):
        for size in (0, 1, 14, 15, 16, 269, 270, 271, 525, 4096):
            original = bytes((i % 256 for i in range(size)))
            with self.subTest(size=size):
                self.assertEqual(self.decode(helper.mozlz4(original)), original)

    def test_module_cli_prepares_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            urls = ["https://example.org/left", "https://example.org/right"]
            result = subprocess.run(
                [sys.executable, "-m", "kiosk.prepare_profile", str(profile)],
                cwd=ROOT,
                env=dict(os.environ, KIOSK_URL_LEFT=urls[0], KIOSK_URL_RIGHT=urls[1]),
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            state = json.loads(
                self.decode((profile / "sessionstore.jsonlz4").read_bytes())
            )
            self.assertEqual(
                [tab["entries"][0]["url"] for tab in state["windows"][0]["tabs"]], urls
            )
            self.assertIn(
                'user_pref("browser.tabs.splitView.enabled", true);',
                (profile / "user.js").read_text(),
            )

    def test_module_cli_rejects_invalid_input_without_creating_profile(self):
        cases = (
            (False, {}, "Usage: python3 -m kiosk.prepare_profile"),
            (True, {}, "KIOSK_URL_LEFT"),
            (
                True,
                {"KIOSK_URL_LEFT": "https://example.org/left"},
                "KIOSK_URL_RIGHT",
            ),
            (
                True,
                {
                    "KIOSK_URL_LEFT": "file:///etc/passwd",
                    "KIOSK_URL_RIGHT": "https://example.org/right",
                },
                "Each kiosk URL must be an absolute http(s) URL",
            ),
        )
        for with_profile, values, message in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                profile = Path(directory) / "profile"
                env = dict(os.environ)
                env.pop("KIOSK_URL_LEFT", None)
                env.pop("KIOSK_URL_RIGHT", None)
                env.update(values)
                command = [sys.executable, "-m", "kiosk.prepare_profile"]
                if with_profile:
                    command.append(str(profile))
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(profile.exists())

    def test_profile_has_exactly_two_associated_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            left, right = (
                'https://example.org/?q="left"&x=%20',
                "https://example.org/\u00fcber",
            )
            helper.prepare(profile, left, right)
            state = json.loads(
                self.decode((profile / "sessionstore.jsonlz4").read_bytes())
            )
            (window,) = state["windows"]
            self.assertEqual(
                [t["entries"][0]["url"] for t in window["tabs"]], [left, right]
            )
            self.assertEqual(
                {t["splitViewId"] for t in window["tabs"]},
                {window["splitViews"][0]["id"]},
            )
            self.assertEqual(window["splitViews"], [{"id": 1, "numberOfTabs": 2}])
            self.assertEqual(state["maxSplitViewId"], 1)
            self.assertEqual(window["selected"], 1)
            self.assertIn(
                'user_pref("browser.startup.page", 3);',
                (profile / "user.js").read_text(),
            )
            self.assertIn(
                'user_pref("browser.tabs.splitView.enabled", true);',
                (profile / "user.js").read_text(),
            )

    def test_reseeding_replaces_urls_without_reading_an_old_session(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "sessionstore.jsonlz4").write_bytes(b"interrupted old session")
            helper.prepare(
                profile, "https://example.org/new-left", "https://example.org/new-right"
            )
            state = json.loads(
                self.decode((profile / "sessionstore.jsonlz4").read_bytes())
            )
            self.assertEqual(
                [tab["entries"][0]["url"] for tab in state["windows"][0]["tabs"]],
                ["https://example.org/new-left", "https://example.org/new-right"],
            )

    def test_urls_cannot_inject_config_lines(self):
        for url in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://example.org/\nOTHER=value",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                helper.validate_url(url)


class FixtureTests(unittest.TestCase):
    def test_readiness_and_page_fetches_do_not_count_as_browser_load_callbacks(self):
        class Handler(fixture.FixtureHandler):
            loaded = set()

            def log_message(self, *_args):
                pass

        server = fixture.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address

            def get(path):
                connection = HTTPConnection(host, port, timeout=2)
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return response.read()
                finally:
                    connection.close()

            for path in ("/ready", "/left", "/right"):
                get(path)
            self.assertEqual(json.loads(get("/status")), [])
            get("/loaded/left")
            self.assertEqual(json.loads(get("/status")), ["left"])
            with patch.object(fixture, "ADDRESS", f"http://{host}:{port}"):
                # A ready server and only one callback must not pass the check.
                with (
                    patch.object(fixture.time, "monotonic", side_effect=[0, 1, 61]),
                    patch.object(fixture.time, "sleep"),
                    self.assertRaises(RuntimeError),
                ):
                    fixture.wait_for("loaded")
                get("/loaded/right")
                fixture.wait_for("loaded")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.capture = self.directory / "capture.json"
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            CAPTURE=str(self.capture),
            HOME=str(self.directory / "home"),
            XDG_RUNTIME_DIR=str(self.directory / "runtime"),
            KIOSK_URL_LEFT="https://example.org/left?q=one&two=3",
            KIOSK_URL_RIGHT="https://example.org/right",
            WLR_BACKENDS="headless",
            DISPLAY=":123",
            WAYLAND_DISPLAY="old-socket",
            LIBSEAT_BACKEND="old-backend",
            SEATD_SOCK="old-seatd",
        )

        # Run the checkout module; the image smoke test verifies isolated imports
        # from site-packages. Replace the final graphics launch with a stub.
        self.executable(
            "python3",
            f"""import os, sys
assert sys.argv[1:4] == ['-I', '-m', 'kiosk.prepare_profile']
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[2:]])
""",
        )
        self.executable(
            "cage",
            """import json, os, sys
keys = ('HOME', 'XDG_RUNTIME_DIR', 'WLR_BACKENDS', 'WAYLAND_DISPLAY', 'DISPLAY',
        'LIBSEAT_BACKEND', 'SEATD_SOCK', 'MOZ_ENABLE_WAYLAND', 'GDK_BACKEND')
with open(os.environ['CAPTURE'], 'w') as stream:
    json.dump({'argv': sys.argv[1:], 'env': {key: os.environ.get(key) for key in keys}}, stream)
""",
        )

    def executable(self, name, contents):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + contents)
        path.chmod(0o755)
        return path

    def start(self):
        return subprocess.run(
            ["bash", str(ROOT / "container/kiosk-start")],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_headless_prepares_profile_and_launches_wayland_firefox(self):
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(self.capture.read_text())
        profile = Path(self.env["HOME"]) / "profile"
        self.assertEqual(
            captured["argv"],
            [
                "--",
                "firefox",
                "--no-remote",
                "--profile",
                str(profile),
                "--kiosk",
            ],
        )
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "LIBSEAT_BACKEND", "SEATD_SOCK"):
            self.assertIsNone(captured["env"][name])
        self.assertEqual(captured["env"]["GDK_BACKEND"], "wayland")
        self.assertEqual(captured["env"]["MOZ_ENABLE_WAYLAND"], "1")
        self.assertTrue((profile / "sessionstore.jsonlz4").is_file())
        for path in (profile, Path(self.env["XDG_RUNTIME_DIR"])):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for name in ("user.js", "sessionstore.jsonlz4"):
            self.assertEqual((profile / name).stat().st_mode & 0o777, 0o600)

    def test_invalid_profile_url_prevents_launch(self):
        self.env["KIOSK_URL_RIGHT"] = "file:///etc/passwd"
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Each kiosk URL must be an absolute http(s) URL", result.stderr)
        self.assertFalse(self.capture.exists())
        profile = Path(self.env["HOME"]) / "profile"
        self.assertFalse((profile / "sessionstore.jsonlz4").exists())

    def test_nested_wayland_preserves_parent_socket(self):
        for relative in (False, True):
            with (
                self.subTest(relative=relative),
                socket.socket(socket.AF_UNIX) as parent,
            ):
                runtime = Path(self.env["XDG_RUNTIME_DIR"])
                runtime.mkdir(exist_ok=True)
                name = f"parent-{relative}"
                parent.bind(str(runtime / name))
                self.env.update(
                    WLR_BACKENDS="wayland",
                    WAYLAND_DISPLAY=name if relative else str(runtime / name),
                )
                result = self.start()
                self.assertEqual(result.returncode, 0, result.stderr)
                captured = json.loads(self.capture.read_text())
                self.assertEqual(
                    captured["env"]["WAYLAND_DISPLAY"], self.env["WAYLAND_DISPLAY"]
                )
                self.assertIsNone(captured["env"]["DISPLAY"])
                self.assertIsNone(captured["env"]["SEATD_SOCK"])

    def test_unsupported_backend_prevents_launch(self):
        self.env["WLR_BACKENDS"] = "invalid"
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported WLR_BACKENDS", result.stderr)
        self.assertFalse(self.capture.exists())


class QuadletTests(unittest.TestCase):
    def test_generated_unit_uses_init_and_discards_container(self):
        if os.environ.get("KIOSK_SKIP_QUADLET_TEST") == "1":
            self.skipTest("KIOSK_SKIP_QUADLET_TEST is set")
        if not QUADLET_GENERATOR.is_file():
            self.skipTest(
                f"Podman systemd generator not installed at {QUADLET_GENERATOR}"
            )
        with tempfile.TemporaryDirectory() as directory:
            source = (ROOT / "host/kiosk.container.in").read_text()
            source = source.replace("@IMAGE@", "registry.example.org/kiosk:test")
            source = source.replace("@RENDER_GID@", "44")
            (Path(directory) / "kiosk.container").write_text(source)
            env = dict(os.environ, QUADLET_UNIT_DIRS=directory)
            result = subprocess.run(
                [QUADLET_GENERATOR, "--dryrun"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--init", result.stdout)
            self.assertIn("--rm", result.stdout)
            self.assertIn("--stop-timeout 10", result.stdout)
            self.assertNotIn("/var/lib/kiosk/home", result.stdout)
            command = next(
                line.removeprefix("ExecStart=")
                for line in result.stdout.splitlines()
                if line.startswith("ExecStart=")
            )
            # Quadlet versions may use --option=value or --option value.
            arguments = [
                part for arg in shlex.split(command) for part in arg.split("=", 1)
            ]
            for option, value in (("--cap-drop", "all"), ("--cap-add", "sys_chroot")):
                self.assertIn(option, arguments)
                self.assertEqual(arguments[arguments.index(option) + 1].lower(), value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
