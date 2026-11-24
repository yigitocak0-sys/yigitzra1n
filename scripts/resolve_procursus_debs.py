#!/usr/bin/env python3
import argparse
import hashlib
import os
import re
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://apt.procurs.us/"
PACKAGES_XZ_URL = BASE_URL + "dists/1900/main/binary-iphoneos-arm64/Packages.xz"
BOOTSTRAP_URL = BASE_URL + "bootstraps/1900/bootstrap-iphoneos-arm64.tar.zst"
BOOTSTRAP_SHA256 = "8354c3aa1ecdad8ebc47d9a76dfca6f830a2b757278068bd33b98bf1d638a9cb"

DEFAULT_PACKAGES = [
    "gawk",
    "gzip",
    "zstd",
    "curl",
    "less",
    "file",
    "nano",
    "openssh-client",
    "openssh-sftp-server",
    "top",
    "lsof",
    "htop",
    "rsync",
]

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def download_if_needed(url: str, dest: Path, expected_sha256: str = "") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and expected_sha256:
        if sha256_file(dest) == expected_sha256:
            return dest
        dest.unlink()
    elif dest.exists() and not expected_sha256:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "procursus-bootstrap-helper/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as out:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            out.write(chunk)
    if expected_sha256:
        got = sha256_file(tmp)
        if got != expected_sha256:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 uyuşmazlığı ({dest.name}): beklenen={expected_sha256}, gelen={got}")
    tmp.replace(dest)
    return dest

def _ver_key(ver: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"([0-9]+)", ver)]

def parse_packages(raw_text: str):
    db = {}
    provides_map = {}
    cur = {}

    def commit(entry):
        p = entry.get("Package")
        if not p:
            return
        if p not in db or _ver_key(entry.get("Version", "")) > _ver_key(db[p].get("Version", "")):
            db[p] = dict(entry)
        for prov in entry.get("Provides", "").split(","):
            prov = prov.strip()
            if prov:
                provides_map.setdefault(prov.split()[0], []).append(p)

    for line in raw_text.splitlines():
        if not line.strip():
            commit(cur)
            cur = {}
        elif ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            cur[k.strip()] = v.strip()
    commit(cur)
    return db, provides_map

def parse_installed_from_bootstrap(bootstrap_tar_zst: Path) -> set:
    import subprocess
    cmd = ["sh", "-c", f'zstd -dc "{bootstrap_tar_zst}" | tar -xOf - ./var/jb/Library/dpkg/status']
    out = subprocess.check_output(cmd, text=True, errors="replace")
    installed = set()
    for line in out.splitlines():
        if line.startswith("Package:"):
            installed.add(line.split(":", 1)[1].strip())
    return installed

def resolve_tree(wanted, db, provides_map, installed):
    order = []
    seen = set(installed)

    def walk(pkg):
        if pkg in seen:
            return
        if pkg not in db:
            if pkg in provides_map:
                pkg = provides_map[pkg][0]
                if pkg in seen:
                    return
            else:
                raise KeyError(f"Depoda paket bulunamadı: {pkg}")
        seen.add(pkg)
        deps_raw = db[pkg].get("Pre-Depends", "") + "," + db[pkg].get("Depends", "")
        for group in deps_raw.split(","):
            group = group.strip()
            if not group:
                continue
            alts = [re.split(r"[\s(:]", a.strip())[0] for a in group.split("|")]
            if any(a in seen for a in alts):
                continue
            walk(alts[0])
        order.append(pkg)

    for w in wanted:
        walk(w)
    return order

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="/tmp/procursus_cli_cache")
    ap.add_argument("--packages", nargs="*", default=DEFAULT_PACKAGES)
    ap.add_argument("--list-only", action="store_true", help=".deb dosyalarını indirmeden sadece planı yazdır")
    args = ap.parse_args()

    cache = Path(args.cache_dir)
    bs_path = cache / "bootstrap-iphoneos-arm64.tar.zst"
    print(f"[*] Bootstrap kontrol ediliyor: {bs_path}")
    download_if_needed(BOOTSTRAP_URL, bs_path, BOOTSTRAP_SHA256)

    pkg_xz = cache / "Packages.xz"
    print(f"[*] Paket indeksi indiriliyor: {PACKAGES_XZ_URL}")
    download_if_needed(PACKAGES_XZ_URL, pkg_xz)

    import lzma
    raw_packages = lzma.decompress(pkg_xz.read_bytes()).decode("utf-8", errors="replace")
    db, provides_map = parse_packages(raw_packages)
    installed = parse_installed_from_bootstrap(bs_path)
    order = resolve_tree(args.packages, db, provides_map, installed)

    deb_dir = cache / "debs"
    total = 0
    for p in order:
        d = db[p]
        sz = int(d.get("Size", 0))
        total += sz
        fn = d["Filename"]
        sha = d.get("SHA256", "")
        print(f"  + {p:22s} {d.get('Version', '?'):16s} ({sz} B)")
        if not args.list_only:
            out_deb = deb_dir / os.path.basename(fn)
            download_if_needed(BASE_URL + fn, out_deb, sha)

    print(f"[OK] Toplam {len(order)} ek paket ({total / 1024 / 1024:.2f} MB) hazır: {deb_dir}")

if __name__ == "__main__":
    main()
