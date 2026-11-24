#!/usr/bin/env python3
import argparse
import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
import pyimg4

SCRIPT_DIR = Path(__file__).resolve().parent

BASE_KERNEL_PATCHES = [
    (0x05A2948, "609243f918feff97", "7f7a04b997ffff17", "XNU proc_prepareexit RequireSuccess bypass"),
    (0x0656E88, "e0630591", "80920391", "bindfs_mount f_mntonname bypass"),
    (0x09E4090, "53656e73", "58656e73", "Disable incompatible sensor match string #1"),
    (0x0B5C210, "0053656e", "0058656e", "Disable incompatible sensor match string #2"),
    (0x15743FC, "ff4301d1f85f01a9", "20008052c0035fd6", "AppleHDQGasGaugeControl patch #1"),
    (0x15745DC, "ff4301d1f65702a9", "00008052c0035fd6", "AppleHDQGasGaugeControl patch #2"),
    (0x157CDEC, "ff8300d1fd7b01a9", "00008052c0035fd6", "AppleHDQGasGaugeControl patch #3"),
    (0x15E6D24, "37180034", "1f2003d5", "AppleARMPMU / gas-gauge check bypass"),
    (0x1B86C64, "c8000035", "1f2003d5", "AppleBCMWLANCore Wi-Fi patch #1"),
    (0x1B8EF34, "7a940094", "1f2003d5", "AppleBCMWLANCore Wi-Fi patch #2"),
    (0x1B8EFBC, "58940094", "1f2003d5", "AppleBCMWLANCore Wi-Fi patch #3"),
    (0x1B8F8FC, "08920094", "1f2003d5", "AppleBCMWLANCore Wi-Fi patch #4"),
    (0x1B8F998, "e1910094", "1f2003d5", "AppleBCMWLANCore Wi-Fi patch #5"),
    (0x1B95A38, "68010035", "1f2003d5", "AppleBCMWLANCore Wi-Fi patch #6"),
    (0x1B95A80, "e0d4ff97", "00008052", "AppleBCMWLANCore Wi-Fi patch #7"),
    (0x1B9959C, "e8b380b9", "c80080d2", "AppleBCMWLANCore Wi-Fi patch #8"),
    (0x1B9B484, "26630094", "1f2003d5", "AppleBCMWLANCore Wi-Fi patch #9"),
    (
        0x1BB11B0,
        "e9ad00f029e1613989040034f40301aaf6ad00f0d57648b9",
        "f40301aac8008052e80300b968008052e80700b90e000014",
        "AppleBCMWLANCore Wi-Fi patch #10",
    ),
    (0x1C9B2DC, "480e0037", "1f2003d5", "apfs.kext handle_mount unencrypted volume allow"),
    (0x1DB1C94, "800000b4", "1f2003d5", "AppleKeyStore / AMFI SEP-less patch #1"),
    (0x1DB1D98, "680240f9084140f9", "0000805206000014", "AppleKeyStore / AMFI SEP-less patch #2"),
    (0x1DF08F8, "ff0302d1fa6703a9", "000080d2c0035fd6", "AppleKeyStore / AMFI SEP-less patch #3"),
    (0x2329258, "40050034", "36000014", "IOWatchdog disable"),
]

LATE_KERNEL_PATCHES = [
    (
        0x15EC24C,
        "ff3f0039684241b96803003414008052",
        "687643b91f010071e8d79f1a1a000014",
        "AppleARMPMU Site1: IsCharging from InstantAmperage > 0",
    ),
    (
        0x15F2A88,
        "682649391f0009ebc001005408191f1268260939a801003503000014",
        "7f2609391f0009ebe8079f1a6872073968760739687a073914000014",
        "AppleARMPMU Site2: ExternalConnected live update",
    ),
    (
        0x15EC60C,
        "1f040071e1000054",
        "400f00341f2003d5",
        "AppleARMPMU Site3: num-docks==0 empty OSArray NULL panic guard",
    ),
    (
        0x11F9A0C,
        "21008052",
        "01008052",
        "AppleFirmwareUpdateKext::sendFirmware AOP skipFWValidation=1 status=0 (v6)",
    ),
]

MULTITOUCH_N71_XML = (
    '<dict><key>CFBundleName</key><string>AppleMultitouchSPIC1F15</string>'
    '<key>CFBundleVersion</key><string IDREF="133"/>'
    '<key>CFBundlePackageType</key><string IDREF="4"/>'
    '<key>CFBundleShortVersionString</key><string IDREF="133"/>'
    '<key>_PrelinkExecutableRelativePath</key><string>AppleMultitouchSPIC1F15</string>'
    '<key>CFBundleInfoDictionaryVersion</key><string IDREF="7"/>'
    '<key>CFBundleIdentifier</key><string>com.apple.driver.AppleMultitouchSPIC1F15</string>'
    '<key>IOKitPersonalities</key><dict><key>AppleMultitouchSPI - C1,N71,2</key><dict>'
    '<key>IOCFPlugInTypes</key><dict><key>0516B563-B15B-11DA-96EB-0014519758EF</key>'
    '<string>AppleMultitouchSPI.kext/PlugIns/MultitouchHID.plugin</string></dict>'
    '<key>CFBundleIdentifier</key><string>com.apple.driver.AppleMultitouchSPI</string>'
    '<key>IOClass</key><string>AppleMultitouchN1SPI</string>'
    '<key>IOProviderClass</key><string>AppleARMSPIDevice</string>'
    '<key>IONameMatch</key><array><string>multi-touch,n71,2</string><string>multi-touch,j72</string></array>'
    '<key>mt-merge-personality</key><string>C1F5B,2</string>'
    '<key>fll-mval</key><integer size="64">0x17d3</integer>'
    '<key>fll-mval-addr</key><integer size="64">0x10003060</integer>'
    '<key>clk32-clock-enable-addr</key><integer size="64">0x10003518</integer>'
    '<key>clk32-clock-enable-val</key><integer IDREF="36"/>'
    '<key>fw-execute-addr</key><integer size="64">0x10003400</integer>'
    '<key>cal-dl-addr</key><integer size="64">0x10009000</integer>'
    '<key>prox-cal-addr</key><integer size="64">0x10009600</integer>'
    '<key>reset-deassert-delay</key><integer ID="691" size="64">0xf</integer>'
    '<key>touch-size-id</key><integer IDREF="78"/>'
    '<key>ref-clk-div-val</key><integer IDREF="78"/>'
    '<key>parser-type</key><integer IDREF="36"/>'
    '<key>MinDigitizerPressureValue</key><integer IDREF="1"/>'
    '<key>AccurateMaxDigitizerPressureValue</key><integer IDREF="294"/>'
    '<key>ExtendedMaxDigitizerPressureValue</key><integer IDREF="82"/>'
    '<key>inter-packet-delay-us</key><integer size="64">0x7d</integer>'
    '<key>QueryIOReporterOnBoot</key><true/>'
    '<key>Z2Compliant</key><true/>'
    '<key>ForceSupported</key><true/>'
    '<key>HIDServiceSupport</key><true/>'
    '<key>SurrenderPowerControl</key><true/>'
    '<key>Placement</key><integer size="64">1</integer>'
    '</dict></dict>{PAD}'
    '<key>CFBundleSignature</key><string IDREF="8"/>'
    '<key>DTPlatformVersion</key><string IDREF="23"/>'
    '<key>DTXcodeBuild</key><string IDREF="35"/>'
    '<key>OSBundleLibraries</key><dict>'
    '<key>com.apple.kpi.bsd</key><string IDREF="50"/>'
    '<key>com.apple.kpi.libkern</key><string IDREF="50"/>'
    '<key>com.apple.kpi.mach</key><string IDREF="50"/>'
    '<key>com.apple.kpi.iokit</key><string IDREF="50"/>'
    '<key>com.apple.driver.AppleMultitouchSPI</key><string IDREF="43"/>'
    '<key>com.apple.kpi.unsupported</key><string IDREF="50"/>'
    '<key>com.apple.driver.AppleARMPlatform</key><string IDREF="49"/>'
    '</dict>'
    '<key>UIDeviceFamily</key><array><integer IDREF="36"/></array>'
    '<key>_PrelinkBundlePath</key><string>/System/Library/Extensions/AppleMultitouchSPI.kext/PlugIns/AppleMultitouchSPIC1F15.kext</string>'
    '<key>DTPlatformBuild</key><string IDREF="71"/></dict>'
)

def apply_patches(buf: bytearray, patches: list) -> None:
    for off, orig_hex, repl_hex, desc in patches:
        orig = bytes.fromhex(orig_hex)
        repl = bytes.fromhex(repl_hex)
        assert len(orig) == len(repl), f"Length mismatch: {desc}"
        cur = bytes(buf[off : off + len(orig)])
        if cur == repl:
            continue
        if cur != orig:
            raise RuntimeError(
                f"Kernel patch mismatch at 0x{off:x} ({desc}): got={cur.hex()} expected={orig.hex()}"
            )
        buf[off : off + len(repl)] = repl

def patch_multitouch_prelink_xml(buf: bytearray) -> None:
    off = 32
    ncmds = struct.unpack_from("<I", buf, 16)[0]
    pi_fo = pi_fsz = 0
    for _ in range(ncmds):
        c, cs = struct.unpack_from("<2I", buf, off)
        if c == 0x19 and buf[off + 8 : off + 24].split(b"\x00", 1)[0] == b"__PRELINK_INFO":
            _, _, pi_fo, pi_fsz = struct.unpack_from("<4Q", buf, off + 24)
            break
        off += cs
    assert pi_fo and pi_fsz, "__PRELINK_INFO segment not found"

    pi = bytes(buf[pi_fo : pi_fo + pi_fsz])
    needle = b"<key>CFBundleIdentifier</key><string>com.apple.driver.AppleMultitouchSPIC1F15</string>"
    pos = pi.find(needle)
    assert pos >= 0, "AppleMultitouchSPIC1F15 not found in __PRELINK_INFO"

    depth = 0
    idx = pos
    start = -1
    while idx >= 0:
        o = pi.rfind(b"<dict>", 0, idx)
        c = pi.rfind(b"</dict>", 0, idx)
        if c > o:
            depth += 1
            idx = c
        else:
            if depth == 0:
                start = o
                break
            depth -= 1
            idx = o
    depth = 0
    idx = start
    end = -1
    while idx < len(pi):
        o = pi.find(b"<dict>", idx)
        c = pi.find(b"</dict>", idx)
        if 0 <= o < c:
            depth += 1
            idx = o + 6
        else:
            depth -= 1
            idx = c + 7
            if depth == 0:
                end = idx
                break
    assert start >= 0 and end > start, "Could not locate AppleMultitouchSPIC1F15 dict span"

    orig_len = end - start
    base_xml = MULTITOUCH_N71_XML.replace("{PAD}", "").encode("utf-8")
    pad_len = orig_len - len(base_xml)
    assert pad_len >= 0, f"N71 multitouch XML ({len(base_xml)}B) exceeds original dict ({orig_len}B)"
    new_xml = MULTITOUCH_N71_XML.replace("{PAD}", " " * pad_len).encode("utf-8")
    assert len(new_xml) == orig_len
    buf[pi_fo + start : pi_fo + end] = new_xml
    print(f"  [+] Patched AppleMultitouchSPIC1F15 __PRELINK_INFO in-place ({orig_len} B, pad={pad_len} B)")

def extract_kernel_from_ipsw(ipsw_path: Path, member: str):
    with zipfile.ZipFile(ipsw_path, "r") as zf:
        raw_im4p = zf.read(member)
    im = pyimg4.IM4P(raw_im4p)
    fourcc, desc, extra = im.fourcc, im.description, im.payload.extra
    im.payload.decompress()
    return fourcc, desc, extra, bytes(im.payload.data)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ipsw-1588", required=True, help="Stock iPhone 6s 15.8.8 IPSW")
    ap.add_argument("--ipsw-ipad5", required=True, help="Stock iPad 5 16.7.16 IPSW")
    ap.add_argument("--out", required=True, help="Output IM4P path (e.g. build/kernelcache.release.n71_v6.im4p)")
    ap.add_argument("--board", choices=["n71m", "n71"], default="n71m")
    args = ap.parse_args()

    kc15_member = "kernelcache.release.n71"
    kc16_member = "kernelcache.release.ipad6f"

    with tempfile.TemporaryDirectory(prefix="kc16_v6_") as tmp_s:
        tmp = Path(tmp_s)
        print(f"[1/6] Extracting {kc16_member} (16.7.16) and {kc15_member} (15.8.8)...")
        fourcc, desc, extra, kc16_raw = extract_kernel_from_ipsw(Path(args.ipsw_ipad5), kc16_member)
        _, _, _, kc15_raw = extract_kernel_from_ipsw(Path(args.ipsw_1588), kc15_member)
        assert extra and len(extra) == 49152, f"Expected 49152-byte KPP extra, got {len(extra) if extra else 0}"

        kc15_dec = tmp / "kc15.dec"
        kc15_dec.write_bytes(kc15_raw)

        print(f"[2/6] Applying {len(BASE_KERNEL_PATCHES)} base v1..v4 patches + AppleMultitouchSPIC1F15 personality...")
        buf = bytearray(kc16_raw)
        apply_patches(buf, BASE_KERNEL_PATCHES)
        patch_multitouch_prelink_xml(buf)
        kc_base = tmp / "kc16_base.dec"
        kc_base.write_bytes(buf)

        print("[3/6] Relinking AppleCS35L19Amp (speaker/Taptic) from 15.8.8 -> 16.7.16 (in-place)...")
        kc_cs35 = tmp / "kc16_cs35.dec"
        subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "relink_cs35l19.py"),
                "--kc15", str(kc15_dec),
                "--kc16", str(kc_base),
                "--output", str(kc_cs35),
                "--symbol-map", str(tmp / "cs35_symmap.json"),
                "--mode", "inplace",
            ],
            check=True,
        )

        print("[4/6] Relinking AppleCS42L71Audio (audio/mic) from 15.8.8 -> 16.7.16 (in-place)...")
        kc_cs42 = tmp / "kc16_cs42.dec"
        subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "relink_cs42l71.py"),
                "--kc15", str(kc15_dec),
                "--kc16", str(kc_cs35),
                "--out", str(kc_cs42),
                "--symbol-map", str(SCRIPT_DIR / "cs42l71_symbol_map.json"),
                "--mode", "inplace",
            ],
            check=True,
        )

        print("[5/6] Applying AppleARMPMU Site1/2/3 (charge icon + NULL guard) and AOP v6 patch...")
        final_buf = bytearray(kc_cs42.read_bytes())
        apply_patches(final_buf, LATE_KERNEL_PATCHES)

        print("[6/6] Packing LZSS IM4P with 49152-byte KPP extra and verifying round-trip...")
        out_im4p = pyimg4.IM4P(fourcc=fourcc, description=desc, payload=bytes(final_buf))
        out_im4p.payload.extra = extra
        out_im4p.payload.compress(pyimg4.Compression.LZSS)
        out_bytes = out_im4p.output()

        chk = pyimg4.IM4P(out_bytes)
        assert chk.payload.extra == extra, "KPP extra mismatch on round-trip!"
        chk.payload.decompress()
        assert bytes(chk.payload.data) == bytes(final_buf), "LZSS round-trip mismatch!"

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(out_bytes)

        digest = hashlib.sha256(out_bytes).hexdigest()
        dec_digest = hashlib.sha256(bytes(final_buf)).hexdigest()
        print(f"[+] Built kernelcache v6: {out_path}")
        print(f"    im4p={len(out_bytes)} B, dec={len(final_buf)} B, extra={len(extra)} B")
        print(f"    im4p_sha256={digest[:16]}  dec_sha256={dec_digest[:16]}")

if __name__ == "__main__":
    main()
