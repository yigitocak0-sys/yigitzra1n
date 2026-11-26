#!/usr/bin/env python3
import argparse
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

DEFAULT_REPO_URL = "https://github.com/yigitocak0-sys/yigitzra1n.git"
DEFAULT_TARBALL_URL = "https://github.com/yigitocak0-sys/yigitzra1n/archive/refs/heads/main.tar.gz"
DEFAULT_SHA_URL = "https://api.github.com/repos/yigitocak0-sys/yigitzra1n/commits/main"
DEFAULT_VERSION_URL = "https://raw.githubusercontent.com/yigitocak0-sys/yigitzra1n/main/VERSION"
DEFAULT_BRANCH = "main"

PRESERVE_TOP = {
    ".git",
    ".cache",
    ".venv",
    "build",
    "ipsw",
    "out",
    "work",
    ".DS_Store",
    ".yigitzra1n_init",
    ".yigitzra1n_commit",
    ".yigitzra1n_installed",
    ".yigitzra1n_source_dir",
}

EXIT_OK = 0
EXIT_ERR = 1
EXIT_UPDATED = 10


def has_working_git() -> bool:
    if not shutil.which("git"):
        return False
    try:
        r = subprocess.run(
            ["xcode-select", "-p"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def run_cmd(cmd, cwd=None, timeout=10, env=None):
    merged_env = os.environ.copy()
    merged_env["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=merged_env,
    )


def get_remote_sha(repo_url: str, sha_url: str, branch: str, timeout: int) -> str:
    if has_working_git():
        try:
            r = run_cmd(
                ["git", "ls-remote", "--heads", repo_url, f"refs/heads/{branch}"],
                timeout=timeout,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.strip().split()
                    if parts and re.fullmatch(r"[0-9a-fA-F]{40}", parts[0]):
                        return parts[0].lower()
        except Exception:
            pass

    req = urllib.request.Request(
        sha_url,
        headers={
            "Accept": "application/vnd.github.sha",
            "User-Agent": "yigitzra1n-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            val = resp.read().decode("utf-8", errors="ignore").strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", val):
                return val.lower()
    except Exception:
        pass
    if shutil.which("curl"):
        try:
            r = run_cmd(
                [
                    "curl",
                    "-fsSL",
                    "--connect-timeout",
                    str(timeout),
                    "--max-time",
                    str(max(timeout, 10)),
                    "-H",
                    "Accept: application/vnd.github.sha",
                    "-A",
                    "yigitzra1n-updater",
                    sha_url,
                ],
                timeout=max(timeout + 2, 12),
            )
            val = r.stdout.strip()
            if r.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40}", val):
                return val.lower()
        except Exception:
            pass
    return ""


def ensure_exec_permissions(root: pathlib.Path) -> None:
    targets = [root / "yigitzra1n.sh"]
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        for p in scripts_dir.iterdir():
            if p.suffix in {".sh", ".py"} and p.is_file():
                targets.append(p)
    for t in targets:
        if t.is_file() and not t.is_symlink():
            try:
                mode = t.stat().st_mode
                t.chmod(mode | 0o755)
            except OSError:
                pass


def update_git_repo(
    root: pathlib.Path,
    repo_url: str,
    sha_url: str,
    branch: str,
    timeout: int,
    explicit: bool,
    check_only: bool,
    force: bool,
) -> int:
    head_r = run_cmd(["git", "rev-parse", "HEAD"], cwd=root, timeout=5)
    if head_r.returncode != 0:
        return EXIT_ERR if explicit else EXIT_OK
    local_sha = head_r.stdout.strip().lower()

    remote_sha = get_remote_sha(repo_url, sha_url, branch, timeout)
    if not remote_sha:
        msg = "[!] Update check skipped (GitHub unreachable)."
        print(msg, file=sys.stderr if explicit else sys.stdout)
        return EXIT_ERR if explicit else EXIT_OK

    if local_sha == remote_sha:
        if explicit or check_only:
            print(f"[+] yigitzra1n is already up to date ({local_sha[:7]}).")
        return EXIT_OK

    fetch_r = run_cmd(["git", "fetch", "--quiet", repo_url, branch], cwd=root, timeout=max(timeout, 15))
    if fetch_r.returncode != 0:
        print("[!] Failed to fetch updates from GitHub.", file=sys.stderr if explicit else sys.stdout)
        return EXIT_ERR if explicit else EXIT_OK

    fh_r = run_cmd(["git", "rev-parse", "FETCH_HEAD"], cwd=root, timeout=5)
    if fh_r.returncode != 0:
        return EXIT_ERR if explicit else EXIT_OK
    fetch_sha = fh_r.stdout.strip().lower()

    ahead_r = run_cmd(["git", "rev-list", "--count", f"{fetch_sha}..HEAD"], cwd=root, timeout=5)
    behind_r = run_cmd(["git", "rev-list", "--count", f"HEAD..{fetch_sha}"], cwd=root, timeout=5)
    try:
        ahead = int(ahead_r.stdout.strip())
        behind = int(behind_r.stdout.strip())
    except ValueError:
        return EXIT_ERR if explicit else EXIT_OK

    if ahead > 0 and behind == 0:
        if explicit or check_only:
            print(f"[*] Local repo is ahead of origin ({local_sha[:7]}); keeping local commits.")
        return EXIT_OK

    if ahead > 0 and behind > 0:
        print("[!] Local branch diverged from origin; skipping auto-update.")
        return EXIT_ERR if explicit else EXIT_OK

    if behind == 0:
        if explicit or check_only:
            print(f"[+] yigitzra1n is already up to date ({local_sha[:7]}).")
        return EXIT_OK

    if check_only:
        print(f"[*] Update available: {local_sha[:7]} -> {fetch_sha[:7]}")
        return EXIT_OK

    dirty_r = run_cmd(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, timeout=5)
    if dirty_r.returncode == 0 and dirty_r.stdout.strip():
        run_cmd(["git", "reset", "--hard", "HEAD"], cwd=root, timeout=10)

    print(f"[*] Updating yigitzra1n from GitHub ({local_sha[:7]} -> {fetch_sha[:7]})...")
    merge_r = run_cmd(["git", "merge", "--ff-only", fetch_sha], cwd=root, timeout=15)
    if merge_r.returncode != 0:
        reset_r = run_cmd(["git", "reset", "--hard", fetch_sha], cwd=root, timeout=15)
        if reset_r.returncode != 0:
            print("[!] Force update failed; please check git status.", file=sys.stderr)
            return EXIT_ERR if explicit else EXIT_OK

    ensure_exec_permissions(root)
    print(f"[+] Updated yigitzra1n to {fetch_sha[:7]}.")
    return EXIT_UPDATED


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(65536)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def safe_extract_tarball(tar_path: pathlib.Path, dest_dir: pathlib.Path) -> pathlib.Path:
    dest_resolved = dest_dir.resolve()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            target = (dest_dir / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise RuntimeError(f"Unsafe tar member path: {member.name}")
        tf.extractall(dest_dir)
    entries = [p for p in dest_dir.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest_dir


def tree_matches_source(src_root: pathlib.Path, dst_root: pathlib.Path) -> bool:
    for src_item in src_root.rglob("*"):
        rel = src_item.relative_to(src_root)
        if rel.parts and rel.parts[0] in PRESERVE_TOP:
            continue
        dst_item = dst_root / rel
        if src_item.is_symlink():
            if not dst_item.is_symlink():
                return False
            if os.readlink(src_item) != os.readlink(dst_item):
                return False
        elif src_item.is_dir():
            if not dst_item.is_dir() or dst_item.is_symlink():
                return False
        elif src_item.is_file():
            if not dst_item.is_file() or dst_item.is_symlink():
                return False
            if sha256_file(src_item) != sha256_file(dst_item):
                return False
    return True


def sync_from_source(src_root: pathlib.Path, dst_root: pathlib.Path) -> None:
    for src_item in sorted(src_root.rglob("*")):
        rel = src_item.relative_to(src_root)
        if rel.parts and rel.parts[0] in PRESERVE_TOP:
            continue
        dst_item = dst_root / rel
        if src_item.is_symlink():
            link_target = os.readlink(src_item)
            if dst_item.exists() or dst_item.is_symlink():
                if dst_item.is_dir() and not dst_item.is_symlink():
                    shutil.rmtree(dst_item)
                else:
                    dst_item.unlink()
            dst_item.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link_target, dst_item)
        elif src_item.is_dir():
            if dst_item.is_symlink():
                dst_item.unlink()
            dst_item.mkdir(parents=True, exist_ok=True)
        elif src_item.is_file():
            dst_item.parent.mkdir(parents=True, exist_ok=True)
            if dst_item.is_symlink():
                dst_item.unlink()
            fd, tmp_name = tempfile.mkstemp(prefix=".upd_", dir=str(dst_item.parent))
            os.close(fd)
            tmp_path = pathlib.Path(tmp_name)
            try:
                shutil.copy2(src_item, tmp_path)
                os.replace(tmp_path, dst_item)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise


def update_zip_install(
    root: pathlib.Path,
    repo_url: str,
    sha_url: str,
    tarball_url: str,
    branch: str,
    timeout: int,
    explicit: bool,
    check_only: bool,
    force: bool,
) -> int:
    commit_file = root / ".yigitzra1n_commit"
    local_sha = ""
    if commit_file.is_file():
        local_sha = commit_file.read_text(encoding="utf-8", errors="ignore").strip().lower()

    remote_sha = get_remote_sha(repo_url, sha_url, branch, timeout)
    if not remote_sha:
        print("[!] Update check skipped (GitHub unreachable).", file=sys.stderr if explicit else sys.stdout)
        return EXIT_ERR if explicit else EXIT_OK

    if local_sha and local_sha == remote_sha and not force:
        if explicit or check_only:
            print(f"[+] yigitzra1n is already up to date ({remote_sha[:7]}).")
        return EXIT_OK

    if check_only and local_sha:
        print(f"[*] Update available: {local_sha[:7]} -> {remote_sha[:7]}")
        return EXIT_OK

    with tempfile.TemporaryDirectory(prefix="yigitzra1n_upd_") as td:
        td_path = pathlib.Path(td)
        tar_path = td_path / "repo.tar.gz"
        extract_dir = td_path / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)

        req = urllib.request.Request(tarball_url, headers={"User-Agent": "yigitzra1n-updater"})
        try:
            with urllib.request.urlopen(req, timeout=max(timeout, 20)) as resp, tar_path.open("wb") as out:
                shutil.copyfileobj(resp, out)
        except Exception as e:
            if shutil.which("curl"):
                cr = run_cmd(
                    [
                        "curl",
                        "-fsSL",
                        "--connect-timeout",
                        str(timeout),
                        "--max-time",
                        str(max(timeout, 30)),
                        "-A",
                        "yigitzra1n-updater",
                        "-o",
                        str(tar_path),
                        tarball_url,
                    ],
                    timeout=max(timeout + 5, 35),
                )
                if cr.returncode != 0 or not tar_path.is_file():
                    print(f"[!] Update download failed: {e}", file=sys.stderr if explicit else sys.stdout)
                    return EXIT_ERR if explicit else EXIT_OK
            else:
                print(f"[!] Update download failed: {e}", file=sys.stderr if explicit else sys.stdout)
                return EXIT_ERR if explicit else EXIT_OK

        src_root = safe_extract_tarball(tar_path, extract_dir)
        if not (src_root / "yigitzra1n.sh").is_file():
            print("[!] Downloaded archive does not look like yigitzra1n.", file=sys.stderr)
            return EXIT_ERR if explicit else EXIT_OK

        if not force and tree_matches_source(src_root, root):
            commit_file.write_text(remote_sha + "\n", encoding="utf-8")
            if explicit or check_only:
                print(f"[+] yigitzra1n is already up to date ({remote_sha[:7]}).")
            return EXIT_OK

        if check_only:
            print(f"[*] Update available (remote {remote_sha[:7]}).")
            return EXIT_OK

        label = local_sha[:7] if local_sha else "zip"
        print(f"[*] Updating yigitzra1n from GitHub ({label} -> {remote_sha[:7]})...")
        sync_from_source(src_root, root)
        ensure_exec_permissions(root)
        commit_file.write_text(remote_sha + "\n", encoding="utf-8")
        print(f"[+] Updated yigitzra1n to {remote_sha[:7]}.")
        return EXIT_UPDATED


def get_remote_version(version_url: str, timeout: int) -> str:
    req = urllib.request.Request(
        version_url,
        headers={
            "User-Agent": "yigitzra1n-updater",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            val = resp.read().decode("utf-8", errors="ignore").strip()
            if val and len(val) <= 32 and "\n" not in val:
                return val
    except Exception:
        pass
    if shutil.which("curl"):
        try:
            r = run_cmd(
                [
                    "curl",
                    "-fsSL",
                    "--connect-timeout",
                    str(timeout),
                    "--max-time",
                    str(max(timeout, 10)),
                    "-A",
                    "yigitzra1n-updater",
                    version_url,
                ],
                timeout=max(timeout + 2, 12),
            )
            val = r.stdout.strip()
            if r.returncode == 0 and val and len(val) <= 32 and "\n" not in val:
                return val
        except Exception:
            pass
    return ""


def get_local_version_info(root: pathlib.Path) -> tuple:
    ver_file = root / "VERSION"
    local_ver = ver_file.read_text(encoding="utf-8", errors="ignore").strip() if ver_file.is_file() else "unknown"
    local_sha = ""
    if (root / ".git").exists() and has_working_git():
        r = run_cmd(["git", "rev-parse", "HEAD"], cwd=root, timeout=5)
        if r.returncode == 0:
            local_sha = r.stdout.strip().lower()
    if not local_sha:
        cf = root / ".yigitzra1n_commit"
        if cf.is_file():
            local_sha = cf.read_text(encoding="utf-8", errors="ignore").strip().lower()
    return local_ver, local_sha


def show_version(
    root: pathlib.Path,
    repo_url: str,
    sha_url: str,
    version_url: str,
    branch: str,
    timeout: int,
) -> int:
    local_ver, local_sha = get_local_version_info(root)
    remote_sha = get_remote_sha(repo_url, sha_url, branch, timeout)
    effective_ver_url = version_url
    if remote_sha and "YIGITZRA1N_VERSION_URL" not in os.environ and version_url == DEFAULT_VERSION_URL:
        effective_ver_url = f"https://raw.githubusercontent.com/yigitocak0-sys/yigitzra1n/{remote_sha}/VERSION"
    remote_ver = get_remote_version(effective_ver_url, timeout)
    if not remote_ver and effective_ver_url != version_url:
        remote_ver = get_remote_version(version_url, timeout)

    local_label = f"{local_ver} ({local_sha[:7]})" if local_sha else local_ver
    print(f"Installed version : {local_label}")
    print(f"Install path      : {root}")

    if not remote_sha and not remote_ver:
        print("Latest version    : unavailable (offline or GitHub unreachable)")
        return EXIT_OK

    latest_ver = remote_ver or local_ver
    latest_label = f"{latest_ver} ({remote_sha[:7]})" if remote_sha else latest_ver
    print(f"Latest version    : {latest_label}")

    if remote_sha and local_sha and remote_sha == local_sha:
        print("Status            : Up to date.")
    elif remote_ver and local_ver != remote_ver:
        print("Status            : Update available! Run 'yigitzra1n update' to update.")
    elif remote_sha and local_sha and remote_sha != local_sha:
        if (root / ".git").exists() and has_working_git():
            ahead_r = run_cmd(["git", "rev-list", "--count", "HEAD"], cwd=root, timeout=5)
            # Check locally known relation without modifying working tree
            status_r = run_cmd(["git", "branch", "-vv"], cwd=root, timeout=5)
            if status_r.returncode == 0 and "[origin/" in status_r.stdout and "ahead " in status_r.stdout:
                print("Status            : Local repo has unpushed/newer commits than GitHub.")
                return EXIT_OK
        print("Status            : Newer commit available on GitHub! Run 'yigitzra1n update' to update.")
    else:
        print("Status            : Up to date.")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--mode", choices=["auto", "check", "apply", "version"], default="auto")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    root = pathlib.Path(args.root).resolve()
    repo_url = os.environ.get("YIGITZRA1N_UPDATE_REPO_URL", DEFAULT_REPO_URL)
    tarball_url = os.environ.get("YIGITZRA1N_UPDATE_TARBALL_URL", DEFAULT_TARBALL_URL)
    sha_url = os.environ.get("YIGITZRA1N_UPDATE_SHA_URL", DEFAULT_SHA_URL)
    version_url = os.environ.get("YIGITZRA1N_VERSION_URL", DEFAULT_VERSION_URL)
    branch = os.environ.get("YIGITZRA1N_UPDATE_BRANCH", DEFAULT_BRANCH)
    try:
        timeout = int(os.environ.get("YIGITZRA1N_UPDATE_TIMEOUT", "6"))
    except ValueError:
        timeout = 6

    if args.mode == "version":
        return show_version(root, repo_url, sha_url, version_url, branch, timeout)

    explicit = args.mode == "apply"
    check_only = args.mode == "check"

    if (root / ".git").exists() and has_working_git():
        return update_git_repo(root, repo_url, sha_url, branch, timeout, explicit, check_only, args.force)
    return update_zip_install(root, repo_url, sha_url, tarball_url, branch, timeout, explicit, check_only, args.force)


if __name__ == "__main__":
    sys.exit(main())
