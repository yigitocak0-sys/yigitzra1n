#!/usr/bin/env python3
"""Generate iOS 16 StatusBarOverrideData binary (3944 bytes) matching UIStatusBarServer."""
import argparse
import struct
from pathlib import Path

STATUS_BAR_OVERRIDE_SIZE = 3944  # 0xF68 bytes (UIStatusBarServerListener init on iOS 16.7.16)
OFF_OVERRIDE_ITEM_ENABLED = 0
OFF_OVERRIDE_FLAGS_BYTE2 = 46  # 0x2E: bit 4 = overrideGSMSignalStrengthBars, bit 6 = overrideServiceString
OFF_VALUES = 64  # 0x40: StatusBarRawData start
OFF_ITEM_ENABLED = OFF_VALUES + 0
OFF_GSM_BARS = 504       # 0x1F8 (int32)
OFF_SERVICE_STR = 512    # 0x200 (char[100])
OFF_CROSSFADE_STR = 712  # 0x2C8 (char[100])

ITEM_CELLULAR_SIGNAL = 4
ITEM_CELLULAR_SERVICE = 6


def build_statusbar_overrides(carrier: str = "yigitzra1n", bars: int = 4) -> bytes:
    buf = bytearray(STATUS_BAR_OVERRIDE_SIZE)

    # Enable full cellular signal bars (0..4)
    bars_clamped = max(0, min(4, int(bars)))
    buf[OFF_OVERRIDE_ITEM_ENABLED + ITEM_CELLULAR_SIGNAL] = 1
    buf[OFF_ITEM_ENABLED + ITEM_CELLULAR_SIGNAL] = 1
    buf[OFF_OVERRIDE_FLAGS_BYTE2] |= (1 << 4)
    struct.pack_into("<i", buf, OFF_GSM_BARS, bars_clamped)

    # Enable and override carrier service text
    raw_carrier = carrier.encode("utf-8", errors="ignore")[:99]
    buf[OFF_OVERRIDE_ITEM_ENABLED + ITEM_CELLULAR_SERVICE] = 1
    buf[OFF_ITEM_ENABLED + ITEM_CELLULAR_SERVICE] = 1
    buf[OFF_OVERRIDE_FLAGS_BYTE2] |= (1 << 6)
    buf[OFF_SERVICE_STR : OFF_SERVICE_STR + len(raw_carrier)] = raw_carrier
    buf[OFF_CROSSFADE_STR : OFF_CROSSFADE_STR + len(raw_carrier)] = raw_carrier

    return bytes(buf)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--carrier", default="yigitzra1n")
    ap.add_argument("--bars", type=int, default=4)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_statusbar_overrides(carrier=args.carrier, bars=args.bars))


if __name__ == "__main__":
    main()
