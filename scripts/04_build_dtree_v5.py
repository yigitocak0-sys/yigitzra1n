#!/usr/bin/env python3
import argparse
import hashlib
import struct
import zipfile
from pathlib import Path
import pyimg4

class DTNode:
    def __init__(self):
        self.props = {}
        self.children = []

    @property
    def name(self) -> str:
        raw = self.props.get("name", (b"", 0))[0]
        return raw.split(b"\x00", 1)[0].decode("latin1", errors="replace")

def parse_dt(data: bytes, offset: int = 0):
    n_props, n_children = struct.unpack_from("<II", data, offset)
    offset += 8
    node = DTNode()
    for _ in range(n_props):
        name = data[offset : offset + 32].split(b"\x00", 1)[0].decode("latin1", errors="replace")
        length_flags = struct.unpack_from("<I", data, offset + 32)[0]
        length = length_flags & 0x7FFFFFFF
        offset += 36
        node.props[name] = (data[offset : offset + length], length_flags & 0x80000000)
        offset = (offset + length + 3) & ~3
    for _ in range(n_children):
        child, offset = parse_dt(data, offset)
        node.children.append(child)
    return node, offset

def serialize_dt(node: DTNode) -> bytes:
    out = bytearray(struct.pack("<II", len(node.props), len(node.children)))
    for name, (val, flags) in node.props.items():
        nb = name.encode("latin1")[:31]
        out += nb + b"\x00" * (32 - len(nb))
        out += struct.pack("<I", len(val) | flags)
        out += val + b"\x00" * ((4 - (len(val) & 3)) & 3)
    for c in node.children:
        out += serialize_dt(c)
    return bytes(out)

def build_path_map(node: DTNode, prefix: str = "", out=None) -> dict:
    if out is None:
        out = {}
    cur = f"{prefix}/{node.name}" if prefix else f"/{node.name}"
    out[cur] = node
    for c in node.children:
        build_path_map(c, cur, out)
    return out

def chrp_checksum(hdr16: bytearray) -> int:
    s = hdr16[0] + sum(hdr16[2:16])
    while s > 0xFF:
        s = (s >> 8) + (s & 0xFF)
    return s

def make_chrp_header(sig: int, blocks16: int, name: bytes) -> bytes:
    hdr = bytearray(16)
    hdr[0] = sig
    struct.pack_into("<H", hdr, 2, blocks16)
    hdr[4 : 4 + len(name)] = name[:12]
    hdr[1] = chrp_checksum(hdr)
    return bytes(hdr)

def build_valid_chrp_nvram(size: int = 0x2000) -> bytes:
    buf = bytearray(size)
    buf[0:16] = make_chrp_header(0x5A, 2, b"nvram")
    struct.pack_into("<I", buf, 20, 1)
    common_blocks = 0x80
    buf[0x20:0x30] = make_chrp_header(0x70, common_blocks, b"common")
    sys_off = 0x20 + (common_blocks << 4)
    sys_blocks = (size - sys_off) >> 4
    buf[sys_off : sys_off + 16] = make_chrp_header(0x70, sys_blocks, b"system")
    s1, s2 = 1, 0
    for b in buf[0x14:size]:
        s1 = (s1 + b) % 65521
        s2 = (s2 + s1) % 65521
    struct.pack_into("<I", buf, 0x10, ((s2 << 16) | s1) & 0xFFFFFFFF)
    return bytes(buf)

def extract_dt_from_ipsw(ipsw_path: Path, member: str) -> bytes:
    with zipfile.ZipFile(ipsw_path, "r") as zf:
        im = pyimg4.IM4P(zf.read(member))
    im.payload.decompress()
    return bytes(im.payload.data)

def build_v5(raw15: bytes, raw16: bytes, board: str = "n71map", ramdisk_mode: bool = False) -> bytes:
    root15, end15 = parse_dt(raw15)
    assert end15 == len(raw15) and serialize_dt(root15) == raw15, "15.8.8 DT round-trip failed"
    root16, _ = parse_dt(raw16)
    m15, m16 = build_path_map(root15), build_path_map(root16)

    pmu_path = "/device-tree/arm-io/i2c0/pmu"
    isp_path = "/device-tree/arm-io/isp"
    skip_merge_paths = {
        pmu_path,
        "/device-tree/arm-io/spi1/audio-codec",
        "/device-tree/arm-io/uart1",
    }

    for path, n15 in m15.items():
        if path not in m16 or path in skip_merge_paths:
            continue
        for k, v in m16[path].props.items():
            if k in n15.props:
                continue
            if path == isp_path and k in ("function-focus_ldo", "function-cam_dvdd_gpio"):
                continue
            if path == "/device-tree" and k == "target-sub-type":
                sub = b"N71AP\x00" if board == "n71ap" else b"N71mAP\x00"
                n15.props[k] = (sub, v[1])
            else:
                n15.props[k] = v

    root15.props["target-type"] = (b"J71s\x00" if board == "n71ap" else b"J71t\x00", 0)

    for fs_path in ("/device-tree/filesystems/fstab", "/device-tree/filesystems/fstab-ephemeral-diag-data"):
        if fs_path in m15:
            fs_node = m15[fs_path]
            fs_node.children = [c for c in fs_node.children if c.name != "baseband-vol"]
            fs_node.props["max_fs_entries"] = (struct.pack("<I", 6), 0)

    disp = m15["/device-tree/arm-io/disp0"]
    disp.props["display-timing-info"] = (
        struct.pack("<8I", 750, 16, 16, 16, 1334, 16, 4, 16),
        0,
    )
    mipi = m15["/device-tree/arm-io/mipi-dsim"]
    mipi.props["compatible"] = (b"mipi-dsim-1,disabled\x00", mipi.props["compatible"][1])

    pmu = m15[pmu_path]
    pmu.props["compatible"] = (b"pmu,d2255\x00", pmu.props["compatible"][1])

    m15["/device-tree/arm-io/usb-complex"].props["no-pmu"] = (struct.pack("<I", 1), 0)
    if "/device-tree/arm-io/wdt" in m15:
        m15["/device-tree/arm-io/wdt"].props["no-pmu"] = (struct.pack("<I", 1), 0)

    usb_dev = m15["/device-tree/arm-io/usb-complex/usb-device"]
    usb_dev.props.pop("function-usb_500_100", None)
    usb_dev.props.pop("function-dock_parent", None)
    usb_dev.props["disable-charger-detect"] = (struct.pack("<I", 1), 0)

    if "/device-tree/arm-io/i2c1/tristar" in m15:
        m15["/device-tree/arm-io/i2c1/tristar"].props["function-power_supply_change"] = (
            b"\x95\x00\x00\x00Iump\x00\x00\x00\x00",
            0,
        )

    if "/device-tree/charger" in m15:
        chg = m15["/device-tree/charger"]
        chg.props["num-docks"] = (struct.pack("<I", 0), 0)
        for p in ("function-dock_parent", "function-set_charger", "function-tristar_current"):
            chg.props.pop(p, None)

    dead_phandles = {struct.unpack_from("<I", pmu.props["AAPL,phandle"][0])[0]}
    for dead_p in (
        "/device-tree/arm-io/i2c0/display-pmu",
        "/device-tree/arm-io/i2c1/tigris",
        "/device-tree/arm-io/i2c1/charger",
    ):
        if dead_p in m15 and "AAPL,phandle" in m15[dead_p].props:
            dead_phandles.add(struct.unpack_from("<I", m15[dead_p].props["AAPL,phandle"][0])[0])

    for p_node, node in m15.items():
        if p_node == "/device-tree/arm-io/mipi-dsim/lcd":
            continue
        for k in list(node.props.keys()):
            if k.startswith("function-") and len(node.props[k][0]) >= 4:
                if struct.unpack_from("<I", node.props[k][0])[0] in dead_phandles:
                    node.props.pop(k)

    if "/device-tree/arm-io/spi1/audio-codec" in m15:
        m15["/device-tree/arm-io/spi1/audio-codec"].props.pop("function-mbus_dock_parent", None)
        m15["/device-tree/arm-io/spi1/audio-codec"].props.pop("function-digitalldo_pulldown", None)

    mt = m15["/device-tree/arm-io/spi2/multi-touch"]
    mt.props["compatible"] = (b"multi-touch,n71,2\x00multi-touch,j72\x00", mt.props["compatible"][1])
    mt.props["Placement"] = (struct.pack("<I", 1), 0)

    chosen = m15["/device-tree/chosen"]
    chosen.props["nvram-bank-size"] = (struct.pack("<I", 0x2000), 0)
    chosen.props["nvram-bank-count"] = (struct.pack("<I", 2), 0)
    chosen.props["nvram-current-bank"] = (struct.pack("<I", 0), 0)
    chosen.props["nvram-proxy-data"] = (build_valid_chrp_nvram(0x2000), 0)
    if ramdisk_mode:
        chosen.props["sepfw-load-at-boot"] = (struct.pack("<I", 1), 0)
        chosen.props["protected-data-access"] = (struct.pack("<I", 1), 0)
    else:
        chosen.props["sepfw-load-at-boot"] = (struct.pack("<I", 0), 0)
        chosen.props["protected-data-access"] = (struct.pack("<I", 0), 0)

    if "/device-tree/arm-io/sep" in m15 and not ramdisk_mode:
        m15["/device-tree/arm-io/sep"].props.pop("sepfw-loaded", None)

    if "/device-tree/product" in m15:
        prod = m15["/device-tree/product"]
        prod.props["boot-ios-diagnostics"] = (struct.pack("<I", 1), 0)
        for drop_k in (
            "baseband-chipset",
            "car-integration",
            "gps-capable",
            "hearingaid-audio-equalization",
            "location-reminders",
            "low-power-wallet-mode",
            "personal-hotspot",
            "ui-reachability",
            "watch-companion",
        ):
            prod.props.pop(drop_k, None)

    if "/device-tree/defaults" in m15:
        if ramdisk_mode:
            m15["/device-tree/defaults"].props.pop("no-effaceable-storage", None)
        else:
            m15["/device-tree/defaults"].props["no-effaceable-storage"] = (struct.pack("<I", 1), 0)

    return serialize_dt(root15)

def write_dt_im4p(out_raw: bytes, out_path: Path, label: str) -> None:
    root_chk, end_chk = parse_dt(out_raw)
    assert end_chk == len(out_raw), f"{label} trailing bytes"

    im4p = pyimg4.IM4P(fourcc="dtre", payload=out_raw)
    im4p.payload.compress(pyimg4.Compression.LZFSE)
    out_bytes = im4p.output()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out_bytes)

    chk = pyimg4.IM4P(out_bytes)
    chk.payload.decompress()
    assert chk.fourcc == "dtre" and bytes(chk.payload.data) == out_raw, f"{label} LZFSE round-trip failed"

    print(f"[+] Built {label}: {out_path} (raw={len(out_raw)} B, im4p={len(out_bytes)} B, sha256={hashlib.sha256(out_bytes).hexdigest()[:16]})")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ipsw-1588", required=True, help="Stock iPhone 6s 15.8.8 IPSW")
    ap.add_argument("--ipsw-ipad5", required=True, help="Stock iPad 5 16.7.16 IPSW")
    ap.add_argument("--out", required=True, help="Output IM4P path (e.g. build/DeviceTree.n71map_v5.im4p)")
    ap.add_argument("--out-ramdisk", default="", help="Optional Maintenance SSH RamDisk IM4P path (SEP/Effaceable enabled)")
    ap.add_argument("--board", choices=["n71map", "n71ap"], default="n71map")
    args = ap.parse_args()

    dt15_member = f"Firmware/all_flash/DeviceTree.{args.board}.im4p"
    dt16_member = "Firmware/all_flash/DeviceTree.j71sap.im4p" if args.board == "n71ap" else "Firmware/all_flash/DeviceTree.j71tap.im4p"

    print(f"[*] Extracting {dt15_member} and {dt16_member}...")
    raw15 = extract_dt_from_ipsw(Path(args.ipsw_1588), dt15_member)
    raw16 = extract_dt_from_ipsw(Path(args.ipsw_ipad5), dt16_member)

    out_raw = build_v5(raw15, raw16, board=args.board, ramdisk_mode=False)
    write_dt_im4p(out_raw, Path(args.out), "DeviceTree v5")

    if args.out_ramdisk:
        rd_raw = build_v5(raw15, raw16, board=args.board, ramdisk_mode=True)
        write_dt_im4p(rd_raw, Path(args.out_ramdisk), "DeviceTree RamDisk (SEP+Effaceable)")

if __name__ == "__main__":
    main()
