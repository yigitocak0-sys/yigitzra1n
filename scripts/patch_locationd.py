#!/usr/bin/env python3
import argparse
import subprocess
import tempfile
from pathlib import Path

PATCH_OFFSET = 0x09FF6E0
ORIG_BYTES = bytes.fromhex("fd7bbfa9fd030091")
REPL_BYTES = bytes.fromhex("00008052c0035fd6")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Input stock locationd binary")
    ap.add_argument("--out", dest="out", required=True, help="Output patched locationd binary")
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)

    ent_xml = subprocess.check_output(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(inp)],
        stderr=subprocess.DEVNULL,
    )

    buf = bytearray(inp.read_bytes())
    cur = bytes(buf[PATCH_OFFSET : PATCH_OFFSET + len(ORIG_BYTES)])
    if cur != REPL_BYTES:
        if cur != ORIG_BYTES:
            raise RuntimeError(
                f"Unexpected bytes in locationd @ 0x{PATCH_OFFSET:x}: got={cur.hex()} expected={ORIG_BYTES.hex()}"
            )
        buf[PATCH_OFFSET : PATCH_OFFSET + len(REPL_BYTES)] = REPL_BYTES

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buf)

    with tempfile.NamedTemporaryFile(suffix=".ent.plist", delete=False) as tf:
        tf.write(ent_xml)
        ent_path = Path(tf.name)
    try:
        subprocess.run(
            ["codesign", "-f", "-s", "-", "-i", "com.apple.locationd", "--entitlements", str(ent_path), str(out)],
            check=True,
            capture_output=True,
        )
    finally:
        ent_path.unlink(missing_ok=True)

    print(f"[+] Patched and signed locationd -> {out} ({out.stat().st_size} B)")

if __name__ == "__main__":
    main()
