#!/usr/bin/env python3
import argparse
import base64
import json
import os
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PAGE_SIZE = 0x4000
BUNDLE_ID = "com.apple.driver.AppleCS35L19Amp"

KC15_KEXT_LOAD_VA = 0xFFFFFFF0057AD9C0
KC15_KEXT_KMOD_VA = 0xFFFFFFF007A0C140

KC15_TEXT_VA = 0xFFFFFFF0057AD9C0
KC15_TEXT_SZ = 0x940
KC15_EXEC_VA = 0xFFFFFFF0064E8000
KC15_EXEC_SZ = 0x4204
KC15_TEXT_ONLY_SZ = 0x3FAC
KC15_STUBS_VA = 0xFFFFFFF0064EBFAC
KC15_STUBS_SZ = 0x258
KC15_DC_VA = 0xFFFFFFF006ED57C0
KC15_DC_SZ = 0xE08
KC15_DATA_VA = 0xFFFFFFF007A0C140
KC15_DATA_SZ = 0x110

KC15_VT_VA = 0xFFFFFFF006ED59B8
KC15_MC_VT_VA = 0xFFFFFFF006ED6510
KC15_KALLOC_VA = 0xFFFFFFF006ED6588
KC15_AEA_VT_VA = 0xFFFFFFF006E5A578
KC16_AEA_VT_VA = 0xFFFFFFF006DCFFC0

OLD_ALLOC_SIZE = 0x278
NEW_ALLOC_SIZE = 0x280

KC16_CS42_BUNDLE_ID = "com.apple.driver.AppleCS42L81Audio"
KC16_CS42_TEXT_VA = 0xFFFFFFF005478F40
KC16_CS42_TEXT_SZ = 0x1438
KC16_CS42_EXEC_VA = 0xFFFFFFF005E6D000
KC16_CS42_EXEC_SZ = 0x9268
KC16_CS42_DC_VA = 0xFFFFFFF006E07A00
KC16_CS42_DC_SZ = 0x4D70
KC16_CS42_DATA_VA = 0xFFFFFFF007A5EF80
KC16_CS42_DATA_SZ = 0x1F8

INTER_KEXT_GOT = {
    7:  ("com.apple.driver.AppleARMPlatform",   0xFFFFFFF0059B7238, "AppleARMFunction::callFunction wrapper"),
    8:  ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA8274, "AppleEmbeddedAudio::getFunction('function-%s_master/_active')"),
    9:  ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA8510, "AppleEmbeddedAudio::callPlatformFunction_9"),
    10: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA74B4, "AppleEmbeddedAudio::computeRateScaleHelper"),
    11: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA8448, "AppleEmbeddedAudio::callPlatformFunction_11"),
    12: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA85B8, "AppleEmbeddedAudio::releaseFunctionHelper"),
    14: ("com.apple.iokit.AppleARMIISAudio",    0xFFFFFFF005D995D4, "AppleARMIISAudio::sampleRateHelper"),
    16: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF007A5BD10, "AppleCSI2CMultipleAudio::gMetaClass"),
    17: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DAD40C, "AppleCSI2CMultipleAudio::AppleCSI2CMultipleAudio(C2)"),
    18: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DAD42C, "AppleCSI2CMultipleAudio::~AppleCSI2CMultipleAudio(D2)"),
    19: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DB71E0, "AppleEmbeddedAudio::regCacheLookup"),
    20: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DB71D8, "AppleEmbeddedAudio::regCacheGetCount"),
    21: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DB7098, "AppleEmbeddedAudio::regCacheInit"),
    22: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DB7234, "AppleEmbeddedAudio::regCacheHasEntries"),
    23: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DB6450, "AppleEmbeddedAudio::parseAcousticTrim"),
    24: ("com.apple.driver.AppleIISController", 0xFFFFFFF005C655C8, "AppleIISController::createActiveFunction"),
    25: ("com.apple.driver.AppleIISController", 0xFFFFFFF005C657BC, "AppleIISController::setChannelConfig"),
    26: ("com.apple.iokit.IOAudio2Family",      0xFFFFFFF005C795B8, "IOAudio2Control::createGainRangeHelper"),
    34: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA401C, "AppleEmbeddedAudio::getPropertyUInt32Helper"),
    35: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA9DA0, "AppleEmbeddedAudio::helper_35"),
    36: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA7EB8, "AppleEmbeddedAudio::getTransportDevice"),
    37: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF005DA7ADC, "AppleEmbeddedAudio::loadAcousticTrimGains"),
    38: ("com.apple.driver.AppleIISController", 0xFFFFFFF005C657B4, "AppleIISController::getIISRouteState"),
    39: ("com.apple.driver.AppleEmbeddedAudio", 0xFFFFFFF006DCFFB0, "AppleCSI2CMultipleAudio::vtable_base (ZTV)"),
}

KC16_CSI2C_SIG_60 = "121111121222121211212121112111111111211111112111211121111222"

def align_up(val: int, align: int = PAGE_SIZE) -> int:
    return (val + align - 1) & ~(align - 1)

def sign_extend(val: int, bits: int) -> int:
    if val & (1 << (bits - 1)):
        return val - (1 << bits)
    return val

def parse_macho(data: bytes):
    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = struct.unpack_from("<IIIIIIII", data, 0)
    if magic != 0xFEEDFACF:
        raise ValueError(f"Unexpected Mach-O magic: 0x{magic:x}")
    segs = []
    symtab = None
    first_sect_off = len(data)
    off = 32
    for idx in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x19:
            segname, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, segflags = struct.unpack_from(
                "<16sQQQQIIII", data, off + 8
            )
            sname = segname.split(b"\x00", 1)[0].decode()
            secs = []
            soff = off + 72
            for _ in range(nsects):
                secname, seg2, addr, size, foff, align, reloff, nreloc, sflags, r1, r2, r3 = struct.unpack_from(
                    "<16s16sQQIIIIIIII", data, soff
                )
                sec_s = secname.split(b"\x00", 1)[0].decode()
                if foff and foff < first_sect_off:
                    first_sect_off = foff
                secs.append({
                    "sectname": sec_s, "segname": sname, "addr": addr, "size": size,
                    "offset": foff, "align": align, "flags": sflags, "hdr_off": soff,
                })
                soff += 80
            segs.append({
                "index": idx, "hdr_off": off, "cmdsize": cmdsize, "segname": sname,
                "vmaddr": vmaddr, "vmsize": vmsize, "fileoff": fileoff, "filesize": filesize,
                "maxprot": maxprot, "initprot": initprot, "nsects": nsects, "flags": segflags,
                "sects": secs,
            })
        elif cmd == 0x2:
            symtab = struct.unpack_from("<IIII", data, off + 8)
        off += cmdsize
    return {
        "ncmds": ncmds, "sizeofcmds": sizeofcmds, "hdr_end": 32 + sizeofcmds,
        "first_sect_off": first_sect_off, "segs": segs, "symtab": symtab,
    }

def make_va2off(segs):
    def va2off(va: int):
        for s in segs:
            if s["vmaddr"] <= va < s["vmaddr"] + s["vmsize"]:
                off = va - s["vmaddr"]
                if off < s["filesize"]:
                    return s["fileoff"] + off
                return None
        return None
    return va2off

def make_is_mapped_va(segs):
    def is_mapped(va: int) -> bool:
        return any(s["vmaddr"] <= va < s["vmaddr"] + s["vmsize"] for s in segs)
    return is_mapped

def load_symtab(data: bytes, symtab):
    symoff, nsyms, stroff, _ = symtab
    s2v, v2s = {}, {}
    for i in range(nsyms):
        nx, _, _, _, nv = struct.unpack_from("<IBBHQ", data, symoff + i * 16)
        if nv:
            end = data.find(b"\x00", stroff + nx)
            nm = data[stroff + nx:end].decode(errors="replace")
            s2v[nm] = nv
            v2s[nv] = nm
    return s2v, v2s

def encode_adrp(rd: int, pc: int, target_va: int) -> int:
    delta = (target_va & ~0xFFF) - (pc & ~0xFFF)
    if delta % 0x1000 != 0:
        raise ValueError(f"ADRP delta not 4K-aligned at 0x{pc:x}")
    imm = delta >> 12
    if not (-(1 << 20) <= imm < (1 << 20)):
        raise ValueError(f"ADRP target 0x{target_va:x} out of +/-4GB range from 0x{pc:x}")
    imm_u = imm & 0x1FFFFF
    immlo = imm_u & 0x3
    immhi = (imm_u >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | (rd & 0x1F)

def encode_add_imm12(rd: int, rn: int, imm12: int) -> int:
    if not (0 <= imm12 <= 0xFFF):
        raise ValueError(f"ADD imm12 out of range: 0x{imm12:x}")
    return 0x91000000 | (imm12 << 10) | ((rn & 0x1F) << 5) | (rd & 0x1F)

def encode_mem_uimm(orig_insn: int, byte_off: int) -> int:
    size = (orig_insn >> 30) & 0x3
    step = 1 << size
    if byte_off % step != 0 or byte_off < 0 or (byte_off >> size) > 0xFFF:
        raise ValueError(f"MEM uimm 0x{byte_off:x} invalid for insn 0x{orig_insn:08x}")
    imm12 = byte_off >> size
    return (orig_insn & ~(0xFFF << 10)) | (imm12 << 10)

def encode_branch26(is_bl: bool, pc: int, target_va: int) -> int:
    delta = target_va - pc
    if delta % 4 != 0:
        raise ValueError("Unaligned branch target")
    imm26 = delta >> 2
    if not (-(1 << 25) <= imm26 < (1 << 25)):
        raise ValueError(f"Branch target 0x{target_va:x} out of +/-128MB range from 0x{pc:x}")
    return (0x94000000 if is_bl else 0x14000000) | (imm26 & 0x03FFFFFF)

def map_vtable_slot_15_to_16(slot15: int) -> int:
    if slot15 < 180:
        return slot15
    if slot15 in (331, 332):
        raise ValueError(f"Slot {slot15} moved to 180/181 in kc16 and is not overridden")
    if slot15 < 331:
        return slot15 + 3
    return slot15 + 1

def build_symbol_map(d15: bytes, m15, d16: bytes, m16) -> dict:
    v15 = make_va2off(m15["segs"])
    mapped16 = make_is_mapped_va(m16["segs"])
    s2v15, v2s15 = load_symtab(d15, m15["symtab"])
    s2v16, v2s16 = load_symtab(d16, m16["symtab"])

    got_off15 = v15(KC15_DC_VA)
    got_entries = []
    for idx in range(57):
        va15 = struct.unpack_from("<Q", d15, got_off15 + idx * 8)[0]
        if idx in INTER_KEXT_GOT:
            owner, va16, role = INTER_KEXT_GOT[idx]
            if not mapped16(va16):
                raise RuntimeError(f"Inter-kext GOT[{idx}] target 0x{va16:x} not mapped in kc16")
            got_entries.append({
                "index": idx, "kind": "inter_kext", "owner": owner,
                "symbol": role, "kc15_va": f"0x{va15:016x}", "kc16_va": f"0x{va16:016x}",
            })
        else:
            sym = v2s15.get(va15)
            if not sym or sym not in s2v16:
                raise RuntimeError(f"GOT[{idx}] XNU symbol {sym!r} (0x{va15:x}) missing in kc16")
            va16 = s2v16[sym]
            got_entries.append({
                "index": idx, "kind": "xnu", "owner": "com.apple.kernel",
                "symbol": sym, "kc15_va": f"0x{va15:016x}", "kc16_va": f"0x{va16:016x}",
            })

    return {
        "bundle_id": BUNDLE_ID,
        "kc15_version": "550.3",
        "kc16_aea_version": "640.8",
        "superclass": {
            "name": "AppleCSI2CMultipleAudio",
            "kc15_size": 0x1D8,
            "kc16_size": 0x1E0,
            "kc15_vtable_slots": 359,
            "kc16_vtable_slots": 360,
        },
        "subclass": {
            "name": "AppleCS35L19Amp",
            "kc15_size": OLD_ALLOC_SIZE,
            "kc16_size": NEW_ALLOC_SIZE,
            "kc15_vtable_slots": 360,
            "kc16_vtable_slots": 361,
            "vtable_slot_mapping": {
                "0..179": "shift 0 (IOService + AppleEmbeddedAudio base)",
                "180..182": "inherited from kc16 AppleCSI2CMultipleAudio (2 moved from 331..332 + transmitter tone id)",
                "180..330 (kc15)": "-> 183..333 (kc16, shift +3)",
                "331..332 (kc15)": "moved up to 180..181 (not overridden by CS35L19Amp)",
                "333..358 (kc15)": "-> 334..359 (kc16, shift +1)",
                "359 (kc15 own virtual)": "-> 360 (kc16, shift +1)",
            },
        },
        "got_entries": got_entries,
    }

def find_kext_dict_span(pi_raw: bytes, bundle_id: str):
    needle = f"<key>CFBundleIdentifier</key><string>{bundle_id}</string>".encode("utf-8")
    pos = pi_raw.find(needle)
    if pos < 0:
        return None
    depth = 0
    idx = pos
    start = -1
    while idx >= 0:
        o_tag = pi_raw.rfind(b"<dict>", 0, idx)
        c_tag = pi_raw.rfind(b"</dict>", 0, idx)
        if o_tag < 0 and c_tag < 0:
            break
        if c_tag > o_tag:
            depth += 1
            idx = c_tag
        else:
            if depth == 0:
                start = o_tag
                break
            depth -= 1
            idx = o_tag
    if start < 0:
        raise RuntimeError(f"Could not locate opening <dict> for {bundle_id}")
    depth = 0
    idx = start
    while idx < len(pi_raw):
        o_tag = pi_raw.find(b"<dict>", idx)
        c_tag = pi_raw.find(b"</dict>", idx)
        if c_tag < 0:
            break
        if 0 <= o_tag < c_tag:
            depth += 1
            idx = o_tag + 6
        else:
            depth -= 1
            idx = c_tag + 7
            if depth == 0:
                return (start, idx)
    raise RuntimeError(f"Could not locate closing </dict> for {bundle_id}")

def relink_kext(kc15_path: str, kc16_path: str, out_path: str, sym_map_out: str = None, mode: str = "inplace"):
    if mode not in ("inplace", "newseg"):
        raise ValueError(f"Unsupported mode: {mode!r} (expected 'inplace' or 'newseg')")

    d15 = Path(kc15_path).read_bytes()
    d16 = bytearray(Path(kc16_path).read_bytes())

    m15 = parse_macho(d15)
    m16 = parse_macho(d16)
    v15 = make_va2off(m15["segs"])
    v16 = make_va2off(m16["segs"])
    s2v15, v2s15 = load_symtab(d15, m15["symtab"])
    s2v16, v2s16 = load_symtab(d16, m16["symtab"])

    sym_map = build_symbol_map(d15, m15, bytes(d16), m16)

    prelink_seg = next(s for s in m16["segs"] if s["segname"] == "__PRELINK_INFO")
    pi_sect = prelink_seg["sects"][0]
    pi_raw = bytes(d16[pi_sect["offset"]:pi_sect["offset"] + pi_sect["size"]]).split(b"\x00", 1)[0]

    if f"<string>{BUNDLE_ID}</string>".encode("utf-8") in pi_raw:
        raise RuntimeError(f"Target {kc16_path} already contains {BUNDLE_ID}!")

    raw_text = bytearray(d15[v15(KC15_TEXT_VA):v15(KC15_TEXT_VA) + KC15_TEXT_SZ])
    raw_exec = bytearray(d15[v15(KC15_EXEC_VA):v15(KC15_EXEC_VA) + KC15_EXEC_SZ])
    raw_dc = bytearray(d15[v15(KC15_DC_VA):v15(KC15_DC_VA) + KC15_DC_SZ])
    raw_data = bytearray(d15[v15(KC15_DATA_VA):v15(KC15_DATA_VA) + 0xC8]) + bytearray(0x48)

    old_sig_off = 0x64D
    old_sig = raw_text[old_sig_off:raw_text.find(b"\x00", old_sig_off)].decode()
    assert len(old_sig) == 79, f"Unexpected old kt_signature len={len(old_sig)}"
    new_sig = KC16_CSI2C_SIG_60 + old_sig[59:]
    assert len(new_sig) == 80
    new_sig_rel = len(raw_text)
    raw_text.extend(new_sig.encode() + b"\x00")
    while len(raw_text) % 16 != 0:
        raw_text.append(0)
    new_text_vsz = len(raw_text)
    new_dc_sz = KC15_DC_SZ + 8

    cs42_linkedit = None
    if mode == "inplace":
        if find_kext_dict_span(pi_raw, KC16_CS42_BUNDLE_ID) is None:
            raise RuntimeError(f"In-place target {KC16_CS42_BUNDLE_ID} not found in __PRELINK_INFO!")
        assert new_text_vsz <= KC16_CS42_TEXT_SZ, "CS35 __TEXT exceeds CS42L81 slot"
        assert KC15_EXEC_SZ <= KC16_CS42_EXEC_SZ, "CS35 __TEXT_EXEC exceeds CS42L81 slot"
        assert new_dc_sz <= KC16_CS42_DC_SZ, "CS35 __DATA_CONST exceeds CS42L81 slot"
        assert KC15_DATA_SZ <= KC16_CS42_DATA_SZ, "CS35 __DATA exceeds CS42L81 slot"

        seg_text_vm, seg_text_fo, seg_text_sz = KC16_CS42_TEXT_VA, v16(KC16_CS42_TEXT_VA), KC16_CS42_TEXT_SZ
        seg_exec_vm, seg_exec_fo, seg_exec_sz = KC16_CS42_EXEC_VA, v16(KC16_CS42_EXEC_VA), KC16_CS42_EXEC_SZ
        seg_dc_vm, seg_dc_fo, seg_dc_sz = KC16_CS42_DC_VA, v16(KC16_CS42_DC_VA), KC16_CS42_DC_SZ
        seg_data_vm, seg_data_fo, seg_data_sz = KC16_CS42_DATA_VA, v16(KC16_CS42_DATA_VA), KC16_CS42_DATA_SZ
        base_vm = prelink_seg["vmaddr"]
        base_fo = prelink_seg["fileoff"]
        next_vm = base_vm
        next_fo = base_fo

        assert d16[seg_text_fo:seg_text_fo + 4] == b"\xcf\xfa\xed\xfe"
        c42_ncmds = struct.unpack_from("<I", d16, seg_text_fo + 16)[0]
        c42_off = seg_text_fo + 32
        for _ in range(c42_ncmds):
            cc, ccs = struct.unpack_from("<II", d16, c42_off)
            if cc == 0x19 and d16[c42_off + 8:c42_off + 24].split(b"\x00", 1)[0] == b"__LINKEDIT":
                cs42_linkedit = struct.unpack_from("<QQQQ", d16, c42_off + 24)
                break
            c42_off += ccs
        assert cs42_linkedit is not None
    else:
        if any(s["segname"] == "__CS35_TEXT" for s in m16["segs"]):
            raise RuntimeError(f"Target {kc16_path} already contains __CS35_TEXT!")
        if prelink_seg["fileoff"] + prelink_seg["filesize"] != len(d16):
            raise RuntimeError("__PRELINK_INFO is not at EOF; cannot safely push without shifting downstream segments.")
        base_vm = prelink_seg["vmaddr"]
        base_fo = prelink_seg["fileoff"]
        seg_text_vm, seg_text_fo, seg_text_sz = base_vm, base_fo, align_up(new_text_vsz)
        seg_exec_vm, seg_exec_fo, seg_exec_sz = seg_text_vm + seg_text_sz, seg_text_fo + seg_text_sz, align_up(KC15_EXEC_SZ)
        seg_dc_vm, seg_dc_fo, seg_dc_sz = seg_exec_vm + seg_exec_sz, seg_exec_fo + seg_exec_sz, align_up(new_dc_sz)
        seg_data_vm, seg_data_fo, seg_data_sz = seg_dc_vm + seg_dc_sz, seg_dc_fo + seg_dc_sz, align_up(KC15_DATA_SZ)
        next_vm = seg_data_vm + seg_data_sz
        next_fo = seg_data_fo + seg_data_sz

    KC15_MC_PREFIX_VA = 0xFFFFFFF006ED64F8
    def map_kext_va(old_va: int) -> int:
        if KC15_TEXT_VA <= old_va < KC15_TEXT_VA + KC15_TEXT_SZ:
            return seg_text_vm + (old_va - KC15_TEXT_VA)
        if KC15_EXEC_VA <= old_va < KC15_EXEC_VA + KC15_EXEC_SZ:
            return seg_exec_vm + (old_va - KC15_EXEC_VA)
        if KC15_DC_VA <= old_va < KC15_DC_VA + KC15_DC_SZ:
            shift = 8 if old_va >= KC15_MC_PREFIX_VA else 0
            return seg_dc_vm + (old_va - KC15_DC_VA) + shift
        if KC15_DATA_VA <= old_va < KC15_DATA_VA + KC15_DATA_SZ:
            return seg_data_vm + (old_va - KC15_DATA_VA)
        raise ValueError(f"Old VA 0x{old_va:x} outside AppleCS35L19Amp segments")

    new_dc = bytearray(new_dc_sz)
    new_dc[0:0x1F8] = raw_dc[0:0x1F8]

    for entry in sym_map["got_entries"]:
        idx = entry["index"]
        va16 = int(entry["kc16_va"], 16)
        struct.pack_into("<Q", new_dc, idx * 8, va16)

    for off_dc in (0x1C8, 0x1D0):
        old_fn = struct.unpack_from("<Q", raw_dc, off_dc)[0]
        struct.pack_into("<Q", new_dc, off_dc, map_kext_va(old_fn))

    old_gmc = struct.unpack_from("<Q", raw_dc, 0x1D8)[0]
    struct.pack_into("<Q", new_dc, 0x1D8, map_kext_va(old_gmc))
    struct.pack_into("<Q", new_dc, 0x1E0, int(sym_map["got_entries"][16]["kc16_va"], 16))

    vt16_aea = [struct.unpack_from("<Q", d16, v16(KC16_AEA_VT_VA) + i * 8)[0] for i in range(360)]
    vt15_aea = [struct.unpack_from("<Q", d15, v15(KC15_AEA_VT_VA) + i * 8)[0] for i in range(359)]
    vt15_cs35 = [struct.unpack_from("<Q", raw_dc, 0x1F8 + i * 8)[0] for i in range(360)]

    vt16_cs35 = list(vt16_aea) + [0]
    overridden_slots = 0
    for s15 in range(360):
        ptr15 = vt15_cs35[s15]
        is_own = (KC15_EXEC_VA <= ptr15 < KC15_EXEC_VA + KC15_TEXT_ONLY_SZ)
        if is_own:
            s16 = map_vtable_slot_15_to_16(s15)
            vt16_cs35[s16] = map_kext_va(ptr15)
            overridden_slots += 1
        else:
            assert s15 < 359 and ptr15 == vt15_aea[s15], f"Unexpected non-own diff at slot {s15}"

    for s16, ptr16 in enumerate(vt16_cs35):
        struct.pack_into("<Q", new_dc, 0x1F8 + s16 * 8, ptr16)

    tail_src = 0x1F8 + 360 * 8
    tail_dst = 0x1F8 + 361 * 8
    new_dc[tail_dst:new_dc_sz] = raw_dc[tail_src:KC15_DC_SZ]

    mc_vt_off = tail_dst + 0x18
    for i in range(14):
        q = struct.unpack_from("<Q", new_dc, mc_vt_off + i * 8)[0]
        if KC15_EXEC_VA <= q < KC15_EXEC_VA + KC15_TEXT_ONLY_SZ:
            struct.pack_into("<Q", new_dc, mc_vt_off + i * 8, map_kext_va(q))
        else:
            sym = v2s15.get(q)
            if not sym or sym not in s2v16:
                raise RuntimeError(f"Missing MetaClass vtable XNU symbol {sym!r} at slot {i}")
            struct.pack_into("<Q", new_dc, mc_vt_off + i * 8, s2v16[sym])

    kt_off = new_dc_sz - 0x40
    site_va15 = struct.unpack_from("<Q", new_dc, kt_off + 0x10)[0]
    struct.pack_into("<Q", new_dc, kt_off + 0x10, map_kext_va(site_va15))
    struct.pack_into("<Q", new_dc, kt_off + 0x20, seg_text_vm + new_sig_rel)
    flags_sz = struct.unpack_from("<Q", new_dc, kt_off + 0x28)[0]
    new_flags_sz = (NEW_ALLOC_SIZE << 32) | (flags_sz & 0xFFFFFFFF)
    struct.pack_into("<Q", new_dc, kt_off + 0x28, new_flags_sz)

    old_start = struct.unpack_from("<Q", raw_data, 0xB4)[0]
    old_stop = struct.unpack_from("<Q", raw_data, 0xBC)[0]
    kmod_total_sz = (
        (KC16_CS42_TEXT_SZ + KC16_CS42_EXEC_SZ + KC16_CS42_DC_SZ + KC16_CS42_DATA_SZ)
        if mode == "inplace"
        else 0x14000
    )
    struct.pack_into("<Q", raw_data, 0x9C, seg_text_vm)
    struct.pack_into("<Q", raw_data, 0xA4, kmod_total_sz)
    struct.pack_into("<Q", raw_data, 0xB4, map_kext_va(old_start))
    struct.pack_into("<Q", raw_data, 0xBC, map_kext_va(old_stop))

    stubs_rel_off = KC15_STUBS_VA - KC15_EXEC_VA
    for i in range(KC15_STUBS_SZ // 12):
        s_off = stubs_rel_off + i * 12
        old_sva = KC15_STUBS_VA + i * 12
        new_sva = seg_exec_vm + s_off
        i0, i1, i2 = struct.unpack_from("<III", raw_exec, s_off)
        assert (i0 & 0x9F00001F) == 0x90000010 and (i1 & 0xFFC003FF) == 0xF9400210 and i2 == 0xD61F0200
        imm = sign_extend((((i0 >> 5) & 0x7FFFF) << 2) | ((i0 >> 29) & 3), 21)
        old_gva = (old_sva & ~0xFFF) + (imm << 12) + ((i1 >> 10) & 0xFFF) * 8
        new_gva = map_kext_va(old_gva)
        new_i0 = encode_adrp(16, new_sva, new_gva)
        new_i1 = encode_mem_uimm(i1, new_gva & 0xFFF)
        struct.pack_into("<II", raw_exec, s_off, new_i0, new_i1)

    insns = [struct.unpack_from("<I", raw_exec, p)[0] for p in range(0, KC15_TEXT_ONLY_SZ, 4)]
    reg_kind = {}
    adrp_pages_at = {}
    stats = {"adrp_pairs": 0, "struct_shifts": 0, "vtable_shifts": 0, "alloc_size_patches": 0}

    for idx in (0x0158 // 4, 0x3E5C // 4):
        ins = insns[idx]
        assert (ins & 0xFFE0FFE0) == 0x52804F00 or (ins & 0x7F800000) == 0x52800000
        imm16 = (ins >> 5) & 0xFFFF
        assert imm16 == OLD_ALLOC_SIZE, f"Expected 0x278 at +0x{idx*4:x}, got 0x{imm16:x}"
        insns[idx] = (ins & ~(0xFFFF << 5)) | (NEW_ALLOC_SIZE << 5)
        stats["alloc_size_patches"] += 1

    for i in range(len(insns)):
        ins = insns[i]
        old_pc = KC15_EXEC_VA + i * 4
        new_pc = seg_exec_vm + i * 4

        if (ins & 0x9F000000) == 0x90000000:
            rd = ins & 0x1F
            imm = sign_extend((((ins >> 5) & 0x7FFFF) << 2) | ((ins >> 29) & 3), 21)
            old_page = (old_pc & ~0xFFF) + (imm << 12)
            reg_kind[rd] = ("ADRP", old_page)
            adrp_pages_at[i] = old_page
            continue

        if (ins & 0xFF800000) == 0x91000000:
            rd = ins & 0x1F
            rn = (ins >> 5) & 0x1F
            imm12 = (ins >> 10) & 0xFFF
            if (ins >> 22) & 1:
                imm12 <<= 12
            rk = reg_kind.get(rn)
            if rk and rk[0] == "ADRP":
                old_target = rk[1] + imm12
                new_target = map_kext_va(old_target)
                for j in range(i - 1, -1, -1):
                    if j in adrp_pages_at and (insns[j] & 0x1F) == rn:
                        insns[j] = encode_adrp(rn, seg_exec_vm + j * 4, new_target)
                        break
                insns[i] = encode_add_imm12(rd, rn, new_target & 0xFFF)
                stats["adrp_pairs"] += 1
                reg_kind.pop(rd, None)
                continue
            elif rn not in (29, 31) and 0x160 <= imm12 < 0x280:
                insns[i] = encode_add_imm12(rd, rn, imm12 + 8)
                stats["struct_shifts"] += 1
                reg_kind.pop(rd, None)
                continue
            reg_kind.pop(rd, None)
            continue

        if (ins & 0x3B000000) == 0x39000000:
            rt = ins & 0x1F
            rn = (ins >> 5) & 0x1F
            sz = (ins >> 30) & 3
            imm = ((ins >> 10) & 0xFFF) << sz
            is_load = bool((ins >> 22) & 1)
            rk = reg_kind.get(rn)
            if rk and rk[0] == "ADRP":
                old_target = rk[1] + imm
                new_target = map_kext_va(old_target)
                for j in range(i - 1, -1, -1):
                    if j in adrp_pages_at and (insns[j] & 0x1F) == rn:
                        insns[j] = encode_adrp(rn, seg_exec_vm + j * 4, new_target)
                        break
                insns[i] = encode_mem_uimm(ins, new_target & 0xFFF)
                stats["adrp_pairs"] += 1
                if is_load and old_target == KC15_DC_VA + 39 * 8:
                    reg_kind[rt] = ("VT", "GOT39")
                elif is_load:
                    reg_kind.pop(rt, None)
                continue
            elif rk and rk[0] == "VT" and is_load and sz == 3:
                if rk[1] == "GOT39":
                    slot15 = (imm - 16) // 8
                    if slot15 >= 180:
                        slot16 = map_vtable_slot_15_to_16(slot15)
                        insns[i] = encode_mem_uimm(ins, slot16 * 8 + 16)
                        stats["vtable_shifts"] += 1
                else:
                    pc_rel = i * 4
                    if pc_rel not in (0x164C, 0x166C):
                        slot15 = imm // 8
                        if slot15 >= 180:
                            slot16 = map_vtable_slot_15_to_16(slot15)
                            insns[i] = encode_mem_uimm(ins, slot16 * 8)
                            stats["vtable_shifts"] += 1
                reg_kind.pop(rt, None)
                continue
            elif rn != 31:
                if is_load and sz == 3 and imm == 0:
                    reg_kind[rt] = ("VT", f"x{rn}")
                    continue
                if 0x160 <= imm < 0x280:
                    insns[i] = encode_mem_uimm(ins, imm + 8)
                    stats["struct_shifts"] += 1
                if is_load:
                    reg_kind.pop(rt, None)
                continue
            else:
                if is_load:
                    reg_kind.pop(rt, None)
                continue

        if (ins & 0x3A000000) == 0x28000000:
            rn = (ins >> 5) & 0x1F
            opc = (ins >> 30) & 3
            sc = 8 if opc >= 2 else 4
            imm7 = sign_extend((ins >> 15) & 0x7F, 7)
            imm = imm7 * sc
            if rn != 31 and 0x160 <= imm < 0x280:
                new_imm7 = (imm + 8) // sc
                assert -64 <= new_imm7 < 64
                insns[i] = (ins & ~(0x7F << 15)) | ((new_imm7 & 0x7F) << 15)
                stats["struct_shifts"] += 1
            if (ins >> 22) & 1:
                reg_kind.pop(ins & 0x1F, None)
                reg_kind.pop((ins >> 10) & 0x1F, None)
            continue

        if (ins & 0xFFE0FFE0) == 0xAA0003E0:
            src_r = (ins >> 16) & 0x1F
            dst_r = ins & 0x1F
            if src_r in reg_kind:
                reg_kind[dst_r] = reg_kind[src_r]
            else:
                reg_kind.pop(dst_r, None)
        elif (ins & 0x7C000000) == 0x14000000 or (ins & 0xFFFFFC1F) in (0xD63F0000, 0xD61F0000):
            for r in range(19):
                reg_kind.pop(r, None)
        elif ins == 0xD65F03C0:
            reg_kind.clear()
        elif (ins & 0x7F800000) in (0x52800000, 0x72800000, 0x12800000):
            reg_kind.pop(ins & 0x1F, None)
        elif (ins & 0x1F000000) in (0x0A000000, 0x0B000000, 0x11000000, 0x12000000, 0x13000000, 0x1A000000, 0x1B000000):
            reg_kind.pop(ins & 0x1F, None)

    assert stats["adrp_pairs"] == 149, f"Expected 149 ADRP uses (145 ADRP + 4 reuses), got {stats['adrp_pairs']}"
    assert stats["struct_shifts"] == 180, f"Expected 180 struct shifts, got {stats['struct_shifts']}"

    for i, ins in enumerate(insns):
        struct.pack_into("<I", raw_exec, i * 4, ins)

    embedded_text_sz = KC16_CS42_TEXT_SZ if mode == "inplace" else new_text_vsz
    embedded_linkedit = cs42_linkedit if mode == "inplace" else (next_vm, 0, next_fo - seg_text_fo, 0)
    k_ncmds = struct.unpack_from("<I", raw_text, 16)[0]
    coff = 32
    new_seg_map = {
        "__TEXT":       (seg_text_vm, embedded_text_sz, 0, embedded_text_sz),
        "__TEXT_EXEC":  (seg_exec_vm, KC15_EXEC_SZ, seg_exec_fo - seg_text_fo, KC15_EXEC_SZ),
        "__DATA_CONST": (seg_dc_vm,   new_dc_sz,    seg_dc_fo - seg_text_fo,   new_dc_sz),
        "__DATA":       (seg_data_vm, KC15_DATA_SZ, seg_data_fo - seg_text_fo, 0xC8),
        "__LINKEDIT":   embedded_linkedit,
    }
    for _ in range(k_ncmds):
        c, cs = struct.unpack_from("<II", raw_text, coff)
        if c == 0x19:
            sn = raw_text[coff + 8:coff + 24].split(b"\x00", 1)[0].decode()
            nvm, nvsz, nfo, nfsz = new_seg_map[sn]
            struct.pack_into("<QQQQ", raw_text, coff + 24, nvm, nvsz, nfo, nfsz)
            nsects = struct.unpack_from("<I", raw_text, coff + 64)[0]
            soff = coff + 72
            for _ in range(nsects):
                secname = raw_text[soff:soff + 16].split(b"\x00", 1)[0].decode()
                old_addr, old_sz, old_fo = struct.unpack_from("<QQI", raw_text, soff + 32)
                if sn == "__TEXT":
                    new_addr = map_kext_va(old_addr)
                    new_s_fo = new_addr - seg_text_vm
                    struct.pack_into("<QQI", raw_text, soff + 32, new_addr, old_sz, new_s_fo)
                elif sn == "__TEXT_EXEC":
                    new_addr = map_kext_va(old_addr)
                    new_s_fo = (seg_exec_fo - seg_text_fo) + (new_addr - seg_exec_vm)
                    struct.pack_into("<QQI", raw_text, soff + 32, new_addr, old_sz, new_s_fo)
                elif sn == "__DATA_CONST":
                    new_addr = map_kext_va(old_addr)
                    new_sz = old_sz + 8 if secname == "__const" else old_sz
                    new_s_fo = (seg_dc_fo - seg_text_fo) + (new_addr - seg_dc_vm)
                    struct.pack_into("<QQI", raw_text, soff + 32, new_addr, new_sz, new_s_fo)
                elif sn == "__DATA":
                    new_addr = map_kext_va(old_addr)
                    new_s_fo = 0 if secname == "__common" else (seg_data_fo - seg_text_fo) + (new_addr - seg_data_vm)
                    struct.pack_into("<QQI", raw_text, soff + 32, new_addr, old_sz, new_s_fo)
                soff += 80
        coff += cs

    new_kmod_va = seg_data_vm
    prelink_exec_sz_xml = KC16_CS42_TEXT_SZ if mode == "inplace" else new_text_vsz
    new_entry_xml = (
        f"<dict>"
        f"<key>CFBundleName</key><string>AppleCS35L19Amp</string>"
        f"<key>DTSDKName</key><string>iphoneos15.8.internal</string>"
        f"<key>DTXcode</key><string>1330</string>"
        f"<key>_PrelinkExecutableLoadAddr</key><integer size=\"64\">0x{seg_text_vm:x}</integer>"
        f"<key>_PrelinkKmodInfo</key><integer size=\"64\">0x{new_kmod_va:x}</integer>"
        f"<key>DTSDKBuild</key><string>19H402</string>"
        f"<key>_PrelinkExecutableSize</key><integer size=\"64\">0x{prelink_exec_sz_xml:x}</integer>"
        f"<key>CFBundleDevelopmentRegion</key><string>English</string>"
        f"<key>CFBundleVersion</key><string>550.3</string>"
        f"<key>BuildMachineOSBuild</key><string>20A241133</string>"
        f"<key>DTPlatformName</key><string>iphoneos</string>"
        f"<key>CFBundlePackageType</key><string>KEXT</string>"
        f"<key>CFBundleShortVersionString</key><string>550.3</string>"
        f"<key>CFBundleSupportedPlatforms</key><array><string>iPhoneOS</string></array>"
        f"<key>_PrelinkExecutableSourceAddr</key><integer size=\"64\">0x{seg_text_vm:x}</integer>"
        f"<key>_PrelinkExecutableRelativePath</key><string>AppleCS35L19Amp</string>"
        f"<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>"
        f"<key>CFBundleExecutable</key><string>AppleCS35L19Amp</string>"
        f"<key>DTCompiler</key><string>com.apple.compilers.llvm.clang.1_0</string>"
        f"<key>UIRequiredDeviceCapabilities</key><array><string>arm64</string></array>"
        f"<key>MinimumOSVersion</key><string>15.8</string>"
        f"<key>CFBundleIdentifier</key><string>com.apple.driver.AppleCS35L19Amp</string>"
        f"<key>IOKitPersonalities</key><dict><key>AppleCS35L19Audio</key><dict>"
        f"<key>CodecRegisterStartIndex</key><integer size=\"64\">0x1</integer>"
        f"<key>IOClass</key><string>AppleCS35L19Amp</string>"
        f"<key>IONameMatch</key><array><string>audio-control,cs35l19</string></array>"
        f"<key>IOProviderClass</key><string>AppleARMIICDevice</string>"
        f"<key>CodecRegisterDisplayBase</key><integer size=\"64\">0x10</integer>"
        f"</dict></dict>"
        f"<key>CFBundleSignature</key><string>????</string>"
        f"<key>DTPlatformVersion</key><string>15.8</string>"
        f"<key>DTXcodeBuild</key><string>13E6049a</string>"
        f"<key>OSBundleLibraries</key><dict>"
        f"<key>com.apple.kpi.bsd</key><string>8.0</string>"
        f"<key>com.apple.kpi.libkern</key><string>8.0</string>"
        f"<key>com.apple.driver.AppleIISController</key><string>1.0</string>"
        f"<key>com.apple.iokit.IOHIDFamily</key><string>1.5</string>"
        f"<key>com.apple.kpi.iokit</key><string>8.0</string>"
        f"<key>com.apple.kpi.mach</key><string>8.0</string>"
        f"<key>com.apple.driver.AppleARMPlatform</key><string>1.0</string>"
        f"<key>com.apple.iokit.AppleARMIISAudio</key><string>1.0</string>"
        f"<key>com.apple.driver.AppleEmbeddedAudio</key><string>1.1.0</string>"
        f"<key>com.apple.iokit.IOAudio2Family</key><string>1.0</string>"
        f"</dict>"
        f"<key>UIDeviceFamily</key><array><integer size=\"64\">0x1</integer></array>"
        f"<key>_PrelinkBundlePath</key><string>/System/Library/Extensions/AppleEmbeddedAudio.kext/PlugIns/AppleCS35L19Amp.kext</string>"
        f"<key>DTPlatformBuild</key><string>19H402</string>"
        f"</dict>"
    ).encode("utf-8")

    arr_end_marker = b"</array><key>_PrelinkLinkKASLROffsets</key><data>"
    if mode == "inplace":
        span = find_kext_dict_span(pi_raw, KC16_CS42_BUNDLE_ID)
        assert span is not None
        old_dict_root = ET.fromstring(pi_raw[span[0]:span[1]])
        defined_ids = {
            el.attrib["ID"]: (el.tag, el.text or "")
            for el in old_dict_root.iter()
            if "ID" in el.attrib
        }
        pi_updated = pi_raw[:span[0]] + new_entry_xml + pi_raw[span[1]:]
        for id_val, (tag, txt) in defined_ids.items():
            idref_pat = f'<{tag} IDREF="{id_val}"/>'.encode("utf-8")
            if idref_pat in pi_updated:
                promoted = f'<{tag} ID="{id_val}">{txt}</{tag}>'.encode("utf-8")
                pi_updated = pi_updated.replace(idref_pat, promoted, 1)
    else:
        arr_idx0 = pi_raw.find(arr_end_marker)
        if arr_idx0 < 0:
            raise RuntimeError("Could not locate </array><key>_PrelinkLinkKASLROffsets</key> in __PRELINK_INFO")
        pi_updated = pi_raw[:arr_idx0] + new_entry_xml + pi_raw[arr_idx0:]

    arr_idx = pi_updated.find(arr_end_marker)
    if arr_idx < 0:
        raise RuntimeError("Could not locate </array><key>_PrelinkLinkKASLROffsets</key> in __PRELINK_INFO")

    data_start = arr_idx + len(arr_end_marker)
    data_end = pi_updated.find(b"</data>", data_start)
    if data_end < 0:
        raise RuntimeError("Could not locate </data> for _PrelinkLinkKASLROffsets")

    lowest_vm = min(s["vmaddr"] for s in m16["segs"] if s["vmsize"] > 0)
    orig_kaslr_blob = base64.b64decode(pi_updated[data_start:data_end])
    old_cnt = struct.unpack_from("<I", orig_kaslr_blob, 0)[0]
    assert len(orig_kaslr_blob) == 4 + old_cnt * 4
    orig_offs = list(struct.unpack_from(f"<{old_cnt}I", orig_kaslr_blob, 4))

    removed_kaslr_cnt = 0
    if mode == "inplace":
        def in_cs42(off_rel: int) -> bool:
            va = lowest_vm + off_rel
            return (
                (KC16_CS42_TEXT_VA <= va < KC16_CS42_TEXT_VA + KC16_CS42_TEXT_SZ)
                or (KC16_CS42_EXEC_VA <= va < KC16_CS42_EXEC_VA + KC16_CS42_EXEC_SZ)
                or (KC16_CS42_DC_VA <= va < KC16_CS42_DC_VA + KC16_CS42_DC_SZ)
                or (KC16_CS42_DATA_VA <= va < KC16_CS42_DATA_VA + KC16_CS42_DATA_SZ)
            )
        kept_offs = [o for o in orig_offs if not in_cs42(o)]
        removed_kaslr_cnt = len(orig_offs) - len(kept_offs)
    else:
        kept_offs = orig_offs

    new_kaslr_offs = []
    for idx in range(57):
        new_kaslr_offs.append((seg_dc_vm + idx * 8) - lowest_vm)
    for rel in (0x1C8, 0x1D0, 0x1D8, 0x1E0):
        new_kaslr_offs.append((seg_dc_vm + rel) - lowest_vm)
    for s16 in range(361):
        new_kaslr_offs.append((seg_dc_vm + 0x1F8 + s16 * 8) - lowest_vm)
    for s16 in range(14):
        new_kaslr_offs.append((seg_dc_vm + mc_vt_off + s16 * 8) - lowest_vm)
    new_kaslr_offs.append((seg_dc_vm + kt_off + 0x10) - lowest_vm)
    new_kaslr_offs.append((seg_dc_vm + kt_off + 0x20) - lowest_vm)
    new_kaslr_offs.append((seg_data_vm + 0xB4) - lowest_vm)
    new_kaslr_offs.append((seg_data_vm + 0xBC) - lowest_vm)
    new_kaslr_offs.sort()

    all_kaslr_offs = kept_offs + new_kaslr_offs
    kaslr_blob = struct.pack("<I", len(all_kaslr_offs)) + struct.pack(f"<{len(all_kaslr_offs)}I", *all_kaslr_offs)
    new_kaslr_b64 = base64.b64encode(kaslr_blob)

    new_pi_xml = pi_updated[:data_start] + new_kaslr_b64 + pi_updated[data_end:] + b"\x00"

    def pad_seg(b: bytes, total: int) -> bytes:
        assert len(b) <= total
        return b + b"\x00" * (total - len(b))

    if mode == "inplace":
        if len(new_pi_xml) > pi_sect["size"]:
            raise RuntimeError(f"__PRELINK_INFO overflow in inplace mode: {len(new_pi_xml)} > {pi_sect['size']}")
        d16[seg_text_fo:seg_text_fo + KC16_CS42_TEXT_SZ] = pad_seg(raw_text, KC16_CS42_TEXT_SZ)
        d16[seg_exec_fo:seg_exec_fo + KC16_CS42_EXEC_SZ] = pad_seg(raw_exec, KC16_CS42_EXEC_SZ)
        d16[seg_dc_fo:seg_dc_fo + KC16_CS42_DC_SZ] = pad_seg(new_dc, KC16_CS42_DC_SZ)
        d16[seg_data_fo:seg_data_fo + KC16_CS42_DATA_SZ] = pad_seg(raw_data, KC16_CS42_DATA_SZ)
        d16[pi_sect["offset"]:pi_sect["offset"] + pi_sect["size"]] = pad_seg(new_pi_xml, pi_sect["size"])
        Path(out_path).write_bytes(d16)

        sym_map["mode"] = "inplace"
        sym_map["replaced_bundle_id"] = KC16_CS42_BUNDLE_ID
        sym_map["injected_layout"] = {
            "__TEXT (in __PRELINK_TEXT)":     {"vmaddr": f"0x{seg_text_vm:016x}", "slot_size": f"0x{seg_text_sz:x}", "used_size": f"0x{new_text_vsz:x}", "fileoff": f"0x{seg_text_fo:x}", "prot": "r--"},
            "__TEXT_EXEC (in __PLK_TEXT_EXEC)": {"vmaddr": f"0x{seg_exec_vm:016x}", "slot_size": f"0x{seg_exec_sz:x}", "used_size": f"0x{KC15_EXEC_SZ:x}", "fileoff": f"0x{seg_exec_fo:x}", "prot": "r-x"},
            "__DATA_CONST (in __PLK_DATA_CONST)": {"vmaddr": f"0x{seg_dc_vm:016x}", "slot_size": f"0x{seg_dc_sz:x}", "used_size": f"0x{new_dc_sz:x}", "fileoff": f"0x{seg_dc_fo:x}", "prot": "r--"},
            "__DATA (in __PRELINK_DATA)":     {"vmaddr": f"0x{seg_data_vm:016x}", "slot_size": f"0x{seg_data_sz:x}", "used_size": f"0x{KC15_DATA_SZ:x}", "fileoff": f"0x{seg_data_fo:x}", "prot": "rw-"},
            "__PRELINK_INFO":                 {"vmaddr": f"0x{prelink_seg['vmaddr']:016x}", "slot_size": f"0x{pi_sect['size']:x}", "xml_len": len(new_pi_xml) - 1, "slack": pi_sect["size"] - len(new_pi_xml)},
            "kaslr_removed_cs42": removed_kaslr_cnt,
            "kaslr_added_cs35": len(new_kaslr_offs),
            "patch_stats": stats,
            "overridden_vtable_slots": overridden_slots,
        }
    else:
        new_pi_seg_sz = align_up(len(new_pi_xml))
        new_pi_padded = new_pi_xml + b"\x00" * (new_pi_seg_sz - len(new_pi_xml))

        def make_seg_cmd(segname: str, sectname: str, vm: int, vsz: int, fo: int, fsz: int, prot: int, sflags: int) -> bytes:
            cmd = struct.pack(
                "<II16sQQQQIIII",
                0x19, 72 + 80, segname.encode().ljust(16, b"\x00"),
                vm, vsz, fo, fsz, prot, prot, 1, 0,
            )
            sec = struct.pack(
                "<16s16sQQIIIIIIII",
                sectname.encode().ljust(16, b"\x00"), segname.encode().ljust(16, b"\x00"),
                vm, vsz, fo, 4, 0, 0, sflags, 0, 0, 0,
            )
            return cmd + sec

        new_seg_cmds = [
            make_seg_cmd("__CS35_TEXT", "__text", seg_text_vm, seg_text_sz, seg_text_fo, seg_text_sz, 1, 0),
            make_seg_cmd("__CS35_EXEC", "__text", seg_exec_vm, seg_exec_sz, seg_exec_fo, seg_exec_sz, 5, 0x80000400),
            make_seg_cmd("__CS35_DC",   "__data", seg_dc_vm,   seg_dc_sz,   seg_dc_fo,   seg_dc_sz,   1, 0),
            make_seg_cmd("__CS35_DATA", "__data", seg_data_vm, seg_data_sz, seg_data_fo, seg_data_sz, 3, 0),
        ]

        orig_cmds = []
        non_seg_cmds = []
        prelink_cmd_blob = None
        off = 32
        for _ in range(m16["ncmds"]):
            c, cs = struct.unpack_from("<II", d16, off)
            blob = bytearray(d16[off:off + cs])
            if c == 0x19:
                sn = blob[8:24].split(b"\x00", 1)[0].decode()
                if sn in ("__PLK_LLVM_COV", "__PLK_LINKEDIT"):
                    struct.pack_into("<QQQ", blob, 24, next_vm, 0, next_fo)
                    if struct.unpack_from("<I", blob, 64)[0] > 0:
                        struct.pack_into("<QQI", blob, 72 + 32, next_vm, 0, next_fo)
                    orig_cmds.append(bytes(blob))
                elif sn == "__PRELINK_INFO":
                    struct.pack_into("<QQQQ", blob, 24, next_vm, new_pi_seg_sz, next_fo, new_pi_seg_sz)
                    struct.pack_into("<QQI", blob, 72 + 32, next_vm, new_pi_seg_sz, next_fo)
                    prelink_cmd_blob = bytes(blob)
                else:
                    orig_cmds.append(bytes(blob))
            else:
                non_seg_cmds.append(bytes(blob))
            off += cs

        assert prelink_cmd_blob is not None
        all_cmds = b"".join(orig_cmds + new_seg_cmds + [prelink_cmd_blob] + non_seg_cmds)
        new_ncmds = m16["ncmds"] + 4
        new_sizeofcmds = len(all_cmds)
        if 32 + new_sizeofcmds > m16["first_sect_off"]:
            raise RuntimeError("Not enough Mach-O header slack for 4 new LC_SEGMENT_64 commands!")

        out_buf = bytearray(d16[:base_fo])
        struct.pack_into("<II", out_buf, 16, new_ncmds, new_sizeofcmds)
        out_buf[32:32 + new_sizeofcmds] = all_cmds
        out_buf[32 + new_sizeofcmds:m16["first_sect_off"]] = b"\x00" * (m16["first_sect_off"] - (32 + new_sizeofcmds))

        out_buf.extend(pad_seg(raw_text, seg_text_sz))
        out_buf.extend(pad_seg(raw_exec, seg_exec_sz))
        out_buf.extend(pad_seg(new_dc,   seg_dc_sz))
        out_buf.extend(pad_seg(raw_data, seg_data_sz))
        out_buf.extend(new_pi_padded)
        Path(out_path).write_bytes(out_buf)

        sym_map["mode"] = "newseg"
        sym_map["injected_layout"] = {
            "__CS35_TEXT":  {"vmaddr": f"0x{seg_text_vm:016x}", "vmsize": f"0x{seg_text_sz:x}", "fileoff": f"0x{seg_text_fo:x}", "prot": "r--"},
            "__CS35_EXEC":  {"vmaddr": f"0x{seg_exec_vm:016x}", "vmsize": f"0x{seg_exec_sz:x}", "fileoff": f"0x{seg_exec_fo:x}", "prot": "r-x"},
            "__CS35_DC":    {"vmaddr": f"0x{seg_dc_vm:016x}",   "vmsize": f"0x{seg_dc_sz:x}",   "fileoff": f"0x{seg_dc_fo:x}",   "prot": "r--"},
            "__CS35_DATA":  {"vmaddr": f"0x{seg_data_vm:016x}", "vmsize": f"0x{seg_data_sz:x}", "fileoff": f"0x{seg_data_fo:x}", "prot": "rw-"},
            "__PRELINK_INFO": {"vmaddr": f"0x{next_vm:016x}", "vmsize": f"0x{new_pi_seg_sz:x}", "fileoff": f"0x{next_fo:x}"},
            "patch_stats": stats,
            "overridden_vtable_slots": overridden_slots,
        }

    if sym_map_out:
        Path(sym_map_out).write_text(json.dumps(sym_map, indent=2) + "\n")
    return sym_map

def verify_output(out_path: str, sym_map: dict, mode: str = None):
    mode = mode or sym_map.get("mode", "inplace")
    out_data = Path(out_path).read_bytes()
    m = parse_macho(out_data)
    v2o = make_va2off(m["segs"])
    is_mapped = make_is_mapped_va(m["segs"])
    s2v, v2s = load_symtab(out_data, m["symtab"])

    seg_by_name = {s["segname"]: s for s in m["segs"]}
    pi_seg = seg_by_name["__PRELINK_INFO"]

    if mode == "inplace":
        t_seg = {"vmaddr": KC16_CS42_TEXT_VA, "vmsize": KC16_CS42_TEXT_SZ, "fileoff": v2o(KC16_CS42_TEXT_VA)}
        e_seg = {"vmaddr": KC16_CS42_EXEC_VA, "vmsize": KC16_CS42_EXEC_SZ, "fileoff": v2o(KC16_CS42_EXEC_VA)}
        dc_seg = {"vmaddr": KC16_CS42_DC_VA,  "vmsize": KC16_CS42_DC_SZ,   "fileoff": v2o(KC16_CS42_DC_VA)}
        d_seg = {"vmaddr": KC16_CS42_DATA_VA, "vmsize": KC16_CS42_DATA_SZ, "fileoff": v2o(KC16_CS42_DATA_VA)}
        for sn in ("__CS35_TEXT", "__CS35_EXEC", "__CS35_DC", "__CS35_DATA"):
            assert sn not in seg_by_name, f"Unexpected {sn} in inplace mode"
        plk_txt = seg_by_name["__PRELINK_TEXT"]
        plk_exec = seg_by_name["__PLK_TEXT_EXEC"]
        plk_dc = seg_by_name["__PLK_DATA_CONST"]
        plk_data = seg_by_name["__PRELINK_DATA"]
        assert plk_txt["vmaddr"] <= t_seg["vmaddr"] and t_seg["vmaddr"] + t_seg["vmsize"] <= plk_txt["vmaddr"] + plk_txt["vmsize"]
        assert plk_exec["vmaddr"] <= e_seg["vmaddr"] and e_seg["vmaddr"] + e_seg["vmsize"] <= plk_exec["vmaddr"] + plk_exec["vmsize"]
        assert plk_dc["vmaddr"] <= dc_seg["vmaddr"] and dc_seg["vmaddr"] + dc_seg["vmsize"] <= plk_dc["vmaddr"] + plk_dc["vmsize"]
        assert plk_data["vmaddr"] <= d_seg["vmaddr"] and d_seg["vmaddr"] + d_seg["vmsize"] <= plk_data["vmaddr"] + plk_data["vmsize"]
    else:
        for sn in ("__CS35_TEXT", "__CS35_EXEC", "__CS35_DC", "__CS35_DATA", "__PRELINK_INFO"):
            assert sn in seg_by_name, f"Missing segment {sn}"
            s = seg_by_name[sn]
            assert s["vmaddr"] % PAGE_SIZE == 0 and s["vmsize"] % PAGE_SIZE == 0
            assert s["fileoff"] % PAGE_SIZE == 0 and s["filesize"] % PAGE_SIZE == 0
        t_seg = seg_by_name["__CS35_TEXT"]
        e_seg = seg_by_name["__CS35_EXEC"]
        dc_seg = seg_by_name["__CS35_DC"]
        d_seg = seg_by_name["__CS35_DATA"]

    res = subprocess.run(["otool", "-l", out_path], capture_output=True, text=True)
    assert res.returncode == 0, f"otool -l failed: {res.stderr}"
    if mode == "newseg":
        assert "__CS35_TEXT" in res.stdout and "__CS35_DATA" in res.stdout
    else:
        assert "__PLK_TEXT_EXEC" in res.stdout and "__PRELINK_INFO" in res.stdout

    pi_xml = out_data[pi_seg["fileoff"]:pi_seg["fileoff"] + pi_seg["filesize"]].split(b"\x00", 1)[0]
    root = ET.fromstring(pi_xml)
    seen_ids = set()
    for el in root.iter():
        if "ID" in el.attrib:
            assert el.attrib["ID"] not in seen_ids, f"Duplicate XML ID={el.attrib['ID']}"
            seen_ids.add(el.attrib["ID"])
        if "IDREF" in el.attrib:
            assert el.attrib["IDREF"] in seen_ids, f"Dangling XML IDREF={el.attrib['IDREF']}"

    arr = root[1]
    found_entry = None
    found_cs42 = False
    for d_el in arr:
        ch = list(d_el)
        kv = {ch[i].text: ch[i + 1] for i in range(0, len(ch), 2)}
        bid = kv.get("CFBundleIdentifier")
        if bid is not None and bid.text == BUNDLE_ID:
            found_entry = kv
        if bid is not None and bid.text == KC16_CS42_BUNDLE_ID:
            found_cs42 = True
    assert found_entry is not None, "AppleCS35L19Amp missing from __PRELINK_INFO"
    if mode == "inplace":
        assert not found_cs42, "AppleCS42L81Audio should be replaced in inplace mode"
    load_addr = int(found_entry["_PrelinkExecutableLoadAddr"].text, 16)
    kmod_addr = int(found_entry["_PrelinkKmodInfo"].text, 16)
    assert load_addr == t_seg["vmaddr"] and kmod_addr == d_seg["vmaddr"]

    assert out_data[t_seg["fileoff"]:t_seg["fileoff"] + 4] == b"\xcf\xfa\xed\xfe"
    plk_txt_sec = seg_by_name["__PRELINK_TEXT"]["sects"][0]
    walk_cur = plk_txt_sec["offset"]
    walk_end = walk_cur + plk_txt_sec["size"]
    walk_count = 0
    walk_saw_cs35 = False
    while walk_cur < walk_end:
        w_magic, _, _, _, w_ncmds, w_szcmds = struct.unpack_from("<IIIIII", out_data, walk_cur)
        if w_magic == 0 and out_data[walk_cur:walk_end] == b"\x00" * (walk_end - walk_cur):
            break
        assert w_magic == 0xFEEDFACF, f"kalloc_type_init walk failed at fileoff=0x{walk_cur:x}: magic=0x{w_magic:x}"
        if walk_cur == t_seg["fileoff"]:
            walk_saw_cs35 = True
        w_txt_fsz = 0
        wo = walk_cur + 32
        for _ in range(w_ncmds):
            wc, wcs = struct.unpack_from("<II", out_data, wo)
            if wc == 0x19 and out_data[wo + 8:wo + 24].split(b"\x00", 1)[0] == b"__TEXT":
                w_txt_fsz = struct.unpack_from("<Q", out_data, wo + 24 + 24)[0]
                break
            wo += wcs
        walk_count += 1
        walk_cur += ((w_txt_fsz + 0x3F) & ~0x3F) if w_txt_fsz else ((w_szcmds + 0x3F) & ~0x3F)
    assert walk_count == 208, f"Expected 208 kext headers in __PRELINK_TEXT.__text, walked {walk_count}"
    if mode == "inplace":
        assert walk_saw_cs35, "kalloc_type_init walk did not visit embedded AppleCS35L19Amp"

    for i in range(57):
        gval = struct.unpack_from("<Q", out_data, dc_seg["fileoff"] + i * 8)[0]
        assert gval != 0 and is_mapped(gval), f"Unresolved GOT[{i}] = 0x{gval:x}"

    stubs_fo = e_seg["fileoff"] + KC15_TEXT_ONLY_SZ
    stubs_va = e_seg["vmaddr"] + KC15_TEXT_ONLY_SZ
    for i in range(KC15_STUBS_SZ // 12):
        sva = stubs_va + i * 12
        i0, i1, i2 = struct.unpack_from("<III", out_data, stubs_fo + i * 12)
        assert i2 == 0xD61F0200
        imm = sign_extend((((i0 >> 5) & 0x7FFFF) << 2) | ((i0 >> 29) & 3), 21)
        gva = (sva & ~0xFFF) + (imm << 12) + ((i1 >> 10) & 0xFFF) * 8
        assert dc_seg["vmaddr"] <= gva < dc_seg["vmaddr"] + 57 * 8

    valid_exec_ranges = [
        (s["vmaddr"], s["vmaddr"] + s["vmsize"])
        for s in m["segs"]
        if (s["initprot"] & 4)
    ]
    def is_valid_code(va: int) -> bool:
        return any(a <= va < b for a, b in valid_exec_ranges)

    vt_fo = dc_seg["fileoff"] + 0x1F8
    for s in range(361):
        ptr = struct.unpack_from("<Q", out_data, vt_fo + s * 8)[0]
        assert is_valid_code(ptr), f"Invalid CS35L19Amp vtable slot {s}: 0x{ptr:x}"

    mc_vt_fo = dc_seg["fileoff"] + 0xD58
    for s in range(14):
        ptr = struct.unpack_from("<Q", out_data, mc_vt_fo + s * 8)[0]
        assert is_valid_code(ptr), f"Invalid MetaClass vtable slot {s}: 0x{ptr:x}"

    text_fo = e_seg["fileoff"]
    text_va = e_seg["vmaddr"]
    adrp_count = 0
    bl_count = 0
    regs = {}
    for p in range(0, KC15_TEXT_ONLY_SZ, 4):
        pc = text_va + p
        ins = struct.unpack_from("<I", out_data, text_fo + p)[0]
        if (ins & 0x7C000000) == 0x14000000:
            imm26 = sign_extend(ins & 0x03FFFFFF, 26)
            tgt = pc + (imm26 << 2)
            assert e_seg["vmaddr"] <= tgt < e_seg["vmaddr"] + KC15_EXEC_SZ, f"Branch out of kext at 0x{pc:x} -> 0x{tgt:x}"
            bl_count += 1
            for r in range(19):
                regs.pop(r, None)
        elif (ins & 0x9F000000) == 0x90000000:
            rd = ins & 0x1F
            imm = sign_extend((((ins >> 5) & 0x7FFFF) << 2) | ((ins >> 29) & 3), 21)
            regs[rd] = (pc & ~0xFFF) + (imm << 12)
            adrp_count += 1
        elif (ins & 0xFF800000) == 0x91000000:
            rd = ins & 0x1F
            rn = (ins >> 5) & 0x1F
            imm12 = (ins >> 10) & 0xFFF
            if (ins >> 22) & 1:
                imm12 <<= 12
            if rn in regs:
                tgt = regs[rn] + imm12
                assert any(seg["vmaddr"] <= tgt < seg["vmaddr"] + seg["vmsize"] for seg in (t_seg, e_seg, dc_seg, d_seg)), (
                    f"ADRP+ADD out of kext at 0x{pc:x} -> 0x{tgt:x}"
                )
                regs.pop(rd, None)
            else:
                regs.pop(rd, None)
        elif (ins & 0x3B000000) == 0x39000000:
            rt = ins & 0x1F
            rn = (ins >> 5) & 0x1F
            sz = (ins >> 30) & 3
            imm = ((ins >> 10) & 0xFFF) << sz
            is_load = bool((ins >> 22) & 1)
            if rn in regs:
                tgt = regs[rn] + imm
                assert any(seg["vmaddr"] <= tgt < seg["vmaddr"] + seg["vmsize"] for seg in (t_seg, e_seg, dc_seg, d_seg)), (
                    f"ADRP+MEM out of kext at 0x{pc:x} -> 0x{tgt:x}"
                )
                if is_load:
                    regs.pop(rt, None)
            elif is_load:
                regs.pop(rt, None)
        elif (ins & 0x7C000000) == 0x14000000 or (ins & 0xFFFFFC1F) in (0xD63F0000, 0xD61F0000):
            for r in range(19):
                regs.pop(r, None)
        elif ins == 0xD65F03C0:
            regs.clear()

    kt_fo = dc_seg["fileoff"] + (0xE10 - 0x40)
    sig_va = struct.unpack_from("<Q", out_data, kt_fo + 0x20)[0]
    fsz = struct.unpack_from("<Q", out_data, kt_fo + 0x28)[0]
    assert (fsz >> 32) == NEW_ALLOC_SIZE
    sig_fo = v2o(sig_va)
    sig_str = out_data[sig_fo:out_data.find(b"\x00", sig_fo)].decode()
    assert len(sig_str) == NEW_ALLOC_SIZE // 8 == 80

    km_fo = d_seg["fileoff"]
    assert struct.unpack_from("<Q", out_data, km_fo + 0x9C)[0] == t_seg["vmaddr"]
    km_start = struct.unpack_from("<Q", out_data, km_fo + 0xB4)[0]
    km_stop = struct.unpack_from("<Q", out_data, km_fo + 0xBC)[0]
    assert is_valid_code(km_start) and is_valid_code(km_stop)
    mod_init_ptr = struct.unpack_from("<Q", out_data, dc_seg["fileoff"] + 0x1C8)[0]
    assert is_valid_code(mod_init_ptr)

    lowest_vm = min(s["vmaddr"] for s in m["segs"] if s["vmsize"] > 0)
    ch_top = list(root)
    kaslr_b64 = next(ch_top[i + 1].text for i in range(0, len(ch_top), 2) if ch_top[i].text == "_PrelinkLinkKASLROffsets")
    k_blob = base64.b64decode(kaslr_b64)
    k_cnt = struct.unpack_from("<I", k_blob, 0)[0]
    assert len(k_blob) == 4 + k_cnt * 4
    tail_offs = struct.unpack_from("<440I", k_blob, len(k_blob) - 440 * 4)
    assert tail_offs == tuple(sorted(tail_offs)), "New CS35 KASLR offsets must be sorted"
    for ko in tail_offs:
        va = lowest_vm + ko
        ptr = struct.unpack_from("<Q", out_data, v2o(va))[0]
        assert is_mapped(ptr), f"Invalid KASLR pointer at 0x{va:x} -> 0x{ptr:x}"
    if mode == "inplace":
        all_offs = struct.unpack_from(f"<{k_cnt}I", k_blob, 4)
        cs42_unused_exec = (KC16_CS42_EXEC_VA, KC16_CS42_EXEC_VA + KC16_CS42_EXEC_SZ)
        cs42_unused_dc = (KC16_CS42_DC_VA + 0xE10, KC16_CS42_DC_VA + KC16_CS42_DC_SZ)
        cs42_unused_d = (KC16_CS42_DATA_VA + KC15_DATA_SZ, KC16_CS42_DATA_VA + KC16_CS42_DATA_SZ)
        for ko in all_offs:
            va = lowest_vm + ko
            assert not (cs42_unused_exec[0] <= va < cs42_unused_exec[1]), f"Stale CS42 exec KASLR 0x{va:x}"
            assert not (cs42_unused_dc[0] <= va < cs42_unused_dc[1]), f"Stale CS42 dc KASLR 0x{va:x}"
            assert not (cs42_unused_d[0] <= va < cs42_unused_d[1]), f"Stale CS42 data KASLR 0x{va:x}"

    return {
        "mode": mode,
        "otool_l": "PASS",
        "prelink_xml": f"PASS ({len(arr)} kexts, {len(seen_ids)} XML IDs valid)",
        "kalloc_type_walk": f"PASS ({walk_count} __PRELINK_TEXT kexts walked cleanly)",
        "kaslr_offsets": f"PASS (total={k_cnt}, +440 new CS35 pointers)",
        "got_entries": "57/57 resolved",
        "stubs": "50/50 valid",
        "vtable_slots": "361/361 + 14/14 valid",
        "adrp_relocs": f"{adrp_count}/145 verified",
        "branches": f"{bl_count} internal branches verified",
        "kalloc_type": f"size=0x{NEW_ALLOC_SIZE:x} sig_len={len(sig_str)} PASS",
        "file_size": len(out_data),
    }

def main():
    parser = argparse.ArgumentParser(description="Port & relink AppleCS35L19Amp (kc15 -> kc16)")
    parser.add_argument("--kc15", default="/tmp/kc15_n71m.dec", help="Decompressed iOS 15.8.8 kernelcache")
    parser.add_argument("--kc16", default="/tmp/kc16_gold_v4.dec", help="Input decompressed iOS 16.7.16 kernelcache")
    parser.add_argument("--output", default="/tmp/kc16_test_cs35l19.dec", help="Output patched kernelcache")
    parser.add_argument("--symbol-map", default=str(Path(__file__).with_name("symbol_map.json")), help="Output symbol_map.json path")
    parser.add_argument("--mode", choices=["inplace", "newseg"], default="inplace", help="Placement strategy (default: inplace into AppleCS42L81Audio)")
    parser.add_argument("--verify", action="store_true", default=True, help="Run post-relink verification suite")
    args = parser.parse_args()

    sym_map = relink_kext(args.kc15, args.kc16, args.output, args.symbol_map, mode=args.mode)
    print("Relink completed:")
    print(f"  Mode:   {args.mode}")
    print(f"  Output: {args.output}")
    print(f"  Layout: {json.dumps(sym_map['injected_layout'], indent=2)}")
    if args.verify:
        v_res = verify_output(args.output, sym_map, mode=args.mode)
        print("Verification:", json.dumps(v_res, indent=2))

if __name__ == "__main__":
    main()
