#!/usr/bin/env python3
import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path

REPL_BYTES = bytes.fromhex("20008052c0035fd6")
ALLOWED_TARGETS = {
    0x170D4: (bytes.fromhex("00484039c0035fd6"), 2351472),
    0x15B9C: (bytes.fromhex("00444039c0035fd6"), 2380144),
}
GATE_DIGEST = "ae9f8348227ab6b9dc263cd00f719c2792d051be95fec380fe2d6c293c731ced"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--gate", dest="gate", required=True)
    args = ap.parse_args()

    if hashlib.sha256(args.gate.encode("utf-8")).hexdigest() != GATE_DIGEST:
        raise RuntimeError("Target environment validation failed (code 78)")

    inp = Path(args.inp)
    out = Path(args.out)

    ent_xml = subprocess.check_output(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(inp)],
        stderr=subprocess.DEVNULL,
    )

    buf = bytearray(inp.read_bytes())
    matched_off = None
    for off, (orig, _) in ALLOWED_TARGETS.items():
        if off + 8 <= len(buf):
            cur = bytes(buf[off : off + 8])
            if cur in (orig, REPL_BYTES):
                matched_off = off
                break

    if matched_off is None:
        raise RuntimeError("Unsupported target binary build")

    buf[matched_off : matched_off + 8] = REPL_BYTES
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buf)

    with tempfile.NamedTemporaryFile(suffix=".ent.plist", delete=False) as tf:
        tf.write(ent_xml)
        ent_path = Path(tf.name)
    try:
        subprocess.run(
            [
                "codesign",
                "-f",
                "-s",
                "-",
                "-i",
                "com.apple.mobileactivationd",
                "--entitlements",
                str(ent_path),
                str(out),
            ],
            check=True,
            capture_output=True,
        )
    finally:
        ent_path.unlink(missing_ok=True)

    print(f"[+] Updated component -> {out.name}")


if __name__ == "__main__":
    main()
