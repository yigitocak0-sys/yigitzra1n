#!/usr/bin/env python3
"""Verify that a local IPSW path is a real ZIP archive (and not a macOS Alias)."""

from pathlib import Path
import sys
import zipfile


def main() -> int:
    if len(sys.argv) < 2:
        print("[ERROR] Missing IPSW path argument.", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else "IPSW"

    if not path.is_file():
        print(f"[ERROR] {label} not found: {path}", file=sys.stderr)
        return 20

    try:
        with path.open("rb") as f:
            magic = f.read(8)
    except OSError as exc:
        print(f"[ERROR] Cannot read {label} ({path}): {exc}", file=sys.stderr)
        return 20

    if magic.startswith(b"book\x00\x00\x00\x00"):
        print(
            f"[ERROR] {label} is a macOS Finder Alias, not the actual .ipsw file: {path}",
            file=sys.stderr,
        )
        print(
            "        Copy or move the real .ipsw file into ipsw/ (or pass --ipsw-dir).",
            file=sys.stderr,
        )
        return 21

    if not zipfile.is_zipfile(path):
        print(
            f"[ERROR] {label} is corrupted or not a valid ZIP archive: {path}",
            file=sys.stderr,
        )
        return 22

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
