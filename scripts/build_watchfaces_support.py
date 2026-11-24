#!/usr/bin/env python3
import argparse
import glob
import os
import struct
import subprocess

PAGE_SIZE = 0x4000
SEG_TEXT_BASE = 0x00000
SEG_DATACONST_BASE = 0x10000
SEG_DATA_BASE = 0x14000
SEG_DATADIRTY_BASE = 0x18000
SEG_LINKEDIT_BASE = 0x1C000

OLD_TEXT_BASE = 0x20C933000
OLD_TEXT_END = 0x20C93FFF6
OLD_DATACONST_BASE = 0x21497E670
OLD_DATACONST_END = 0x21497F720
OLD_DATA_BASE = 0x213A037F0
OLD_DATA_END = 0x213A04018
OLD_DATADIRTY_BASE = 0x214F12900
OLD_DATADIRTY_END = 0x214F12A28

def map_old_va(va: int):
    if OLD_TEXT_BASE <= va < OLD_TEXT_END:
        return SEG_TEXT_BASE + (va - OLD_TEXT_BASE)
    if OLD_DATACONST_BASE <= va <= OLD_DATACONST_END:
        return SEG_DATACONST_BASE + (va - 0x21497E000)
    if OLD_DATA_BASE <= va <= OLD_DATA_END:
        return SEG_DATA_BASE + (va - 0x213A03000)
    if OLD_DATADIRTY_BASE <= va <= OLD_DATADIRTY_END:
        return SEG_DATADIRTY_BASE + (va - 0x214F12000)
    return None

def map_old_page(pg: int):
    if (OLD_TEXT_BASE & ~0xFFF) <= pg <= (OLD_TEXT_END & ~0xFFF):
        return SEG_TEXT_BASE + (pg - OLD_TEXT_BASE)
    if 0x21497E000 <= pg <= 0x21497F000:
        return SEG_DATACONST_BASE + (pg - 0x21497E000)
    if 0x213A03000 <= pg <= 0x213A04000:
        return SEG_DATA_BASE + (pg - 0x213A03000)
    if pg == 0x214F12000:
        return SEG_DATADIRTY_BASE
    return None

def uleb(buf: bytes, pos: int):
    val, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, pos

def enc_uleb(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)

def enc_adrp(rd: int, pc: int, target_page: int) -> int:
    delta_pages = (target_page - (pc & ~0xFFF)) >> 12
    assert -(1 << 20) <= delta_pages < (1 << 20), f"ADRP out of range: {delta_pages}"
    imm = delta_pages & 0x1FFFFF
    immlo = imm & 0x3
    immhi = (imm >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | rd

def enc_ldr_u64(rt: int, rn: int, byte_off: int) -> int:
    assert byte_off % 8 == 0 and 0 <= byte_off // 8 < 0x1000
    return 0xF9400000 | ((byte_off // 8) << 10) | (rn << 5) | rt

def enc_bl(pc: int, target: int) -> int:
    diff = target - pc
    assert diff % 4 == 0
    return 0x94000000 | ((diff >> 2) & 0x3FFFFFF)

def enc_b(pc: int, target: int) -> int:
    diff = target - pc
    assert diff % 4 == 0
    return 0x14000000 | ((diff >> 2) & 0x3FFFFFF)

def load_cache(cdir: str):
    files = sorted(glob.glob(os.path.join(cdir, "dyld_shared_cache_arm64*")))
    vm_r = []
    le_f = []
    for fp in files:
        if fp.endswith(".symbols"):
            continue
        with open(fp, "rb") as f:
            h = f.read(512)
            if not h.startswith(b"dyld_v1"):
                continue
            mo, mc = struct.unpack_from("<II", h, 0x10)
            f.seek(mo)
            for _ in range(mc):
                a, sz, fo, _, _ = struct.unpack("<QQQII", f.read(32))
                vm_r.append((a, a + sz, fo, fp))
                if "dyldlinkedit" in fp:
                    le_f.append((a, a + sz, fo, fp))

    def rvm(va: int, ln: int) -> bytes:
        for s, e, fo, fp in vm_r:
            if s <= va < e:
                with open(fp, "rb") as f:
                    f.seek(fo + (va - s))
                    return f.read(ln)
        return b""

    def rlinkedit(le_vm: int, file_off: int, ln: int) -> bytes:
        for s, e, fo, fp in le_f:
            if s <= le_vm < e:
                with open(fp, "rb") as f:
                    f.seek(file_off)
                    return f.read(ln)
        return b""

    mc = os.path.join(cdir, "dyld_shared_cache_arm64")
    imgs = {}
    with open(mc, "rb") as f:
        h = f.read(512)
        io, ic = struct.unpack_from("<II", h, 0x1C0)
        f.seek(io)
        raw = f.read(ic * 32)
        for i in range(ic):
            a, _mt, _ino, po, _pad = struct.unpack_from("<QQQII", raw, i * 32)
            f.seek(po)
            s = b""
            while True:
                c = f.read(64)
                nul = c.find(b"\x00")
                if nul != -1:
                    s += c[:nul]
                    break
                s += c
            imgs[s.decode("utf-8", "ignore")] = a

    addr_to_sym = {}
    img_exports = {}
    img_reexports = {}
    for dp, base in imgs.items():
        dh = rvm(base, 32768)
        if len(dh) < 32:
            continue
        nc = struct.unpack_from("<I", dh, 16)[0]
        doff = 32
        le_vm = 0
        to = ts = 0
        reex = []
        for _ in range(nc):
            if doff + 8 > len(dh):
                break
            c, cs = struct.unpack_from("<II", dh, doff)
            if c == 0x19 and dh[doff + 8 : doff + 24].split(b"\x00")[0] == b"__LINKEDIT":
                le_vm = struct.unpack_from("<Q", dh, doff + 24)[0]
            elif c == 0x80000033:
                to, ts = struct.unpack_from("<II", dh, doff + 8)
            elif c == 0x8000001F:
                so = struct.unpack_from("<I", dh, doff + 8)[0]
                reex.append(dh[doff + so : dh.find(b"\x00", doff + so)].decode())
            doff += cs
        img_reexports[dp] = reex
        sset = set()
        if ts > 0:
            trie = rlinkedit(le_vm, to, ts)
            if trie:
                def walk(pos, pref):
                    tsz, p = uleb(trie, pos)
                    if tsz > 0:
                        fl, p2 = uleb(trie, p)
                        sset.add(pref)
                        if not (fl & 0x08):
                            a, _ = uleb(trie, p2)
                            addr_to_sym[base + a] = (dp, pref)
                    cp = p + tsz
                    nch = trie[cp]
                    cp += 1
                    for _ in range(nch):
                        z = trie.find(b"\x00", cp)
                        edge = trie[cp:z].decode("utf-8", "ignore")
                        o2, cp = uleb(trie, z + 1)
                        walk(o2, pref + edge)
                walk(0, "")
        img_exports[dp] = sset

    def has_sym_recursive(dp, sym, seen=None):
        if seen is None:
            seen = set()
        if dp in seen:
            return False
        seen.add(dp)
        if sym in img_exports.get(dp, ()):
            return True
        for r in img_reexports.get(dp, ()):
            if has_sym_recursive(r, sym, seen):
                return True
        return False

    return rvm, rlinkedit, addr_to_sym, has_sym_recursive, imgs

def build_export_trie(symbols):
    root = {"children": {}, "term": None}
    for name, flags, addr in symbols:
        node = root
        for ch in name.encode("utf-8"):
            b = bytes([ch])
            if b not in node["children"]:
                node["children"][b] = {"children": {}, "term": None}
            node = node["children"][b]
        node["term"] = (flags, addr)

    def compress(node):
        new_ch = {}
        for edge, child in node["children"].items():
            while child["term"] is None and len(child["children"]) == 1:
                e2, c2 = next(iter(child["children"].items()))
                edge += e2
                child = c2
            compress(child)
            new_ch[edge] = child
        node["children"] = new_ch

    compress(root)
    nodes = []

    def collect(n):
        nodes.append(n)
        n["off"] = 0
        for e in sorted(n["children"].keys()):
            collect(n["children"][e])

    collect(root)

    def serialize_node(n):
        if n["term"] is not None:
            fl, ad = n["term"]
            tbytes = enc_uleb(fl) + enc_uleb(ad)
            out = bytearray(enc_uleb(len(tbytes)) + tbytes)
        else:
            out = bytearray(b"\x00")
        out.append(len(n["children"]))
        for edge in sorted(n["children"].keys()):
            out.extend(edge)
            out.append(0)
            out.extend(enc_uleb(n["children"][edge]["off"]))
        return bytes(out)

    for _ in range(20):
        changed = False
        cur = 0
        for n in nodes:
            if n["off"] != cur:
                n["off"] = cur
                changed = True
            cur += len(serialize_node(n))
        if not changed:
            break

    buf = bytearray()
    for n in nodes:
        buf.extend(serialize_node(n))
    while len(buf) % 8 != 0:
        buf.append(0)
    return bytes(buf)

def build_rebase_opcodes(offsets_by_seg):
    out = bytearray([0x11])
    for seg_idx, soff in sorted(offsets_by_seg):
        out.append(0x20 | seg_idx)
        out.extend(enc_uleb(soff))
        out.append(0x51)
    out.append(0x00)
    while len(out) % 8 != 0:
        out.append(0x00)
    return bytes(out)

def build_bind_opcodes(binds):
    out = bytearray()
    for seg_idx, soff, ord_idx, sym_name, is_weak in sorted(binds):
        if ord_idx < 16:
            out.append(0x10 | ord_idx)
        else:
            out.append(0x20)
            out.extend(enc_uleb(ord_idx))
        out.append(0x40 | (0x01 if is_weak else 0x00))
        out.extend(sym_name.encode("utf-8") + b"\x00")
        out.append(0x51)
        out.append(0x70 | seg_idx)
        out.extend(enc_uleb(soff))
        out.append(0x90)
    out.append(0x00)
    while len(out) % 8 != 0:
        out.append(0x00)
    return bytes(out)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip8-cache-dir", required=True, help="Directory containing iPhone 8 16.7.16 dyld_shared_cache_arm64*")
    ap.add_argument("--r16-cache-dir", required=True, help="Directory containing iPad 5 16.7.16 dyld_shared_cache_arm64*")
    ap.add_argument("--out", required=True, help="Output path for standalone WatchFacesWallpaperSupport dylib")
    args = ap.parse_args()

    print("[*] Loading iPhone 8 & iPad 5 16.7.16 dyld_shared_cache tables...")
    rvm_ip8, rle_ip8, a2s_ip8, _, imgs_ip8 = load_cache(args.ip8_cache_dir)
    _, _, _, has_sym_r16, _ = load_cache(args.r16_cache_dir)

    id_dylib_name = "/System/Library/PrivateFrameworks/WatchFacesWallpaperSupport.framework/WatchFacesWallpaperSupport"
    wf_base = imgs_ip8.get(id_dylib_name, OLD_TEXT_BASE)
    assert wf_base == OLD_TEXT_BASE, f"Unexpected WatchFacesWallpaperSupport base: 0x{wf_base:x}"

    hdr_text = rvm_ip8(OLD_TEXT_BASE, 0xCFF6)
    _, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from("<8I", hdr_text, 0)

    off = 32
    dylibs = []
    segs_raw = {}
    le_vm = 0
    fs_do = fs_ds = 0
    exp_to = exp_ts = 0
    uuid_bytes = b""
    build_ver = b""
    src_ver = b""

    while off < 32 + sizeofcmds:
        cmd, cs = struct.unpack_from("<II", hdr_text, off)
        if cmd == 0x19:
            sname = hdr_text[off + 8 : off + 24].split(b"\x00")[0].decode()
            nsects = struct.unpack_from("<I", hdr_text, off + 64)[0]
            segs_raw[sname] = hdr_text[off + 72 : off + 72 + nsects * 80]
            if sname == "__LINKEDIT":
                le_vm = struct.unpack_from("<Q", hdr_text, off + 24)[0]
        elif cmd in (0x0C, 0x80000018):
            so, _ts, cv, cmpv = struct.unpack_from("<IIII", hdr_text, off + 8)
            nm = hdr_text[off + so : hdr_text.find(b"\x00", off + so)].decode()
            dylibs.append((cmd, nm, cv, cmpv))
        elif cmd == 0x26:
            fs_do, fs_ds = struct.unpack_from("<II", hdr_text, off + 8)
        elif cmd == 0x80000033:
            exp_to, exp_ts = struct.unpack_from("<II", hdr_text, off + 8)
        elif cmd == 0x1B:
            uuid_bytes = hdr_text[off + 8 : off + 24]
        elif cmd == 0x32:
            build_ver = hdr_text[off : off + cs]
        elif cmd == 0x2A:
            src_ver = hdr_text[off : off + cs]
        off += cs

    func_starts = rlinkedit_bytes = rle_ip8(le_vm, fs_do, fs_ds) if fs_ds else b""
    orig_trie = rle_ip8(le_vm, exp_to, exp_ts)
    assert orig_trie, "Failed reading original export trie from iPhone 8 dyldlinkedit"

    def find_ordinal(orig_dp, sym):
        for idx, (cmd, nm, _, _) in enumerate(dylibs, 1):
            if nm == orig_dp and has_sym_r16(nm, sym):
                return idx, (cmd == 0x80000018)
        for idx, (cmd, nm, _, _) in enumerate(dylibs, 1):
            if has_sym_r16(nm, sym):
                return idx, (cmd == 0x80000018)
        raise RuntimeError(f"Cannot find ordinal for {orig_dp}:{sym} in iPad 5 shared cache")

    text_buf = bytearray(PAGE_SIZE * 4)
    text_buf[:0xCFF6] = hdr_text
    text_buf[:0x166C] = b"\x00" * 0x166C

    dc_buf = bytearray(PAGE_SIZE)
    dc_buf[0x670:0x1720] = rvm_ip8(OLD_DATACONST_BASE, 0x10B0)

    da_buf = bytearray(PAGE_SIZE)
    da_buf[0x7F0:0x1018] = rvm_ip8(OLD_DATA_BASE, 0x828)

    dd_buf = bytearray(PAGE_SIZE)
    dd_buf[0x900:0xA10] = rvm_ip8(OLD_DATADIRTY_BASE, 0x110)

    rebases = []
    binds = []

    str_cursor = 0xD000
    sel_va_map = {}

    def intern_sel_str(s_bytes):
        nonlocal str_cursor
        if s_bytes not in sel_va_map:
            sel_va_map[s_bytes] = SEG_TEXT_BASE + str_cursor
            text_buf[str_cursor : str_cursor + len(s_bytes)] = s_bytes
            text_buf[str_cursor + len(s_bytes)] = 0
            str_cursor += len(s_bytes) + 1
        return sel_va_map[s_bytes]

    sel_name_to_selref_va = {}
    for i in range(20):
        slot_off = 0x1680 + i * 8
        old_v = struct.unpack_from("<Q", dc_buf, slot_off)[0] & 0x3FFFFFFFF
        s_bytes = rvm_ip8(old_v, 128).split(b"\x00")[0]
        new_str_va = intern_sel_str(s_bytes)
        struct.pack_into("<Q", dc_buf, slot_off, new_str_va)
        rebases.append((1, slot_off))
        sel_name_to_selref_va[s_bytes] = SEG_DATACONST_BASE + slot_off

    sym_to_got_va = {}
    for i in range(178):
        slot_off = 0x670 + i * 8
        old_v = struct.unpack_from("<Q", dc_buf, slot_off)[0] & 0x3FFFFFFFF
        local = map_old_va(old_v)
        if local is not None:
            struct.pack_into("<Q", dc_buf, slot_off, local)
            rebases.append((1, slot_off))
        elif old_v in a2s_ip8:
            orig_dp, sym = a2s_ip8[old_v]
            ord_idx, is_weak = find_ordinal(orig_dp, sym)
            struct.pack_into("<Q", dc_buf, slot_off, 0)
            binds.append((1, slot_off, ord_idx, sym, is_weak))
            sym_to_got_va[(orig_dp, sym)] = SEG_DATACONST_BASE + slot_off
        else:
            raise RuntimeError(f"Unresolved __got[{i}] = 0x{old_v:x}")

    for off in range(0xC00, 0x12B8, 8):
        raw_v = struct.unpack_from("<Q", dc_buf, off)[0]
        if raw_v == 0:
            continue
        t = raw_v & 0x3FFFFFFFF
        hi = raw_v >> 34
        if raw_v in (
            0xFD00000007, 0x800000000, 0xFD00200000, 0x1000000080, 0x2800000081,
            0xF7FBCF28F7FBCF28, 0xF7FBCF0C00000001, 0x200000000, 0x200000005,
            0x170000FFFFFF28, 0xFC00200007,
        ) or hi not in (0x0, 0x80, 0x100, 0x180, 0x200, 0x280, 0x300):
            if raw_v < 0x100000000 or (hi not in (0x0, 0x80, 0x100, 0x180, 0x200, 0x280, 0x300)):
                continue
        if t in (0x200000000, 0x200000005):
            continue
        local = map_old_va(t)
        if local is not None:
            struct.pack_into("<Q", dc_buf, off, local)
            rebases.append((1, off))
        elif t in a2s_ip8:
            orig_dp, sym = a2s_ip8[t]
            ord_idx, is_weak = find_ordinal(orig_dp, sym)
            struct.pack_into("<Q", dc_buf, off, 0)
            binds.append((1, off, ord_idx, sym, is_weak))

    for rel_off in (0x3F8, 0x3FC, 0x404):
        off = 0xC00 + rel_off
        old_val = struct.unpack_from("<i", dc_buf, off)[0]
        new_tgt = map_old_va(0x21497EC00 + rel_off + old_val)
        assert new_tgt is not None
        struct.pack_into("<i", dc_buf, off, new_tgt - (SEG_DATACONST_BASE + off))

    for off in range(0x12B8, 0x12D8, 8):
        raw_v = struct.unpack_from("<Q", dc_buf, off)[0]
        if raw_v == 0:
            continue
        local = map_old_va(raw_v & 0x3FFFFFFFF)
        assert local is not None
        struct.pack_into("<Q", dc_buf, off, local)
        rebases.append((1, off))

    for off in range(0x12E0, 0x1680, 8):
        raw_v = struct.unpack_from("<Q", dc_buf, off)[0]
        if raw_v == 0:
            continue
        rel = off - 0x12E0
        t = raw_v & 0x3FFFFFFFF
        if t < 0x180000000 or rel in (0x000, 0x018, 0x080, 0x0C8, 0x298, 0x2B8):
            continue
        if t == 0x1E1AC9CE8:
            struct.pack_into("<Q", dc_buf, off, SEG_DATA_BASE + 0x8B0)
            rebases.append((1, off))
            continue
        local = map_old_va(t)
        if local is not None:
            struct.pack_into("<Q", dc_buf, off, local)
            rebases.append((1, off))
        else:
            s_bytes = rvm_ip8(t, 128).split(b"\x00")[0]
            assert len(s_bytes) > 0 and all(32 <= b < 127 for b in s_bytes)
            struct.pack_into("<Q", dc_buf, off, intern_sel_str(s_bytes))
            rebases.append((1, off))

    img_flags = struct.unpack_from("<I", dc_buf, 0x12D8 + 4)[0]
    struct.pack_into("<I", dc_buf, 0x12D8 + 4, img_flags & ~0x08)
    for ml_off in (0x12F8, 0x13A8, 0x1578):
        entsize_flags = struct.unpack_from("<I", dc_buf, ml_off)[0]
        struct.pack_into("<I", dc_buf, ml_off, entsize_flags & ~0x03)

    for idx, local_proto_off in [(0, 0x848), (1, 0x8B0)]:
        off = 0x7F0 + idx * 8
        struct.pack_into("<Q", da_buf, off, SEG_DATA_BASE + local_proto_off)
        rebases.append((2, off))

    for i in range(6):
        off = 0x800 + i * 8
        t = struct.unpack_from("<Q", da_buf, off)[0] & 0x3FFFFFFFF
        orig_dp, sym = a2s_ip8[t]
        ord_idx, is_weak = find_ordinal(orig_dp, sym)
        struct.pack_into("<Q", da_buf, off, 0)
        binds.append((2, off, ord_idx, sym, is_weak))

    for off in (0x850, 0x858, 0x860, 0x880, 0x890, 0x8B8, 0x8C8, 0x8D8, 0x8E8, 0x8F8):
        local = map_old_va(struct.unpack_from("<Q", da_buf, off)[0] & 0x3FFFFFFFF)
        assert local is not None
        struct.pack_into("<Q", da_buf, off, local)
        rebases.append((2, off))

    for rel_off in (0x0E0, 0x1F0, 0x1F8, 0x200, 0x220, 0x230, 0x238, 0x240):
        off = 0x830 + rel_off
        old_val = struct.unpack_from("<i", da_buf, off)[0]
        new_tgt = map_old_va(0x213A03830 + rel_off + old_val)
        assert new_tgt is not None
        struct.pack_into("<i", da_buf, off, new_tgt - (SEG_DATA_BASE + off))

    off = 0x900 + 0x108
    old_val = struct.unpack_from("<i", dd_buf, off)[0]
    new_tgt = map_old_va(0x214F12900 + 0x108 + old_val)
    assert new_tgt is not None
    struct.pack_into("<i", dd_buf, off, new_tgt - (SEG_DATADIRTY_BASE + off))

    for rel_off in (0x004, 0x0D0):
        off = 0xC15C + rel_off
        old_val = struct.unpack_from("<i", text_buf, off)[0]
        new_tgt = map_old_va(0x20C93F15C + rel_off + old_val)
        assert new_tgt is not None
        struct.pack_into("<i", text_buf, off, new_tgt - (SEG_TEXT_BASE + off))

    extra_got_cursor = 0x1720

    def get_or_add_got_for_sym(orig_dp, sym):
        nonlocal extra_got_cursor
        key = (orig_dp, sym)
        if key in sym_to_got_va:
            return sym_to_got_va[key]
        ord_idx, is_weak = find_ordinal(orig_dp, sym)
        slot_off = extra_got_cursor
        extra_got_cursor += 8
        struct.pack_into("<Q", dc_buf, slot_off, 0)
        binds.append((1, slot_off, ord_idx, sym, is_weak))
        va = SEG_DATACONST_BASE + slot_off
        sym_to_got_va[key] = va
        return va

    stub_cursor = (str_cursor + 15) & ~15
    island_to_new_stub = {}

    for i in range(151):
        soff = 0xAD5C + i * 12
        i0, _i1, _i2 = struct.unpack_from("<3I", text_buf, soff)
        assert (i0 & 0x9F00001F) == 0x90000010
        pc = OLD_TEXT_BASE + soff
        imm = (((i0 >> 5) & 0x7FFFF) << 2) | ((i0 >> 29) & 0x3)
        if imm & (1 << 20):
            imm -= (1 << 21)
        new_page = map_old_page((pc & ~0xFFF) + (imm << 12))
        assert new_page is not None
        struct.pack_into("<I", text_buf, soff, enc_adrp(16, SEG_TEXT_BASE + soff, new_page))

    ext_adrp_pages = {}
    for i in range(0, 0x96F0, 4):
        foff = 0x166C + i
        insn = struct.unpack_from("<I", text_buf, foff)[0]
        old_pc = OLD_TEXT_BASE + foff
        new_pc = SEG_TEXT_BASE + foff

        if (insn & 0x9F000000) == 0x90000000:
            rd = insn & 0x1F
            imm = (((insn >> 5) & 0x7FFFF) << 2) | ((insn >> 29) & 0x3)
            if imm & (1 << 20):
                imm -= (1 << 21)
            old_page = (old_pc & ~0xFFF) + (imm << 12)
            new_page = map_old_page(old_page)
            if new_page is not None:
                struct.pack_into("<I", text_buf, foff, enc_adrp(rd, new_pc, new_page))
                ext_adrp_pages.pop(rd, None)
            else:
                ext_adrp_pages[rd] = old_page
                struct.pack_into("<I", text_buf, foff, enc_adrp(rd, new_pc, SEG_DATACONST_BASE + 0x1000))
        elif (insn & 0xFF800000) == 0x91000000:
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            if rn in ext_adrp_pages and rd == 1:
                old_page = ext_adrp_pages[rn]
                off12 = (insn >> 10) & 0xFFF
                sel_bytes = rvm_ip8(old_page + off12, 128).split(b"\x00")[0]
                selref_va = sel_name_to_selref_va[sel_bytes]
                struct.pack_into("<I", text_buf, foff, enc_ldr_u64(rd, rn, selref_va & 0xFFF))
        elif (insn & 0x7C000000) == 0x14000000:
            is_bl = (insn & 0x80000000) != 0
            imm26 = insn & 0x3FFFFFF
            if imm26 & (1 << 25):
                imm26 -= (1 << 26)
            old_tgt = old_pc + (imm26 << 2)
            if not (OLD_TEXT_BASE <= old_tgt < OLD_TEXT_END):
                if old_tgt not in island_to_new_stub:
                    i0, i1, _i2 = struct.unpack_from("<3I", rvm_ip8(old_tgt, 12), 0)
                    imm = (((i0 >> 5) & 0x7FFFF) << 2) | ((i0 >> 29) & 0x3)
                    if imm & (1 << 20):
                        imm -= (1 << 21)
                    dest = (old_tgt & ~0xFFF) + (imm << 12) + ((i1 >> 10) & 0xFFF)
                    orig_dp, sym = a2s_ip8[dest]
                    got_va = get_or_add_got_for_sym(orig_dp, sym)
                    stub_va = SEG_TEXT_BASE + stub_cursor
                    struct.pack_into(
                        "<3I", text_buf, stub_cursor,
                        enc_adrp(16, stub_va, got_va & ~0xFFF),
                        enc_ldr_u64(16, 16, got_va & 0xFFF),
                        0xD61F0200,
                    )
                    island_to_new_stub[old_tgt] = stub_va
                    stub_cursor += 12
                new_tgt = island_to_new_stub[old_tgt]
                struct.pack_into("<I", text_buf, foff, enc_bl(new_pc, new_tgt) if is_bl else enc_b(new_pc, new_tgt))

    assert stub_cursor <= len(text_buf)
    assert extra_got_cursor <= PAGE_SIZE

    new_exports = []

    def walk_orig(pos, pref):
        tsz, p = uleb(orig_trie, pos)
        if tsz > 0:
            fl, p2 = uleb(orig_trie, p)
            ad, _ = uleb(orig_trie, p2)
            new_va = map_old_va(OLD_TEXT_BASE + ad)
            assert new_va is not None
            new_exports.append((pref, fl, new_va - SEG_TEXT_BASE))
        cp = p + tsz
        nch = orig_trie[cp]
        cp += 1
        for _ in range(nch):
            z = orig_trie.find(b"\x00", cp)
            edge = orig_trie[cp:z].decode("utf-8", "ignore")
            o2, cp = uleb(orig_trie, z + 1)
            walk_orig(o2, pref + edge)

    walk_orig(0, "")
    new_trie = build_export_trie(new_exports)

    rebase_ops = build_rebase_opcodes(rebases)
    bind_ops = build_bind_opcodes([b for b in binds if not b[4]])
    weak_ops = build_bind_opcodes([b for b in binds if b[4]])

    strtab = bytearray(b"\x00")
    nlists = []
    extdefs = [
        ("_WatchFacesWallpaperSupportVersionNumber", 1, SEG_TEXT_BASE + 0xB4B0),
        ("_WatchFacesWallpaperSupportVersionString", 1, SEG_TEXT_BASE + 0xB470),
    ]
    for nm, sect, va in extdefs:
        so = len(strtab)
        strtab.extend(nm.encode("utf-8") + b"\x00")
        nlists.append((so, 0x0F, sect, 0, va))

    unique_undefs = sorted({(ord_idx, sym, is_weak) for _, _, ord_idx, sym, is_weak in binds}, key=lambda x: (x[1], x[0]))
    for ord_idx, sym, is_weak in unique_undefs:
        so = len(strtab)
        strtab.extend(sym.encode("utf-8") + b"\x00")
        desc = (ord_idx << 8) | (0x0040 if is_weak else 0x0000)
        nlists.append((so, 0x01, 0, desc, 0))

    while len(strtab) % 8 != 0:
        strtab.append(0)

    symtab_bytes = b"".join(struct.pack("<IBBHQ", *nl) for nl in nlists)
    indirs = [0x40000000] * 151 + [0xC0000000] * 178
    indir_bytes = struct.pack(f"<{len(indirs)}I", *indirs)

    le_buf = bytearray()

    def append_le(b):
        off_in_le = len(le_buf)
        le_buf.extend(b)
        while len(le_buf) % 8 != 0:
            le_buf.append(0)
        return SEG_LINKEDIT_BASE + off_in_le, len(b)

    rebase_off, rebase_sz = append_le(rebase_ops)
    bind_off, bind_sz = append_le(bind_ops)
    weak_off, weak_sz = append_le(weak_ops)
    exp_off, exp_sz = append_le(new_trie)
    fs_off, fs_sz = append_le(func_starts)
    sym_off, _ = append_le(symtab_bytes)
    ind_off, _ = append_le(indir_bytes)
    str_off, _ = append_le(bytes(strtab))

    le_vmsize = ((len(le_buf) + PAGE_SIZE - 1) // PAGE_SIZE) * PAGE_SIZE
    le_filesize = len(le_buf)

    def lc_dylib(cmd, nm, cv, cmpv):
        sb = nm.encode("utf-8") + b"\x00"
        sz = (24 + len(sb) + 7) & ~7
        return struct.pack("<IIIIII", cmd, sz, 24, 2, cv, cmpv) + sb + b"\x00" * (sz - 24 - len(sb))

    def lc_sect(sname, segname, addr, sz, off, align, flags, r1=0, r2=0):
        return struct.pack("<16s16sQQIIIIIIII", sname.encode(), segname.encode(), addr, sz, off, align, 0, 0, flags, r1, r2, 0)

    def lc_seg(segname, vmaddr, vmsize, fileoff, filesize, maxp, initp, sects_bytes):
        nsects = len(sects_bytes) // 80
        cs = 72 + len(sects_bytes)
        return struct.pack("<II16sQQQQIIII", 0x19, cs, segname.encode(), vmaddr, vmsize, fileoff, filesize, maxp, initp, nsects, 0) + sects_bytes

    def remap_sects(raw_sects: bytes, old_page_base: int, new_seg_base: int, is_text: bool = False) -> bytearray:
        out = bytearray()
        for si in range(len(raw_sects) // 80):
            sb = bytearray(raw_sects[si * 80 : (si + 1) * 80])
            oa, _osz, oo = struct.unpack_from("<QQI", sb, 32)
            na = new_seg_base + (oa - old_page_base)
            struct.pack_into("<Q", sb, 32, na)
            struct.pack_into("<I", sb, 48, na if (is_text or oo != 0) else 0)
            out.extend(sb)
        return out

    new_text_sects = remap_sects(segs_raw["__TEXT"], OLD_TEXT_BASE, SEG_TEXT_BASE, is_text=True)
    new_dc_sects = remap_sects(segs_raw["__DATA_CONST"], 0x21497E000, SEG_DATACONST_BASE)
    if extra_got_cursor > 0x1720:
        new_dc_sects.extend(lc_sect("__got_ext", "__DATA_CONST", SEG_DATACONST_BASE + 0x1720, extra_got_cursor - 0x1720, SEG_DATACONST_BASE + 0x1720, 3, 0))
    new_da_sects = remap_sects(segs_raw["__DATA"], 0x213A03000, SEG_DATA_BASE)
    new_dd_sects = remap_sects(segs_raw["__DATA_DIRTY"], 0x214F12000, SEG_DATADIRTY_BASE)

    cmds = [
        lc_seg("__TEXT", SEG_TEXT_BASE, PAGE_SIZE * 4, 0, PAGE_SIZE * 4, 5, 5, bytes(new_text_sects)),
        lc_seg("__DATA_CONST", SEG_DATACONST_BASE, PAGE_SIZE, SEG_DATACONST_BASE, PAGE_SIZE, 3, 3, bytes(new_dc_sects)),
        lc_seg("__DATA", SEG_DATA_BASE, PAGE_SIZE, SEG_DATA_BASE, PAGE_SIZE, 3, 3, bytes(new_da_sects)),
        lc_seg("__DATA_DIRTY", SEG_DATADIRTY_BASE, PAGE_SIZE, SEG_DATADIRTY_BASE, PAGE_SIZE, 3, 3, bytes(new_dd_sects)),
        lc_seg("__LINKEDIT", SEG_LINKEDIT_BASE, le_vmsize, SEG_LINKEDIT_BASE, le_filesize, 1, 1, b""),
        lc_dylib(0x0D, id_dylib_name, 0x00010000, 0x00010000),
        struct.pack("<12I", 0x80000022, 48, rebase_off, rebase_sz, bind_off, bind_sz, weak_off, weak_sz, 0, 0, 0, 0),
        struct.pack("<IIII", 0x80000033, 16, exp_off, exp_sz),
        struct.pack("<IIIIII", 0x02, 24, sym_off, len(nlists), str_off, len(strtab)),
        struct.pack("<20I", 0x0B, 80, 0, 0, 0, len(extdefs), len(extdefs), len(unique_undefs), 0, 0, 0, 0, 0, 0, ind_off, len(indirs), 0, 0, 0, 0),
        struct.pack("<II", 0x1B, 24) + uuid_bytes,
        build_ver,
        src_ver,
    ]
    for cmd, nm, cv, cmpv in dylibs:
        cmds.append(lc_dylib(cmd, nm, cv, cmpv))
    cmds.append(struct.pack("<IIII", 0x26, 16, fs_off, fs_sz))
    cmds.append(struct.pack("<IIII", 0x29, 16, 0, 0))

    cmds_blob = b"".join(cmds)
    assert 32 + len(cmds_blob) <= 0x166C
    mach_hdr = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 6, len(cmds), len(cmds_blob), 0x02100085, 0)
    text_buf[:32] = mach_hdr
    text_buf[32 : 32 + len(cmds_blob)] = cmds_blob

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(text_buf)
        f.write(dc_buf)
        f.write(da_buf)
        f.write(dd_buf)
        f.write(le_buf)

    subprocess.run(
        ["codesign", "-f", "-s", "-", "-i", "com.apple.WatchFacesWallpaperSupport", args.out],
        check=True,
        capture_output=True,
    )
    print(f"[+] Built standalone WatchFacesWallpaperSupport dylib: {args.out} ({os.path.getsize(args.out)} B)")

if __name__ == "__main__":
    main()
