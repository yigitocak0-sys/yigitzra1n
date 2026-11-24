#!/usr/bin/env python3
import argparse
import base64
import bisect
import json
import os
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

PAGE_SIZE = 0x4000
SEG_ALIGN_MASK = PAGE_SIZE - 1

K15_TEXT_VA, K15_TEXT_SZ = 0xFFFFFFF005A6B900, 0x2A3B
K15_TXEC_VA, K15_TXEC_SZ = 0xFFFFFFF006BD5000, 0x10D1C
K15_TEXT_ONLY_SZ = 0x10944
K15_STUBS_VA, K15_STUBS_SZ = 0xFFFFFFF006BE5944, 0x3D8
K15_DCST_VA, K15_DCST_SZ = 0xFFFFFFF006FCBA80, 0x5050
K15_DATA_VA, K15_DATA_SZ = 0xFFFFFFF007A5EE80, 0x250
K15_DATA_FILE_SZ = 0xC8

K15_GOT_VA, K15_GOT_N = 0xFFFFFFF006FCBA80, 103
K15_MOD_INIT_VA = 0xFFFFFFF006FCBDB8
K15_MOD_TERM_VA = 0xFFFFFFF006FCBDC8
K15_CONST_VA, K15_CONST_SZ = 0xFFFFFFF006FCBDD8, 0x4AF8
K15_KALLOC_VA, K15_KALLOC_SZ = 0xFFFFFFF006FD08D0, 0x200

KEXT_KMOD_SIZE = 0x24000

def align_up(v, a=PAGE_SIZE):
    return (v + a - 1) & ~(a - 1)

def parse_macho(data):
    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = struct.unpack_from("<8I", data, 0)
    if magic != 0xFEEDFACF:
        raise ValueError(f"Invalid Mach-O 64 magic: {hex(magic)}")
    segs = []
    symtab = None
    dysymtab = None
    off = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<2I", data, off)
        if cmd == 0x19:
            segname = data[off + 8 : off + 24].split(b"\x00", 1)[0].decode("latin1")
            vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, segflags = struct.unpack_from(
                "<4Q4I", data, off + 24
            )
            sects = []
            soff = off + 72
            for _ in range(nsects):
                sectname = data[soff : soff + 16].split(b"\x00", 1)[0].decode("latin1")
                s_segname = data[soff + 16 : soff + 32].split(b"\x00", 1)[0].decode("latin1")
                addr, size, offset, align, reloff, nreloc, sflags, r1, r2, r3 = struct.unpack_from(
                    "<2Q8I", data, soff + 32
                )
                sects.append(
                    {
                        "hdr_off": soff,
                        "sectname": sectname,
                        "segname": s_segname,
                        "addr": addr,
                        "size": size,
                        "offset": offset,
                        "align": align,
                        "reloff": reloff,
                        "nreloc": nreloc,
                        "flags": sflags,
                        "r1": r1,
                        "r2": r2,
                        "r3": r3,
                    }
                )
                soff += 80
            segs.append(
                {
                    "cmd_off": off,
                    "cmdsize": cmdsize,
                    "segname": segname,
                    "vmaddr": vmaddr,
                    "vmsize": vmsize,
                    "fileoff": fileoff,
                    "filesize": filesize,
                    "maxprot": maxprot,
                    "initprot": initprot,
                    "nsects": nsects,
                    "flags": segflags,
                    "sects": sects,
                }
            )
        elif cmd == 0x2:
            symtab = struct.unpack_from("<4I", data, off + 8)
        elif cmd == 0xB:
            dysymtab = struct.unpack_from("<18I", data, off + 8)
        off += cmdsize
    return {
        "cputype": cputype,
        "cpusubtype": cpusubtype,
        "filetype": filetype,
        "ncmds": ncmds,
        "sizeofcmds": sizeofcmds,
        "flags": flags,
        "reserved": reserved,
        "hdr_end": off,
        "segs": segs,
        "symtab": symtab,
        "dysymtab": dysymtab,
    }

def va_to_off(segs, va):
    for s in segs:
        if s["vmaddr"] <= va < s["vmaddr"] + s["filesize"]:
            return s["fileoff"] + (va - s["vmaddr"])
    return None

def off_to_va(segs, off):
    for s in segs:
        if s["filesize"] > 0 and s["fileoff"] <= off < s["fileoff"] + s["filesize"]:
            return s["vmaddr"] + (off - s["fileoff"])
    return None

def load_symtab(data, symtab):
    symoff, nsyms, stroff, _ = symtab
    syms = {}
    addr_to_names = {}
    for i in range(nsyms):
        n_strx, _, _, _, n_value = struct.unpack_from("<IBBHQ", data, symoff + i * 16)
        end = data.find(b"\x00", stroff + n_strx)
        name = data[stroff + n_strx : end].decode("utf-8", "replace")
        syms[name] = n_value
        addr_to_names.setdefault(n_value, []).append(name)
    return syms, addr_to_names

def parse_iocf_xml(raw_bytes):
    root = ET.fromstring(raw_bytes.decode("utf-8"))
    ids = {}

    def decode(el):
        tag = el.tag
        if "IDREF" in el.attrib:
            return ids[el.attrib["IDREF"]]
        val = None
        if tag == "dict":
            val = {}
            ch = list(el)
            for i in range(0, len(ch), 2):
                val[ch[i].text or ""] = decode(ch[i + 1])
        elif tag == "array":
            val = [decode(c) for c in el]
        elif tag == "string":
            val = el.text or ""
        elif tag == "integer":
            val = int((el.text or "0").strip(), 0)
        elif tag == "true":
            val = True
        elif tag == "false":
            val = False
        elif tag == "data":
            val = (el.text or "").strip()
        elif tag == "real":
            val = float(el.text or "0")
        if "ID" in el.attrib:
            ids[el.attrib["ID"]] = val
        return val

    return decode(root)

def load_prelink_info(data, segs):
    for s in segs:
        if s["segname"] == "__PRELINK_INFO":
            sec = s["sects"][0]
            raw = data[sec["offset"] : sec["offset"] + sec["size"]]
            end = raw.find(b"\x00")
            if end != -1:
                raw = raw[:end]
            return parse_iocf_xml(raw), raw, s, sec
    raise ValueError("__PRELINK_INFO section not found")

def serialize_iocf_value(v):
    if isinstance(v, bool):
        return "<true/>" if v else "<false/>"
    if isinstance(v, int):
        return f'<integer size="64">{hex(v)}</integer>'
    if isinstance(v, str):
        return f"<string>{escape(v)}</string>"
    if isinstance(v, list):
        return "<array>" + "".join(serialize_iocf_value(x) for x in v) + "</array>"
    if isinstance(v, dict):
        items = []
        for k, val in v.items():
            items.append(f"<key>{escape(str(k))}</key>{serialize_iocf_value(val)}")
        return "<dict>" + "".join(items) + "</dict>"
    raise TypeError(f"Unsupported IOCF plist type: {type(v)}")

def find_kext_vtable(data, segs, pl, bid, expected_len):
    for it in pl["_PrelinkInfoDictionary"]:
        if it.get("CFBundleIdentifier") != bid:
            continue
        load_addr = it.get("_PrelinkExecutableLoadAddr", 0)
        off = va_to_off(segs, load_addr)
        ncmds = struct.unpack_from("<I", data, off + 16)[0]
        coff = off + 32
        k_const = None
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from("<2I", data, coff)
            if cmd == 0x19:
                sname = data[coff + 8 : coff + 24].split(b"\x00", 1)[0].decode("latin1")
                nsects = struct.unpack_from("<I", data, coff + 64)[0]
                soff = coff + 72
                for _ in range(nsects):
                    secn = data[soff : soff + 16].split(b"\x00", 1)[0].decode("latin1")
                    s_addr, s_sz = struct.unpack_from("<2Q", data, soff + 32)
                    if sname == "__DATA_CONST" and secn == "__const":
                        k_const = (s_addr, s_sz)
                    soff += 80
            coff += cmdsize
        coff2 = va_to_off(segs, k_const[0])
        pos = 0
        out = []
        while pos < k_const[1] - 16:
            q0, q1 = struct.unpack_from("<QQ", data, coff2 + pos)
            if q0 == 0 and q1 == 0:
                ptrs = []
                p2 = pos + 16
                while p2 <= k_const[1] - 8:
                    v = struct.unpack_from("<Q", data, coff2 + p2)[0]
                    if (v >> 36) == 0xFFFFFFF:
                        ptrs.append(v)
                        p2 += 8
                    else:
                        break
                if len(ptrs) >= 8:
                    if len(ptrs) == expected_len:
                        out.append((k_const[0] + pos + 16, ptrs))
                    pos = p2
                    continue
            pos += 8
        return out
    return []

def encode_adrp(pc, target_page, rd):
    diff_pages = (target_page - (pc & ~0xFFF)) >> 12
    if not (-(1 << 20) <= diff_pages < (1 << 20)):
        raise ValueError(f"ADRP out of 4GB range: pc={hex(pc)} target_page={hex(target_page)}")
    imm21 = diff_pages & 0x1FFFFF
    immlo = imm21 & 0x3
    immhi = (imm21 >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | (rd & 0x1F)

def encode_add_imm12(rd, rn, imm12):
    if not (0 <= imm12 < 0x1000):
        raise ValueError(f"ADD imm12 out of range: {hex(imm12)}")
    return 0x91000000 | ((imm12 & 0xFFF) << 10) | ((rn & 0x1F) << 5) | (rd & 0x1F)

def encode_movz_w(rd, imm16):
    if not (0 <= imm16 <= 0xFFFF):
        raise ValueError(f"MOVZ imm16 out of range: {hex(imm16)}")
    return 0x52800000 | ((imm16 & 0xFFFF) << 5) | (rd & 0x1F)

def encode_b_or_bl(pc, target, is_bl):
    delta = (target - pc) >> 2
    if not (-(1 << 25) <= delta < (1 << 25)):
        raise ValueError(f"B/BL out of 128MB range: pc={hex(pc)} target={hex(target)}")
    op = 0x94000000 if is_bl else 0x14000000
    return op | (delta & 0x03FFFFFF)

def pack_lc_segment_64(segname, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, sects, flags=0):
    cmdsize = 72 + 80 * len(sects)
    b = bytearray()
    b += struct.pack("<2I16s4Q4I", 0x19, cmdsize, segname.encode("latin1")[:16].ljust(16, b"\x00"),
                     vmaddr, vmsize, fileoff, filesize, maxprot, initprot, len(sects), flags)
    for sec in sects:
        b += struct.pack(
            "<16s16s2Q8I",
            sec["sectname"].encode("latin1")[:16].ljust(16, b"\x00"),
            sec["segname"].encode("latin1")[:16].ljust(16, b"\x00"),
            sec["addr"],
            sec["size"],
            sec["offset"],
            sec["align"],
            sec.get("reloff", 0),
            sec.get("nreloc", 0),
            sec.get("flags", 0),
            sec.get("r1", 0),
            sec.get("r2", 0),
            sec.get("r3", 0),
        )
    return bytes(b)

def find_kext_kalloc_sigs(data, segs, pl, bid):
    for it in pl["_PrelinkInfoDictionary"]:
        if it.get("CFBundleIdentifier") != bid:
            continue
        off = va_to_off(segs, it.get("_PrelinkExecutableLoadAddr", 0))
        em = parse_macho(data[off : off + 0x4000])
        res = {}
        for s in em["segs"]:
            for sec in s["sects"]:
                if sec["sectname"] == "__kalloc_type":
                    ko = va_to_off(segs, sec["addr"])
                    for pos in range(ko, ko + sec["size"], 64):
                        q = struct.unpack_from("<8Q", data, pos)
                        so = va_to_off(segs, q[2])
                        sigo = va_to_off(segs, q[4])
                        if not so or not sigo:
                            continue
                        site = data[so : data.find(b"\x00", so)].decode("latin1")
                        sig = data[sigo : data.find(b"\x00", sigo)].decode("latin1")
                        res[site] = (q[5] >> 32, sig)
        return res
    return {}

def analyze_kc15_cs42l71(kc15, info15, pl15):
    segs15 = info15["segs"]
    toff = va_to_off(segs15, K15_TXEC_VA)
    n_insns = K15_TEXT_ONLY_SZ // 4
    insns = [struct.unpack_from("<I", kc15, toff + i * 4)[0] for i in range(n_insns)]

    coff = va_to_off(segs15, K15_CONST_VA)
    all_vts = []
    pos = 0
    while pos < K15_CONST_SZ - 16:
        q0, q1 = struct.unpack_from("<QQ", kc15, coff + pos)
        if q0 == 0 and q1 == 0:
            ptrs = []
            p2 = pos + 16
            while p2 <= K15_CONST_SZ - 8:
                v = struct.unpack_from("<Q", kc15, coff + p2)[0]
                if (v >> 36) == 0xFFFFFFF:
                    ptrs.append(v)
                    p2 += 8
                else:
                    break
            if len(ptrs) >= 8:
                all_vts.append((K15_CONST_VA + pos + 16, ptrs))
                pos = p2
                continue
        pos += 8

    jump_tgts = {}
    for i, w in enumerate(insns):
        if (w & 0xFFFFFC1F) == 0xD61F0000 and i >= 5:
            i_ldr, i_adr, i_add1, i_adrp = insns[i - 2], insns[i - 3], insns[i - 4], insns[i - 5]
            if (
                (i_adrp & 0x9F000000) == 0x90000000
                and (i_adr & 0x9F000000) == 0x10000000
                and (i_add1 & 0xFF800000) == 0x91000000
            ):
                pc_adrp = K15_TXEC_VA + (i - 5) * 4
                imm = (((i_adrp >> 5) & 0x7FFFF) << 2) | ((i_adrp >> 29) & 3)
                if imm & (1 << 20):
                    imm -= 1 << 21
                tbl_va = (pc_adrp & ~0xFFF) + (imm << 12) + ((i_add1 >> 10) & 0xFFF)
                pc_adr = K15_TXEC_VA + (i - 3) * 4
                aimm = (((i_adr >> 5) & 0x7FFFF) << 2) | ((i_adr >> 29) & 3)
                if aimm & (1 << 20):
                    aimm -= 1 << 21
                base_va = pc_adr + aimm
                n_cases = 0
                for k in range(6, 20):
                    if i - k < 0:
                        break
                    cw = insns[i - k]
                    if (cw & 0x7F80001F) == 0x7100001F:
                        n_cases = ((cw >> 10) & 0xFFF) + 1
                        break
                toff_tbl = va_to_off(segs15, tbl_va)
                esize = 1 if (i_ldr >> 30) == 0 else 4
                tgts = set()
                for idx_c in range(max(n_cases, 1)):
                    disp = (
                        kc15[toff_tbl + idx_c]
                        if esize == 1
                        else struct.unpack_from("<i", kc15, toff_tbl + idx_c * 4)[0]
                    ) * 4
                    tgt_c = base_va + disp
                    if K15_TXEC_VA <= tgt_c < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                        tgts.add(tgt_c)
                if tgts:
                    jump_tgts[K15_TXEC_VA + i * 4] = tgts

    fn_starts = {K15_TXEC_VA, 0xFFFFFFF006BE55FC, 0xFFFFFFF006BE5630}
    fn_to_cls = {
        0xFFFFFFF006BE55FC: {"KMOD"},
        0xFFFFFFF006BE5630: {"KMOD"},
    }
    vt_names = {
        0xFFFFFFF006FCBDF8: "AppleCS42L71Audio",
        0xFFFFFFF006FCC9F0: "AppleCS42L71Mikey",
        0xFFFFFFF006FCD378: "AppleCS42L71MikeyBus",
        0xFFFFFFF006FCDEA8: "AppleCS42L71MicBiasFunction",
        0xFFFFFFF006FCDFE0: "AppleCSCodecMikeyBus",
        0xFFFFFFF006FCEA88: "AppleCSCodecMikeyBusV11",
        0xFFFFFFF006FCF5B8: "AppleCSCodecLineOutMikeyBus",
        0xFFFFFFF006FD0060: "AppleCSCodecMikeyBusAudio",
    }
    for vt_va, ptrs in all_vts:
        cn = vt_names.get(vt_va)
        for p in ptrs:
            if K15_TXEC_VA <= p < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                fn_starts.add(p)
                if cn:
                    fn_to_cls.setdefault(p, set()).add(cn)

    for sec_va in [K15_MOD_INIT_VA, K15_MOD_INIT_VA + 8, K15_MOD_TERM_VA, K15_MOD_TERM_VA + 8]:
        p = struct.unpack_from("<Q", kc15, va_to_off(segs15, sec_va))[0]
        if K15_TXEC_VA <= p < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
            fn_starts.add(p)
            fn_to_cls.setdefault(p, set()).add("MOD_INIT_TERM")

    branch_tgts = set()
    for s in jump_tgts.values():
        branch_tgts |= s
    adr_jtbl = {b - 12 for b in jump_tgts}

    for i, w in enumerate(insns):
        pc = K15_TXEC_VA + i * 4
        if (w & 0xFC000000) == 0x94000000:
            imm = w & 0x03FFFFFF
            if imm & (1 << 25):
                imm -= 1 << 26
            tgt = pc + (imm << 2)
            if K15_TXEC_VA <= tgt < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                fn_starts.add(tgt)
        elif (w & 0xFC000000) == 0x14000000:
            imm = w & 0x03FFFFFF
            if imm & (1 << 25):
                imm -= 1 << 26
            tgt = pc + (imm << 2)
            if K15_TXEC_VA <= tgt < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                branch_tgts.add(tgt)
        elif (w & 0xFF000010) == 0x54000000 or (w & 0x7E000000) == 0x34000000:
            imm = (w >> 5) & 0x7FFFF
            if imm & (1 << 18):
                imm -= 1 << 19
            tgt = pc + (imm << 2)
            if K15_TXEC_VA <= tgt < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                branch_tgts.add(tgt)
        elif (w & 0x7E000000) == 0x36000000:
            imm = (w >> 5) & 0x3FFF
            if imm & (1 << 13):
                imm -= 1 << 14
            tgt = pc + (imm << 2)
            if K15_TXEC_VA <= tgt < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                branch_tgts.add(tgt)
        elif (w & 0x9F000000) == 0x10000000 and pc not in adr_jtbl:
            imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3)
            if imm & (1 << 20):
                imm -= 1 << 21
            tgt = pc + imm
            if K15_TXEC_VA <= tgt < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                fn_starts.add(tgt)

    adrp_regs = {}
    cb_edges = []
    for i, w in enumerate(insns):
        pc = K15_TXEC_VA + i * 4
        if (w & 0x9F000000) == 0x90000000:
            imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3)
            if imm & (1 << 20):
                imm -= 1 << 21
            adrp_regs[w & 0x1F] = (pc & ~0xFFF) + (imm << 12)
        elif (w & 0xFF800000) == 0x91000000:
            rn = (w >> 5) & 0x1F
            if rn in adrp_regs:
                sh, imm12 = (w >> 22) & 1, (w >> 10) & 0xFFF
                tgt = adrp_regs[rn] + (imm12 << (12 if sh else 0))
                if K15_TXEC_VA <= tgt < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                    fn_starts.add(tgt)
                    cb_edges.append((pc, tgt))
            adrp_regs.pop(w & 0x1F, None)
        elif w in (0xD65F03C0, 0xD65F0FFF):
            adrp_regs.clear()

    for i, w in enumerate(insns[:-1]):
        nxt_pc = K15_TXEC_VA + (i + 1) * 4
        nxt = insns[i + 1]
        is_ret = w in (0xD65F03C0, 0xD65F0FFF, 0xD65F0BFF)
        is_term = is_ret or (w & 0xFFFFFC1F) == 0xD61F0000 or (w & 0xFC000000) == 0x14000000
        if not is_term or nxt == 0xD503201F:
            continue
        is_sub_sp = (nxt & 0xFF8003FF) == 0xD10003FF
        is_stp_sp = (nxt & 0xFFC003E0) == 0xA98003E0 or (nxt & 0xFFC003E0) == 0x6D8003E0
        if is_sub_sp or is_stp_sp or (nxt_pc not in branch_tgts):
            fn_starts.add(nxt_pc)

    sorted_fns = sorted(fn_starts) + [K15_TXEC_VA + K15_TEXT_ONLY_SZ]

    def find_fn(pc):
        return sorted_fns[bisect.bisect_right(sorted_fns, pc) - 1]

    for f in sorted_fns[:-1]:
        if f in fn_to_cls:
            continue
        if 0xFFFFFFF006BD51EC <= f < 0xFFFFFFF006BDFB00:
            fn_to_cls[f] = {"AppleCS42L71Audio"}
        elif 0xFFFFFFF006BE41B8 <= f < 0xFFFFFFF006BE55FC:
            fn_to_cls[f] = {"AppleCSCodecMikeyBusAudio"}

    callers = {}
    for i, w in enumerate(insns):
        pc = K15_TXEC_VA + i * 4
        if (w & 0xFC000000) in (0x94000000, 0x14000000):
            imm = w & 0x03FFFFFF
            if imm & (1 << 25):
                imm -= 1 << 26
            tgt = pc + (imm << 2)
            if K15_TXEC_VA <= tgt < K15_TXEC_VA + K15_TEXT_ONLY_SZ:
                cfn = find_fn(pc)
                if tgt != cfn and tgt in fn_starts:
                    callers.setdefault(tgt, set()).add(cfn)
    for pc, tgt in cb_edges:
        cfn = find_fn(pc)
        if tgt != cfn:
            callers.setdefault(tgt, set()).add(cfn)

    for _ in range(10):
        for f in sorted_fns[:-1]:
            if f in fn_to_cls:
                continue
            cs = set()
            for c in callers.get(f, set()):
                cs |= fn_to_cls.get(c, set())
            if cs:
                fn_to_cls[f] = cs

    audio_fns = sorted(f for f in sorted_fns[:-1] if fn_to_cls.get(f) == {"AppleCS42L71Audio"})
    mba_fns = sorted(f for f in sorted_fns[:-1] if fn_to_cls.get(f) == {"AppleCSCodecMikeyBusAudio"})
    mikey_fns = sorted(f for f in sorted_fns[:-1] if fn_to_cls.get(f) == {"AppleCS42L71Mikey"})
    micbias_fns = sorted(f for f in sorted_fns[:-1] if fn_to_cls.get(f) == {"AppleCS42L71MicBiasFunction"})
    mikeybus_family_fns = sorted(
        f for f in sorted_fns[:-1] if fn_to_cls.get(f, set()) & {
            "AppleCS42L71MikeyBus", "AppleCSCodecMikeyBus", "AppleCSCodecMikeyBusV11", "AppleCSCodecLineOutMikeyBus"
        }
    )

    adrp_sites = {}
    for fi in range(len(sorted_fns) - 1):
        fva, fend = sorted_fns[fi], sorted_fns[fi + 1]
        s_i = (fva - K15_TXEC_VA) // 4
        n = (fend - fva) // 4
        in_state = [None] * n
        in_state[0] = {}
        work = [0]
        while work:
            idx = work.pop(0)
            st = dict(in_state[idx])
            while idx < n:
                pc = fva + idx * 4
                w = insns[s_i + idx]
                nxt = [idx + 1]
                if (w & 0x9F000000) == 0x90000000:
                    rd = w & 0x1F
                    imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3)
                    if imm & (1 << 20):
                        imm -= 1 << 21
                    st[rd] = (pc, (pc & ~0xFFF) + (imm << 12))
                    adrp_sites.setdefault(pc, set())
                else:
                    if (w & 0xFF800000) == 0x91000000:
                        rd, rn = w & 0x1F, (w >> 5) & 0x1F
                        if rn != 31 and rn in st:
                            apc, page = st[rn]
                            sh, imm12 = (w >> 22) & 1, (w >> 10) & 0xFFF
                            tgt = page + (imm12 << (12 if sh else 0))
                            adrp_sites[apc].add((pc, tgt, "ADD"))
                        if rd != 31:
                            st.pop(rd, None)
                    elif (w & 0x3B000000) == 0x39000000:
                        rn, rt = (w >> 5) & 0x1F, w & 0x1F
                        size, v, opc = (w >> 30) & 3, (w >> 26) & 1, (w >> 22) & 3
                        scale = 4 if (v == 1 and (opc & 2)) else size
                        if rn != 31 and rn in st:
                            apc, page = st[rn]
                            tgt = page + (((w >> 10) & 0xFFF) << scale)
                            adrp_sites[apc].add((pc, tgt, f"MEM_s{scale}"))
                        is_load = (opc & 1) == 1 or (opc == 2 and v == 0)
                        if not v and is_load and rt != 31:
                            st.pop(rt, None)
                    elif (w & 0xFC000000) == 0x94000000 or (w & 0xFFFFFC1F) == 0xD63F0000:
                        for r in range(19):
                            st.pop(r, None)
                    elif (w & 0xFFE0FFE0) == 0xAA0003E0:
                        rd, rm = w & 0x1F, (w >> 16) & 0x1F
                        if rm in st:
                            st[rd] = st[rm]
                        elif rd != 31:
                            st.pop(rd, None)
                    elif w in (0xD65F03C0, 0xD65F0FFF, 0xD65F0BFF):
                        nxt = []
                    elif (w & 0xFFFFFC1F) == 0xD61F0000:
                        if pc in jump_tgts:
                            nxt = [(t - fva) // 4 for t in jump_tgts[pc] if fva <= t < fend]
                        else:
                            nxt = []
                    elif (w & 0xFC000000) == 0x14000000:
                        imm = w & 0x03FFFFFF
                        if imm & (1 << 25):
                            imm -= 1 << 26
                        t = idx + imm
                        nxt = [t] if 0 <= t < n else []
                    elif (w & 0xFF000010) == 0x54000000 or (w & 0x7E000000) == 0x34000000:
                        imm = (w >> 5) & 0x7FFFF
                        if imm & (1 << 18):
                            imm -= 1 << 19
                        t = idx + imm
                        if 0 <= t < n:
                            nxt.append(t)
                    elif (w & 0x7E000000) == 0x36000000:
                        imm = (w >> 5) & 0x3FFF
                        if imm & (1 << 13):
                            imm -= 1 << 14
                        t = idx + imm
                        if 0 <= t < n:
                            nxt.append(t)
                    else:
                        if (w & 0x1F000000) != 0x1F000000 and (w & 0x3A000000) != 0x28000000:
                            rd = w & 0x1F
                            if rd != 31:
                                st.pop(rd, None)
                        elif (w & 0x3A000000) == 0x28000000 and ((w >> 22) & 1):
                            st.pop(w & 0x1F, None)
                            st.pop((w >> 10) & 0x1F, None)

                if len(nxt) == 1 and nxt[0] == idx + 1 and idx + 1 < n and in_state[idx + 1] is None:
                    in_state[idx + 1] = dict(st)
                    idx += 1
                    continue
                for t in nxt:
                    if 0 <= t < n:
                        if in_state[t] is None:
                            in_state[t] = dict(st)
                            work.append(t)
                        else:
                            ch = False
                            for k, val in st.items():
                                if k not in in_state[t]:
                                    in_state[t][k] = val
                                    ch = True
                            if ch:
                                work.append(t)
                break

    raw_adrp_count = sum(1 for w in insns if (w & 0x9F000000) == 0x90000000)
    assert len(adrp_sites) == raw_adrp_count, (
        f"ADRP coverage incomplete: {len(adrp_sites)}/{raw_adrp_count}"
    )

    return {
        "insns": insns,
        "sorted_fns": sorted_fns,
        "jump_tgts": jump_tgts,
        "all_vts": all_vts,
        "fn_to_cls": fn_to_cls,
        "audio_fns": audio_fns,
        "mba_fns": mba_fns,
        "mikey_fns": mikey_fns,
        "micbias_fns": micbias_fns,
        "mikeybus_family_fns": mikeybus_family_fns,
        "adrp_sites": adrp_sites,
    }

def trace_fn_typed_regs(insns, sorted_fns, jump_tgts, fva, init_x0_type):
    s_i = (fva - K15_TXEC_VA) // 4
    fend = sorted_fns[sorted_fns.index(fva) + 1]
    e_i = (fend - K15_TXEC_VA) // 4
    n = e_i - s_i
    in_regs = [None] * n
    in_sp = [None] * n
    in_regs[0] = {r: (init_x0_type if r == 0 else "OTHER") for r in range(31)}
    in_sp[0] = {}
    work = [0]

    def join_dict(d1, d2):
        out = dict(d1)
        ch = False
        for k, v in d2.items():
            ov = out.get(k, "OTHER")
            if ov == v:
                continue
            nv = v if ov == "OTHER" else (ov if v == "OTHER" else "MIXED")
            if nv != out.get(k):
                out[k] = nv
                ch = True
        return out, ch

    while work:
        idx = work.pop(0)
        regs = dict(in_regs[idx])
        sp = dict(in_sp[idx])
        while idx < n:
            pc = fva + idx * 4
            w = insns[s_i + idx]
            nxt = [idx + 1]
            if w in (0xD65F03C0, 0xD65F0FFF, 0xD65F0BFF):
                nxt = []
            elif (w & 0xFFFFFC1F) == 0xD61F0000:
                if pc in jump_tgts:
                    nxt = [(t - fva) // 4 for t in jump_tgts[pc] if fva <= t < fend]
                else:
                    nxt = []
            elif (w & 0xFC000000) == 0x14000000:
                imm = w & 0x03FFFFFF
                if imm & (1 << 25):
                    imm -= 1 << 26
                t = idx + imm
                nxt = [t] if 0 <= t < n else []
            elif (w & 0xFF000010) == 0x54000000 or (w & 0x7E000000) == 0x34000000:
                imm = (w >> 5) & 0x7FFFF
                if imm & (1 << 18):
                    imm -= 1 << 19
                t = idx + imm
                if 0 <= t < n:
                    nxt.append(t)
            elif (w & 0x7E000000) == 0x36000000:
                imm = (w >> 5) & 0x3FFF
                if imm & (1 << 13):
                    imm -= 1 << 14
                t = idx + imm
                if 0 <= t < n:
                    nxt.append(t)
            elif (w & 0xFC000000) == 0x94000000 or (w & 0xFFFFFC1F) == 0xD63F0000:
                for r in range(19):
                    regs[r] = "OTHER"
            elif (w & 0xFFE0FFE0) == 0xAA0003E0:
                rd, rm = w & 0x1F, (w >> 16) & 0x1F
                if rd != 31:
                    regs[rd] = regs.get(rm, "OTHER") if rm != 31 else "OTHER"
            elif (w & 0x9FE00000) == 0x9BA00000:
                rd, ra = w & 0x1F, (w >> 10) & 0x1F
                if rd != 31:
                    regs[rd] = regs.get(ra, "OTHER") if ra != 31 else "OTHER"
            elif (w & 0x7F200000) == 0x0B000000 or (w & 0x7F200000) == 0x0B200000:
                rd, rn, rm = w & 0x1F, (w >> 5) & 0x1F, (w >> 16) & 0x1F
                if ((w >> 31) & 1) and rd != 31:
                    trn = regs.get(rn, "OTHER") if rn != 31 else "OTHER"
                    trm = regs.get(rm, "OTHER") if rm != 31 else "OTHER"
                    regs[rd] = trn if trn == "AUDIO" else (trm if trm == "AUDIO" else "OTHER")
                elif rd != 31:
                    regs[rd] = "OTHER"
            elif (w & 0x3B000000) == 0x39000000:
                rn, rt = (w >> 5) & 0x1F, w & 0x1F
                size, v, opc = (w >> 30) & 3, (w >> 26) & 1, (w >> 22) & 3
                scale = 4 if (v == 1 and (opc & 2)) else size
                imm = ((w >> 10) & 0xFFF) << scale
                is_load = (opc & 1) == 1 or (opc == 2 and v == 0)
                if rn == 31 and not v and size == 3:
                    if is_load:
                        regs[rt] = sp.get(imm, "OTHER")
                    else:
                        sp[imm] = regs.get(rt, "OTHER")
                elif not v and is_load and rt != 31:
                    btype = regs.get(rn, "OTHER") if rn != 31 else "OTHER"
                    if size == 3 and (
                        (btype == "MIKEY" and imm == 0x1B0)
                        or (btype == "MIKEYBUS" and imm == 0x4E8)
                        or (btype == "MICBIAS" and imm == 0x28)
                    ):
                        regs[rt] = "AUDIO"
                    else:
                        regs[rt] = "OTHER"
            elif (w & 0x3A000000) == 0x28000000:
                opc, v, L = (w >> 30) & 3, (w >> 26) & 1, (w >> 22) & 1
                imm7 = (w >> 15) & 0x7F
                if imm7 & 0x40:
                    imm7 -= 0x80
                scale = (2 + (opc >> 1)) if v == 0 else (2 + opc)
                imm = imm7 << scale
                rn, rt1, rt2 = (w >> 5) & 0x1F, w & 0x1F, (w >> 10) & 0x1F
                if rn == 31 and not v and scale == 3 and ((w >> 23) & 3) == 2:
                    if L:
                        regs[rt1] = sp.get(imm, "OTHER")
                        regs[rt2] = sp.get(imm + 8, "OTHER")
                    else:
                        sp[imm] = regs.get(rt1, "OTHER")
                        sp[imm + 8] = regs.get(rt2, "OTHER")
                elif L and not v:
                    if rt1 != 31:
                        regs[rt1] = "OTHER"
                    if rt2 != 31:
                        regs[rt2] = "OTHER"
            elif (w & 0x1F000000) != 0x1F000000:
                rd = w & 0x1F
                if rd != 31:
                    regs[rd] = "OTHER"

            if len(nxt) == 1 and nxt[0] == idx + 1 and idx + 1 < n and in_regs[idx + 1] is None:
                in_regs[idx + 1] = dict(regs)
                in_sp[idx + 1] = dict(sp)
                idx += 1
                continue
            for t in nxt:
                if 0 <= t < n:
                    if in_regs[t] is None:
                        in_regs[t] = dict(regs)
                        in_sp[t] = dict(sp)
                        work.append(t)
                    else:
                        nr, cr = join_dict(in_regs[t], regs)
                        ns, cs = join_dict(in_sp[t], sp)
                        if cr or cs:
                            in_regs[t] = nr
                            in_sp[t] = ns
                            work.append(t)
            break
    return in_regs

def map_ea_slot(s15):
    if s15 < 180:
        return s15
    if s15 <= 330:
        return s15 + 3
    if s15 in (331, 332):
        return 180 + (s15 - 331)
    return s15 + 1

def map_mikey_slot(s15):
    return s15 if s15 < 205 else s15 + 2

def patch_ldr_voff(w, new_slot):
    assert (w & 0xFFC00000) == 0xF9400000, f"Unexpected LDR encoding: {hex(w)}"
    return (w & ~(0xFFF << 10)) | ((new_slot & 0xFFF) << 10)

def patch_mem_or_add_delta(w, delta):
    if (w & 0x3B000000) == 0x39000000:
        size, v, opc = (w >> 30) & 3, (w >> 26) & 1, (w >> 22) & 3
        scale = 4 if (v == 1 and (opc & 2)) else size
        assert (delta % (1 << scale)) == 0, f"Unaligned delta {delta} for scale {scale}"
        imm12 = ((w >> 10) & 0xFFF) + (delta >> scale)
        assert 0 <= imm12 < 0x1000, f"imm12 overflow: {imm12}"
        return (w & ~(0xFFF << 10)) | (imm12 << 10)
    elif (w & 0xFF800000) == 0x91000000:
        sh = (w >> 22) & 1
        assert sh == 0, "Unexpected shifted ADD"
        imm12 = ((w >> 10) & 0xFFF) + delta
        assert 0 <= imm12 < 0x1000, f"ADD imm12 overflow: {imm12}"
        return (w & ~(0xFFF << 10)) | (imm12 << 10)
    raise ValueError(f"Unsupported insn for offset delta: {hex(w)}")

def find_prelink_array_end(raw_pl):
    key_pat = b"<key>_PrelinkInfoDictionary</key><array>"
    start = raw_pl.find(key_pat)
    if start == -1:
        raise RuntimeError("Could not locate _PrelinkInfoDictionary <array>")
    pos = start + len(key_pat)
    depth = 1
    for m in re.finditer(rb"<(/?)array(?:\s[^>]*)?(/?)>", raw_pl[pos:]):
        is_close = m.group(1) == b"/"
        is_self = m.group(2) == b"/"
        if is_self:
            continue
        depth += -1 if is_close else 1
        if depth == 0:
            return pos + m.start()
    raise RuntimeError("Unclosed _PrelinkInfoDictionary <array>")

def find_prelink_dict_span(raw_pl, bundle_id_bytes):
    key_pat = b"<key>_PrelinkInfoDictionary</key><array>"
    arr_start = raw_pl.find(key_pat)
    if arr_start == -1:
        raise RuntimeError("Could not locate _PrelinkInfoDictionary <array>")
    pos = arr_start + len(key_pat)
    while pos < len(raw_pl):
        if raw_pl[pos : pos + 8] == b"</array>":
            break
        if raw_pl[pos : pos + 6] != b"<dict>":
            raise RuntimeError(f"Unexpected XML token at {pos}: {raw_pl[pos:pos+40]!r}")
        depth = 1
        scan = pos + 6
        while depth > 0:
            nxt_o = raw_pl.find(b"<dict>", scan)
            nxt_c = raw_pl.find(b"</dict>", scan)
            if nxt_c == -1:
                raise RuntimeError("Unclosed <dict> in _PrelinkInfoDictionary")
            if nxt_o != -1 and nxt_o < nxt_c:
                depth += 1
                scan = nxt_o + 6
            else:
                depth -= 1
                scan = nxt_c + 7
        blob = raw_pl[pos:scan]
        if f"<string>{bundle_id_bytes.decode()}</string>".encode() in blob:
            return pos, scan
        pos = scan
    raise RuntimeError(f"Bundle {bundle_id_bytes!r} not found in _PrelinkInfoDictionary")

def preserve_referenced_xml_ids(before_bytes, removed_bytes, after_bytes):
    out_before = before_bytes
    out_after = after_bytes
    for m in re.finditer(rb'<([a-zA-Z0-9_]+)\s+ID="([^"]+)"([^>]*)>(.*?)</\1>', removed_bytes, flags=re.DOTALL):
        tag, id_val, extra_attrs, body = m.group(1), m.group(2), m.group(3), m.group(4)
        idref_pat = re.compile(
            rb"<" + re.escape(tag) + rb'\s+IDREF="' + re.escape(id_val) + rb'"\s*(?:/>|>\s*</' + re.escape(tag) + rb">)"
        )
        full_def = b"<" + tag + b' ID="' + id_val + b'"' + extra_attrs + b">" + body + b"</" + tag + b">"
        if idref_pat.search(out_before):
            out_before = idref_pat.sub(full_def, out_before, count=1)
        elif idref_pat.search(out_after):
            out_after = idref_pat.sub(full_def, out_after, count=1)
    return out_before, out_after

def get_embedded_kext_segs(data, outer_segs, load_addr):
    off = va_to_off(outer_segs, load_addr)
    if off is None:
        raise RuntimeError(f"Unmapped embedded kext load_addr {hex(load_addr)}")
    magic = struct.unpack_from("<I", data, off)[0]
    if magic != 0xFEEDFACF:
        raise RuntimeError(f"Bad embedded Mach-O magic at {hex(load_addr)}: {hex(magic)}")
    ncmds = struct.unpack_from("<I", data, off + 16)[0]
    c = off + 32
    res = {}
    for _ in range(ncmds):
        cmd, csz = struct.unpack_from("<2I", data, c)
        if cmd == 0x19:
            sn = data[c + 8 : c + 24].split(b"\x00", 1)[0].decode("latin1")
            vm, vsz, fo, fsz = struct.unpack_from("<4Q", data, c + 24)
            res[sn] = (vm, vsz, fo, fsz)
        c += csz
    return res

def relink_cs42l71(kc15_path, kc16_path, out_path, symmap_path, mode="inplace", slot_bundle="com.apple.driver.AppleANXDPTX"):
    with open(kc15_path, "rb") as f:
        kc15 = f.read()
    with open(kc16_path, "rb") as f:
        kc16 = bytearray(f.read())
    with open(symmap_path, "r") as f:
        smap = json.load(f)

    info15 = parse_macho(kc15)
    info16 = parse_macho(kc16)
    pl15, _, _, _ = load_prelink_info(kc15, info15["segs"])
    pl16, raw_pl16, prelink_seg16, prelink_sec16 = load_prelink_info(kc16, info16["segs"])

    existing_segs = {s["segname"] for s in info16["segs"]}
    if "__CS42L71_TEXT" in existing_segs:
        raise RuntimeError("Input kernelcache already contains __CS42L71_TEXT")
    if any(it.get("CFBundleIdentifier") == "com.apple.driver.AppleCS42L71Audio" for it in pl16["_PrelinkInfoDictionary"]):
        raise RuntimeError("Input kernelcache __PRELINK_INFO already contains com.apple.driver.AppleCS42L71Audio")

    syms16, addr16 = load_symtab(kc16, info16["symtab"])
    syms15, addr15 = load_symtab(kc15, info15["symtab"])

    if mode == "newseg":
        first_sect_off = min(
            sec["offset"] for s in info16["segs"] for sec in s["sects"] if sec["offset"] > 0
        )
        need_lc_bytes = 4 * (72 + 80 * 5)
        if first_sect_off - info16["hdr_end"] < need_lc_bytes:
            raise RuntimeError("Not enough Mach-O header space for new LC_SEGMENT_64 commands")

    got16_targets = []
    for entry in smap["got_entries"]:
        if entry["kind"] == "xnu":
            sym = entry["symbol"]
            if sym not in syms16:
                raise RuntimeError(f"Missing XNU symbol in kc16: {sym}")
            got16_targets.append(syms16[sym])
        else:
            t16 = int(entry["kc16_va"], 16)
            if va_to_off(info16["segs"], t16) is None:
                raise RuntimeError(f"Invalid interkext target VA {hex(t16)} for GOT[{entry['index']}]")
            got16_targets.append(t16)
    assert len(got16_targets) == 103

    vt_spi16 = find_kext_vtable(kc16, info16["segs"], pl16, "com.apple.driver.AppleEmbeddedAudio", 362)[0]
    vt_htd16 = find_kext_vtable(kc16, info16["segs"], pl16, "com.apple.driver.AppleCSEmbeddedAudio", 285)[0]
    vt_armfn16 = find_kext_vtable(kc16, info16["segs"], pl16, "com.apple.driver.AppleARMPlatform", 17)[0]
    vt_mbus16 = find_kext_vtable(kc16, info16["segs"], pl16, "com.apple.iokit.IOMikeyBusFamily", 303)[0]
    vt_iis16 = find_kext_vtable(kc16, info16["segs"], pl16, "com.apple.iokit.AppleARMIISAudio", 252)[0]
    osmc_vtable16 = syms16["__ZTV11OSMetaClass"] + 16
    assert vt_spi16[0] == int(smap["superclass_vtable_targets_kc16"]["AppleCSSPIv2Audio"], 16)
    assert vt_htd16[0] == int(smap["superclass_vtable_targets_kc16"]["AppleCSHTDCodecMikey"], 16)
    assert vt_armfn16[0] == int(smap["superclass_vtable_targets_kc16"]["AppleARMFunction"], 16)
    assert vt_mbus16[0] == int(smap["superclass_vtable_targets_kc16"]["IOMikeyBusController"], 16)
    assert vt_iis16[0] == int(smap["superclass_vtable_targets_kc16"]["AppleARMIISAudioDevice"], 16)

    cs42_pl = next(
        it for it in pl15["_PrelinkInfoDictionary"] if it.get("CFBundleIdentifier") == "com.apple.driver.AppleCS42L71Audio"
    )
    kc16_ea_kalloc = find_kext_kalloc_sigs(kc16, info16["segs"], pl16, "com.apple.driver.AppleEmbeddedAudio")
    kc16_iis_kalloc = find_kext_kalloc_sigs(kc16, info16["segs"], pl16, "com.apple.iokit.AppleARMIISAudio")
    kc15_kalloc_self = find_kext_kalloc_sigs(kc15, info15["segs"], pl15, "com.apple.driver.AppleCS42L71Audio")

    _, spi16_sig = kc16_ea_kalloc["site.AppleCSSPIv2Audio"]
    _, iis16_sig = kc16_iis_kalloc["site.AppleARMIISAudioDevice"]
    _, old_audio_sig = kc15_kalloc_self["site.AppleCS42L71Audio"]
    _, old_mba_sig = kc15_kalloc_self["site.AppleCSCodecMikeyBusAudio"]
    assert len(spi16_sig) == 0x340 // 8 == 104
    assert len(iis16_sig) == 0x240 // 8 == 72
    assert len(old_audio_sig) == 0xC40 // 8 == 392
    assert len(old_mba_sig) == 0x288 // 8 == 81

    new_audio_sig = spi16_sig + old_audio_sig[103:]
    new_mba_sig = iis16_sig + old_mba_sig[71:]
    assert len(new_audio_sig) == 0xC48 // 8 == 393
    assert len(new_mba_sig) == 0x290 // 8 == 82

    buf_text = bytearray(kc15[va_to_off(info15["segs"], K15_TEXT_VA) : va_to_off(info15["segs"], K15_TEXT_VA) + K15_TEXT_SZ])
    while len(buf_text) % 16 != 0:
        buf_text.append(0)
    new_audio_sig_rel = len(buf_text)
    buf_text.extend(new_audio_sig.encode("latin1") + b"\x00")
    new_mba_sig_rel = len(buf_text)
    buf_text.extend(new_mba_sig.encode("latin1") + b"\x00")
    while len(buf_text) % 16 != 0:
        buf_text.append(0)
    new_text_raw_sz = len(buf_text)

    EXT_AUDIO_VT_OFF = K15_DCST_SZ
    EXT_MIKEY_VT_OFF = EXT_AUDIO_VT_OFF + (2 + 362) * 8
    new_dcst_raw_sz = EXT_MIKEY_VT_OFF + (2 + 285) * 8

    if mode == "inplace":
        slot_pl = next(
            (it for it in pl16["_PrelinkInfoDictionary"] if it.get("CFBundleIdentifier") == slot_bundle), None
        )
        if slot_pl is None:
            raise RuntimeError(f"Slot bundle {slot_bundle!r} not found in __PRELINK_INFO")
        slot_segs = get_embedded_kext_segs(kc16, info16["segs"], slot_pl["_PrelinkExecutableLoadAddr"])
        new_text_va, slot_text_sz, _, _ = slot_segs["__TEXT"]
        new_txec_va, slot_txec_sz, _, _ = slot_segs["__TEXT_EXEC"]
        new_dcst_va, slot_dcst_sz, _, _ = slot_segs["__DATA_CONST"]
        new_data_va, slot_data_sz, _, _ = slot_segs["__DATA"]
        assert new_text_raw_sz <= slot_text_sz, f"__TEXT overflow: {hex(new_text_raw_sz)} > {hex(slot_text_sz)}"
        assert K15_TXEC_SZ <= slot_txec_sz, f"__TEXT_EXEC overflow: {hex(K15_TXEC_SZ)} > {hex(slot_txec_sz)}"
        assert new_dcst_raw_sz <= slot_dcst_sz, f"__DATA_CONST overflow: {hex(new_dcst_raw_sz)} > {hex(slot_dcst_sz)}"
        assert K15_DATA_SZ <= slot_data_sz, f"__DATA overflow: {hex(K15_DATA_SZ)} > {hex(slot_data_sz)}"

        new_text_foff = va_to_off(info16["segs"], new_text_va)
        new_txec_foff = va_to_off(info16["segs"], new_txec_va)
        new_dcst_foff = va_to_off(info16["segs"], new_dcst_va)
        new_data_foff = va_to_off(info16["segs"], new_data_va)
        new_text_sz, new_txec_sz, new_dcst_sz, new_data_sz = slot_text_sz, slot_txec_sz, slot_dcst_sz, slot_data_sz
        emb_text_keep_sz = slot_text_sz
    else:
        if prelink_seg16["fileoff"] + prelink_seg16["filesize"] != len(kc16):
            raise RuntimeError("__PRELINK_INFO is not at EOF; cannot safely push without shifting downstream segments.")
        base_vm = prelink_seg16["vmaddr"]
        base_fo = prelink_seg16["fileoff"]
        new_text_sz = align_up(new_text_raw_sz)
        new_txec_sz = align_up(K15_TXEC_SZ)
        new_dcst_sz = align_up(new_dcst_raw_sz)
        new_data_sz = align_up(K15_DATA_SZ)
        new_text_va = base_vm
        new_txec_va = new_text_va + new_text_sz
        new_dcst_va = new_txec_va + new_txec_sz
        new_data_va = new_dcst_va + new_dcst_sz
        new_text_foff = base_fo
        new_txec_foff = new_text_foff + new_text_sz
        new_dcst_foff = new_txec_foff + new_txec_sz
        new_data_foff = new_dcst_foff + new_dcst_sz
        new_pl_va = new_data_va + new_data_sz
        new_pl_foff = new_data_foff + new_data_sz
        emb_text_keep_sz = K15_TEXT_SZ

    buf_text.extend(b"\x00" * (new_text_sz - len(buf_text)))

    cs42_new_pl = dict(cs42_pl)
    cs42_new_pl["_PrelinkExecutableLoadAddr"] = new_text_va
    cs42_new_pl["_PrelinkExecutableSourceAddr"] = new_text_va
    cs42_new_pl["_PrelinkExecutableSize"] = emb_text_keep_sz
    cs42_new_pl["_PrelinkKmodInfo"] = new_data_va
    new_item_xml = serialize_iocf_value(cs42_new_pl).encode("utf-8")

    def map_internal_va(va15):
        if K15_TEXT_VA <= va15 < K15_TEXT_VA + K15_TEXT_SZ:
            return new_text_va + (va15 - K15_TEXT_VA)
        if K15_TXEC_VA <= va15 < K15_TXEC_VA + K15_TXEC_SZ:
            return new_txec_va + (va15 - K15_TXEC_VA)
        if K15_DCST_VA <= va15 < K15_DCST_VA + K15_DCST_SZ:
            return new_dcst_va + (va15 - K15_DCST_VA)
        if K15_DATA_VA <= va15 < K15_DATA_VA + K15_DATA_SZ:
            return new_data_va + (va15 - K15_DATA_VA)
        return None

    buf_txec = bytearray(kc15[va_to_off(info15["segs"], K15_TXEC_VA) : va_to_off(info15["segs"], K15_TXEC_VA) + K15_TXEC_SZ])
    buf_txec.extend(b"\x00" * (new_txec_sz - len(buf_txec)))

    buf_dcst = bytearray(kc15[va_to_off(info15["segs"], K15_DCST_VA) : va_to_off(info15["segs"], K15_DCST_VA) + K15_DCST_SZ])
    buf_dcst.extend(b"\x00" * (new_dcst_sz - len(buf_dcst)))

    buf_data = bytearray(kc15[va_to_off(info15["segs"], K15_DATA_VA) : va_to_off(info15["segs"], K15_DATA_VA) + K15_DATA_FILE_SZ])
    buf_data.extend(b"\x00" * (new_data_sz - len(buf_data)))

    analysis = analyze_kc15_cs42l71(kc15, info15, pl15)
    insns = analysis["insns"]
    sorted_fns = analysis["sorted_fns"]

    new_audio_vt_va = new_dcst_va + EXT_AUDIO_VT_OFF + 16
    new_mikey_vt_va = new_dcst_va + EXT_MIKEY_VT_OFF + 16

    movz_patches = 0
    for i, w in enumerate(insns):
        if (w & 0x7F800000) == 0x52800000:
            hw = (w >> 21) & 3
            imm16 = (w >> 5) & 0xFFFF
            val = imm16 << (hw * 16)
            if val == 0xC40:
                insns[i] = encode_movz_w(w & 0x1F, 0xC48)
                movz_patches += 1
            elif val == 0x288:
                insns[i] = encode_movz_w(w & 0x1F, 0x290)
                movz_patches += 1

    orig_insns = list(insns)
    jump_tgts = analysis["jump_tgts"]
    audio_struct_patches = 0
    mba_struct_patches = 0

    typed_groups = [
        (analysis["audio_fns"], "AUDIO"),
        (analysis["mba_fns"], "MBA"),
        (analysis["mikey_fns"], "MIKEY"),
        (analysis["mikeybus_family_fns"], "MIKEYBUS"),
        (analysis["micbias_fns"], "MICBIAS"),
    ]
    for fn_list, init_t in typed_groups:
        for fva in fn_list:
            in_regs = trace_fn_typed_regs(orig_insns, sorted_fns, jump_tgts, fva, init_t)
            s_i = (fva - K15_TXEC_VA) // 4
            for idx, regs in enumerate(in_regs):
                if regs is None:
                    continue
                w = orig_insns[s_i + idx]
                rn = (w >> 5) & 0x1F
                if rn == 31:
                    continue
                rtype = regs.get(rn, "OTHER")
                if rtype not in ("AUDIO", "MBA"):
                    continue
                imm = None
                if (w & 0x3B000000) == 0x39000000:
                    size, v, opc = (w >> 30) & 3, (w >> 26) & 1, (w >> 22) & 3
                    scale = 4 if (v == 1 and (opc & 2)) else size
                    imm = ((w >> 10) & 0xFFF) << scale
                elif (w & 0xFF800000) == 0x91000000:
                    sh, imm12 = (w >> 22) & 1, (w >> 10) & 0xFFF
                    imm = imm12 << (12 if sh else 0)
                if imm is None:
                    continue
                if rtype == "AUDIO" and (imm in (0x198, 0x1C0) or imm >= 0x338):
                    insns[s_i + idx] = patch_mem_or_add_delta(insns[s_i + idx], 8)
                    audio_struct_patches += 1
                elif rtype == "MBA" and (imm == 0x1B8 or imm >= 0x238):
                    insns[s_i + idx] = patch_mem_or_add_delta(insns[s_i + idx], 8)
                    mba_struct_patches += 1

    vcall_patches = {"audio": 0, "mikey": 0, "skipped_other_obj": 0}
    GOT_APPLE_EMBEDDED_AUDIO_VTABLE = K15_GOT_VA + 83 * 8
    GOT_APPLE_CS_MIKEY_VTABLE = K15_GOT_VA + 84 * 8

    for fva in sorted(
        set(analysis["audio_fns"] + analysis["mikey_fns"] + analysis["mikeybus_family_fns"] + analysis["micbias_fns"])
    ):
        cls = analysis["fn_to_cls"].get(fva, set())
        if cls == {"AppleCS42L71Audio"}:
            init_t = "AUDIO"
        elif cls == {"AppleCS42L71Mikey"}:
            init_t = "MIKEY"
        elif cls == {"AppleCS42L71MicBiasFunction"}:
            init_t = "MICBIAS"
        else:
            init_t = "MIKEYBUS"
        in_regs = trace_fn_typed_regs(orig_insns, sorted_fns, jump_tgts, fva, init_t)
        s_i = (fva - K15_TXEC_VA) // 4
        is_audio_fn = init_t == "AUDIO"
        is_mikey_fn = init_t == "MIKEY"
        is_mbus_fn = init_t == "MIKEYBUS"

        for idx in range(len(in_regs)):
            if in_regs[idx] is None:
                continue
            w = orig_insns[s_i + idx]
            if (w & 0xFFFFFC1F) not in (0xD63F0000, 0xD61F0000):
                continue
            rm = (w >> 5) & 0x1F
            if rm == 16:
                continue
            ldr_idx = None
            for back in range(1, min(10, idx + 1)):
                p = orig_insns[s_i + idx - back]
                if (p & 0xFFC00000) == 0xF9400000 and (p & 0x1F) == rm:
                    ldr_idx = idx - back
                    break
            if ldr_idx is None:
                continue
            ldr_w = orig_insns[s_i + ldr_idx]
            rn = (ldr_w >> 5) & 0x1F
            slot = (ldr_w >> 10) & 0xFFF
            if slot < 180:
                continue

            target_kind = None
            for b2 in range(1, min(12, ldr_idx + 1)):
                p2 = orig_insns[s_i + ldr_idx - b2]
                if (p2 & 0xFFC00000) == 0xF9400000 and (p2 & 0x1F) == rn:
                    obj_reg = (p2 >> 5) & 0x1F
                    o2 = ((p2 >> 10) & 0xFFF) * 8
                    if o2 != 0:
                        if ldr_idx - b2 - 1 >= 0:
                            p3 = orig_insns[s_i + ldr_idx - b2 - 1]
                            if (p3 & 0x9F000000) == 0x90000000 and (p3 & 0x1F) == obj_reg:
                                imm = (((p3 >> 5) & 0x7FFFF) << 2) | ((p3 >> 29) & 3)
                                if imm & (1 << 20):
                                    imm -= 1 << 21
                                pc3 = fva + (ldr_idx - b2 - 1) * 4
                                gaddr = (pc3 & ~0xFFF) + (imm << 12) + o2
                                if gaddr == GOT_APPLE_EMBEDDED_AUDIO_VTABLE:
                                    target_kind = "audio"
                                elif gaddr == GOT_APPLE_CS_MIKEY_VTABLE:
                                    target_kind = "mikey"
                                else:
                                    target_kind = "other"
                        break
                    else:
                        st = in_regs[ldr_idx - b2].get(obj_reg, "OTHER")
                        if st == "AUDIO":
                            target_kind = "audio"
                        elif st == "MIKEY":
                            target_kind = "mikey"
                        elif st in ("MIKEYBUS", "MICBIAS"):
                            target_kind = "other"
                        else:
                            for b3 in range(1, min(14, ldr_idx - b2 + 1)):
                                p3 = orig_insns[s_i + ldr_idx - b2 - b3]
                                if (p3 & 0xFFC00000) == 0xF9400000 and (p3 & 0x1F) == obj_reg:
                                    o3 = ((p3 >> 10) & 0xFFF) * 8
                                    r3 = (p3 >> 5) & 0x1F
                                    if is_audio_fn and o3 == 0xC08 and in_regs[ldr_idx - b2 - b3].get(r3) == "AUDIO":
                                        target_kind = "mikey"
                                    elif is_audio_fn and o3 == 0xC18 and in_regs[ldr_idx - b2 - b3].get(r3) == "AUDIO":
                                        target_kind = "other"
                                    elif is_mikey_fn and o3 == 0x1B0 and in_regs[ldr_idx - b2 - b3].get(r3) == "MIKEY":
                                        target_kind = "audio"
                                    elif is_mbus_fn and o3 in (0x4E8, 0x3D8) and in_regs[ldr_idx - b2 - b3].get(r3) == "MIKEYBUS":
                                        target_kind = "audio"
                                    break
                                if (p3 & 0xFC000000) == 0x94000000 and obj_reg == 0:
                                    target_kind = "other"
                                    break
                            if target_kind is None and is_mikey_fn and slot >= 337:
                                target_kind = "audio"
                        break

            if target_kind == "audio":
                new_slot = map_ea_slot(slot)
                if new_slot != slot:
                    insns[s_i + ldr_idx] = patch_ldr_voff(insns[s_i + ldr_idx], new_slot)
                    vcall_patches["audio"] += 1
            elif target_kind == "mikey" and slot >= 205:
                new_slot = map_mikey_slot(slot)
                insns[s_i + ldr_idx] = patch_ldr_voff(insns[s_i + ldr_idx], new_slot)
                vcall_patches["mikey"] += 1
            else:
                vcall_patches["skipped_other_obj"] += 1

    OLD_AUDIO_VT_VA = 0xFFFFFFF006FCBDF8
    OLD_MIKEY_VT_VA = 0xFFFFFFF006FCC9F0

    def map_adrp_target(tgt15):
        if tgt15 == OLD_AUDIO_VT_VA:
            return new_audio_vt_va
        if tgt15 == OLD_MIKEY_VT_VA:
            return new_mikey_vt_va
        nv = map_internal_va(tgt15)
        if nv is None:
            raise RuntimeError(f"ADRP target outside kext: {hex(tgt15)}")
        return nv

    adrp_patched = 0
    cons_patched = 0
    for apc, cons in analysis["adrp_sites"].items():
        if not cons:
            raise RuntimeError(f"Unconsumed ADRP at {hex(apc)}")
        new_tgts = []
        for cpc, tgt15, kind in cons:
            new_tgts.append((cpc, map_adrp_target(tgt15), kind))
        new_pages = {t & ~0xFFF for _, t, _ in new_tgts}
        assert len(new_pages) == 1, f"Split page after relink for ADRP {hex(apc)}: {new_pages}"
        new_page = next(iter(new_pages))

        a_idx = (apc - K15_TXEC_VA) // 4
        new_apc = new_txec_va + (apc - K15_TXEC_VA)
        rd = orig_insns[a_idx] & 0x1F
        insns[a_idx] = encode_adrp(new_apc, new_page, rd)
        adrp_patched += 1

        for cpc, ntgt, kind in new_tgts:
            c_idx = (cpc - K15_TXEC_VA) // 4
            cw = insns[c_idx]
            new_off12 = ntgt & 0xFFF
            if kind == "ADD":
                assert (cw & 0xFF800000) == 0x91000000 and ((cw >> 22) & 1) == 0
                insns[c_idx] = (cw & ~(0xFFF << 10)) | (new_off12 << 10)
            else:
                scale = int(kind.split("_s")[1])
                assert (new_off12 % (1 << scale)) == 0, f"Unaligned MEM page offset {hex(ntgt)} scale {scale}"
                insns[c_idx] = (cw & ~(0xFFF << 10)) | ((new_off12 >> scale) << 10)
            cons_patched += 1

    for i, w in enumerate(insns):
        struct.pack_into("<I", buf_txec, i * 4, w)

    n_stubs = K15_STUBS_SZ // 12
    for si in range(n_stubs):
        old_sva = K15_STUBS_VA + si * 12
        off_in_txec = K15_TEXT_ONLY_SZ + si * 12
        i0, i1, i2 = struct.unpack_from("<3I", buf_txec, off_in_txec)
        assert (i0 & 0x9F00001F) == 0x90000010 and (i1 & 0xFFC003FF) == 0xF9400210 and i2 == 0xD61F0200
        imm = (((i0 >> 5) & 0x7FFFF) << 2) | ((i0 >> 29) & 3)
        if imm & (1 << 20):
            imm -= 1 << 21
        old_got_ptr = (old_sva & ~0xFFF) + (imm << 12) + (((i1 >> 10) & 0xFFF) * 8)
        g_idx = (old_got_ptr - K15_GOT_VA) // 8
        assert 0 <= g_idx < K15_GOT_N
        new_sva = new_txec_va + off_in_txec
        new_got_ptr = new_dcst_va + g_idx * 8
        new_i0 = encode_adrp(new_sva, new_got_ptr & ~0xFFF, 16)
        new_i1 = 0xF9400210 | (((new_got_ptr & 0xFFF) >> 3) << 10)
        struct.pack_into("<3I", buf_txec, off_in_txec, new_i0, new_i1, 0xD61F0200)

    for i, t16 in enumerate(got16_targets):
        struct.pack_into("<Q", buf_dcst, i * 8, t16)

    for sec_va in [K15_MOD_INIT_VA, K15_MOD_INIT_VA + 8, K15_MOD_TERM_VA, K15_MOD_TERM_VA + 8]:
        off = sec_va - K15_DCST_VA
        val = struct.unpack_from("<Q", buf_dcst, off)[0]
        struct.pack_into("<Q", buf_dcst, off, map_internal_va(val))

    super_vt_map = {
        0xFFFFFFF006FCBDF8: vt_spi16[1],
        0xFFFFFFF006FCC9F0: vt_htd16[1],
        0xFFFFFFF006FCD378: vt_mbus16[1],
        0xFFFFFFF006FCDEA8: vt_armfn16[1],
        0xFFFFFFF006FCDFE0: vt_mbus16[1],
        0xFFFFFFF006FCEA88: vt_mbus16[1],
        0xFFFFFFF006FCF5B8: vt_mbus16[1],
        0xFFFFFFF006FD0060: vt_iis16[1],
    }

    def resolve_ext_slot(slot_idx, old_ptr, super_ptrs):
        if old_ptr in addr15:
            sym = addr15[old_ptr][0]
            if sym in syms16:
                return syms16[sym]
        return super_ptrs[slot_idx]

    for vt_va, ptrs15 in analysis["all_vts"]:
        if len(ptrs15) == 14:
            for s_i, p15 in enumerate(ptrs15):
                nv = map_internal_va(p15)
                if nv is None:
                    if p15 in addr15 and addr15[p15][0] in syms16:
                        nv = syms16[addr15[p15][0]]
                    else:
                        osmc_off = va_to_off(info16["segs"], osmc_vtable16)
                        nv = struct.unpack_from("<Q", kc16, osmc_off + s_i * 8)[0]
                struct.pack_into("<Q", buf_dcst, (vt_va - K15_DCST_VA) + s_i * 8, nv)
        elif vt_va in (OLD_AUDIO_VT_VA, OLD_MIKEY_VT_VA):
            super_ptrs = super_vt_map[vt_va]
            for s_i, p15 in enumerate(ptrs15):
                nv = map_internal_va(p15)
                if nv is None:
                    s16 = map_ea_slot(s_i) if vt_va == OLD_AUDIO_VT_VA else map_mikey_slot(s_i)
                    nv = resolve_ext_slot(s16, p15, super_ptrs)
                struct.pack_into("<Q", buf_dcst, (vt_va - K15_DCST_VA) + s_i * 8, nv)
        else:
            super_ptrs = super_vt_map[vt_va]
            for s_i, p15 in enumerate(ptrs15):
                nv = map_internal_va(p15)
                if nv is None:
                    nv = resolve_ext_slot(s_i, p15, super_ptrs)
                struct.pack_into("<Q", buf_dcst, (vt_va - K15_DCST_VA) + s_i * 8, nv)

    audio15_ptrs = next(p for va, p in analysis["all_vts"] if va == OLD_AUDIO_VT_VA)
    spi16_ptrs = vt_spi16[1]
    audio16_ptrs = [0] * 362
    for s15, p15 in enumerate(audio15_ptrs):
        if s15 in (331, 332):
            continue
        s16 = map_ea_slot(s15)
        nv = map_internal_va(p15)
        audio16_ptrs[s16] = nv if nv is not None else resolve_ext_slot(s16, p15, spi16_ptrs)
    for ins_s in (180, 181, 182):
        audio16_ptrs[ins_s] = spi16_ptrs[ins_s]
    assert all(x != 0 for x in audio16_ptrs)
    for idx_s, p16 in enumerate(audio16_ptrs):
        struct.pack_into("<Q", buf_dcst, EXT_AUDIO_VT_OFF + 16 + idx_s * 8, p16)

    mikey15_ptrs = next(p for va, p in analysis["all_vts"] if va == OLD_MIKEY_VT_VA)
    htd16_ptrs = vt_htd16[1]
    mikey16_ptrs = [0] * 285
    for s15, p15 in enumerate(mikey15_ptrs):
        s16 = map_mikey_slot(s15)
        nv = map_internal_va(p15)
        mikey16_ptrs[s16] = nv if nv is not None else resolve_ext_slot(s16, p15, htd16_ptrs)
    for ins_s in (205, 206):
        mikey16_ptrs[ins_s] = htd16_ptrs[ins_s]
    assert all(x != 0 for x in mikey16_ptrs)
    for idx_s, p16 in enumerate(mikey16_ptrs):
        struct.pack_into("<Q", buf_dcst, EXT_MIKEY_VT_OFF + 16 + idx_s * 8, p16)

    ext_gmetaclass_map = {
        0xFFFFFFF0079FCA20: int(smap["got_entries"][27]["kc16_va"], 16),
        0xFFFFFFF0079FCE50: int(smap["got_entries"][42]["kc16_va"], 16),
        0xFFFFFFF0079A6C10: int(smap["got_entries"][20]["kc16_va"], 16),
        0xFFFFFFF0079FD098: int(smap["got_entries"][46]["kc16_va"], 16),
        0xFFFFFFF0079FC398: int(smap["got_entries"][52]["kc16_va"], 16),
    }
    vt_off_ranges = [(va - K15_DCST_VA, va - K15_DCST_VA + len(p) * 8) for va, p in analysis["all_vts"]]
    for pos in range(K15_CONST_VA - K15_DCST_VA, (K15_CONST_VA - K15_DCST_VA) + K15_CONST_SZ, 8):
        if any(s <= pos < e for s, e in vt_off_ranges):
            continue
        val = struct.unpack_from("<Q", buf_dcst, pos)[0]
        if (val >> 36) == 0xFFFFFFF:
            nv = map_internal_va(val)
            if nv is None:
                if val not in ext_gmetaclass_map:
                    raise RuntimeError(f"Unmapped non-vtable pointer in __const +0x{pos:x}: {hex(val)}")
                nv = ext_gmetaclass_map[val]
            struct.pack_into("<Q", buf_dcst, pos, nv)

    kalloc_base = K15_KALLOC_VA - K15_DCST_VA
    for ki in range(K15_KALLOC_SZ // 64):
        off = kalloc_base + ki * 64
        site_va = struct.unpack_from("<Q", buf_dcst, off + 16)[0]
        new_site_va = map_internal_va(site_va)
        assert new_site_va is not None, f"Invalid __kalloc_type site ptr {hex(site_va)}"
        struct.pack_into("<Q", buf_dcst, off + 16, new_site_va)

        sig_va = struct.unpack_from("<Q", buf_dcst, off + 32)[0]
        q5 = struct.unpack_from("<Q", buf_dcst, off + 40)[0]
        sz = q5 >> 32
        fl = q5 & 0xFFFFFFFF
        if sz == 0xC40:
            struct.pack_into("<Q", buf_dcst, off + 32, new_text_va + new_audio_sig_rel)
            struct.pack_into("<Q", buf_dcst, off + 40, (0xC48 << 32) | fl)
        elif sz == 0x288:
            struct.pack_into("<Q", buf_dcst, off + 32, new_text_va + new_mba_sig_rel)
            struct.pack_into("<Q", buf_dcst, off + 40, (0x290 << 32) | fl)
        else:
            new_sig_va = map_internal_va(sig_va)
            assert new_sig_va is not None, f"Invalid __kalloc_type sig ptr {hex(sig_va)}"
            struct.pack_into("<Q", buf_dcst, off + 32, new_sig_va)

    old_addr, old_sz, old_hdr, old_start, old_stop = struct.unpack_from("<5Q", buf_data, 0x9C)
    assert old_addr == K15_TEXT_VA
    struct.pack_into(
        "<5Q",
        buf_data,
        0x9C,
        new_text_va,
        KEXT_KMOD_SIZE,
        0,
        map_internal_va(old_start),
        map_internal_va(old_stop) if old_stop else 0,
    )

    emb_ncmds = struct.unpack_from("<I", buf_text, 16)[0]
    coff = 32
    emb_seg_map = {
        "__TEXT": (new_text_va, emb_text_keep_sz, 0, emb_text_keep_sz),
        "__TEXT_EXEC": (new_txec_va, K15_TXEC_SZ, new_txec_foff - new_text_foff, K15_TXEC_SZ),
        "__DATA_CONST": (new_dcst_va, new_dcst_raw_sz, new_dcst_foff - new_text_foff, new_dcst_raw_sz),
        "__DATA": (new_data_va, K15_DATA_SZ, new_data_foff - new_text_foff, K15_DATA_FILE_SZ),
        "__LINKEDIT": (
            new_data_va + new_data_sz,
            0,
            (new_data_foff + new_data_sz - new_text_foff) if mode == "newseg" else len(kc16),
            0,
        ),
    }
    for _ in range(emb_ncmds):
        cmd, cmdsize = struct.unpack_from("<2I", buf_text, coff)
        if cmd == 0x19:
            sname = buf_text[coff + 8 : coff + 24].split(b"\x00", 1)[0].decode("latin1")
            if sname in emb_seg_map:
                svm, svmsz, sfoff, sfsz = emb_seg_map[sname]
                nsects = struct.unpack_from("<I", buf_text, coff + 64)[0]
                struct.pack_into("<4Q", buf_text, coff + 24, svm, svmsz, sfoff, sfsz)
                soff = coff + 72
                for _ in range(nsects):
                    s_addr, s_sz, s_off = struct.unpack_from("<2QI", buf_text, soff + 32)
                    n_addr = map_internal_va(s_addr)
                    if s_off == 0:
                        n_off = 0
                    elif sname == "__TEXT":
                        n_off = n_addr - new_text_va
                    elif sname == "__TEXT_EXEC":
                        n_off = (new_txec_foff - new_text_foff) + (n_addr - new_txec_va)
                    elif sname == "__DATA_CONST":
                        n_off = (new_dcst_foff - new_text_foff) + (n_addr - new_dcst_va)
                    else:
                        n_off = (new_data_foff - new_text_foff) + (n_addr - new_data_va)
                    struct.pack_into("<2QI", buf_text, soff + 32, n_addr, s_sz, n_off)
                    soff += 80
        coff += cmdsize

    lowest_vm = min(s["vmaddr"] for s in info16["segs"] if s["vmsize"] > 0)
    new_kaslr_offs = []
    for pos in range(0, new_dcst_raw_sz, 8):
        val = struct.unpack_from("<Q", buf_dcst, pos)[0]
        if (val >> 36) == 0xFFFFFFF:
            new_kaslr_offs.append((new_dcst_va + pos) - lowest_vm)
    new_kaslr_offs.append((new_data_va + 0xB4) - lowest_vm)
    new_kaslr_offs.append((new_data_va + 0xBC) - lowest_vm)

    if mode == "inplace":
        span_s, span_e = find_prelink_dict_span(raw_pl16, slot_bundle.encode("utf-8"))
        before_pl = raw_pl16[:span_s]
        removed_pl = raw_pl16[span_s:span_e]
        after_pl = raw_pl16[span_e:]
        before_pl, after_pl = preserve_referenced_xml_ids(before_pl, removed_pl, after_pl)
        updated_pl_xml = before_pl + new_item_xml + after_pl
    else:
        idx_arr_end = find_prelink_array_end(raw_pl16)
        updated_pl_xml = raw_pl16[:idx_arr_end] + new_item_xml + raw_pl16[idx_arr_end:]

    kaslr_marker = b"</array><key>_PrelinkLinkKASLROffsets</key><data>"
    k_idx = updated_pl_xml.find(kaslr_marker)
    if k_idx < 0:
        raise RuntimeError("Could not locate _PrelinkLinkKASLROffsets in __PRELINK_INFO")
    d_start = k_idx + len(kaslr_marker)
    d_end = updated_pl_xml.find(b"</data>", d_start)
    if d_end < 0:
        raise RuntimeError("Could not locate closing </data> for _PrelinkLinkKASLROffsets")

    kaslr_blob = base64.b64decode(updated_pl_xml[d_start:d_end])
    old_cnt = struct.unpack_from("<I", kaslr_blob, 0)[0]
    old_offs = struct.unpack_from(f"<{old_cnt}I", kaslr_blob, 4)

    removed_kaslr = 0
    if mode == "inplace":
        dc_lo, dc_hi = new_dcst_va - lowest_vm, (new_dcst_va + slot_dcst_sz) - lowest_vm
        da_lo, da_hi = new_data_va - lowest_vm, (new_data_va + slot_data_sz) - lowest_vm
        kept_offs = [o for o in old_offs if not ((dc_lo <= o < dc_hi) or (da_lo <= o < da_hi))]
        removed_kaslr = len(old_offs) - len(kept_offs)
    else:
        kept_offs = list(old_offs)

    merged_offs = sorted(set(kept_offs) | set(new_kaslr_offs))
    new_kaslr_blob = struct.pack("<I", len(merged_offs)) + struct.pack(f"<{len(merged_offs)}I", *merged_offs)
    new_kaslr_b64 = base64.b64encode(new_kaslr_blob)
    new_pl_xml = updated_pl_xml[:d_start] + new_kaslr_b64 + updated_pl_xml[d_end:]

    if mode == "inplace":
        sec_sz = prelink_sec16["size"]
        if len(new_pl_xml) + 1 > sec_sz:
            raise RuntimeError(f"__PRELINK_INFO overflow in inplace mode: {len(new_pl_xml)+1} > {sec_sz}")
        kc16[new_text_foff : new_text_foff + new_text_sz] = buf_text
        kc16[new_txec_foff : new_txec_foff + new_txec_sz] = buf_txec
        kc16[new_dcst_foff : new_dcst_foff + new_dcst_sz] = buf_dcst
        kc16[new_data_foff : new_data_foff + new_data_sz] = buf_data
        sec_off = prelink_sec16["offset"]
        kc16[sec_off : sec_off + sec_sz] = new_pl_xml + b"\x00" * (sec_sz - len(new_pl_xml))
    else:
        new_pl_sz = align_up(len(new_pl_xml) + 1)
        new_pl_padded = new_pl_xml + b"\x00" * (new_pl_sz - len(new_pl_xml))
        new_seg_cmds = [
            pack_lc_segment_64(
                "__CS42L71_TEXT",
                new_text_va,
                new_text_sz,
                new_text_foff,
                new_text_sz,
                1,
                1,
                [
                    {"sectname": "__text", "segname": "__CS42L71_TEXT", "addr": new_text_va, "size": new_text_raw_sz, "offset": new_text_foff, "align": 4}
                ],
            ),
            pack_lc_segment_64(
                "__CS42L71_TXEC",
                new_txec_va,
                new_txec_sz,
                new_txec_foff,
                new_txec_sz,
                5,
                5,
                [
                    {"sectname": "__text", "segname": "__CS42L71_TXEC", "addr": new_txec_va, "size": K15_TEXT_ONLY_SZ, "offset": new_txec_foff, "align": 2, "flags": 0x80000400},
                    {"sectname": "__stubs", "segname": "__CS42L71_TXEC", "addr": new_txec_va + K15_TEXT_ONLY_SZ, "size": K15_STUBS_SZ, "offset": new_txec_foff + K15_TEXT_ONLY_SZ, "align": 2, "flags": 0x80000408},
                ],
            ),
            pack_lc_segment_64(
                "__CS42L71_DCST",
                new_dcst_va,
                new_dcst_sz,
                new_dcst_foff,
                new_dcst_sz,
                1,
                1,
                [
                    {"sectname": "__got", "segname": "__CS42L71_DCST", "addr": new_dcst_va, "size": K15_GOT_N * 8, "offset": new_dcst_foff, "align": 3},
                    {"sectname": "__mod_init_func", "segname": "__CS42L71_DCST", "addr": new_dcst_va + (K15_MOD_INIT_VA - K15_DCST_VA), "size": 0x10, "offset": new_dcst_foff + (K15_MOD_INIT_VA - K15_DCST_VA), "align": 3, "flags": 0x9},
                    {"sectname": "__mod_term_func", "segname": "__CS42L71_DCST", "addr": new_dcst_va + (K15_MOD_TERM_VA - K15_DCST_VA), "size": 0x10, "offset": new_dcst_foff + (K15_MOD_TERM_VA - K15_DCST_VA), "align": 3, "flags": 0xA},
                    {"sectname": "__const", "segname": "__CS42L71_DCST", "addr": new_dcst_va + (K15_CONST_VA - K15_DCST_VA), "size": K15_CONST_SZ, "offset": new_dcst_foff + (K15_CONST_VA - K15_DCST_VA), "align": 3},
                    {"sectname": "__kalloc_type", "segname": "__CS42L71_DCST", "addr": new_dcst_va + (K15_KALLOC_VA - K15_DCST_VA), "size": K15_KALLOC_SZ, "offset": new_dcst_foff + (K15_KALLOC_VA - K15_DCST_VA), "align": 3},
                    {"sectname": "__const_ext", "segname": "__CS42L71_DCST", "addr": new_dcst_va + EXT_AUDIO_VT_OFF, "size": new_dcst_raw_sz - EXT_AUDIO_VT_OFF, "offset": new_dcst_foff + EXT_AUDIO_VT_OFF, "align": 3},
                ],
            ),
            pack_lc_segment_64(
                "__CS42L71_DATA",
                new_data_va,
                new_data_sz,
                new_data_foff,
                new_data_sz,
                3,
                3,
                [
                    {"sectname": "__data", "segname": "__CS42L71_DATA", "addr": new_data_va, "size": K15_DATA_FILE_SZ, "offset": new_data_foff, "align": 3},
                    {"sectname": "__common", "segname": "__CS42L71_DATA", "addr": new_data_va + K15_DATA_FILE_SZ, "size": 0x188, "offset": new_data_foff + K15_DATA_FILE_SZ, "align": 3},
                ],
            ),
        ]

        orig_cmds = []
        non_seg_cmds = []
        prelink_cmd_blob = None
        off = 32
        for _ in range(info16["ncmds"]):
            c, cs = struct.unpack_from("<2I", kc16, off)
            blob = bytearray(kc16[off : off + cs])
            if c == 0x19:
                sn = blob[8:24].split(b"\x00", 1)[0].decode("latin1")
                if sn in ("__PLK_LLVM_COV", "__PLK_LINKEDIT"):
                    struct.pack_into("<3Q", blob, 24, new_pl_va, 0, new_pl_foff)
                    if struct.unpack_from("<I", blob, 64)[0] > 0:
                        struct.pack_into("<2QI", blob, 72 + 32, new_pl_va, 0, new_pl_foff)
                    orig_cmds.append(bytes(blob))
                elif sn == "__PRELINK_INFO":
                    struct.pack_into("<4Q", blob, 24, new_pl_va, new_pl_sz, new_pl_foff, new_pl_sz)
                    struct.pack_into("<2QI", blob, 72 + 32, new_pl_va, new_pl_sz, new_pl_foff)
                    prelink_cmd_blob = bytes(blob)
                else:
                    orig_cmds.append(bytes(blob))
            else:
                non_seg_cmds.append(bytes(blob))
            off += cs

        assert prelink_cmd_blob is not None
        all_cmds = b"".join(orig_cmds + new_seg_cmds + [prelink_cmd_blob] + non_seg_cmds)
        assert 32 + len(all_cmds) <= first_sect_off

        kc16 = bytearray(kc16[:base_fo])
        struct.pack_into("<2I", kc16, 16, info16["ncmds"] + 4, len(all_cmds))
        kc16[32 : 32 + len(all_cmds)] = all_cmds
        kc16[32 + len(all_cmds) : first_sect_off] = b"\x00" * (first_sect_off - (32 + len(all_cmds)))
        kc16.extend(buf_text)
        kc16.extend(buf_txec)
        kc16.extend(buf_dcst)
        kc16.extend(buf_data)
        kc16.extend(new_pl_padded)

    with open(out_path, "wb") as f:
        f.write(kc16)

    return {
        "mode": mode,
        "slot_bundle": slot_bundle if mode == "inplace" else None,
        "out_path": out_path,
        "out_size": len(kc16),
        "new_text_va": hex(new_text_va),
        "new_txec_va": hex(new_txec_va),
        "new_dcst_va": hex(new_dcst_va),
        "new_data_va": hex(new_data_va),
        "got_resolved": len(got16_targets),
        "stubs_rebuilt": n_stubs,
        "adrp_patched": adrp_patched,
        "adrp_consumers_patched": cons_patched,
        "movz_alloc_patched": movz_patches,
        "audio_struct_patches": audio_struct_patches,
        "mba_struct_patches": mba_struct_patches,
        "vcall_patches": vcall_patches,
        "kaslr_removed": removed_kaslr,
        "kaslr_added": len(new_kaslr_offs),
        "kaslr_total": len(merged_offs),
    }

def verify_output(out_path, smap):
    with open(out_path, "rb") as f:
        out = f.read()
    info = parse_macho(out)
    segs = info["segs"]
    syms, _ = load_symtab(out, info["symtab"])

    res = subprocess.run(["otool", "-l", out_path], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"otool -l failed: {res.stderr}")

    pl, raw_pl, _, _ = load_prelink_info(out, segs)
    items = pl["_PrelinkInfoDictionary"]
    cs42 = [x for x in items if x.get("CFBundleIdentifier") == "com.apple.driver.AppleCS42L71Audio"]
    assert len(cs42) == 1, f"Expected 1 AppleCS42L71Audio in __PRELINK_INFO, got {len(cs42)}"
    cs42 = cs42[0]

    defined_ids = set(re.findall(rb'\bID="([^"]+)"', raw_pl))
    used_idrefs = set(re.findall(rb'\bIDREF="([^"]+)"', raw_pl))
    dangling = used_idrefs - defined_ids
    assert not dangling, f"Dangling XML IDREFs in __PRELINK_INFO: {dangling}"

    seg_by_name = {s["segname"]: s for s in segs}
    mode = "newseg" if "__CS42L71_TEXT" in seg_by_name else "inplace"
    text_va = cs42["_PrelinkExecutableLoadAddr"]
    data_va = cs42["_PrelinkKmodInfo"]
    text_off = va_to_off(segs, text_va)
    data_off = va_to_off(segs, data_va)
    assert text_off is not None and data_off is not None

    emb = get_embedded_kext_segs(out, segs, text_va)
    txec_va = emb["__TEXT_EXEC"][0]
    dcst_va = emb["__DATA_CONST"][0]
    txec_off = va_to_off(segs, txec_va)
    dcst_off = va_to_off(segs, dcst_va)
    assert emb["__TEXT"][0] == text_va and emb["__DATA"][0] == data_va

    def va_in_vm(va):
        return any(s["vmaddr"] <= va < s["vmaddr"] + s["vmsize"] for s in segs)

    def is_exec_va(va):
        return any(s["vmaddr"] <= va < s["vmaddr"] + s["filesize"] and (s["initprot"] & 4) for s in segs)

    if mode == "inplace":
        prelink_text = seg_by_name["__PRELINK_TEXT"]
        walk_va = prelink_text["vmaddr"]
        walk_end = walk_va + prelink_text["vmsize"]
        kext_count = 0
        saw_cs42 = False
        while walk_va < walk_end:
            woff = va_to_off(segs, walk_va)
            magic, _, _, ftype, _, sizeofcmds = struct.unpack_from("<6I", out, woff)
            if magic != 0xFEEDFACF or ftype != 11:
                break
            k_segs = get_embedded_kext_segs(out, segs, walk_va)
            if walk_va == text_va:
                saw_cs42 = True
            if "__TEXT" in k_segs:
                t_fsz = k_segs["__TEXT"][3]
                step = (t_fsz + 0x3F) & ~0x3F
            else:
                step = 0x80
            walk_va += step
            kext_count += 1
        assert saw_cs42, "kalloc_type_init walk did not land on AppleCS42L71Audio"
        assert kext_count == 208, f"Expected 208 kexts in __PRELINK_TEXT walk, got {kext_count}"

    lowest_vm = min(s["vmaddr"] for s in segs if s["vmsize"] > 0)
    kaslr_m = b"</array><key>_PrelinkLinkKASLROffsets</key><data>"
    ks = raw_pl.find(kaslr_m) + len(kaslr_m)
    ke = raw_pl.find(b"</data>", ks)
    kblob = base64.b64decode(raw_pl[ks:ke])
    kcnt = struct.unpack_from("<I", kblob, 0)[0]
    assert len(kblob) == 4 + kcnt * 4
    koffs = struct.unpack_from(f"<{kcnt}I", kblob, 4)
    assert all(koffs[i] < koffs[i + 1] for i in range(kcnt - 1)), "_PrelinkLinkKASLROffsets not strictly sorted"
    dc_lo, dc_hi = dcst_va - lowest_vm, (dcst_va + emb["__DATA_CONST"][1]) - lowest_vm
    new_in_kaslr = [o for o in koffs if dc_lo <= o < dc_hi]
    for ko in new_in_kaslr:
        ptr = struct.unpack_from("<Q", out, va_to_off(segs, lowest_vm + ko))[0]
        assert va_in_vm(ptr), f"Invalid KASLR ptr at {hex(lowest_vm + ko)}: {hex(ptr)}"

    got_vals = [struct.unpack_from("<Q", out, dcst_off + i * 8)[0] for i in range(K15_GOT_N)]
    for i, val in enumerate(got_vals):
        assert va_in_vm(val), f"GOT[{i}] invalid target {hex(val)}"

    stubs_off = txec_off + K15_TEXT_ONLY_SZ
    stubs_va = txec_va + K15_TEXT_ONLY_SZ
    stub_targets = {}
    for si in range(K15_STUBS_SZ // 12):
        sva = stubs_va + si * 12
        i0, i1, i2 = struct.unpack_from("<3I", out, stubs_off + si * 12)
        assert (i0 & 0x9F00001F) == 0x90000010 and (i1 & 0xFFC003FF) == 0xF9400210 and i2 == 0xD61F0200
        imm = (((i0 >> 5) & 0x7FFFF) << 2) | ((i0 >> 29) & 3)
        if imm & (1 << 20):
            imm -= 1 << 21
        gptr = (sva & ~0xFFF) + (imm << 12) + (((i1 >> 10) & 0xFFF) * 8)
        assert dcst_va <= gptr < dcst_va + K15_GOT_N * 8
        stub_targets[sva] = struct.unpack_from("<Q", out, va_to_off(segs, gptr))[0]

    n_insns = K15_TEXT_ONLY_SZ // 4
    insns = [struct.unpack_from("<I", out, txec_off + i * 4)[0] for i in range(n_insns)]
    valid_pages = set()
    for va_base, sz in [(text_va, 0x4000), (txec_va, K15_TXEC_SZ), (dcst_va, 0x64A8), (data_va, K15_DATA_SZ)]:
        p = va_base & ~0xFFF
        while p < va_base + sz:
            valid_pages.add(p)
            p += 0x1000
    adrp_checked = 0
    for i, w in enumerate(insns):
        pc = txec_va + i * 4
        if (w & 0x9F000000) == 0x90000000:
            imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3)
            if imm & (1 << 20):
                imm -= 1 << 21
            page = (pc & ~0xFFF) + (imm << 12)
            assert page in valid_pages, f"Out-of-kext ADRP page at {hex(pc)} -> {hex(page)}"
            adrp_checked += 1
        elif (w & 0xFC000000) in (0x94000000, 0x14000000):
            imm = w & 0x03FFFFFF
            if imm & (1 << 25):
                imm -= 1 << 26
            tgt = pc + (imm << 2)
            assert txec_va <= tgt < txec_va + K15_TXEC_SZ, f"Branch out of TXEC at {hex(pc)} -> {hex(tgt)}"
        elif (w & 0x9F000000) == 0x10000000:
            imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3)
            if imm & (1 << 20):
                imm -= 1 << 21
            tgt = pc + imm
            assert txec_va <= tgt < txec_va + K15_TEXT_ONLY_SZ

    dcst_raw_end = K15_DCST_SZ + (2 + 362) * 8 + (2 + 285) * 8
    kc15_ranges = [
        (K15_TEXT_VA, K15_TEXT_VA + K15_TEXT_SZ),
        (K15_TXEC_VA, K15_TXEC_VA + K15_TXEC_SZ),
        (K15_DCST_VA, K15_DCST_VA + K15_DCST_SZ),
        (K15_DATA_VA, K15_DATA_VA + K15_DATA_SZ),
    ]
    for pos in range(0, dcst_raw_end, 8):
        val = struct.unpack_from("<Q", out, dcst_off + pos)[0]
        if (val >> 36) == 0xFFFFFFF:
            assert va_in_vm(val), f"Invalid __DATA_CONST ptr at +{hex(pos)}: {hex(val)}"
            assert not any(s15 <= val < e15 for s15, e15 in kc15_ranges), f"Stale kc15 VA leak at +{hex(pos)}: {hex(val)}"

    for off_rel, n_slots, label in [
        (K15_DCST_SZ + 16, 362, "AppleCS42L71Audio_ext"),
        (K15_DCST_SZ + (2 + 362) * 8 + 16, 285, "AppleCS42L71Mikey_ext"),
    ]:
        for s_i in range(n_slots):
            p = struct.unpack_from("<Q", out, dcst_off + off_rel + s_i * 8)[0]
            assert is_exec_va(p), f"Non-exec ptr in {label}[{s_i}]: {hex(p)}"

    kalloc_rel = K15_KALLOC_VA - K15_DCST_VA
    for ki in range(K15_KALLOC_SZ // 64):
        ko = dcst_off + kalloc_rel + ki * 64
        q = struct.unpack_from("<8Q", out, ko)
        sz = q[5] >> 32
        sigo = va_to_off(segs, q[4])
        sig = out[sigo : out.find(b"\x00", sigo)].decode("latin1")
        assert len(sig) == sz // 8, f"__kalloc_type[{ki}] sig len {len(sig)} != {sz // 8}"

    for m_off in (K15_MOD_INIT_VA - K15_DCST_VA, K15_MOD_INIT_VA + 8 - K15_DCST_VA):
        p = struct.unpack_from("<Q", out, dcst_off + m_off)[0]
        assert is_exec_va(p), f"Invalid __mod_init_func ptr: {hex(p)}"
    kaddr, _, _, kstart, kstop = struct.unpack_from("<5Q", out, data_off + 0x9C)
    assert kaddr == text_va and is_exec_va(kstart) and is_exec_va(kstop)

    mc_ctor_16 = syms["__ZN11OSMetaClassC2EPKcPKS_j"]
    mi_fns = [struct.unpack_from("<Q", out, dcst_off + (K15_MOD_INIT_VA - K15_DCST_VA) + i * 8)[0] for i in range(2)]
    mc_calls = []
    queue = list(mi_fns)
    seen = set()
    while queue:
        fn = queue.pop(0)
        if fn in seen:
            continue
        seen.add(fn)
        foff = va_to_off(segs, fn)
        regs = {}
        for i in range(200):
            pc = fn + i * 4
            w = struct.unpack_from("<I", out, foff + i * 4)[0]
            if w in (0xD65F03C0, 0xD65F0FFF):
                break
            if (w & 0xFC000000) == 0x94000000:
                imm = w & 0x03FFFFFF
                if imm & (1 << 25):
                    imm -= 1 << 26
                tgt = pc + (imm << 2)
                if stub_targets.get(tgt) == mc_ctor_16:
                    co = va_to_off(segs, regs[1])
                    cname = out[co : out.find(b"\x00", co)].decode("utf-8")
                    mc_calls.append((cname, regs[0], regs[2], regs[3]))
                elif txec_va <= tgt < txec_va + K15_TEXT_ONLY_SZ:
                    queue.append(tgt)
                for r in range(19):
                    regs.pop(r, None)
                continue
            if (w & 0x9F000000) == 0x90000000:
                rd = w & 0x1F
                imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3)
                if imm & (1 << 20):
                    imm -= 1 << 21
                regs[rd] = (pc & ~0xFFF) + (imm << 12)
            elif (w & 0xFF800000) == 0x91000000:
                rd, rn = w & 0x1F, (w >> 5) & 0x1F
                sh, imm12 = (w >> 22) & 1, (w >> 10) & 0xFFF
                if rn in regs:
                    regs[rd] = regs[rn] + (imm12 << (12 if sh else 0))
            elif (w & 0xFFC00000) == 0xF9400000:
                rt, rn = w & 0x1F, (w >> 5) & 0x1F
                imm12 = ((w >> 10) & 0xFFF) * 8
                if rn in regs:
                    poff = va_to_off(segs, regs[rn] + imm12)
                    if poff:
                        regs[rt] = struct.unpack_from("<Q", out, poff)[0]
            elif (w & 0x7F800000) == 0x52800000:
                regs[w & 0x1F] = ((w >> 5) & 0xFFFF) << (((w >> 21) & 3) * 16)
            elif (w & 0x7FE0FFE0) == 0x2A0003E0:
                rd, rm = w & 0x1F, (w >> 16) & 0x1F
                if rm in regs:
                    regs[rd] = regs[rm]
            elif (w & 0xFFC00000) != 0xF9000000 and (w & 0x3FC00000) != 0xA9000000:
                rd = w & 0x1F
                if rd != 31:
                    regs.pop(rd, None)

    expected_sizes = {
        "AppleCS42L71Audio": 0xC48,
        "AppleCS42L71Mikey": 0x1C0,
        "AppleCS42L71MikeyBus": 0x4F8,
        "AppleCS42L71MicBiasFunction": 0x38,
        "AppleCSCodecMikeyBus": 0x4D8,
        "AppleCSCodecMikeyBusV11": 0x4E8,
        "AppleCSCodecLineOutMikeyBus": 0x4E8,
        "AppleCSCodecMikeyBusAudio": 0x290,
    }
    seen_mc = {}
    for cn, mc_va, super_va, sz in mc_calls:
        seen_mc[cn] = sz
        assert data_va <= mc_va < data_va + K15_DATA_SZ
        assert va_in_vm(super_va)
        assert expected_sizes[cn] == sz, f"OSMetaClass size mismatch for {cn}: {hex(sz)}"
    assert len(seen_mc) == 8, f"Expected 8 OSMetaClass calls, got {len(seen_mc)}"

    return {
        "mode": mode,
        "file_size": len(out),
        "otool_l": "OK",
        "prelink_plist": f"OK ({len(items)} items, 0 dangling IDREFs)",
        "kext_walk_208": "OK (208/208)" if mode == "inplace" else "N/A (newseg)",
        "kaslr_offsets": f"OK (sorted={kcnt}, cs42_dcst={len(new_in_kaslr)})",
        "got_entries": "103/103 valid",
        "stubs": "82/82 valid",
        "adrp_checked": f"{adrp_checked}/{adrp_checked} valid",
        "vtable_ext": "362 + 285 all exec",
        "kalloc_type_sigs": "8/8 exact len",
        "osmetaclass_sim": "8/8 (0xc48 & 0x290)",
    }

def main():
    default_symmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbol_map.json")
    ap = argparse.ArgumentParser(description="Relink & ABI-patch AppleCS42L71Audio from kc15 into kc16")
    ap.add_argument("--kc15", default="/tmp/kc15_n71m.dec", help="Source decrypted iOS 15 kernelcache")
    ap.add_argument("--kc16", default="/tmp/kc16_gold_v4.dec", help="Input decrypted iOS 16 kernelcache")
    ap.add_argument("--out", "--output", dest="out", default="/tmp/kc16_test_cs42l71.dec", help="Output decrypted kernelcache")
    ap.add_argument("--symbol-map", default=default_symmap, help="Path to symbol_map.json")
    ap.add_argument("--mode", choices=["inplace", "newseg"], default="inplace", help="Injection mode (default: inplace into AppleANXDPTX)")
    ap.add_argument("--slot-bundle", default="com.apple.driver.AppleANXDPTX", help="Target unused kext bundle in inplace mode")
    ap.add_argument("--verify", action="store_true", default=True, help="Run post-relink verification suite")
    ap.add_argument("--no-verify", dest="verify", action="store_false", help="Skip verification")
    args = ap.parse_args()

    stats = relink_cs42l71(args.kc15, args.kc16, args.out, args.symbol_map, mode=args.mode, slot_bundle=args.slot_bundle)
    print("RELINK STATS:", json.dumps(stats, indent=2))
    if args.verify:
        with open(args.symbol_map) as f:
            smap = json.load(f)
        vstats = verify_output(args.out, smap)
        print("VERIFY STATS:", json.dumps(vstats, indent=2))

if __name__ == "__main__":
    main()
