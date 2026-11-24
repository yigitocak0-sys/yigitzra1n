#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def find_real_iphoneos_sdk() -> str:
    candidates = [[]]
    for app in sorted(Path("/Applications").glob("Xcode*.app")):
        dev = app / "Contents/Developer"
        if dev.is_dir():
            candidates.append([f"DEVELOPER_DIR={dev}"])
    for env_prefix in candidates:
        cmd = (["env", *env_prefix] if env_prefix else []) + ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        p = r.stdout.strip()
        if r.returncode == 0 and p and Path(p).is_dir():
            return p
    return ""


def patch_tbd(src: Path, dst: Path) -> None:
    txt = src.read_text(encoding="utf-8", errors="ignore")
    txt = re.sub(r"\barm64e?-macos\b", lambda m: f"{m.group(0)}, arm64-ios, arm64e-ios", txt)
    txt = re.sub(r"(/System/Library/Frameworks/[^/]+\.framework)/Versions/[A-Z]+/", r"\1/", txt)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(txt, encoding="utf-8")


def build_clt_ios_stub_sdk(cache_dir: Path) -> str:
    clt_sdk_str = ""
    r = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip() and Path(r.stdout.strip()).is_dir():
        clt_sdk_str = r.stdout.strip()
    elif Path("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk").is_dir():
        clt_sdk_str = "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
    if not clt_sdk_str:
        raise RuntimeError("Neither Xcode iPhoneOS SDK nor macOS CommandLineTools SDK was found. Run: xcode-select --install")

    clt_sdk = Path(clt_sdk_str).resolve()
    stamp_file = cache_dir / ".clt_src"
    out_sdk = cache_dir / "ios_stub_sdk"
    if (
        out_sdk.is_dir()
        and (out_sdk / "usr/lib/libSystem.B.tbd").is_file()
        and (out_sdk / "System/Library/Frameworks/Foundation.framework/Foundation.tbd").is_file()
        and stamp_file.is_file()
        and stamp_file.read_text(encoding="utf-8").strip() == str(clt_sdk)
    ):
        return str(out_sdk)

    if out_sdk.exists():
        shutil.rmtree(out_sdk)
    (out_sdk / "usr").mkdir(parents=True, exist_ok=True)
    os.symlink(clt_sdk / "usr/include", out_sdk / "usr/include")

    for sub in (Path("usr/lib"), Path("usr/lib/system")):
        sdir = clt_sdk / sub
        ddir = out_sdk / sub
        ddir.mkdir(parents=True, exist_ok=True)
        for item in sdir.iterdir():
            if item.name.endswith(".tbd") and item.is_file() and not item.is_symlink():
                patch_tbd(item, ddir / item.name)
        for item in sdir.iterdir():
            if item.is_symlink():
                tgt = os.readlink(item)
                if not (ddir / item.name).exists():
                    os.symlink(tgt, ddir / item.name)

    fw_src = clt_sdk / "System/Library/Frameworks"
    fw_dst = out_sdk / "System/Library/Frameworks"
    fw_dst.mkdir(parents=True, exist_ok=True)
    patch_fws = {"CoreFoundation.framework", "Foundation.framework", "Security.framework"}
    for item in fw_src.iterdir():
        if item.name in patch_fws:
            dfw = fw_dst / item.name
            dfw.mkdir(parents=True, exist_ok=True)
            hdr = (item / "Headers").resolve()
            if hdr.exists():
                os.symlink(hdr, dfw / "Headers")
            fw_base = item.name.replace(".framework", "")
            tbd = (item / f"{fw_base}.tbd").resolve()
            if tbd.exists():
                patch_tbd(tbd, dfw / f"{fw_base}.tbd")
        else:
            os.symlink(item, fw_dst / item.name)

    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(str(clt_sdk), encoding="utf-8")
    return str(out_sdk)


def resolve_ios_sdk(root_dir: Path) -> str:
    if os.environ.get("YIGITZRA1N_FORCE_CLT_SDK") != "1":
        real = find_real_iphoneos_sdk()
        if real:
            return real
    return build_clt_ios_stub_sdk(root_dir / ".cache")


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    print(resolve_ios_sdk(root))


if __name__ == "__main__":
    main()
