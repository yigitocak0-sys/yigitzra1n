#!/usr/bin/env python3
"""Pull iOS panic-full logs over USB SSH and optionally send zipped logs to telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from usb_ssh import open_usb_ssh

DOMAIN_SALT = b"yigitzra1n:one-way-anon-client:v1"


def derive_client_id() -> str:
    try:
        out = subprocess.check_output(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
    except Exception:
        return ""
    m = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', out) or re.search(
        r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out
    )
    if not m or not m.group(1).strip():
        return ""
    return hashlib.pbkdf2_hmac("sha256", m.group(1).strip().encode("utf-8"), DOMAIN_SALT, 200_000).hex()[:16]


def summarize_ips(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    dec = json.JSONDecoder()
    idx = 0
    objs = []
    while idx < len(raw) and len(objs) < 2:
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        try:
            obj, end = dec.raw_decode(raw, idx)
            if isinstance(obj, dict):
                objs.append(obj)
            idx = end
        except Exception:
            break

    meta = objs[0] if len(objs) >= 1 else {}
    body = objs[1] if len(objs) >= 2 else meta
    pstr = str(body.get("panicString") or "").strip().splitlines()
    first_line = pstr[0][:160] if pstr else "(no panicString)"
    return {
        "name": path.name,
        "date": str(body.get("date") or meta.get("timestamp") or "-"),
        "os": str(meta.get("os_version") or body.get("build") or "-"),
        "soc": str(body.get("socId") or "-"),
        "summary": first_line,
    }


def pull_panic_files(host: str, port: int, password: str, ssh_opts: list[str], dest_dir: Path) -> list[Path]:
    remote_find = (
        "find /private/var/mobile/Library/Logs/CrashReporter /private/var/mobile/Library/Logs/CrashReporter/Panics "
        "-maxdepth 1 -type f -name 'panic-full*' 2>/dev/null | sort -u"
    )
    r = subprocess.run(
        ["sshpass", "-p", password, "ssh", *ssh_opts, f"root@{host}", remote_find],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Failed listing panic logs over SSH: {r.stderr.strip()}")
    paths = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not paths:
        return []

    dest_dir.mkdir(parents=True, exist_ok=True)
    # Stream files safely as tar from their parent base
    tar_cmd = (
        "tar -cf - "
        + " ".join(
            f"-C '{os.path.dirname(p)}' '{os.path.basename(p)}'"
            for p in paths
            if "'" not in p and "\n" not in p
        )
    )
    ssh_tar = subprocess.Popen(
        ["sshpass", "-p", password, "ssh", *ssh_opts, f"root@{host}", tar_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ext = subprocess.run(["tar", "-C", str(dest_dir), "-xf", "-"], stdin=ssh_tar.stdout, capture_output=True)
    if ssh_tar.stdout:
        ssh_tar.stdout.close()
    _, err_b = ssh_tar.communicate(timeout=15.0)
    if ssh_tar.returncode != 0 or ext.returncode != 0:
        raise RuntimeError(f"Failed transferring panic logs: {err_b.decode('utf-8', 'replace').strip()}")

    return sorted([p for p in dest_dir.iterdir() if p.is_file() and p.name.startswith("panic-full")])


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull or send iPhone panic-full logs")
    ap.add_argument("action", choices=("check", "send"))
    ap.add_argument("--caller-pwd", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--url", required=True)
    ap.add_argument("--tool-version", default="1.25")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    ts_lbl = datetime.now().strftime("%Y%m%d_%H%M%S")
    caller_pwd = Path(args.caller_pwd)

    with open_usb_ssh(host=args.host, explicit_port=args.port) as (host, port, pw, opts):
        if args.action == "check":
            if args.out:
                out_dir = Path(args.out).expanduser()
                if not out_dir.is_absolute():
                    out_dir = caller_pwd / out_dir
            else:
                base = caller_pwd if os.access(caller_pwd, os.W_OK) else (Path.home() / "Downloads")
                out_dir = base / f"yigitzra1n-panics-{ts_lbl}"
            files = pull_panic_files(host, port, pw, opts, out_dir)
            if not files:
                try:
                    out_dir.rmdir()
                except OSError:
                    pass
                print("[+] No panic-full logs found on device.")
                return 0
            print(f"[+] Pulled {len(files)} panic-full log(s) into: {out_dir}")
            for fp in files:
                info = summarize_ips(fp)
                print(f"  - {info['name']} ({fp.stat().st_size} B) | {info['date']} | SoC {info['soc']}")
                print(f"    {info['summary']}")
            return 0

        with tempfile.TemporaryDirectory(prefix="yigitz_panic_") as td_s:
            td = Path(td_s)
            files_dir = td / "logs"
            files = pull_panic_files(host, port, pw, opts, files_dir)
            if not files:
                print("[+] No panic-full logs found on device. Nothing to send.")
                return 0

            summaries = [summarize_ips(fp) for fp in files]
            print(f"[*] Found {len(files)} panic-full log(s) on device:")
            for info in summaries:
                print(f"  - {info['name']} | {info['date']} | {info['summary']}")

            if not args.yes:
                if not sys.stdin.isatty():
                    print("[ERROR] Confirmation required in non-interactive mode (pass --yes or --force).", file=sys.stderr)
                    return 1
                print("")
                print("[!] All panic-full logs listed above will be zipped and shared with the developer for bug fixing.")
                ans = input("    Continue? [y/N]: ").strip().lower()
                if ans not in ("y", "yes"):
                    print("[*] Cancelled.")
                    return 0

            zip_path = td / f"panic-full-{ts_lbl}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fp in files:
                    zf.write(fp, arcname=fp.name)

            zip_bytes = zip_path.read_bytes()
            host_os = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip()
            top_sum = summaries[-1]["summary"][:200] if summaries else ""
            soc = summaries[-1]["soc"] if summaries else ""

            req = urllib.request.Request(
                args.url,
                data=zip_bytes,
                method="POST",
                headers={
                    "User-Agent": f"yigitzra1n-cli/{args.tool_version}",
                    "Content-Type": "application/zip",
                    "X-Client-Id": derive_client_id(),
                    "X-Host-OS": host_os or platform.mac_ver()[0],
                    "X-Host-Arch": platform.machine(),
                    "X-Tool-Version": args.tool_version,
                    "X-Device-Soc": soc,
                    "X-Panic-Count": str(len(files)),
                    "X-Panic-Summary": top_sum.encode("ascii", errors="ignore").decode("ascii"),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
                    print(f"[+] Sent {len(files)} panic log(s) ({len(zip_bytes)} bytes zip) -> id={body.get('id', '?')}")
            except urllib.error.HTTPError as e:
                err_b = e.read().decode("utf-8", errors="replace")
                print(f"[ERROR] Upload failed (HTTP {e.code}): {err_b}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
