#!/usr/bin/env python3
import argparse
import hashlib
import shutil
import struct
import subprocess
from pathlib import Path

WRAP_OFFSET = 0x00B024
WRAP_ORIG_HEAD = bytes.fromhex("ff4302d1fa6704a9")
WRAP_MAX_LEN = 128

UNWRAP_OFFSET = 0x00678C
UNWRAP_ORIG_HEAD = bytes.fromhex("ff0302d1fa6703a9")
UNWRAP_MAX_LEN = 164

def extract_soft_aks_funcs(obj_path: Path):
    data = obj_path.read_bytes()
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != 0xFEEDFACF:
        raise ValueError(f"Expected 64-bit Mach-O object file: {obj_path}")

    ncmds = struct.unpack_from("<I", data, 16)[0]
    off = 32
    text_fo = text_sz = 0
    symoff = nsyms = stroff = 0
    for _ in range(ncmds):
        c, cs = struct.unpack_from("<2I", data, off)
        if c == 0x19:
            nsects = struct.unpack_from("<I", data, off + 64)[0]
            so = off + 72
            for _ in range(nsects):
                sn = data[so : so + 16].split(b"\x00", 1)[0].decode("latin1")
                _addr, sz, fo = struct.unpack_from("<QQI", data, so + 32)
                if sn == "__text":
                    text_fo, text_sz = fo, sz
                so += 80
        elif c == 0x2:
            symoff, nsyms, stroff, _strsize = struct.unpack_from("<4I", data, off + 8)
        off += cs

    if not text_fo or not text_sz or not symoff:
        raise RuntimeError(f"Could not locate __text or LC_SYMTAB inside {obj_path}")

    syms = {}
    for i in range(nsyms):
        n_strx, _n_type, _n_sect, _n_desc, n_value = struct.unpack_from("<IBBHQ", data, symoff + i * 16)
        end = data.find(b"\x00", stroff + n_strx)
        name = data[stroff + n_strx : end].decode("latin1", errors="replace")
        if name in ("_soft_aks_wrap_key", "_soft_aks_unwrap_key"):
            syms[name] = n_value

    if "_soft_aks_wrap_key" not in syms or "_soft_aks_unwrap_key" not in syms:
        raise RuntimeError(f"Missing soft_aks symbols in {obj_path}: found {list(syms)}")

    ordered = sorted(syms.items(), key=lambda kv: kv[1])
    funcs = {}
    for idx, (name, start_off) in enumerate(ordered):
        end_off = ordered[idx + 1][1] if idx + 1 < len(ordered) else text_sz
        funcs[name] = data[text_fo + start_off : text_fo + end_off]
    return funcs["_soft_aks_wrap_key"], funcs["_soft_aks_unwrap_key"]

def patch_securityd(src_bin: Path, obj_path: Path, ent_plist: Path, out_bin: Path) -> None:
    wrap_code, unwrap_code = extract_soft_aks_funcs(obj_path)
    if len(wrap_code) > WRAP_MAX_LEN:
        raise RuntimeError(f"soft_aks_wrap_key too large ({len(wrap_code)} B > {WRAP_MAX_LEN} B)")
    if len(unwrap_code) > UNWRAP_MAX_LEN:
        raise RuntimeError(f"soft_aks_unwrap_key too large ({len(unwrap_code)} B > {UNWRAP_MAX_LEN} B)")

    buf = bytearray(src_bin.read_bytes())

    cur_wrap = bytes(buf[WRAP_OFFSET : WRAP_OFFSET + len(WRAP_ORIG_HEAD)])
    if cur_wrap != WRAP_ORIG_HEAD and cur_wrap != wrap_code[: len(WRAP_ORIG_HEAD)]:
        raise RuntimeError(f"Unexpected securityd wrap prologue @ 0x{WRAP_OFFSET:x}: {cur_wrap.hex()}")

    cur_unwrap = bytes(buf[UNWRAP_OFFSET : UNWRAP_OFFSET + len(UNWRAP_ORIG_HEAD)])
    if cur_unwrap != UNWRAP_ORIG_HEAD and cur_unwrap != unwrap_code[: len(UNWRAP_ORIG_HEAD)]:
        raise RuntimeError(f"Unexpected securityd unwrap prologue @ 0x{UNWRAP_OFFSET:x}: {cur_unwrap.hex()}")

    buf[WRAP_OFFSET : WRAP_OFFSET + len(wrap_code)] = wrap_code
    buf[UNWRAP_OFFSET : UNWRAP_OFFSET + len(unwrap_code)] = unwrap_code

    out_bin.parent.mkdir(parents=True, exist_ok=True)
    out_bin.write_bytes(buf)
    subprocess.run(
        [
            "codesign", "-f", "-s", "-",
            "-i", "com.apple.securityd",
            "--entitlements", str(ent_plist),
            str(out_bin),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(
        f"[+] Patched securityd with soft_aks (wrap={len(wrap_code)}B @ 0x{WRAP_OFFSET:x}, "
        f"unwrap={len(unwrap_code)}B @ 0x{UNWRAP_OFFSET:x}, sha256={hashlib.sha256(out_bin.read_bytes()).hexdigest()[:12]})"
    )

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True, help="Stock /usr/libexec/securityd")
    ap.add_argument("--soft-aks-obj", required=True, help="Compiled soft_aks.o")
    ap.add_argument("--entitlements", required=True, help="securityd.entitlements.plist")
    ap.add_argument("--out", required=True, help="Output patched securityd")
    args = ap.parse_args()
    patch_securityd(Path(args.src), Path(args.soft_aks_obj), Path(args.entitlements), Path(args.out))

if __name__ == "__main__":
    main()
