#!/usr/bin/env python3
"""yigitzra1n doctor — host environment, build artifacts, and USB iOS device checks."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


def sha256_short(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:16]


def which_bin(name: str, extra_dirs: tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")) -> str:
    found = shutil.which(name)
    if found and os.access(found, os.X_OK):
        return found
    for d in extra_dirs:
        cand = Path(d) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return ""


def run_cmd(cmd: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:
        return 127, "", str(exc)


def main() -> int:
    ap = argparse.ArgumentParser(description="yigitzra1n environment & device doctor")
    ap.add_argument("--root", required=True)
    ap.add_argument("--ipsw-dir", required=True)
    ap.add_argument("--caller-pwd", default="")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    ipsw_dir = Path(args.ipsw_dir)
    build_dir = root / "build"
    engine_dir = build_dir / "engine"
    venv_py = root / ".venv/bin/python3"

    ok_cnt = 0
    warn_cnt = 0
    fail_cnt = 0

    def p_ok(msg: str) -> None:
        nonlocal ok_cnt
        ok_cnt += 1
        print(f"  [OK]   {msg}")

    def p_warn(msg: str) -> None:
        nonlocal warn_cnt
        warn_cnt += 1
        print(f"  [WARN] {msg}")

    def p_fail(msg: str) -> None:
        nonlocal fail_cnt
        fail_cnt += 1
        print(f"  [FAIL] {msg}")

    def p_info(msg: str) -> None:
        print(f"  [INFO] {msg}")

    print("======================================================================")
    print("  yigitzra1n doctor — Host, Toolchain & Device Diagnostics")
    print("======================================================================")

    # 1. Host OS & paths
    print("\n[1] Host & Working Directory")
    if platform.system() != "Darwin":
        p_fail(f"Unsupported OS: {platform.system()} (macOS required)")
    else:
        _, ver_out, _ = run_cmd(["sw_vers", "-productVersion"])
        macos_ver = ver_out.strip() or platform.mac_ver()[0]
        p_ok(f"macOS {macos_ver} ({platform.machine()})")

    check_path = args.caller_pwd or str(root)
    if "Mobile Documents" in check_path or "com~apple~CloudDocs" in check_path:
        p_warn(f"Path is inside iCloud Drive ({check_path}); large IPSWs may be offloaded")
    elif any(seg in check_path for seg in ("/Desktop", "/Documents")) and "Application Support" not in str(root):
        p_warn(f"Source path ({check_path}) may be synced by iCloud Drive Desktop/Documents")
    else:
        p_ok(f"Install path: {root}")

    # 2. Core macOS / Xcode CLT tools
    print("\n[2] Xcode CLT & Host Build Tools")
    rc_clt, clt_out, _ = run_cmd(["xcode-select", "-p"])
    if rc_clt == 0 and clt_out.strip():
        p_ok(f"Developer dir: {clt_out.strip()}")
    else:
        p_fail("Xcode Command Line Tools missing (run: xcode-select --install)")

    for tool in ("clang", "codesign", "hdiutil", "asr", "curl", "tar", "unzip", "python3"):
        path = which_bin(tool)
        if path:
            p_ok(f"{tool}: {path}")
        else:
            p_fail(f"Missing core tool: {tool} (run: xcode-select --install)")

    # 3. Homebrew & USB / SSH dependencies
    print("\n[3] Homebrew & USB/SSH Dependencies")
    brew = which_bin("brew")
    if brew:
        p_ok(f"brew: {brew}")
    else:
        p_fail("Homebrew not installed (https://brew.sh)")

    for tool, pkg in (
        ("zstd", "zstd"),
        ("iproxy", "libimobiledevice"),
        ("idevice_id", "libimobiledevice"),
        ("ideviceinfo", "libimobiledevice"),
        ("irecovery", "libirecovery"),
        ("sshpass", "hudochenkov/sshpass/sshpass"),
    ):
        path = which_bin(tool)
        if path:
            p_ok(f"{tool}: {path}")
        else:
            p_fail(f"Missing {tool} (brew install {pkg})")

    libusb_found = next(
        (p for p in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib") if os.path.isfile(p)),
        "",
    )
    if libusb_found:
        p_ok(f"libusb: {libusb_found}")
    else:
        p_fail("Missing libusb-1.0.dylib (brew install libusb)")

    # 4. Python venv & packages
    print("\n[4] Python Virtual Environment")
    if venv_py.is_file() and os.access(venv_py, os.X_OK):
        rc_mod, mod_out, mod_err = run_cmd(
            [str(venv_py), "-c", "import pyimg4, capstone, remotezip; print('pyimg4,capstone,remotezip')"],
            timeout=5.0,
        )
        if rc_mod == 0:
            p_ok(f".venv modules ready ({mod_out.strip()})")
        else:
            p_fail(f".venv module check failed: {(mod_err or mod_out).strip()}")
    else:
        p_warn(".venv not created yet (created automatically on first prepare/install)")

    # 5. Disk & project artifacts
    print("\n[5] Disk Space, IPSWs & Boot Block")
    try:
        usage = shutil.disk_usage(root if root.exists() else Path.home())
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= 35:
            p_ok(f"Free disk space: {free_gb:.1f} GB")
        elif free_gb >= 5:
            p_warn(f"Free disk space: {free_gb:.1f} GB (35+ GB recommended for a fresh prepare)")
        else:
            p_fail(f"Critically low disk space: {free_gb:.1f} GB (< 5 GB)")
    except Exception as exc:
        p_warn(f"Could not check disk space: {exc}")

    ipsw_globs = [
        ("iPhone 6s 15.8.8", "iPhone_4.7_15.8.8_19H422_Restore.ipsw"),
        ("iPad 5 16.7.16", "iPad_64bit_TouchID_ASTC_16.7.16_20H392_Restore.ipsw"),
        ("iPhone 8 16.7.16", "iPhone_4.7_P3_16.7.16_20H392_Restore.ipsw"),
    ]
    for label, exact in ipsw_globs:
        matches = list(ipsw_dir.glob(exact)) if ipsw_dir.is_dir() else []
        if matches and matches[0].is_file():
            sz_gb = matches[0].stat().st_size / (1024 ** 3)
            p_ok(f"IPSW {label}: {matches[0].name} ({sz_gb:.2f} GB)")
        else:
            p_info(f"IPSW {label}: not downloaded yet (fetched during prepare)")

    block_dir = engine_dir / "block"
    blocks = []
    if block_dir.is_dir():
        for pat in ("*pteblock*.bin", "*shcblock*.bin", "*.bin"):
            blocks.extend(sorted(block_dir.glob(pat), key=lambda x: x.stat().st_mtime, reverse=True))
    if blocks:
        blk = blocks[0]
        p_ok(f"Boot block: {blk.name} ({blk.stat().st_size} B, sha256:{sha256_short(blk)})")
    else:
        p_info("No saved SHC/PTE boot block yet (captured during install)")

    kpf_ok = (build_dir / "kernelcache_v6").is_file() or any(build_dir.glob("kernelcache*")) if build_dir.is_dir() else False
    if (build_dir / "ssh_ramdisk_ipsw").is_dir() and (build_dir / "bin/hw_init_probe.bin").is_file():
        p_ok("Patched build artifacts present (ready for install/boot)")
    elif kpf_ok:
        p_warn("Partial build/ artifacts found — run 'yigitzra1n prepare' to finish")
    else:
        p_info("No build/ artifacts yet (run 'yigitzra1n prepare')")

    # 6. Connected USB device checks
    print("\n[6] Connected USB iOS Device")
    irec = which_bin("irecovery")
    idevice_id = which_bin("idevice_id")
    ideviceinfo = which_bin("ideviceinfo")
    iproxy = which_bin("iproxy")
    sshpass = which_bin("sshpass")

    found_any_dev = False
    if irec:
        rc_ir, ir_out, ir_err = run_cmd([irec, "-q"], timeout=2.0)
        txt = ir_out + "\n" + ir_err
        if rc_ir == 0 and "CPID:" in txt:
            found_any_dev = True
            kv = {}
            for line in txt.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    kv[k.strip().upper()] = v.strip()
            mode = kv.get("MODE", "unknown")
            cpid = kv.get("CPID", "").lower()
            bdid = kv.get("BDID", "").lower()
            pwnd = kv.get("PWND", "none")
            p_info(f"USB Recovery/DFU device: MODE={mode} CPID={cpid} BDID={bdid} PWND={pwnd}")
            if bdid and bdid != "0x04":
                p_fail(f"Unsupported board BDID={bdid} (only 4.7-inch iPhone 6s BDID 0x04 is supported)")
            elif cpid == "0x8003":
                p_ok("TSMC A9 (CPID 0x8003 / N71mAP) — primary supported target")
            elif cpid == "0x8000":
                p_warn("Samsung A9 (CPID 0x8000 / N71AP) — experimental target; issues may occur")
            else:
                p_fail(f"Unsupported CPID {cpid}")
            if mode == "Recovery":
                p_info("Device is in Recovery mode (for DFU hold Power+Home, or run 'irecovery -n' to exit)")

    if not found_any_dev and idevice_id and ideviceinfo:
        rc_id, id_out, _ = run_cmd([idevice_id, "-l"], timeout=2.5)
        udids = [u.strip() for u in id_out.splitlines() if u.strip() and not u.startswith(("SSHRD", "ffffffff"))]
        if rc_id == 0 and udids:
            found_any_dev = True
            udid = udids[0]
            def prop(k: str) -> str:
                r, o, _ = run_cmd([ideviceinfo, "-u", udid, "-k", k], timeout=2.5)
                return o.strip() if r == 0 else ""

            ptype = prop("ProductType")
            hw = prop("HardwareModel")
            ios_ver = prop("ProductVersion")
            ios_build = prop("BuildVersion")
            p_info(f"USB Normal device: ProductType={ptype or '?'} HardwareModel={hw or '?'} iOS={ios_ver or '?'} ({ios_build or '?'})")
            if ptype == "iPhone8,1":
                if hw.lower() == "n71ap":
                    p_warn("Samsung A9 (N71AP / CPID 0x8000) — experimental target; issues may occur")
                else:
                    p_ok(f"Supported iPhone 6s ({hw or 'N71mAP'})")
                if ios_ver == "15.8.8":
                    p_ok("iOS 15.8.8 baseline detected — ready for 'yigitzra1n install'")
                elif ios_ver == "16.7.16":
                    p_ok("iOS 16.7.16 installed on device")
                    if iproxy and sshpass:
                        port = 2297
                        ip_args = [iproxy, "-u", udid, f"{port}:22"]
                        ip_proc = subprocess.Popen(ip_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        try:
                            time.sleep(0.7)
                            rc_ssh, s_out, _ = run_cmd(
                                [
                                    sshpass, "-p", "alpine", "ssh",
                                    "-o", "StrictHostKeyChecking=no",
                                    "-o", "UserKnownHostsFile=/dev/null",
                                    "-o", "PubkeyAuthentication=no",
                                    "-o", "PreferredAuthentications=password",
                                    "-o", "ConnectTimeout=3",
                                    "-p", str(port), "root@127.0.0.1",
                                    "test -S /var/run/yigitzra1n_gmr.sock && echo SSH_AND_GMR_OK || echo SSH_OK",
                                ],
                                timeout=5.0,
                            )
                            if rc_ssh == 0 and "SSH_AND_GMR_OK" in s_out:
                                p_ok("USB SSH & Sileo root helper daemon reachable")
                            elif rc_ssh == 0 and "SSH_OK" in s_out:
                                p_warn("USB SSH reachable, but Sileo gmr daemon socket missing (run 'yigitzra1n sileo')")
                            else:
                                p_warn("iOS 16.7.16 detected, but USB SSH did not respond yet")
                        finally:
                            ip_proc.terminate()
                            try:
                                ip_proc.wait(timeout=1.5)
                            except Exception:
                                ip_proc.kill()
                elif ios_ver:
                    p_warn(f"Device is on iOS {ios_ver} (install requires iOS 15.8.8 baseline)")
            elif ptype:
                p_fail(f"Unsupported device {ptype} (only iPhone 6s / iPhone8,1 is supported)")

    if not found_any_dev:
        p_info("No USB iOS device detected (plug in your iPhone 6s via USB-A to Lightning for device checks)")
        p_info("Tip: Avoid USB-C to Lightning cables — they often fail DFU exploitation on A9")

    print("\n----------------------------------------------------------------------")
    print(f"  Summary: {ok_cnt} ok, {warn_cnt} warnings, {fail_cnt} failures")
    print("----------------------------------------------------------------------")
    return 1 if fail_cnt > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
