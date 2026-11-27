#!/usr/bin/env python3
"""Extract Procursus .deb packages, preserve entitlements on ad-hoc resign, and record dpkg status."""

import argparse
import importlib.util
import plistlib
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
MACHO_MAGICS = {0xFEEDFACE, 0xCEFAEDFE, 0xFEEDFACF, 0xCFFAEDFE, 0xCAFEBABE, 0xBEBAFECA}


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as f:
            hdr = f.read(4)
        if len(hdr) < 4:
            return False
        magic = struct.unpack("<I", hdr)[0]
        return magic in MACHO_MAGICS
    except OSError:
        return False


def sign_binary(binary_path: Path, ent_path: Path = None) -> None:
    subprocess.run(["/usr/bin/xattr", "-c", str(binary_path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = ["codesign", "-f", "-s", "-"]
    if binary_path.name == "Sileo":
        cmd += ["--identifier", "org.coolstar.SileoStore"]
    if ent_path is not None:
        cmd += ["--entitlements", str(ent_path)]
    cmd.append(str(binary_path))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return
    err_txt = (res.stderr or res.stdout).strip()
    if "detritus" in err_txt.lower() or "resource fork" in err_txt.lower():
        with tempfile.TemporaryDirectory(prefix="sign_tmp_", dir="/tmp") as td_s:
            tmp_bin = Path(td_s) / binary_path.name
            shutil.copyfile(binary_path, tmp_bin)
            shutil.copymode(binary_path, tmp_bin)
            subprocess.run(["/usr/bin/xattr", "-c", str(tmp_bin)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tmp_cmd = ["codesign", "-f", "-s", "-"]
            if binary_path.name == "Sileo":
                tmp_cmd += ["--identifier", "org.coolstar.SileoStore"]
            if ent_path is not None:
                tmp_cmd += ["--entitlements", str(ent_path)]
            tmp_cmd.append(str(tmp_bin))
            res2 = subprocess.run(tmp_cmd, capture_output=True, text=True)
            if res2.returncode == 0:
                shutil.copyfile(tmp_bin, binary_path)
                return
            err_txt = (res2.stderr or res2.stdout).strip()
    raise RuntimeError(f"codesign failed for {binary_path}: {err_txt}")


def patch_sileo_binary(sileo_path: Path) -> bool:
    """Patch Sileo Mach-O so spawnAsRoot always uses giveMeRoot and Unknown JB falls back to Procursus."""
    if not sileo_path.is_file() or not is_macho(sileo_path):
        return False
    data = bytearray(sileo_path.read_bytes())

    # 1) In spawnAsRoot: force the pre-iOS-13 giveMeRoot branch instead of persona(99) spawn (ESRCH on our iOS 16 setup).
    p1_prefix = bytes.fromhex("40008052a10180520200805203008052")
    p1_suffix = bytes.fromhex("c80a40f9081300b4")
    p1_matches = []
    pos = 0
    while True:
        idx = data.find(p1_prefix, pos)
        if idx == -1:
            break
        if idx + 32 <= len(data) and data[idx + 24 : idx + 32] == p1_suffix:
            bl_insn = struct.unpack_from("<I", data, idx + 16)[0]
            br_insn = struct.unpack_from("<I", data, idx + 20)[0]
            if (bl_insn & 0xFC000000) == 0x94000000:
                p1_matches.append((idx + 20, br_insn))
        pos = idx + 4

    if len(p1_matches) != 1:
        raise RuntimeError(f"Expected 1 spawnAsRoot site in {sileo_path.name}, found {len(p1_matches)}")
    br_off, br_insn = p1_matches[0]
    changed = False
    if (br_insn & 0xFF00001F) == 0x34000000:  # cbz w0, rel19
        imm19 = (br_insn >> 5) & 0x7FFFF
        if imm19 & 0x40000:
            imm19 -= 0x80000
        b_insn = 0x14000000 | (imm19 & 0x03FFFFFF)
        struct.pack_into("<I", data, br_off, b_insn)
        changed = True
    elif (br_insn & 0xFC000000) != 0x14000000:
        raise RuntimeError(f"Unexpected branch instruction at spawnAsRoot +{br_off:#x}: {br_insn:#x}")

    # 2) In bootstrap detector: redirect Unknown Jailbreak (cmp w9, #0x13; b.hi unknown) to the /var/jb/.procursus_strapped Procursus check.
    p2_raw = bytes.fromhex("3f4d00714805005468008052")
    p2_patched = bytes.fromhex("3f4d00710801005468008052")
    raw_hits = [i for i in range(0, len(data) - len(p2_raw) + 1) if data[i : i + len(p2_raw)] == p2_raw]
    patched_hits = [i for i in range(0, len(data) - len(p2_patched) + 1) if data[i : i + len(p2_patched)] == p2_patched]
    if len(raw_hits) == 1 and not patched_hits:
        struct.pack_into("<I", data, raw_hits[0] + 4, 0x54000108)  # b.hi +0x20 (Procursus rootless check)
        changed = True
    elif len(patched_hits) == 1 and not raw_hits:
        pass
    else:
        raise RuntimeError(
            f"Expected unique bootstrap detector site in {sileo_path.name}, raw={len(raw_hits)} patched={len(patched_hits)}"
        )

    # 3) In APTWrapper.performOperations (inlined spawn): force __isPlatformVersionAtLeast(2, 13, 0, 0) -> 0
    #    so Sileo prepends "giveMeRoot" and spawns giveMeRoot instead of persona(99) (which returns ESRCH silently).
    p3_prefix = bytes.fromhex("40008052a10180520200805203008052")
    p3_suffix = bytes.fromhex("fa0300aa764e00f9a0070034")
    p3_matches = []
    pos = 0
    while True:
        idx = data.find(p3_prefix, pos)
        if idx == -1:
            break
        if idx + 32 <= len(data) and data[idx + 20 : idx + 32] == p3_suffix:
            insn = struct.unpack_from("<I", data, idx + 16)[0]
            if (insn & 0xFC000000) == 0x94000000 or insn == 0xD2800000:
                p3_matches.append((idx + 16, insn))
        pos = idx + 4

    if len(p3_matches) != 1:
        raise RuntimeError(f"Expected 1 APTWrapper persona site in {sileo_path.name}, found {len(p3_matches)}")
    p3_off, p3_insn = p3_matches[0]
    if p3_insn != 0xD2800000:  # mov x0, #0x0
        struct.pack_into("<I", data, p3_off, 0xD2800000)
        changed = True

    if changed:
        sileo_path.write_bytes(data)
    return changed


def load_ramdisk_helpers():
    spec = importlib.util.spec_from_file_location("rd_mod", str(ROOT_DIR / "scripts/05_build_ramdisk.py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def extract_deb_payload(deb_path: Path, stage_jb_parent: Path, zstd_bin: str, meta_out: list) -> None:
    with tempfile.TemporaryDirectory(prefix="deb_stage_", dir="/tmp") as td_s:
        td = Path(td_s)
        subprocess.run(["ar", "x", str(deb_path)], cwd=td, check=True)

        data_tar = next(td.glob("data.tar.*"), None)
        if data_tar is None:
            raise RuntimeError(f"Missing data.tar inside {deb_path.name}")
        if data_tar.suffix == ".zst":
            subprocess.run(
                f'"{zstd_bin}" -dc "{data_tar}" | tar -C "{stage_jb_parent}" -xf -',
                shell=True,
                check=True,
            )
        elif data_tar.suffix == ".xz":
            subprocess.run(["tar", "-C", str(stage_jb_parent), "-xJf", str(data_tar)], check=True)
        elif data_tar.suffix == ".gz":
            subprocess.run(["tar", "-C", str(stage_jb_parent), "-xzf", str(data_tar)], check=True)

        ctrl_tar = next(td.glob("control.tar.*"), None)
        if ctrl_tar is None:
            return
        ctrl_dir = td / "ctrl"
        ctrl_dir.mkdir()
        if ctrl_tar.suffix == ".zst":
            subprocess.run(
                f'"{zstd_bin}" -dc "{ctrl_tar}" | tar -C "{ctrl_dir}" -xf -',
                shell=True,
                check=True,
            )
        elif ctrl_tar.suffix == ".xz":
            subprocess.run(["tar", "-C", str(ctrl_dir), "-xJf", str(ctrl_tar)], check=True)
        elif ctrl_tar.suffix == ".gz":
            subprocess.run(["tar", "-C", str(ctrl_dir), "-xzf", str(ctrl_tar)], check=True)

        ctrl_file = ctrl_dir / "control"
        if not ctrl_file.is_file():
            return
        ctrl_text = ctrl_file.read_text(encoding="utf-8", errors="replace").strip()

        pkg_name = ""
        for line in ctrl_text.splitlines():
            if line.startswith("Package:"):
                pkg_name = line.split(":", 1)[1].strip()
                break
        if not pkg_name:
            return

        # Gather normalized file list from data.tar so dpkg knows installed files.
        if data_tar.suffix == ".zst":
            flist_raw = subprocess.check_output(
                f'"{zstd_bin}" -dc "{data_tar}" | tar -tf -',
                shell=True,
                text=True,
                errors="replace",
            )
        else:
            flist_raw = subprocess.check_output(["tar", "-tf", str(data_tar)], text=True, errors="replace")

        lines = []
        for entry in flist_raw.splitlines():
            p = entry.lstrip(".")
            if not p or p == "/":
                lines.append("/.")
            else:
                lines.append(p.rstrip("/") if p != "/" else "/.")

        scripts = {}
        for sf in ctrl_dir.iterdir():
            if sf.is_file() and sf.name != "control":
                scripts[sf.name] = sf.read_bytes()

        meta_out.append({
            "package": pkg_name,
            "control": ctrl_text,
            "list_lines": lines,
            "scripts": scripts,
        })


def upsert_dpkg_metadata(jb_root: Path, meta_list: list) -> None:
    if not meta_list:
        return
    dpkg_dir = jb_root / "Library/dpkg"
    info_dir = dpkg_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    status_path = dpkg_dir / "status"

    existing_blocks = []
    if status_path.is_file():
        raw = status_path.read_text(encoding="utf-8", errors="replace")
        for blk in raw.split("\n\n"):
            b = blk.strip()
            if b:
                existing_blocks.append(b)

    by_pkg = {}
    order = []
    for blk in existing_blocks:
        pkg = ""
        for ln in blk.splitlines():
            if ln.startswith("Package:"):
                pkg = ln.split(":", 1)[1].strip()
                break
        if pkg:
            if pkg not in by_pkg:
                order.append(pkg)
            by_pkg[pkg] = blk

    for item in meta_list:
        pkg = item["package"]
        lines = item["control"].splitlines()
        out_lines = []
        has_status = False
        for ln in lines:
            if ln.startswith("Status:"):
                out_lines.append("Status: install ok installed")
                has_status = True
            else:
                out_lines.append(ln)
        if not has_status:
            out_lines.insert(1, "Status: install ok installed")
        if pkg not in by_pkg:
            order.append(pkg)
        by_pkg[pkg] = "\n".join(out_lines)

        (info_dir / f"{pkg}.list").write_text("\n".join(item["list_lines"]) + "\n", encoding="utf-8")
        for sname, sbytes in item["scripts"].items():
            sp = info_dir / f"{pkg}.{sname}"
            sp.write_bytes(sbytes)
            sp.chmod(0o755 if sname in ("postinst", "preinst", "prerm", "postrm", "extrainst_") else 0o644)

    status_path.write_text("\n\n".join(by_pkg[p] for p in order) + "\n", encoding="utf-8")


def read_existing_entitlements(bin_path: Path):
    proc = subprocess.run(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(bin_path)],
        capture_output=True,
    )
    if proc.returncode == 0 and proc.stdout and b"<plist" in proc.stdout:
        return proc.stdout

    # Fallback for ldid / ad-hoc blobs where Apple codesign reports "no signature"
    # despite an embedded CSMAGIC_EMBEDDED_ENTITLEMENTS (0xFADE7171) blob.
    try:
        raw = bin_path.read_bytes()
    except OSError:
        return None
    magic = b"\xfa\xde\x71\x71"
    pos = len(raw)
    while True:
        idx = raw.rfind(magic, 0, pos)
        if idx == -1:
            break
        if idx + 8 <= len(raw):
            _, length = struct.unpack(">II", raw[idx : idx + 8])
            if 16 <= length <= 1048576 and idx + length <= len(raw):
                xml_bytes = raw[idx + 8 : idx + length].strip(b"\x00 \t\r\n")
                if b"<plist" in xml_bytes:
                    try:
                        plistlib.loads(xml_bytes)
                        return xml_bytes + b"\n"
                    except Exception:
                        pass
        pos = idx
    return None


def resign_tree(jb_root: Path, fallback_ent: Path = None) -> int:
    signed = 0
    keep_plain_names = {"Sileo", "giveMeRoot"}
    with tempfile.TemporaryDirectory(prefix="ent_sign_", dir="/tmp") as td_s:
        td = Path(td_s)
        idx = 0
        for p in jb_root.rglob("*"):
            if not is_macho(p):
                continue
            if p.suffix == ".dylib" or ".dylib." in p.name:
                sign_binary(p, None)
            elif p.name in keep_plain_names:
                orig_ent = read_existing_entitlements(p)
                if orig_ent is not None:
                    ep = td / f"ent_{idx}.plist"
                    ep.write_bytes(orig_ent)
                    idx += 1
                    sign_binary(p, ep)
                else:
                    sign_binary(p, None)
            else:
                orig_ent = read_existing_entitlements(p)
                if orig_ent is not None:
                    ep = td / f"ent_{idx}.plist"
                    ep.write_bytes(orig_ent)
                    idx += 1
                    sign_binary(p, ep)
                else:
                    sign_binary(p, fallback_ent)
            signed += 1
    return signed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage-root", required=True, help="Target /var/jb staging directory")
    ap.add_argument("--deb-dir", default="", help="Directory of .deb archives to extract")
    ap.add_argument("--deb", action="append", default=[], help="Specific .deb file(s) to extract")
    ap.add_argument("--fallback-ent", default="", help="Fallback entitlements plist for unsigned CLI Mach-O files")
    ap.add_argument("--skip-users", action="store_true", help="Do not reset master.passwd / sshd defaults")
    args = ap.parse_args()

    jb_root = Path(args.stage_root).resolve()
    jb_root.mkdir(parents=True, exist_ok=True)
    jb_parent = jb_root.parent
    if jb_root.name != "jb" or jb_parent.name != "var":
        # Extract into temp root that contains ./var/jb, then merge if custom stage path was passed.
        extract_root = Path(tempfile.mkdtemp(prefix="deb_extract_root_", dir="/tmp"))
        cleanup_extract = True
    else:
        extract_root = jb_parent.parent
        cleanup_extract = False

    zstd_bin = shutil.which("zstd") or "/opt/homebrew/bin/zstd"

    debs = []
    if args.deb_dir:
        debs.extend(sorted(Path(args.deb_dir).glob("*.deb")))
    for d in args.deb:
        debs.append(Path(d).resolve())

    meta_list = []
    try:
        for deb in debs:
            deb = deb.resolve()
            if deb.is_file():
                extract_deb_payload(deb, extract_root, zstd_bin, meta_list)

        if cleanup_extract:
            ext_jb = extract_root / "var/jb"
            if ext_jb.is_dir():
                subprocess.run(["ditto", str(ext_jb), str(jb_root)], check=True)
    finally:
        if cleanup_extract:
            shutil.rmtree(extract_root, ignore_errors=True)

    upsert_dpkg_metadata(jb_root, meta_list)

    if not args.skip_users:
        rd_mod = load_ramdisk_helpers()
        rd_mod.configure_procursus_users_and_shells(jb_root)
        apt_conf_d = jb_root / "etc/apt/apt.conf.d"
        apt_conf_d.mkdir(parents=True, exist_ok=True)
        (apt_conf_d / "01-procursus-bins.conf").write_text(
            'Dir::Bin::gzip "/var/jb/usr/bin/gzip";\n'
            'Dir::Bin::bzip2 "/var/jb/usr/bin/bzip2";\n'
            'Dir::Bin::xz "/var/jb/usr/bin/xz";\n'
            'Dir::Bin::zstd "/var/jb/usr/bin/zstd";\n',
            encoding="utf-8",
        )

    subprocess.run(["/usr/bin/xattr", "-cr", str(jb_root)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sileo_bin = jb_root / "Applications/Sileo.app/Sileo"
    if sileo_bin.is_file():
        if patch_sileo_binary(sileo_bin):
            print("[+] Patched Sileo binary (giveMeRoot + Procursus rootless bootstrap detection)")
    fallback = Path(args.fallback_ent) if args.fallback_ent else None
    signed = resign_tree(jb_root, fallback)
    print(f"[+] Staged {len(meta_list)} debs & ad-hoc signed {signed} Mach-O files (entitlements preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
