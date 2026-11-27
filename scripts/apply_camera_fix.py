#!/usr/bin/env python3
"""Ensure iOS 16 N71/N71m camera TMBNR plist fix is applied on a live device over SSH."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from patch_cmcapture_plist import patch_avcapture_plist
from usb_ssh import open_usb_ssh

MARKER_PATH = "/usr/local/share/yigitzra1n/.cam_v1_ready"
VOLATILE_CACHE = "/var/mobile/Library/Preferences/com.apple.cameracapture.volatile.plist"


def _run_ssh(host: str, opts: list[str], password: str, cmd: str, stdin_bytes: bytes | None = None, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sshpass", "-p", password, "ssh", *opts, f"root@{host}", cmd],
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
    )


def apply_camera_fix(
    host: str,
    port: int,
    password: str,
    wait_tries: int,
    force: bool = False,
    restart_media: bool = True,
) -> bool:
    """Apply AVCaptureSession.plist TMBNR fix if missing; returns True if changes were made."""
    with open_usb_ssh(host=host, explicit_port=port, password=password, wait_tries=wait_tries) as (h, _p, pw, opts):
        if not force:
            chk = _run_ssh(
                h, opts, pw,
                f"test -f '{MARKER_PATH}' && echo CAM_FIX_OK || true",
                timeout=8.0,
            )
            if chk.returncode == 0 and b"CAM_FIX_OK" in chk.stdout:
                print("[+] Camera TMBNR fix already applied on device.")
                return False

        any_changed = False
        with tempfile.TemporaryDirectory(prefix="yigitz_camfix_") as td_s:
            td = Path(td_s)
            for model in ("N71", "N71m"):
                remote_plist = f"/System/Library/PrivateFrameworks/CMCapture.framework/{model}/AVCaptureSession.plist"
                bkp_name = f"{model}_AVCaptureSession.plist.orig"
                pull = _run_ssh(
                    h, opts, pw,
                    f"set -e; /sbin/mount -uw / 2>/dev/null || true; "
                    f"mkdir -p /usr/local/share/backups; "
                    f"[ -f '{remote_plist}' ] || exit 44; "
                    f"[ -f '/usr/local/share/backups/{bkp_name}' ] || cp -p '{remote_plist}' '/usr/local/share/backups/{bkp_name}'; "
                    f"cat '{remote_plist}'",
                    timeout=15.0,
                )
                if pull.returncode == 44:
                    continue
                if pull.returncode != 0 or not pull.stdout:
                    err = pull.stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"Failed reading {remote_plist}: {err}")

                local_plist = td / f"{model}_AVCaptureSession.plist"
                local_plist.write_bytes(pull.stdout)
                if patch_avcapture_plist(local_plist):
                    any_changed = True
                    time.sleep(0.5)
                    push = _run_ssh(
                        h, opts, pw,
                        f"set -e; /sbin/mount -uw / 2>/dev/null || true; "
                        f"cat > /var/tmp/avcap_{model}.plist; "
                        f"mv /var/tmp/avcap_{model}.plist '{remote_plist}'; "
                        f"chown root:wheel '{remote_plist}'; "
                        f"chmod 644 '{remote_plist}'",
                        stdin_bytes=local_plist.read_bytes(),
                        timeout=15.0,
                    )
                    if push.returncode != 0:
                        err = push.stderr.decode("utf-8", errors="replace").strip()
                        raise RuntimeError(f"Failed writing {remote_plist}: {err}")

        # Always clear volatile cache and stamp marker if we modified plists or marker was missing
        finalize_cmd = (
            "set -e; export PATH='/var/jb/usr/bin:/usr/bin:/bin:/sbin'; "
            "/sbin/mount -uw / 2>/dev/null || true; "
            "mkdir -p /usr/local/share/yigitzra1n; "
            f": > '{MARKER_PATH}'; "
            "chown root:wheel /usr/local/share/yigitzra1n "
            f"'{MARKER_PATH}'; chmod 644 '{MARKER_PATH}'"
        )
        if any_changed:
            finalize_cmd += (
                f"; rm -f '{VOLATILE_CACHE}' 2>/dev/null || true; "
                "killall -9 cfprefsd 2>/dev/null || true"
            )
            if restart_media:
                finalize_cmd += (
                    "; if [ -f /System/Library/LaunchDaemons/com.apple.mediaserverd.plist ] && command -v launchctl >/dev/null 2>&1; then "
                    "launchctl bootout user/501 /System/Library/LaunchDaemons/com.apple.mediaserverd.plist >/dev/null 2>&1 || true; "
                    "sleep 0.5; "
                    "launchctl bootstrap user/501 /System/Library/LaunchDaemons/com.apple.mediaserverd.plist >/dev/null 2>&1 || true; "
                    "launchctl kickstart -kp user/501/com.apple.mediaserverd >/dev/null 2>&1 || true; "
                    "else killall mediaserverd 2>/dev/null || true; fi"
                )
        fin = _run_ssh(h, opts, pw, finalize_cmd, timeout=20.0)
        if fin.returncode != 0:
            err = fin.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed finalizing camera fix on device: {err}")

        if any_changed:
            print("[+] Applied camera TMBNR fix (N71/N71m AVCaptureSession.plist + mediaserverd refresh).")
        else:
            print("[+] Verified camera TMBNR fix already present on device.")
        return any_changed


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply N71/N71m iOS 16 camera plist fix over SSH")
    ap.add_argument("--host", default=os.environ.get("DEVICE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DEVICE_PORT", "0") or 0))
    ap.add_argument("--password", default=os.environ.get("SSH_PASS", "alpine"))
    ap.add_argument("--wait-tries", type=int, default=30)
    ap.add_argument("--require-ssh", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-restart-media", action="store_true")
    args = ap.parse_args()

    try:
        apply_camera_fix(
            host=args.host,
            port=args.port,
            password=args.password,
            wait_tries=args.wait_tries,
            force=args.force,
            restart_media=not args.no_restart_media,
        )
        return 0
    except Exception as exc:
        if args.require_ssh:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        print(f"[!] Camera fix check skipped (non-fatal): {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
