#!/usr/bin/env python3
"""Optional native WebRTC browser prototype; localhost ONLY.

Not the tablet LAN service. A full remote browser can be an exfiltration
channel for managed-device software; intentional security boundary here.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"


def create_credentials(path: Path = ENV):
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Credential path must be a normal file")
        if path.stat().st_mode & 0o077:
            raise RuntimeError("Credential file is accessible to other users; chmod 600")
        return
    # O_EXCL prevents overwriting a concurrent file. No user-supplied secrets.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("RELAY_BROWSER_USER=" + secrets.token_urlsafe(32) + "\n")
            out.write("RELAY_BROWSER_ADMIN=" + secrets.token_urlsafe(32) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def docker(*args):
    return subprocess.run(
        ["docker", "compose", "--project-name", "relay-browser-local",
         "--project-directory", str(HERE),
         "-f", str(HERE / "compose.yaml"),
         "--env-file", str(ENV), *args],
        check=False,
    ).returncode


def main(argv=None):
    p = argparse.ArgumentParser(description="RELAY browser prototype (Mac-local ONLY)")
    p.add_argument("action", choices=["start", "stop", "check", "credentials"])
    args = p.parse_args(argv)
    if sys.platform != "darwin":
        print("This deployment is intended for the actual macOS host.", file=sys.stderr)
        return 2
    if not shutil.which("docker"):
        print("Docker Desktop is required. Install it and start the Docker engine.", file=sys.stderr)
        return 2
    if args.action == "stop":
        if not ENV.is_file():
            print("No browser-lab credentials; nothing to stop.")
            return 0
        return docker("down")
    create_credentials()
    if args.action == "credentials":
        # Never print passwords to logs/Terminal without an explicit action.
        print("Credentials: " + str(ENV) + " (owner-only file)")
        return 0
    if docker("config", "--quiet"):
        print("Compose configuration invalid.", file=sys.stderr)
        return 2
    if args.action == "check":
        print("Docker Compose parsed. No container started and no network was opened.")
        return 0
    print("Starting LOCAL-ONLY Firefox browser; only Mac can access 127.0.0.1:18080.")
    if docker("up", "-d"):
        print("Startup failed. Check Docker Desktop status and port conflicts.", file=sys.stderr)
        return 2
    print("Visit http://127.0.0.1:18080 on your Mac.")
    print("Open browser-lab/.env locally to get the generated login password.")
    print("Do not connect the managed tablet to this service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
