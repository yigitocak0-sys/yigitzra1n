#!/usr/bin/env python3
"""Native IPA sideloading onto tethered iOS 16 device over USB SSH."""

from __future__ import annotations

import argparse
import base64
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from usb_ssh import open_usb_ssh

MACHO_MAGICS = {0xFEEDFACE, 0xCEFAEDFE, 0xFEEDFACF, 0xCFFAEDFE, 0xCAFEBABE, 0xBEBAFECA}


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as f:
            hdr = f.read(4)
        return len(hdr) == 4 and struct.unpack("<I", hdr)[0] in MACHO_MAGICS
    except OSError:
        return False


def safe_extract_ipa(ipa_path: Path, out_dir: Path) -> None:
    root_resolved = out_dir.resolve()
    with zipfile.ZipFile(ipa_path, "r") as zf:
        for info in zf.infolist():
            if not info.filename or info.filename.endswith("/"):
                target = (root_resolved / info.filename).resolve()
                if target != root_resolved and not str(target).startswith(str(root_resolved) + os.sep):
                    raise RuntimeError(f"Unsafe zip path: {info.filename}")
                target.mkdir(parents=True, exist_ok=True)
                continue
            target = (root_resolved / info.filename).resolve()
            if not str(target).startswith(str(root_resolved) + os.sep):
                raise RuntimeError(f"Unsafe zip path: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Detect symlinks inside zip (unix mode 0120000)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                link_target = zf.read(info.filename).decode("utf-8", errors="ignore")
                if os.path.isabs(link_target):
                    raise RuntimeError(f"Unsafe absolute symlink inside IPA: {info.filename} -> {link_target}")
                resolved_link = (target.parent / link_target).resolve()
                if resolved_link != root_resolved and not str(resolved_link).startswith(str(root_resolved) + os.sep):
                    raise RuntimeError(f"Unsafe escaping symlink inside IPA: {info.filename} -> {link_target}")
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(link_target, target)
            else:
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                unix_perm = (info.external_attr >> 16) & 0o777
                if unix_perm:
                    target.chmod(unix_perm)


def codesign_adhoc(path: Path, entitlements: Path | None = None) -> None:
    subprocess.run(["/usr/bin/xattr", "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = ["codesign", "-f", "-s", "-"]
    if entitlements is not None:
        cmd += ["--entitlements", str(entitlements)]
    cmd.append(str(path))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"codesign failed for {path.name}: {(r.stderr or r.stdout).strip()}")


def resign_app_bundle(app_dir: Path, bundle_id: str, work_dir: Path) -> int:
    # Strip App Store / provisioning artifacts that break ad-hoc execution
    for junk in ("SC_Info", "Watch", "com.apple.WatchPlaceholder", "embedded.mobileprovision", "_CodeSignature"):
        jp = app_dir / junk
        if jp.is_dir():
            shutil.rmtree(jp)
        elif jp.is_file() or jp.is_symlink():
            jp.unlink()

    # Clean nested _CodeSignature folders
    for cs in list(app_dir.rglob("_CodeSignature")):
        if cs.is_dir():
            shutil.rmtree(cs, ignore_errors=True)

    ent_plist = work_dir / "entitlements.plist"
    ent_data = {
        "application-identifier": bundle_id,
        "keychain-access-groups": [bundle_id],
    }
    ent_plist.write_bytes(plistlib.dumps(ent_data))

    machos = [p for p in app_dir.rglob("*") if is_macho(p)]
    # Sign deepest Mach-O files first (dylibs/frameworks/appex before main bundle)
    machos.sort(key=lambda p: len(p.parts), reverse=True)

    count = 0
    for mp in machos:
        mp.chmod(0o755)
        codesign_adhoc(mp)
        count += 1

    # Also sign any .appex and .framework bundle directories cleanly bottom-up, then main .app
    bundles = sorted(
        [p for p in app_dir.rglob("*") if p.is_dir() and p.suffix in (".framework", ".appex")],
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for bd in bundles:
        codesign_adhoc(bd)

    codesign_adhoc(app_dir, entitlements=ent_plist)
    return count


def cmd_list(host: str, port: int, password: str, opts: list[str]) -> int:
    cmd = (
        "for d in /Applications/*.app; do "
        "[ -f \"$d/Info.plist\" ] || continue; "
        "b=\"$(basename \"$d\")\"; "
        "echo \"===APP:$b===\"; "
        "base64 \"$d/Info.plist\" 2>/dev/null || true; "
        "done"
    )

    r = subprocess.run(
        ["sshpass", "-p", password, "ssh", *opts, f"root@{host}", cmd],
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Failed reading /Applications: {r.stderr.strip()}")

    apps = []
    cur_name = ""
    cur_b64: list[str] = []
    for ln in r.stdout.splitlines() + ["===END==="]:
        if ln.startswith("===APP:") or ln == "===END===":
            if cur_name and cur_b64:
                try:
                    raw = base64.b64decode("".join(cur_b64))
                    pl = plistlib.loads(raw)
                    bid = str(pl.get("CFBundleIdentifier") or "")
                    ver = str(pl.get("CFBundleShortVersionString") or "")
                    if not bid.startswith("com.apple."):
                        apps.append((cur_name, bid, ver))
                except Exception:
                    pass
            cur_name = ln[7:-3] if ln.startswith("===APP:") else ""
            cur_b64 = []
        elif cur_name:
            cur_b64.append(ln.strip())

    if not apps:
        print("[*] No third-party / sideloaded apps found in /Applications.")
        return 0
    print("[+] Non-stock apps in /Applications:")
    for name, bid, ver in sorted(apps):
        print(f"  - {name:30s} {bid} ({ver or '-'})")
    return 0


def cmd_uninstall(target: str, host: str, port: int, password: str, opts: list[str]) -> int:
    if not target:
        print("[ERROR] Provide app name or bundle ID to uninstall.", file=sys.stderr)
        return 2
    cmd = (
        "for d in /Applications/*.app; do "
        "[ -f \"$d/Info.plist\" ] || continue; "
        "echo \"===APP:$(basename \"$d\")===\"; "
        "base64 \"$d/Info.plist\" 2>/dev/null || true; "
        "done"
    )
    r = subprocess.run(
        ["sshpass", "-p", password, "ssh", *opts, f"root@{host}", cmd],
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())

    matched_app = ""
    cur_name = ""
    cur_b64: list[str] = []
    t_low = target.lower()
    for ln in r.stdout.splitlines() + ["===END==="]:
        if ln.startswith("===APP:") or ln == "===END===":
            if cur_name and cur_b64:
                try:
                    pl = plistlib.loads(base64.b64decode("".join(cur_b64)))
                    bid = str(pl.get("CFBundleIdentifier") or "")
                    cname = cur_name[:-4] if cur_name.endswith(".app") else cur_name
                    if bid.lower() == t_low or cur_name.lower() == t_low or cname.lower() == t_low:
                        if bid.startswith("com.apple."):
                            print(f"[STOP] Refusing to uninstall stock Apple app ({bid}).", file=sys.stderr)
                            return 1
                        matched_app = cur_name
                        break
                except Exception:
                    pass
            cur_name = ln[7:-3] if ln.startswith("===APP:") else ""
            cur_b64 = []
        elif cur_name:
            cur_b64.append(ln.strip())

    if not matched_app:
        print(f"[ERROR] App not found in /Applications: {target}", file=sys.stderr)
        return 1

    q_name = matched_app.replace("'", "'\\''")
    rm_cmd = (
        'export PATH="/var/jb/usr/bin:/usr/bin:/bin:/sbin"; '
        "/sbin/mount -uw / 2>/dev/null || true; "
        f"uicache -u '/Applications/{q_name}' >/dev/null 2>&1 || true; "
        f"rm -rf '/Applications/{q_name}'; "
        "uicache -a >/dev/null 2>&1 || true"
    )
    r2 = subprocess.run(
        ["sshpass", "-p", password, "ssh", *opts, f"root@{host}", rm_cmd],
        capture_output=True,
        text=True,
        timeout=25.0,
    )
    if r2.returncode != 0:
        raise RuntimeError(f"Uninstall failed: {r2.stderr.strip()}")
    print(f"[+] Uninstalled /Applications/{matched_app}")
    return 0


def cmd_install(ipa_arg: str, caller_pwd: Path, host: str, port: int, password: str, opts: list[str]) -> int:
    ipa_path = Path(ipa_arg).expanduser()
    if not ipa_path.is_absolute():
        ipa_path = caller_pwd / ipa_path
    if not ipa_path.is_file():
        print(f"[ERROR] IPA file not found: {ipa_path}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="yigitz_ipa_") as td_s:
        td = Path(td_s)
        extract_dir = td / "unzip"
        extract_dir.mkdir(parents=True, exist_ok=True)
        print(f"[*] Extracting {ipa_path.name}...")
        safe_extract_ipa(ipa_path, extract_dir)

        payload_dir = extract_dir / "Payload"
        apps = [p for p in payload_dir.glob("*.app") if p.is_dir()] if payload_dir.is_dir() else []
        if not apps:
            print("[ERROR] Invalid IPA: missing Payload/*.app", file=sys.stderr)
            return 1
        app_dir = apps[0]
        plist_path = app_dir / "Info.plist"
        if not plist_path.is_file():
            print("[ERROR] Invalid IPA: missing Info.plist", file=sys.stderr)
            return 1

        info = plistlib.loads(plist_path.read_bytes())
        bundle_id = str(info.get("CFBundleIdentifier") or "").strip()
        exec_name = str(info.get("CFBundleExecutable") or "").strip()
        app_ver = str(info.get("CFBundleShortVersionString") or "").strip()
        if not bundle_id or not exec_name:
            print("[ERROR] Info.plist missing CFBundleIdentifier or CFBundleExecutable.", file=sys.stderr)
            return 1
        if bundle_id.startswith("com.apple."):
            print(f"[STOP] Refusing to overwrite stock Apple bundle ({bundle_id}).", file=sys.stderr)
            return 1

        main_bin = app_dir / exec_name
        if main_bin.is_file() and shutil.which("otool"):
            ot = subprocess.run(["otool", "-l", str(main_bin)], capture_output=True, text=True)
            if "cryptid 1" in ot.stdout:
                print(
                    "[WARN] Main binary has FairPlay encryption (cryptid 1); decrypted IPA is required for execution.",
                    file=sys.stderr,
                )

        print(f"[*] Signing {app_dir.name} ({bundle_id} {app_ver}) ad-hoc...")
        signed_cnt = resign_app_bundle(app_dir, bundle_id, td)
        print(f"[+] Signed {signed_cnt} Mach-O binary(ies).")

        app_name = app_dir.name
        q_app = app_name.replace("'", "'\\''")
        q_exec = exec_name.replace("'", "'\\''")
        remote_script = (
            "set -e; "
            'export PATH="/var/jb/usr/bin:/usr/bin:/bin:/sbin"; '
            "/sbin/mount -uw / 2>/dev/null || true; "
            f"rm -rf '/Applications/{q_app}'; "
            "mkdir -p /Applications; "
            "tar --no-same-owner -m -C /Applications -xf -; "
            f"chown -R root:wheel '/Applications/{q_app}'; "
            f"chmod -R a+rX '/Applications/{q_app}'; "
            f"chmod 755 '/Applications/{q_app}/{q_exec}'; "
            f"uicache -p '/Applications/{q_app}'; "
            "sync"
        )
        print("[*] Transferring app over USB SSH & registering with uicache...")
        tar_p = subprocess.Popen(
            ["tar", "-C", str(payload_dir), "--no-xattrs", "-cf", "-", app_name],
            env={**os.environ, "COPYFILE_DISABLE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ssh_p = subprocess.run(
            ["sshpass", "-p", password, "ssh", *opts, f"root@{host}", remote_script],
            stdin=tar_p.stdout,
            capture_output=True,
            timeout=120.0,
        )
        if tar_p.stdout:
            tar_p.stdout.close()
        _, tar_err = tar_p.communicate(timeout=10.0)
        if tar_p.returncode != 0 or ssh_p.returncode != 0:
            err = ssh_p.stderr.decode("utf-8", "replace") or tar_err.decode("utf-8", "replace")
            raise RuntimeError(f"Sideload transfer/uicache failed: {err.strip()}")

        print(f"[+] Sideloaded '{app_name}' ({bundle_id}) natively onto device!")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Native IPA sideload tool")
    ap.add_argument("--caller-pwd", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--uninstall", default="")
    ap.add_argument("ipa", nargs="?", default="")
    args = ap.parse_args()

    if not args.list and not args.uninstall and not args.ipa:
        print("Usage: yigitzra1n sideload <app.ipa> | --list | --uninstall <name_or_bundle_id>", file=sys.stderr)
        return 2

    with open_usb_ssh(host=args.host, explicit_port=args.port) as (host, port, pw, opts):
        if args.list:
            return cmd_list(host, port, pw, opts)
        if args.uninstall:
            return cmd_uninstall(args.uninstall, host, port, pw, opts)
        return cmd_install(args.ipa, Path(args.caller_pwd), host, port, pw, opts)


if __name__ == "__main__":
    raise SystemExit(main())
