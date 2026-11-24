#!/usr/bin/env python3
import argparse
import os
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path
import pyimg4

ROOT_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_TMP = ROOT_DIR / "build/.tmp"
_DEFAULT_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMPDIR", str(_DEFAULT_TMP))
tempfile.tempdir = str(_DEFAULT_TMP)

RESTORED_EXTERNAL_PATCHES = [
    (0x0177D0, "f44fbea9fd7b01a9", "00008052c0035fd6", "await_provisioning_mesa -> return 0"),
    (0x026B24, "ff0305d1fc6f11a9", "20008052c0035fd6", "newfs_apfs skip Data Protection (-P) flag"),
    (0x026C98, "5ff00294", "00008052", "create_protected_filesystems skip MKBKeyBagCreateSystem"),
    (0x02AD64, "2fe00294", "00008052", "fixup_var skip MKBKeyBagCreateSystem"),
    (0x02AD8C, "81da0294", "00008052", "fixup_var skip MKBKeyBagCreateBackup"),
    (0x030558, "fc6fbca9f65701a9", "00008052c0035fd6", "install_root_hash -> return 0"),
    (0x035E7C, "f3020034", "17000014", "seal_system_volume skip apfs_sealvolume"),
    (0x03F180, "f44fbea9fd7b01a9", "00008052c0035fd6", "ramrod_device_has_baseband -> return 0"),
    (0x047038, "fd7bbfa9fd030091", "00008052c0035fd6", "ramrod_device_has_sep -> return 0"),
    (0x065050, "ff0301d1f65701a9", "00008052c0035fd6", "RestoredFDRSaveData -> return 0"),
    (0x0651E0, "fc6fbda9f44f01a9", "00008052c0035fd6", "RestoredFDRVerifySealedManifest -> return 0"),
    (0x065494, "f657bda9f44f01a9", "00008052c0035fd6", "RestoredFDRAutoChallengeClaim -> return 0"),
    (0x06553C, "ff4301d1f85f01a9", "00008052c0035fd6", "RestoredFDRPrepare #1 -> return 0"),
    (0x065674, "ff4302d1fc6f03a9", "00008052c0035fd6", "RestoredFDRPrepare #2 -> return 0"),
]

ALPINE_SHA512 = "$6$V7qMYJaNbVKOeh4P$iybXGZgLR3X57uggElv.cYndy9mDF0fKvu6NB57tb3Krbo/eE6/tP7.Vvf04JZ2Ni2/0x1Wwy0NaUrrEYZPpz0"

SSH_ENTITLEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>platform-application</key><true/>
    <key>com.apple.private.security.no-sandbox</key><true/>
    <key>com.apple.private.security.container-required</key><false/>
    <key>com.apple.private.security.disk-device-access</key><true/>
    <key>com.apple.private.usbdevice.setdescription</key><true/>
    <key>com.apple.USBCEntitlement</key><true/>
    <key>com.apple.unencrypted.data-volume</key><true/>
    <key>com.apple.private.vfs.snapshot</key><true/>
    <key>com.apple.security.network.server</key><true/>
    <key>com.apple.security.network.client</key><true/>
    <key>com.apple.security.iokit-user-client-class</key>
    <array>
        <string>AppleAPFSUserClient</string>
        <string>AppleNVMePassThroughUC</string>
        <string>AppleNVMeEANUC</string>
    </array>
    <key>task_for_pid-allow</key><true/>
</dict>
</plist>
"""

def find_tool(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", shutil.which(name) or ""):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError(f"Required tool not found: {name}")

def sign_binary(binary_path: Path, ent_path: Path = None) -> bytes:
    cmd = ["codesign", "-f", "-s", "-"]
    if ent_path is not None:
        cmd += ["--entitlements", str(ent_path)]
    cmd.append(str(binary_path))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    proc = subprocess.run(["codesign", "-dvvv", str(binary_path)], capture_output=True, text=True)
    info = proc.stdout + "\n" + proc.stderr
    m = re.search(r"CandidateCDHash sha256=([0-9a-fA-F]{40})", info) or re.search(r"^CDHash=([0-9a-fA-F]{40})$", info, re.M)
    if not m:
        raise RuntimeError(f"Could not parse SHA-256 CDHash from codesign for {binary_path}:\n{info}")
    return bytes.fromhex(m.group(1))

def append_to_trustcache_v1(raw_tc: bytes, extra_hashes: list) -> bytes:
    ver = struct.unpack_from("<I", raw_tc, 0)[0]
    if ver != 1:
        raise ValueError(f"Expected TrustCache v1, got v{ver}")
    uuid = raw_tc[4:20]
    cnt = struct.unpack_from("<I", raw_tc, 20)[0]
    entries = {}
    off = 24
    for _ in range(cnt):
        cdh = raw_tc[off : off + 20]
        htype, flags = raw_tc[off + 20], raw_tc[off + 21]
        entries[cdh] = (htype, flags)
        off += 22
    for cdh in extra_hashes:
        assert len(cdh) == 20
        entries.setdefault(cdh, (2, 0))
    out = bytearray(struct.pack("<I16sI", 1, uuid, len(entries)))
    for cdh in sorted(entries.keys()):
        htype, flags = entries[cdh]
        out += cdh + bytes([htype, flags])
    return bytes(out)

def find_erase_ramdisk_in_ipsw(ipsw_path: Path):
    with zipfile.ZipFile(ipsw_path, "r") as zf:
        bm = plistlib.loads(zf.read("BuildManifest.plist"))
        for ident in bm.get("BuildIdentities", []):
            info = ident.get("Info", {})
            if info.get("RestoreBehavior") == "Erase":
                rd = ident["Manifest"]["RestoreRamDisk"]["Info"]["Path"]
                tc = f"Firmware/{rd}.trustcache"
                if rd in zf.namelist() and tc in zf.namelist():
                    return rd, tc
    raise RuntimeError(f"Could not locate Erase RestoreRamDisk inside {ipsw_path}")

def patch_restored_external(bin_path: Path) -> bytes:
    ent_xml = subprocess.check_output(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(bin_path)],
        stderr=subprocess.DEVNULL,
    )
    ent_path = bin_path.with_suffix(".ent.plist")
    ent_path.write_bytes(ent_xml)

    buf = bytearray(bin_path.read_bytes())
    for off, orig_hex, repl_hex, desc in RESTORED_EXTERNAL_PATCHES:
        orig = bytes.fromhex(orig_hex)
        repl = bytes.fromhex(repl_hex)
        cur = bytes(buf[off : off + len(orig)])
        if cur == repl:
            continue
        if cur != orig:
            raise RuntimeError(
                f"restored_external mismatch @ 0x{off:x} ({desc}): got={cur.hex()} expected={orig.hex()}"
            )
        buf[off : off + len(repl)] = repl
    bin_path.write_bytes(buf)

    cdhash = sign_binary(bin_path, ent_path)
    ent_path.unlink(missing_ok=True)
    return cdhash

def attach_dmg_rw(dmg_path: Path, mnt_dir: Path) -> None:
    mnt_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "hdiutil", "attach", "-readwrite", "-noverify", "-noautoopen",
            "-mountpoint", str(mnt_dir), str(dmg_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

def detach_dmg(mnt_dir: Path) -> None:
    subprocess.run(["hdiutil", "detach", "-force", str(mnt_dir)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def is_macho(p: Path) -> bool:
    if p.is_symlink() or not p.is_file():
        return False
    try:
        with p.open("rb") as f:
            magic = f.read(4)
        return magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")
    except OSError:
        return False

def patch_sshd_disable_seatbelt(sshd_path: Path) -> bool:
    if not sshd_path.is_file():
        return False
    dis = subprocess.check_output(["otool", "-tvV", str(sshd_path)], text=True)
    patch_vas = []
    for ln in dis.splitlines():
        if "symbol stub for: _sandbox_init" in ln and "\tbl\t" in ln:
            va_str = ln.strip().split()[0]
            patch_vas.append(int(va_str, 16))
    if not patch_vas:
        return False
    buf = bytearray(sshd_path.read_bytes())
    patched = 0
    for va in patch_vas:
        foff = va - 0x100000000
        if 0 <= foff <= len(buf) - 4:
            insn = struct.unpack_from("<I", buf, foff)[0]
            if (insn >> 26) == 0x25:
                struct.pack_into("<I", buf, foff, 0x52800000)
                patched += 1
    if patched:
        sshd_path.write_bytes(buf)
        print(f"[+] Patched {patched} _sandbox_init call(s) in {sshd_path.name} (iOS pure-computation fix)")
        return True
    return False

def configure_procursus_users_and_shells(jb_root: Path) -> None:
    patch_sshd_disable_seatbelt(jb_root / "usr/sbin/sshd")
    (jb_root / "var/empty").mkdir(parents=True, exist_ok=True)
    os.chmod(jb_root / "var/empty", 0o755)
    (jb_root / "var/root").mkdir(parents=True, exist_ok=True)
    os.chmod(jb_root / "var/root", 0o700)
    (jb_root / "etc/ssh").mkdir(parents=True, exist_ok=True)

    mp_path = jb_root / "etc/master.passwd"
    pw_path = jb_root / "etc/passwd"
    if mp_path.is_file():
        has_sshd = False
        new_mp = []
        new_pw = []
        for ln in mp_path.read_text(encoding="utf-8").splitlines():
            if not ln.strip() or ln.startswith("#"):
                new_mp.append(ln)
                continue
            parts = ln.split(":")
            if parts[0] in ("root", "mobile"):
                parts[1] = ALPINE_SHA512
                parts[9] = "/var/jb/usr/bin/bash"
            if parts[0] == "sshd":
                has_sshd = True
            new_mp.append(":".join(parts))
            new_pw.append(f"{parts[0]}:*:{parts[2]}:{parts[3]}:{parts[7]}:{parts[8]}:{parts[9]}")
        if not has_sshd:
            new_mp.append("sshd:*:75:75::0:0:sshd Privilege separation:/var/jb/var/empty:/var/jb/usr/bin/false")
            new_pw.append("sshd:*:75:75:sshd Privilege separation:/var/jb/var/empty:/var/jb/usr/bin/false")
        mp_path.write_text("\n".join(new_mp) + "\n", encoding="utf-8")
        os.chmod(mp_path, 0o600)
        pw_path.write_text("\n".join(new_pw) + "\n", encoding="utf-8")
        os.chmod(pw_path, 0o644)

    for stale_db in ("pwd.db", "spwd.db"):
        (jb_root / "etc" / stale_db).unlink(missing_ok=True)

    shells_path = jb_root / "etc/shells"
    existing = shells_path.read_text(encoding="utf-8").splitlines() if shells_path.is_file() else []
    for sh in ("/var/jb/usr/bin/bash", "/var/jb/bin/bash", "/var/jb/bin/sh", "/bin/bash", "/bin/sh"):
        if sh not in existing:
            existing.append(sh)
    shells_path.write_text("\n".join(existing) + "\n", encoding="utf-8")

def build_restore_ramdisk(raw_rd: bytes, raw_tc: bytes, out_rd: Path, out_tc: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rd_restore_") as tmp_s:
        tmp = Path(tmp_s)
        dmg = tmp / "rd_erase.dmg"
        dmg.write_bytes(raw_rd)
        mnt = tmp / "mnt"
        attach_dmg_rw(dmg, mnt)
        try:
            re_path = mnt / "usr/local/bin/restored_external"
            assert re_path.is_file(), f"Missing {re_path}"
            local_re = tmp / "restored_external"
            shutil.copy2(re_path, local_re)
            cdhash = patch_restored_external(local_re)
            re_path.unlink()
            shutil.copy2(local_re, re_path)
            os.chmod(re_path, 0o755)
        finally:
            detach_dmg(mnt)

        rd_im4p = pyimg4.IM4P(fourcc="rdsk", payload=dmg.read_bytes())
        out_rd.parent.mkdir(parents=True, exist_ok=True)
        out_rd.write_bytes(rd_im4p.output())

        new_tc_raw = append_to_trustcache_v1(raw_tc, [cdhash])
        tc_im4p = pyimg4.IM4P(fourcc="rtsc", payload=new_tc_raw)
        out_tc.parent.mkdir(parents=True, exist_ok=True)
        out_tc.write_bytes(tc_im4p.output())

        print(f"[+] Built Restore RamDisk   : {out_rd} ({out_rd.stat().st_size} B)")
        print(f"[+] Built Restore TrustCache: {out_tc} (cdhash={cdhash.hex()})")

def extract_deb_to(deb_path: Path, dest_dir: Path, zstd_bin: str) -> None:
    deb_abs = deb_path.resolve()
    with tempfile.TemporaryDirectory(prefix="deb_x_") as td:
        subprocess.run(["ar", "x", str(deb_abs)], cwd=td, check=True)
        for cand in Path(td).glob("data.tar.*"):
            if cand.suffix == ".zst":
                p1 = subprocess.Popen([zstd_bin, "-dc", str(cand)], stdout=subprocess.PIPE)
                p2 = subprocess.Popen(["tar", "-C", str(dest_dir), "-xf", "-"], stdin=p1.stdout)
                p1.stdout.close()
                p2.communicate()
                if p2.returncode != 0:
                    raise RuntimeError(f"tar extract failed for {deb_path}")
            elif cand.suffix == ".xz":
                subprocess.run(["tar", "-C", str(dest_dir), "-xJf", str(cand)], check=True)
            elif cand.suffix == ".gz":
                subprocess.run(["tar", "-C", str(dest_dir), "-xzf", str(cand)], check=True)
            else:
                subprocess.run(["tar", "-C", str(dest_dir), "-xf", str(cand)], check=True)
            return

def collect_minimal_ssh_closure(stage: Path) -> list:
    jb = stage / "var/jb"
    roots = [
        "usr/sbin/sshd",
        "usr/bin/ssh-keygen",
        "usr/libexec/sftp-server",
        "usr/sbin/pwd_mkdb",
        "usr/bin/bash",
        "usr/bin/ls",
        "usr/bin/dd",
        "usr/bin/sync",
        "usr/bin/cat",
        "usr/bin/cp",
        "usr/bin/mv",
        "usr/bin/rm",
        "usr/bin/chmod",
        "usr/bin/chown",
        "usr/bin/mkdir",
        "usr/bin/kill",
    ]
    needed = set()
    queue = [jb / r for r in roots if (jb / r).exists()]
    while queue:
        cur = queue.pop()
        rel = cur.relative_to(jb)
        if rel in needed:
            continue
        needed.add(rel)
        if cur.is_symlink():
            tgt = os.readlink(cur)
            next_p = (cur.parent / tgt).resolve() if not tgt.startswith("/") else (stage / tgt.lstrip("/"))
            if next_p.exists() and jb in next_p.parents:
                queue.append(next_p)
            continue
        out = subprocess.run(["otool", "-L", str(cur)], capture_output=True, text=True).stdout
        for ln in out.splitlines()[1:]:
            dep = ln.strip().split(" ")[0]
            if dep.startswith("@rpath/"):
                cand = jb / "usr/lib" / dep[len("@rpath/"):]
                if cand.exists():
                    queue.append(cand)
            elif dep.startswith("/var/jb/"):
                cand = stage / dep.lstrip("/")
                if cand.exists():
                    queue.append(cand)
    for cfg_rel in ("etc/master.passwd", "etc/passwd", "etc/group", "etc/shells", "etc/ssh/moduli"):
        if (jb / cfg_rel).exists():
            needed.add(Path(cfg_rel))
    return sorted(needed)

def build_ramdisk_compat_dylibs(tmp_dir: Path, ent_none: bool = True) -> list:
    from prepare_ios_sdk import resolve_ios_sdk
    sdk = resolve_ios_sdk(Path(__file__).resolve().parent.parent)

    stubs = [
        (
            "libsandbox.1.dylib",
            "/usr/lib/libsandbox.1.dylib",
            "int sandbox_init(const char *p, unsigned long long f, char **e) { (void)p; (void)f; (void)e; return 0; }\n"
            "void sandbox_free_error(char *e) { (void)e; }\n",
        ),
        (
            "libresolv.9.dylib",
            "/usr/lib/libresolv.9.dylib",
            "int res_9_init(void) { return -1; }\n"
            "int res_9_query(const char *d, int c, int t, unsigned char *a, int l) { (void)d; (void)c; (void)t; (void)a; (void)l; return -1; }\n"
            "int res_9_dn_expand(const unsigned char *m, const unsigned char *e, const unsigned char *s, char *d, int l) { (void)m; (void)e; (void)s; (void)d; (void)l; return -1; }\n",
        ),
    ]
    built = []
    for fname, install_name, code in stubs:
        c_path = tmp_dir / f"{fname}.c"
        d_path = tmp_dir / fname
        c_path.write_text(code, encoding="utf-8")
        subprocess.run(
            [
                "clang", "-isysroot", sdk, "-target", "arm64-apple-ios15.0",
                "-dynamiclib", "-Os", "-Wall", "-Wextra",
                "-install_name", install_name,
                "-compatibility_version", "1.0", "-current_version", "1.0",
                str(c_path), "-o", str(d_path),
            ],
            check=True,
        )
        cdh = sign_binary(d_path, None)
        built.append((fname, d_path, cdh))
    return built

def build_ssh_ramdisk(
    raw_rd: bytes, raw_tc: bytes, procursus_dir: Path, ssh_rd_init_bin: Path, out_rd: Path, out_tc: Path
) -> None:
    zstd_bin = find_tool("zstd")
    bs_zst = procursus_dir / "bootstrap-iphoneos-arm64.tar.zst"
    deb_dir = procursus_dir / "debs"
    if not bs_zst.is_file():
        raise FileNotFoundError(
            f"Missing Procursus bootstrap at {bs_zst}. Run ./scripts/01_fetch_assets.sh first."
        )
    if not ssh_rd_init_bin.is_file():
        raise FileNotFoundError(
            f"Missing compiled ssh_rd_init binary at {ssh_rd_init_bin}. Run ./scripts/build_all.sh first."
        )

    with tempfile.TemporaryDirectory(prefix="rd_ssh_") as tmp_s:
        tmp = Path(tmp_s)
        dmg = tmp / "ssh_rd.dmg"
        dmg.write_bytes(raw_rd)

        stage = tmp / "stage"
        stage.mkdir()
        p1 = subprocess.Popen([zstd_bin, "-dc", str(bs_zst)], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["tar", "-C", str(stage), "-xf", "-"], stdin=p1.stdout)
        p1.stdout.close()
        p2.communicate()
        assert p2.returncode == 0, "Failed extracting Procursus bootstrap"

        if deb_dir.is_dir():
            for deb in sorted(deb_dir.glob("*.deb")):
                extract_deb_to(deb, stage, zstd_bin)

        jb_full = stage / "var/jb"
        assert jb_full.is_dir(), "Expected var/jb inside Procursus bootstrap"
        configure_procursus_users_and_shells(jb_full)

        jb_min = tmp / "jb_min"
        jb_min.mkdir()
        for rel in collect_minimal_ssh_closure(stage):
            src = jb_full / rel
            dst = jb_min / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                dst.symlink_to(os.readlink(src))
            else:
                shutil.copy2(src, dst)
        (jb_min / "var/empty").mkdir(parents=True, exist_ok=True)
        os.chmod(jb_min / "var/empty", 0o755)
        (jb_min / "var/root").mkdir(parents=True, exist_ok=True)
        os.chmod(jb_min / "var/root", 0o700)
        (jb_min / "etc/ssh").mkdir(parents=True, exist_ok=True)

        ent_file = tmp / "ssh_ent.plist"
        ent_file.write_text(SSH_ENTITLEMENTS, encoding="utf-8")
        cdhashes = []
        for p in jb_min.rglob("*"):
            if is_macho(p):
                if p.suffix == ".dylib" or ".dylib." in p.name:
                    cdhashes.append(sign_binary(p, None))
                else:
                    cdhashes.append(sign_binary(p, ent_file))

        local_init = tmp / "ssh_rd_init"
        shutil.copy2(ssh_rd_init_bin, local_init)
        cdhashes.append(sign_binary(local_init, ent_file))

        rd_reset_src = ssh_rd_init_bin.parent / "rd_reset_apfs"
        local_reset = None
        if rd_reset_src.is_file():
            local_reset = tmp / "rd_reset_apfs"
            shutil.copy2(rd_reset_src, local_reset)
            cdhashes.append(sign_binary(local_reset, ent_file))

        compat_dylibs = build_ramdisk_compat_dylibs(tmp)
        for _fname, _dpath, _cdh in compat_dylibs:
            cdhashes.append(_cdh)

        mnt_new = tmp / "mnt_new"
        attach_dmg_rw(dmg, mnt_new)
        try:
            usr_lib = mnt_new / "usr/lib"
            usr_lib.mkdir(parents=True, exist_ok=True)
            for fname, dpath, _cdh in compat_dylibs:
                dst_lib = usr_lib / fname
                if not dst_lib.exists():
                    shutil.copy2(dpath, dst_lib)
                    os.chmod(dst_lib, 0o755)
            fw_dir = mnt_new / "usr/standalone/firmware"
            if fw_dir.is_dir():
                keep_fw = {"S3E", "BFH", "storagetunables"}
                for entry in list(fw_dir.iterdir()):
                    if entry.name in keep_fw:
                        continue
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
            shutil.rmtree(mnt_new / "usr/standalone/update", ignore_errors=True)
            for lproj in (mnt_new / "System/Library").rglob("*.lproj"):
                if lproj.is_dir() and lproj.name not in ("en.lproj", "English.lproj", "Base.lproj"):
                    shutil.rmtree(lproj, ignore_errors=True)

            dst_jb = mnt_new / "var/jb"
            dst_jb.parent.mkdir(parents=True, exist_ok=True)
            if dst_jb.exists():
                shutil.rmtree(dst_jb)
            subprocess.run(["cp", "-a", str(jb_min), str(dst_jb)], check=True)
            assert (dst_jb / "usr/sbin/sshd").stat().st_size > 100_000, "sshd truncated in SSH RamDisk"
            assert (dst_jb / "usr/lib/libcrypto.3.dylib").stat().st_size > 1_000_000, "libcrypto.3.dylib truncated in SSH RamDisk"

            (mnt_new / "bin").mkdir(parents=True, exist_ok=True)
            for sh_name in ("sh", "bash"):
                sp = mnt_new / "bin" / sh_name
                if not sp.exists() and not sp.is_symlink():
                    sp.symlink_to("/var/jb/usr/bin/bash")
            (mnt_new / "usr/libexec").mkdir(parents=True, exist_ok=True)
            sftp_link = mnt_new / "usr/libexec/sftp-server"
            if not sftp_link.exists() and not sftp_link.is_symlink():
                sftp_link.symlink_to("/var/jb/usr/libexec/sftp-server")

            re_path = mnt_new / "usr/local/bin/restored_external"
            if re_path.is_file():
                re_path.unlink()

            shutil.copy2(local_init, re_path)
            os.chmod(re_path, 0o755)
            init_dst = mnt_new / "usr/local/bin/ssh_rd_init"
            shutil.copy2(local_init, init_dst)
            os.chmod(init_dst, 0o755)
            if local_reset is not None:
                rst_dst = mnt_new / "usr/local/bin/rd_reset_apfs"
                shutil.copy2(local_reset, rst_dst)
                os.chmod(rst_dst, 0o755)
        finally:
            detach_dmg(mnt_new)

        rd_im4p = pyimg4.IM4P(fourcc="rdsk", payload=dmg.read_bytes())
        out_rd.parent.mkdir(parents=True, exist_ok=True)
        out_rd.write_bytes(rd_im4p.output())

        new_tc_raw = append_to_trustcache_v1(raw_tc, cdhashes)
        tc_im4p = pyimg4.IM4P(fourcc="rtsc", payload=new_tc_raw)
        out_tc.parent.mkdir(parents=True, exist_ok=True)
        out_tc.write_bytes(tc_im4p.output())

        print(f"[+] Built SSH RamDisk       : {out_rd} ({out_rd.stat().st_size} B)")
        print(f"[+] Built SSH TrustCache    : {out_tc} (+{len(cdhashes)} signed Mach-O CDHashes)")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ipsw-ipad5", required=True, help="Stock iPad 5 16.7.16 IPSW")
    ap.add_argument("--out-restore-rd", required=True, help="Output Restore RamDisk IM4P (098-68700-067.dmg)")
    ap.add_argument("--out-restore-tc", required=True, help="Output Restore TrustCache IM4P")
    ap.add_argument("--procursus-dir", default="", help="Procursus cache dir for SSH RamDisk")
    ap.add_argument("--ssh-rd-init", default=str(ROOT_DIR / "build/bin/ssh_rd_init"), help="Compiled native Mach-O ssh_rd_init")
    ap.add_argument("--out-ssh-rd", default="", help="Output SSH RamDisk IM4P")
    ap.add_argument("--out-ssh-tc", default="", help="Output SSH RamDisk TrustCache IM4P")
    args = ap.parse_args()

    ipsw = Path(args.ipsw_ipad5)
    rd_member, tc_member = find_erase_ramdisk_in_ipsw(ipsw)
    print(f"[*] Using stock Erase RamDisk: {rd_member} + {tc_member}")

    with zipfile.ZipFile(ipsw, "r") as zf:
        im_rd = pyimg4.IM4P(zf.read(rd_member))
        if im_rd.payload.compression != pyimg4.Compression.NONE:
            im_rd.payload.decompress()
        raw_rd = bytes(im_rd.payload.data)

        im_tc = pyimg4.IM4P(zf.read(tc_member))
        if im_tc.payload.compression != pyimg4.Compression.NONE:
            im_tc.payload.decompress()
        raw_tc = bytes(im_tc.payload.data)

    build_restore_ramdisk(raw_rd, raw_tc, Path(args.out_restore_rd), Path(args.out_restore_tc))

    if args.out_ssh_rd and args.out_ssh_tc:
        if not args.procursus_dir:
            raise ValueError("--procursus-dir is required when building SSH RamDisk")
        build_ssh_ramdisk(
            raw_rd, raw_tc, Path(args.procursus_dir), Path(args.ssh_rd_init),
            Path(args.out_ssh_rd), Path(args.out_ssh_tc),
        )

if __name__ == "__main__":
    main()
