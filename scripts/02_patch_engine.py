#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ARM64_PATCHES = [
    (0x0031F4, bytes.fromhex("09b78152"), bytes.fromhex("09008052"), "Allow -z with -o and --allow-unsupport"),
    (0x044944, bytes.fromhex("c6028052"), bytes.fromhex("06028052"), "Always pass boot-args in PongoOS path"),
    (0x044974, bytes.fromhex("2801881a"), bytes.fromhex("a8028052"), "Skip sep_racer/pte in restore chain -> kpf"),
    (0x04557C, bytes.fromhex("e9038052"), bytes.fromhex("c9038052"), "Enable xfb module path"),
    (0x0D1D5E, b"cpf", b"xfb", "PongoOS module name cpf -> xfb"),
    (0x0D843C, b"s://api.ipsw.me/v4/device/%s", b"://127.0.0.1:8765/device/%s\x00", "Local API endpoint redirect"),
    (0x0E0598, b"serial=3\n", b"-v\n\x00\x00\x00\x00\x00\x00", "Verbose boot-args"),
    (0x0E07CC, b"cpf", b"xfb", "PongoOS /xfb command"),
]

ARM64_NANDBOOT_PATCHES = [
    (0x0192F4, bytes.fromhex("ff0301d1f65701a9"), bytes.fromhex("00008052c0035fd6"), "Skip RestoreRamDisk upload (MOV W0,#0; RET)"),
    (0x044904, bytes.fromhex("08030035"),         bytes.fromhex("1f2003d5"),         "Skip RamDisk branch check (NOP)"),
    (0x044944, bytes.fromhex("06028052"),         bytes.fromhex("a6038052"),         "Update boot-args length for rd=disk0s1s1 -v"),
    (0x0452CC, bytes.fromhex("08"),               bytes.fromhex("48"),               "NAND boot flag"),
    (0x0D96ED, b"-progress",                      b"wdt=-1 -v",                      "Disable watchdog + verbose (restore boot-arg #1)"),
    (0x0D9BA3, b"-progress",                      b"wdt=-1 -v",                      "Disable watchdog + verbose (restore boot-arg #2)"),
    (0x0D9BD4, b"-progress",                      b"wdt=-1 -v",                      "Disable watchdog + verbose (boot-arg #3)"),
    (0x0E0598, b"-v\n\x00\x00\x00\x00\x00\x00\x00Loaded ", b"rd=disk0s1s1 -v\n\x00", "Default NAND root boot-args"),
]

ARM64_RAMDISK_PATCHES = [
    (0x044904, bytes.fromhex("08030035"), bytes.fromhex("1f2003d5"), "Skip RamDisk branch check (NOP)"),
    (0x044944, bytes.fromhex("06028052"), bytes.fromhex("e6028052"), "Update boot-args length for xargs rd=md0 -v"),
    (0x0D96D6, b"nand-enable-reformat=1 -progress -restore", b"wdt=-1 -v" + b"\x00" * 32, "RamDisk boot-args #1 (disable reformat)"),
    (0x0D9B8C, b"nand-enable-reformat=1 -progress -restore", b"wdt=-1 -v" + b"\x00" * 32, "RamDisk boot-args #2 (disable reformat)"),
    (0x0D9BBD, b"nand-enable-reformat=1 -progress",          b"wdt=-1 -v" + b"\x00" * 23, "RamDisk boot-args #3 (disable reformat)"),
    (0x0E058B, b"sep xargsadd -v\n\x00\x00\x00\x00\x00\x00\x00Loaded ", b"xargs rd=md0 -v\n" + b"\x00" * 14, "Default RamDisk xargs rd=md0 -v"),
]

PONGO_ZSTD_SLICE_OFF = 0x101194
PONGO_ZSTD_SIZE_OFF = 0x11950C
PONGO_LOGO_LOOP_OFF = 0x03F84
PONGO_LOGO_BITMAP_OFF = 0x2DEA4
PONGO_LOGO_BITMAP_LEN = 128

X86_64_PATCHES = [
    (0x0ECA3A, b"s://api.ipsw.me/v4/device/%s", b"://127.0.0.1:8765/device/%s\x00", "Local API endpoint redirect (x86_64)"),
]

ZSTD_MODULE_MAP = {
    0: "mod_4.bin",
    1: "mod_5.bin",
    4: "mod_8.bin",
    5: "mod_9.bin",
}

def find_zstd() -> str:
    for cand in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd", shutil.which("zstd") or ""):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError("zstd binary not found (install via: brew install zstd)")

def parse_fat_slices(data: bytes) -> dict:
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic != 0xCAFEBABE:
        raise ValueError(f"Expected Universal Mach-O (0xCAFEBABE), got 0x{magic:08x}")
    nfat = struct.unpack_from(">I", data, 4)[0]
    slices = {}
    for i in range(nfat):
        cputype, _cpusub, off, sz, _align = struct.unpack_from(">5I", data, 8 + i * 20)
        if cputype == 0x0100000C:
            slices["arm64"] = (off, sz)
        elif cputype == 0x01000007:
            slices["x86_64"] = (off, sz)
    if "arm64" not in slices:
        raise RuntimeError("ARM64 slice missing from upstream binary")
    return slices

def apply_slice_patches(buf: bytearray, slice_off: int, patches: list, label: str) -> None:
    for rel_off, orig, repl, desc in patches:
        assert len(orig) == len(repl), f"Length mismatch in {desc}"
        abs_off = slice_off + rel_off
        cur = bytes(buf[abs_off : abs_off + len(orig)])
        if cur == repl:
            continue
        if cur != orig:
            raise RuntimeError(
                f"[{label}] Unexpected bytes at +0x{rel_off:x} ({desc}): "
                f"got={cur.hex()} expected={orig.hex()}"
            )
        buf[abs_off : abs_off + len(repl)] = repl

def patch_embedded_pongo_logo(buf: bytearray, slice_off: int) -> None:
    zstd_bin = find_zstd()
    slot_abs = slice_off + PONGO_ZSTD_SLICE_OFF
    size_abs = slice_off + PONGO_ZSTD_SIZE_OFF
    orig_comp_len = struct.unpack_from("<I", buf, size_abs)[0]
    slot_cap = PONGO_ZSTD_SIZE_OFF - PONGO_ZSTD_SLICE_OFF
    if orig_comp_len == 0 or orig_comp_len > slot_cap:
        raise RuntimeError(f"Unexpected embedded PongoOS zstd length: {orig_comp_len} (cap={slot_cap})")

    proc = subprocess.run(
        [zstd_bin, "-dc", "--no-check"],
        input=bytes(buf[slot_abs : slot_abs + orig_comp_len]),
        capture_output=True,
        check=True,
    )
    pongo = bytearray(proc.stdout)
    if len(pongo) < PONGO_LOGO_BITMAP_OFF + PONGO_LOGO_BITMAP_LEN:
        raise RuntimeError(f"Unexpected PongoOS decompressed size: {len(pongo)}")

    orig_insn = bytes(pongo[PONGO_LOGO_LOOP_OFF : PONGO_LOGO_LOOP_OFF + 4])
    branch_insn = struct.pack("<I", 0x14000015)
    if orig_insn not in (bytes.fromhex("0e008052"), branch_insn):
        raise RuntimeError(f"Unexpected PongoOS screen_init insn @ 0x{PONGO_LOGO_LOOP_OFF:x}: {orig_insn.hex()}")
    pongo[PONGO_LOGO_LOOP_OFF : PONGO_LOGO_LOOP_OFF + 4] = branch_insn

    pongo[PONGO_LOGO_BITMAP_OFF : PONGO_LOGO_BITMAP_OFF + PONGO_LOGO_BITMAP_LEN] = b"\x00" * PONGO_LOGO_BITMAP_LEN

    comp = None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_in = Path(td) / "pongo.bin"
        tmp_in.write_bytes(bytes(pongo))
        for flags in (["-19", "-c"], ["--ultra", "-22", "-c"]):
            cproc = subprocess.run([zstd_bin, *flags, str(tmp_in)], capture_output=True, check=True)
            if len(cproc.stdout) <= slot_cap:
                comp = cproc.stdout
                break
    if comp is None:
        raise RuntimeError(f"Recompressed PongoOS exceeds embedded slot ({slot_cap} B)")

    buf[slot_abs : slot_abs + len(comp)] = comp
    if len(comp) < slot_cap:
        buf[slot_abs + len(comp) : slot_abs + slot_cap] = b"\x00" * (slot_cap - len(comp))
    struct.pack_into("<I", buf, size_abs, len(comp))
    print(f"  [+] Patched embedded PongoOS logo at source ({orig_comp_len} B -> {len(comp)} B zstd)")

def extract_pongo_modules(arm64_slice: bytes, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    zstd_bin = find_zstd()
    magic = b"\x28\xb5\x2f\xfd"
    offsets = []
    pos = 0
    while True:
        idx = arm64_slice.find(magic, pos)
        if idx < 0:
            break
        offsets.append(idx)
        pos = idx + 4

    if len(offsets) < 6:
        raise RuntimeError(f"Expected >= 6 zstd frames in ARM64 slice, found {len(offsets)}")

    for frame_idx, filename in ZSTD_MODULE_MAP.items():
        start = offsets[frame_idx]
        end = offsets[frame_idx + 1] if frame_idx + 1 < len(offsets) else len(arm64_slice)
        proc = subprocess.run(
            [zstd_bin, "-dc", "--no-check"],
            input=arm64_slice[start:end],
            capture_output=True,
        )
        if not proc.stdout:
            raise RuntimeError(f"Failed to decompress zstd frame #{frame_idx} ({filename}): {proc.stderr.decode('utf-8', 'replace')}")
        out_path = out_dir / filename
        out_path.write_bytes(proc.stdout)
        print(f"  [+] Extracted {filename:10s} ({len(proc.stdout):7d} B, sha256={hashlib.sha256(proc.stdout).hexdigest()[:12]})")

def build_local_api(ipsw_1588: Path, api_dir: Path) -> None:
    dev_dir = api_dir / "device"
    dev_dir.mkdir(parents=True, exist_ok=True)

    link_path = api_dir / "local_1588.ipsw"
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(ipsw_1588.resolve())

    fsize = ipsw_1588.stat().st_size
    payload = {
        "name": "iPhone 6s",
        "identifier": "iPhone8,1",
        "boards": [
            {"boardconfig": "N71AP", "platform": "s8000", "cpid": 32768, "bdid": 4},
            {"boardconfig": "N71mAP", "platform": "s8003", "cpid": 32771, "bdid": 4},
        ],
        "boardconfig": "N71mAP",
        "platform": "s8003",
        "cpid": 32771,
        "bdid": 4,
        "firmwares": [
            {
                "identifier": "iPhone8,1",
                "version": "15.8.8",
                "buildid": "19H422",
                "sha1sum": "b8d93af9335b4182cf4ed5160000000000000000",
                "md5sum": "00000000000000000000000000000000",
                "filesize": fsize,
                "url": "http://127.0.0.1:8765/local_1588.ipsw",
                "releasedate": "2025-10-01T00:00:00Z",
                "uploaddate": "2025-10-01T00:00:00Z",
                "signed": True,
            }
        ],
    }
    (dev_dir / "iPhone8,1").write_text(json.dumps(payload), encoding="utf-8")
    print(f"  [+] Local API ready: {dev_dir / 'iPhone8,1'} -> local_1588.ipsw ({fsize} B)")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", required=True, help="Extracted upstream runtime directory (.cache/engine/raw)")
    ap.add_argument("--out-dir", required=True, help="Output directory (build/engine)")
    ap.add_argument("--api-dir", required=True, help="Output local_api directory (build/local_api)")
    ap.add_argument("--ipsw-1588", required=True, help="Path to stock iPhone 6s 15.8.8 IPSW")
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    bin_dir = out_dir / "bin"
    mod_dir = out_dir / "modules"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "block").mkdir(parents=True, exist_ok=True)
    (out_dir / "image4").mkdir(parents=True, exist_ok=True)

    raw_main = next((p for p in raw_dir.rglob("turdus_merula") if p.is_file()), None)
    raw_helper = next((p for p in raw_dir.rglob("turdusra1n") if p.is_file()), None)
    if not raw_main or not raw_helper:
        raise FileNotFoundError(f"Expected upstream binaries inside {raw_dir}")

    dst_helper = bin_dir / "engine_helper"
    shutil.copy2(raw_helper, dst_helper)
    os.chmod(dst_helper, 0o755)

    orig_bytes = raw_main.read_bytes()
    slices = parse_fat_slices(orig_bytes)
    arm_off, arm_sz = slices["arm64"]

    buf_restore = bytearray(orig_bytes)
    apply_slice_patches(buf_restore, arm_off, ARM64_PATCHES, "arm64")
    if "x86_64" in slices:
        apply_slice_patches(buf_restore, slices["x86_64"][0], X86_64_PATCHES, "x86_64")
    patch_embedded_pongo_logo(buf_restore, arm_off)

    dst_restore = bin_dir / "engine_restore"
    dst_restore.write_bytes(buf_restore)
    os.chmod(dst_restore, 0o755)
    subprocess.run(["codesign", "-f", "-s", "-", str(dst_restore)], check=True, capture_output=True)
    print(f"[+] Patched restore engine ready: {dst_restore}")

    buf_boot = bytearray(buf_restore)
    apply_slice_patches(buf_boot, arm_off, ARM64_NANDBOOT_PATCHES, "arm64-nandboot")

    dst_boot = bin_dir / "engine_boot"
    dst_boot.write_bytes(buf_boot)
    os.chmod(dst_boot, 0o755)
    subprocess.run(["codesign", "-f", "-s", "-", str(dst_boot)], check=True, capture_output=True)
    print(f"[+] Patched boot engine ready: {dst_boot} ({len(ARM64_NANDBOOT_PATCHES)} nandboot patches)")

    buf_ramdisk = bytearray(buf_restore)
    apply_slice_patches(buf_ramdisk, arm_off, ARM64_RAMDISK_PATCHES, "arm64-ramdisk")

    dst_ramdisk = bin_dir / "engine_ramdisk"
    dst_ramdisk.write_bytes(buf_ramdisk)
    os.chmod(dst_ramdisk, 0o755)
    subprocess.run(["codesign", "-f", "-s", "-", str(dst_ramdisk)], check=True, capture_output=True)
    print(f"[+] Patched ramdisk engine ready: {dst_ramdisk} ({len(ARM64_RAMDISK_PATCHES)} ramdisk patches)")

    print("[*] Extracting embedded PongoOS modules...")
    extract_pongo_modules(bytes(buf_restore[arm_off : arm_off + arm_sz]), mod_dir)

    build_local_api(Path(args.ipsw_1588), Path(args.api_dir))
    print("[+] Engine & local API setup complete.")

if __name__ == "__main__":
    main()
