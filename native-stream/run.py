#!/usr/bin/env python3
"""RELAY Island native macOS *view-only* web-window stream.

Uses the independent Lumen CLI; does not install software, modify networking,
start Internet Sharing, or make the managed tablet's web access safe.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from relay_web.isolation import IsolationError, check  # noqa: E402


def parser():
    p = argparse.ArgumentParser(description="View-only macOS browser window over verified-looking offline LAN")
    p.add_argument("--host", required=True, help="Private IPv4 address of the dedicated LAN adapter")
    p.add_argument("--interface", required=True, help="Dedicated LAN interface (e.g. en7)")
    p.add_argument("--window", required=True, type=int, help="Window id from 'lumen windows' (browser only)")
    p.add_argument("--fps", type=int, default=30)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if sys.platform != "darwin":
        print("Only supported on an actual macOS host.", file=sys.stderr)
        return 2
    if a.window <= 0 or not 1 <= a.fps <= 30:
        print("Invalid window id or frame rate (1–30 supported).", file=sys.stderr)
        return 2
    lumen = shutil.which("lumen")
    if not lumen:
        print("Lumen is not installed. See native-stream/README.md; no automatic install performed.", file=sys.stderr)
        return 2

    try:
        result = check(a.host, a.interface)
    except IsolationError as exc:
        print("LAN PREFLIGHT FAILED: " + str(exc), file=sys.stderr)
        return 2

    print("Network preflight passed for", result["interface"], a.host)
    print("NOT VERIFIED: access point WAN isolation, tablet alternate networks, packet-level forwarding.")
    print("The viewer must still be approved locally on the Mac.")
    print("View-only: this process does not forward keyboard or clicks to the Mac.")
    print("This process will terminate Lumen if periodic preflight fails.")

    cmd = [lumen, "serve", "--bind", a.host, "--window", str(a.window),
           "--fps", str(a.fps), "--quality", "medium", "--no-qr"]
    proc = None
    try:
        proc = subprocess.Popen(cmd, start_new_session=True)
        while proc.poll() is None:
            time.sleep(3)
            try:
                check(a.host, a.interface)
            except Exception as exc:
                print("LAN CHECK FAILED; stopping Lumen: " + str(exc), file=sys.stderr)
                return 3
        return proc.returncode
    except KeyboardInterrupt:
        print("Stopping native stream.")
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
