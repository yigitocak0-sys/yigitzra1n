#!/usr/bin/env python3
import argparse
import ctypes
import errno
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT_DIR / "build"
ENGINE_DIR = BUILD_DIR / "engine"
MOD_DIR = ENGINE_DIR / "modules"
LOCAL_API_DIR = BUILD_DIR / "local_api"

HWINIT_MOD = BUILD_DIR / "bin/hw_init_probe.bin"
FAST_DNS = BUILD_DIR / "bin/fast_dns.dylib"
ORIG_SEP = MOD_DIR / "mod_9.bin"
ORIG_KPF = MOD_DIR / "mod_5.bin"

DEFAULT_BOOTARGS_QUIET = "rd=disk0s1s1 wlan.dk=0 v3.internal=1 wlan.factory=1 skipFWValidation=1"
DEFAULT_BOOTARGS_VERBOSE = "rd=disk0s1s1 -v wlan.dk=0 v3.internal=1 wlan.factory=1 skipFWValidation=1"
SSH_RAMDISK_BOOTARGS = "rd=md0 -v wdt=-1"

def load_libusb():
    for cand in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
        if os.path.isfile(cand):
            lib = ctypes.CDLL(cand)
            break
    else:
        raise RuntimeError("libusb-1.0.dylib not found (install via: brew install libusb)")
    lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_init.restype = ctypes.c_int
    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    lib.libusb_exit.restype = None
    lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
    lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_claim_interface.restype = ctypes.c_int
    lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_release_interface.restype = ctypes.c_int
    lib.libusb_close.argtypes = [ctypes.c_void_p]
    lib.libusb_close.restype = None
    lib.libusb_control_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
        ctypes.c_uint16, ctypes.c_char_p, ctypes.c_uint16, ctypes.c_uint,
    ]
    lib.libusb_control_transfer.restype = ctypes.c_int
    lib.libusb_bulk_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_char_p, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
    ]
    lib.libusb_bulk_transfer.restype = ctypes.c_int
    return lib

def find_irecovery() -> str:
    for cand in ("/opt/homebrew/bin/irecovery", "/usr/local/bin/irecovery", shutil.which("irecovery") or ""):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError("irecovery not found (install via: brew install libirecovery)")

def repair_local_api_if_needed() -> int:
    LOCAL_API_DIR.mkdir(parents=True, exist_ok=True)
    link_path = LOCAL_API_DIR / "local_1588.ipsw"
    if not link_path.is_file():
        ipsw_dir = Path(os.environ.get("YIGITZRA1N_IPSW_DIR") or (ROOT_DIR / "ipsw"))
        matches = sorted(
            [p for p in ipsw_dir.glob("iPhone_4.7_15.8.8_*_Restore.ipsw") if p.is_file()],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            raise FileNotFoundError(
                f"Missing {link_path} and no stock iOS 15.8.8 IPSW in {ipsw_dir}. Run './yigitzra1n.sh prepare' first."
            )
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(matches[0].resolve())
    return link_path.stat().st_size

def probe_local_server(expected_size: int) -> bool:
    req = urllib.request.Request(
        "http://127.0.0.1:8765/local_1588.ipsw",
        headers={"Range": "bytes=0-3"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status not in (200, 206):
                return False
            data = resp.read(4)
            if data[:2] != b"PK":
                return False
            cr = resp.headers.get("Content-Range", "")
            if "/" in cr:
                try:
                    return int(cr.rsplit("/", 1)[1]) == expected_size
                except ValueError:
                    return False
            cl = resp.headers.get("Content-Length", "")
            if cl.isdigit():
                return int(cl) == expected_size
            return True
    except Exception:
        return False

def ensure_local_server() -> None:
    expected_size = repair_local_api_if_needed()
    if probe_local_server(expected_size):
        return

    r = subprocess.run(["lsof", "-ti", ":8765"], capture_output=True, text=True)
    for pid_s in r.stdout.split():
        if not pid_s.isdigit():
            continue
        ps = subprocess.run(["ps", "-p", pid_s, "-o", "command="], capture_output=True, text=True)
        if "local_server.py" in ps.stdout:
            try:
                os.kill(int(pid_s), signal.SIGTERM)
            except OSError:
                pass
    time.sleep(0.3)

    print("[*] Starting local firmware metadata server (127.0.0.1:8765)...", flush=True)
    log_path = BUILD_DIR / "local_server.log"
    with log_path.open("ab") as logf:
        subprocess.Popen(
            ["python3", str(ROOT_DIR / "src/local_server.py"), "--root", str(LOCAL_API_DIR)],
            stdout=logf, stderr=logf, start_new_session=True,
        )
    for _ in range(15):
        if probe_local_server(expected_size):
            return
        time.sleep(0.2)
    raise RuntimeError("Local firmware server on 127.0.0.1:8765 failed health check for local_1588.ipsw.")

def find_block_file(explicit: str = "") -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"Block file not found: {p}")
        return p
    block_dir = ENGINE_DIR / "block"
    for pattern in ("*pteblock*.bin", "*shcblock*.bin", "*.bin"):
        matches = sorted(block_dir.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
    raise FileNotFoundError(
        f"No device boot block found in {block_dir}. Run `./yigitzra1n.sh install` first to capture your device block."
    )

def check_and_pwn_dfu(force: bool = False) -> None:
    irec = find_irecovery()
    r = subprocess.run([irec, "-q"], capture_output=True, text=True)
    out = r.stdout + r.stderr
    if "MODE: DFU" not in out:
        raise RuntimeError(f"{out.strip()}\nDevice is not in DFU mode.")

    low = out.lower()
    bdid = ""
    for line in low.splitlines():
        if line.startswith("bdid:"):
            bdid = line.split(":", 1)[1].strip()
            break
    if ("cpid: 0x8003" in low or "cpid: 0x8000" in low) and bdid and bdid != "0x04":
        if bdid == "0x06":
            raise RuntimeError(
                "iPhone 6s Plus (BDID 0x06 / N66AP-N66mAP) is NOT supported yet; this build only targets 4.7-inch iPhone 6s (BDID 0x04)."
            )
        if bdid == "0x02":
            raise RuntimeError(
                "iPhone SE (1st gen / BDID 0x02) is NOT supported; this build only targets 4.7-inch iPhone 6s (BDID 0x04)."
            )
        raise RuntimeError(
            f"Unsupported A9 board BDID {bdid}; expected iPhone 6s (BDID 0x04)."
        )
    if "cpid: 0x8000" in low and not force and os.environ.get("YIGITZRA1N_S8000_CONFIRMED") != "1":
        if sys.stdin.isatty():
            print("\n======================================================================", flush=True)
            print("  NOTE!!! Your phone is using an A9 chip from Samsung (CPID 0x8000 / N71AP).", flush=True)
            print("  Samsung A9 support is EXPERIMENTAL (primary verified target: TSMC s8003).", flush=True)
            print("======================================================================", flush=True)
            ans = input("  Are you sure you want to continue? [y/N]: ").strip()
            if ans.lower() not in ("y", "yes"):
                raise RuntimeError("Aborted by user (Samsung A9 confirmation declined).")
            print("[+] Continuing on experimental Samsung A9 (CPID 0x8000 / N71AP).", flush=True)
        else:
            raise RuntimeError(
                "Samsung A9 (CPID 0x8000 / N71AP) detected (non-interactive shell); re-run interactively to confirm or pass --force."
            )
    if "pwnd: yolo" in low:
        print("[*] Device is already in pwnDFU mode.", flush=True)
        return
    if "iboot-2234" not in low:
        raise RuntimeError(f"{out.strip()}\nExpected clean SecureROM DFU (iBoot-2234...).")

    print("[*] Putting device into pwnDFU mode...", flush=True)
    p = subprocess.run([str(ENGINE_DIR / "bin/engine_helper"), "-D"], cwd=str(ENGINE_DIR))
    if p.returncode != 0:
        raise RuntimeError(f"pwnDFU helper failed (rc={p.returncode})")

def enter_pongo(ipsw_dir: Path, block_path: Path, ssh_ramdisk: bool = False) -> None:
    env = os.environ.copy()
    env["DYLD_INSERT_LIBRARIES"] = str(FAST_DNS)
    blk_flag = "--load-shcblock" if "shcblock" in block_path.name.lower() else "--load-pteblock"
    if ssh_ramdisk:
        ramdisk_bin = ENGINE_DIR / "bin/engine_ramdisk"
        engine_bin = ramdisk_bin if ramdisk_bin.is_file() else (ENGINE_DIR / "bin/engine_restore")
    else:
        engine_bin = ENGINE_DIR / "bin/engine_boot"
    cmd = [
        str(engine_bin),
        "-y", "-z", "-o", "--allow-unsupport",
        blk_flag, str(block_path),
        str(ipsw_dir),
    ]

    print("[*] Uploading bootchain and waiting for PongoOS...", flush=True)
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        cmd, cwd=str(ENGINE_DIR), env=env,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    stopped_cleanly = False
    buf = ""
    recent_lines = []
    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 1.0)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            buf += text
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                stripped = line.strip()
                if stripped:
                    recent_lines.append(stripped)
                    if len(recent_lines) > 25:
                        recent_lines.pop(0)
                if "Found PongoOS device" in line:
                    print("[*] PongoOS detected — taking over USB session...", flush=True)
                    proc.send_signal(signal.SIGKILL)
                    stopped_cleanly = True
                    break
                if any(k in line for k in ("Sending", "Entering", "Error", "ERROR", "Failed", "Unable", "FATAL")):
                    print("  [stage] " + stripped, flush=True)
            if stopped_cleanly:
                break
    finally:
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    if not stopped_cleanly:
        tail = "\n".join("  " + ln for ln in recent_lines[-20:])
        raise RuntimeError(
            "Bootchain did not reach PongoOS. Re-enter DFU mode (if stuck on Checkmate?, replug USB for 1s) and retry.\n"
            f"Last engine output:\n{tail}"
        )
    time.sleep(0.8)

def open_pongo(libusb, timeout_sec: float = 20.0):
    ctx = ctypes.c_void_p()
    rc = libusb.libusb_init(ctypes.byref(ctx))
    if rc != 0:
        raise RuntimeError(f"libusb_init failed: {rc}")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        dev = libusb.libusb_open_device_with_vid_pid(ctx, 0x05AC, 0x4141)
        if dev:
            if libusb.libusb_claim_interface(dev, 0) == 0:
                return ctx, dev
            libusb.libusb_close(dev)
        time.sleep(0.3)
    libusb.libusb_exit(ctx)
    raise RuntimeError("Could not open PongoOS USB device (0x05ac:0x4141).")

def close_pongo(libusb, ctx, dev) -> None:
    try:
        libusb.libusb_release_interface(dev, 0)
    except Exception:
        pass
    libusb.libusb_close(dev)
    libusb.libusb_exit(ctx)

def pongo_drain(libusb, dev) -> None:
    in_prog = ctypes.create_string_buffer(1)
    stdout_buf = ctypes.create_string_buffer(4096)
    while True:
        rc = libusb.libusb_control_transfer(dev, 0xA1, 2, 0, 0, in_prog, 1, 5000)
        if rc < 0:
            raise RuntimeError(f"Pongo status transfer failed: {rc}")
        r2 = libusb.libusb_control_transfer(dev, 0xA1, 1, 0, 0, stdout_buf, 4096, 5000)
        if r2 > 0:
            txt = stdout_buf.raw[:r2].decode("utf-8", errors="replace")
            for ln in txt.splitlines():
                if ln.strip():
                    print(f"  [pongo] {ln}", flush=True)
        if in_prog.raw[0] == 0:
            break
        time.sleep(0.05)
    libusb.libusb_control_transfer(dev, 0x21, 4, 0xFFFF, 0, None, 0, 5000)

def pongo_upload(libusb, dev, name: str, data: bytes) -> None:
    pongo_drain(libusb, dev)
    print(f"[*] Uploading {name} ({len(data)} B)...", flush=True)
    sz_buf = ctypes.create_string_buffer(struct.pack("<I", len(data)), 4)
    rc = libusb.libusb_control_transfer(dev, 0x21, 1, 0, 0, sz_buf, 4, 5000)
    if rc < 0:
        raise RuntimeError(f"Pongo bulk setup failed ({name}): {rc}")
    buf = ctypes.create_string_buffer(data, len(data))
    xfer = ctypes.c_int(0)
    rc = libusb.libusb_bulk_transfer(dev, 0x02, buf, len(data), ctypes.byref(xfer), 30000)
    if rc != 0 or xfer.value != len(data):
        raise RuntimeError(f"Pongo bulk transfer failed ({name}): rc={rc} xfer={xfer.value}")

def pongo_cmd(libusb, dev, cmd_str: str, wait_after: bool = True) -> None:
    pongo_drain(libusb, dev)
    raw = (cmd_str + "\n").encode("ascii")
    buf = ctypes.create_string_buffer(raw, len(raw))
    rc = libusb.libusb_control_transfer(dev, 0x21, 3, 0, 0, buf, len(raw), 5000)
    if rc < 0 and not cmd_str.startswith("boot"):
        raise RuntimeError(f"Pongo command failed ({cmd_str}): {rc}")
    if wait_after:
        time.sleep(0.15)
        pongo_drain(libusb, dev)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh-ramdisk", action="store_true", help="Boot the maintenance SSH RamDisk (rd=md0) instead of NAND.")
    ap.add_argument("--ipsw-dir", default="", help="Override hybrid IPSW or SSH RamDisk staging directory.")
    ap.add_argument("--block", default="", help="Path to device boot block (.bin).")
    ap.add_argument("--boot-args", default="", help="Override kernel boot-args.")
    ap.add_argument("--quiet", action="store_true", help="Disable verbose kernel console (-v).")
    ap.add_argument("--read-only", action="store_true", help="Read hardware registers only (hwinit read).")
    ap.add_argument("--no-boot", action="store_true", help="Stop inside PongoOS after hwinit.")
    ap.add_argument("--already-pongo", action="store_true", help="Device is already in PongoOS (0x4141).")
    ap.add_argument("--skip-load", action="store_true", help="Skip uploading hw_init_probe.bin.")
    ap.add_argument("--force", action="store_true", help="Allow non-TSMC CPID (experimental).")
    args = ap.parse_args()

    libusb = load_libusb()
    if args.ipsw_dir:
        ipsw_dir = Path(args.ipsw_dir)
    else:
        ipsw_dir = BUILD_DIR / ("ssh_ramdisk_ipsw" if args.ssh_ramdisk else "hybrid_ipsw")

    default_args = DEFAULT_BOOTARGS_QUIET if args.quiet else DEFAULT_BOOTARGS_VERBOSE
    boot_args = args.boot_args or (SSH_RAMDISK_BOOTARGS if args.ssh_ramdisk else default_args)

    if not args.already_pongo:
        block_path = find_block_file(args.block)
        ensure_local_server()
        check_and_pwn_dfu(force=args.force)
        enter_pongo(ipsw_dir, block_path, ssh_ramdisk=args.ssh_ramdisk)

    ctx, dev = open_pongo(libusb, 20.0)
    try:
        print("[*] Connected to PongoOS (0x05ac:0x4141).", flush=True)
        pongo_drain(libusb, dev)

        if not args.skip_load:
            pongo_upload(libusb, dev, "hw_init_probe.bin", HWINIT_MOD.read_bytes())
            pongo_cmd(libusb, dev, "modload")

        pongo_cmd(libusb, dev, "bootlogo")
        if not args.ssh_ramdisk:
            pongo_cmd(libusb, dev, "hwinit read" if args.read_only else "hwinit enable")

        if args.no_boot:
            print("[+] --no-boot active; leaving device in PongoOS.", flush=True)
            return

        print("[*] Loading kernel patchfinder & framebuffer...", flush=True)
        pongo_upload(libusb, dev, "boot_args_mod", ORIG_SEP.read_bytes())
        pongo_cmd(libusb, dev, "modload")
        pongo_cmd(libusb, dev, f"sep xargsadd {boot_args}")

        pongo_upload(libusb, dev, "kpf_mod", ORIG_KPF.read_bytes())
        pongo_cmd(libusb, dev, "modload")
        pongo_cmd(libusb, dev, "tethered_flags 1")
        pongo_cmd(libusb, dev, "kpf-tethered")
        pongo_cmd(libusb, dev, "xfb")
        pongo_cmd(libusb, dev, "bootux", wait_after=False)
        if args.ssh_ramdisk:
            print("[+] SSH RamDisk boot started! Connect via USB: iproxy 2222 22 && ssh root@127.0.0.1 -p 2222", flush=True)
        else:
            print("[+] Boot command sent! iPhone 6s is booting into iOS 16.7.16.", flush=True)
    finally:
        close_pongo(libusb, ctx, dev)

if __name__ == "__main__":
    main()
