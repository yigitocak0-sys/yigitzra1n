#!/usr/bin/env python3
"""Shared USB SSH session helper and SHC block export/import helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_free_port(start: int = 2230) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def detect_target_udid() -> str:
    if not shutil.which("idevice_id") or not shutil.which("ideviceinfo"):
        return ""
    try:
        out = subprocess.check_output(["idevice_id", "-l"], text=True, stderr=subprocess.DEVNULL, timeout=3.0)
    except Exception:
        return ""
    for line in out.splitlines():
        uid = line.strip()
        if not uid or uid.startswith(("SSHRD", "ffffffff")):
            continue
        try:
            ptype = subprocess.check_output(
                ["ideviceinfo", "-u", uid, "-k", "ProductType"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            ).strip()
            if ptype == "iPhone8,1":
                return uid
        except Exception:
            continue
    return ""


@contextmanager
def open_usb_ssh(host: str = "127.0.0.1", explicit_port: int = 0, password: str = "alpine", wait_tries: int = 12):
    for req in ("sshpass", "ssh"):
        if not shutil.which(req):
            raise RuntimeError(f"Missing required tool: {req}")

    is_local = host in ("127.0.0.1", "localhost")
    port = explicit_port if explicit_port > 0 else (find_free_port() if is_local else 22)
    iproxy_proc = None

    def ssh_opts(p: int) -> list[str]:
        return [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ConnectTimeout=3",
            "-p", str(p),
        ]

    try:
        if is_local and explicit_port == 0 and shutil.which("iproxy"):
            udid = detect_target_udid()
            cmd = ["iproxy"]
            if udid:
                cmd += ["-u", udid]
            help_txt = subprocess.run(["iproxy", "--help"], capture_output=True, text=True).stdout
            if "LOCAL_PORT:DEVICE_PORT" in help_txt:
                cmd.append(f"{port}:22")
            else:
                cmd += [str(port), "22"]
            iproxy_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)

        ready = False
        for _ in range(wait_tries):
            r = subprocess.run(
                ["sshpass", "-p", password, "ssh", *ssh_opts(port), f"root@{host}", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
            )
            if r.returncode == 0:
                ready = True
                break
            time.sleep(1.5)

        if not ready:
            raise RuntimeError("Could not connect to iOS SSH over USB. Boot into iOS 16 and keep USB connected.")

        yield host, port, password, ssh_opts(port)
    finally:
        if iproxy_proc is not None:
            iproxy_proc.terminate()
            try:
                iproxy_proc.wait(timeout=2.0)
            except Exception:
                iproxy_proc.kill()
