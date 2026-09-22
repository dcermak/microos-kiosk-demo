#!/usr/bin/env python3
"""Create the Combustion config tree from explicit deployment values."""

import argparse
import base64
import getpass
import os
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

from .prepare_profile import validate_url

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_IMAGE = "ghcr.io/dcermak/microos-kiosk-demo/firefox:latest"


def environment_value(value):
    # Podman --env-file is KEY=value, NOT shell or systemd Environment= syntax.
    # EnvironmentFile= in Quadlet becomes Podman's --env-file.
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Environment values cannot contain control characters")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=(
            "Fully qualified registry image, preferably @sha256 digest "
            f"(default: {DEFAULT_IMAGE})"
        ),
    )
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    parser.add_argument(
        "--ssh-key",
        type=Path,
        action="append",
        required=True,
        help="OpenSSH public-key file, repeat for multiple keys",
    )
    parser.add_argument("--extra-package", action="append", default=[])
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "nuc-config")
    args = parser.parse_args(argv)

    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", args.image)
        or "/" not in args.image
    ):
        parser.error("--image must be a fully qualified registry reference")
    if "://" in args.image:
        parser.error("An image reference must not include https://")

    for package in args.extra_package:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.+:_-]*", package):
            parser.error(f"Invalid package name: {package!r}")

    return args


def read_ssh_keys(paths):
    keys = []
    for keyfile in paths:
        for line in keyfile.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue

            fields = line.split()
            if len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
                raise ValueError(f"Expected an OpenSSH public key in {keyfile}")

            try:
                base64.b64decode(fields[1], validate=True)
            except ValueError:
                raise ValueError(f"Invalid public-key encoding in {keyfile}") from None

            keys.append(line)

    if not keys:
        raise ValueError("At least one nonempty SSH public key is required")

    return keys


def prompt_root_password_hash():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("Root password: ")
            confirmation = getpass.getpass("Confirm root password: ")
        except getpass.GetPassWarning:
            raise ValueError(
                "A terminal with hidden password input is required"
            ) from None

    if not password:
        raise ValueError("Root password cannot be empty")
    if password != confirmation:
        raise ValueError("Root passwords do not match")
    if any(c in password for c in "\r\n\0"):
        raise ValueError("Root password cannot contain line breaks or NUL characters")

    # OpenSSL passwd reads at most 256 bytes and silently truncates longer input.
    encoded = password.encode("utf-8")
    if len(encoded) > 256:
        raise ValueError("Root password cannot exceed 256 UTF-8 bytes")

    password_hash = (
        subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            input=encoded + b"\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode("ascii")
        .strip()
    )

    if not password_hash.startswith("$6$") or any(c.isspace() for c in password_hash):
        raise ValueError("OpenSSL did not return a single SHA-512 crypt hash")

    return password_hash


def main():
    args = parse_args()
    left = environment_value(validate_url(args.left))
    right = environment_value(validate_url(args.right))
    keys = read_ssh_keys(args.ssh_key)
    quadlet = (ROOT / "host" / "kiosk.container.in").read_text()
    quadlet = quadlet.replace("@IMAGE@", args.image)

    output = args.output.resolve()
    if output.exists():
        raise ValueError(
            f"Output already exists: {output}; choose a new --output directory"
        )

    password_hash = prompt_root_password_hash()
    config = output / "combustion"
    previous_umask = os.umask(0o077)
    try:
        output.mkdir(parents=True)
        try:
            shutil.copytree(ROOT / "host" / "combustion", config)
            (config / "authorized_keys").write_text("\n".join(keys) + "\n")
            (config / "root-password.hash").write_text(password_hash + "\n")
            (config / "extra-packages.txt").write_text(
                "\n".join(args.extra_package) + "\n"
            )
            (config / "kiosk.env").write_text(
                f"KIOSK_URL_LEFT={left}\nKIOSK_URL_RIGHT={right}\n"
            )
            (config / "kiosk.container").write_text(quadlet)
        except OSError as error:
            raise OSError(
                f"{error}; incomplete output at {output}. Remove it before retrying."
            ) from None
    finally:
        os.umask(previous_umask)

    print(f"Created {output}")
    print(
        "Review the generated configuration, then run ./build-installer.sh INPUT.iso OUTPUT.iso"
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
    except (EOFError, KeyboardInterrupt):
        sys.exit("Configuration cancelled")
