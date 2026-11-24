#!/usr/bin/env python3
import struct
import sys
from pathlib import Path

def inject_dylib(in_path: str, dylib_path: str, out_path: str) -> None:
    data = bytearray(Path(in_path).read_bytes())
    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = struct.unpack_from(
        "<IIIIIIII", data, 0
    )
    if magic != 0xFEEDFACF:
        raise SystemExit(f"Error: {in_path} is not a 64-bit little-endian Mach-O (magic={hex(magic)})")

    if dylib_path.encode("utf-8") in data[32 : 32 + sizeofcmds]:
        print(f"[=] {dylib_path} already present in {in_path}; copying unchanged.")
        Path(out_path).write_bytes(data)
        return

    header_size = 32
    end_cmds = header_size + sizeofcmds
    min_sec_off = len(data)
    off = header_size
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x19:
            nsects = struct.unpack_from("<I", data, off + 64)[0]
            for s in range(nsects):
                sec_off = struct.unpack_from("<I", data, off + 72 + s * 80 + 48)[0]
                if 0 < sec_off < min_sec_off:
                    min_sec_off = sec_off
        off += cmdsize

    raw_path = dylib_path.encode("utf-8") + b"\x00"
    cmdsize = (24 + len(raw_path) + 7) & ~7
    avail = min_sec_off - end_cmds
    if avail < cmdsize:
        raise SystemExit(f"Error: insufficient header padding ({avail} < {cmdsize})")

    lc = struct.pack("<IIIIII", 0xC, cmdsize, 24, 2, 0x10000, 0x10000) + raw_path
    lc += b"\x00" * (cmdsize - len(lc))
    data[end_cmds : end_cmds + cmdsize] = lc
    struct.pack_into("<II", data, 16, ncmds + 1, sizeofcmds + cmdsize)

    Path(out_path).write_bytes(data)
    print(f"[+] Injected {dylib_path} -> {out_path} (ncmds {ncmds} -> {ncmds + 1})")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <input_macho> <dylib_install_name> <output_macho>")
        raise SystemExit(2)
    inject_dylib(sys.argv[1], sys.argv[2], sys.argv[3])
