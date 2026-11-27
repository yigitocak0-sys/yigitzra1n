#!/usr/bin/env python3
"""Export and import saved SHC/PTE boot blocks safely."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from usb_ssh import sha256_file


def find_block_in_dir(block_dir: Path) -> Path | None:
    if not block_dir.is_dir():
        return None
    for pat in ("*pteblock*.bin", "*shcblock*.bin", "*.bin"):
        matches = sorted(
            [p for p in block_dir.glob(pat) if p.is_file() and not p.name.startswith(".")],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]
    return None


def cmd_export(engine_dir: Path, caller_pwd: Path, explicit_block: str, out_arg: str) -> int:
    eng = engine_dir.resolve() if engine_dir.exists() else engine_dir
    src = Path(explicit_block).expanduser() if explicit_block else find_block_in_dir(eng / "block")
    if src is None or not src.is_file():
        print("[ERROR] No boot block found. Run 'yigitzra1n install' or import one with 'yigitzra1n import-block <file.bin>'.", file=sys.stderr)
        return 1

    if out_arg:
        dst = Path(out_arg).expanduser()
        if not dst.is_absolute():
            dst = caller_pwd / dst
        if dst.is_dir():
            dst = dst / src.name
    else:
        dst = caller_pwd / src.name

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[+] Saved boot block copy to: {dst} ({dst.stat().st_size} bytes, sha256 {sha256_file(dst)})")
    return 0


def cmd_import(engine_dir: Path, caller_pwd: Path, src_arg: str, force: bool = False) -> int:
    if not src_arg:
        print("[ERROR] Usage: yigitzra1n import-block <path/to/block.bin>", file=sys.stderr)
        return 2
    src = Path(src_arg).expanduser()
    if not src.is_absolute():
        src = caller_pwd / src
    if not src.is_file():
        print(f"[ERROR] File not found: {src}", file=sys.stderr)
        return 1

    sz = src.stat().st_size
    if sz < 1024 or sz > 64 * 1024 * 1024:
        print(f"[ERROR] Invalid block size ({sz} bytes).", file=sys.stderr)
        return 1

    if engine_dir.is_symlink() and not engine_dir.exists():
        print(f"[ERROR] Engine directory target is unavailable (broken symlink): {engine_dir}", file=sys.stderr)
        return 1
    eng = engine_dir.resolve() if engine_dir.exists() else engine_dir
    block_dir = eng / "block"
    os.makedirs(block_dir, exist_ok=True)

    lower_name = src.name.lower()
    if "shcblock" in lower_name or "pteblock" in lower_name:
        target_name = src.name
    else:
        target_name = f"shcblock_{src.stem}.bin"
    dst = block_dir / target_name

    existing = [p for p in block_dir.glob("*.bin") if p.is_file()]
    if existing and not force:
        backup_dir = block_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        for old in existing:
            shutil.move(str(old), str(backup_dir / f"{old.name}.bak_{ts}"))
            print(f"[*] Existing block moved to: backups/{old.name}.bak_{ts}")

    shutil.copy2(src, dst)
    dst.chmod(0o600)
    print(f"[+] Boot block ready: {dst} ({dst.stat().st_size} bytes, sha256 {sha256_file(dst)})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="SHC/PTE boot block export/import")
    ap.add_argument("action", choices=("export", "import"))
    ap.add_argument("--engine-dir", required=True)
    ap.add_argument("--caller-pwd", required=True)
    ap.add_argument("--block", default="")
    ap.add_argument("--target", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    eng = Path(args.engine_dir)
    cpwd = Path(args.caller_pwd)
    if args.action == "export":
        return cmd_export(eng, cpwd, args.block, args.target)
    return cmd_import(eng, cpwd, args.target, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
