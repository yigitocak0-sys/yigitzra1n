#!/usr/bin/env python3
"""Patch iOS 15.8.8 N71/N71m AVCaptureSession.plist for iOS 16 CMCapture compatibility."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path


def patch_avcapture_plist(path: Path) -> bool:
    """Ensure the rear camera entry defines stillImageNoiseReductionAndFusionScheme=1."""
    if not path.is_file():
        return False

    data = plistlib.loads(path.read_bytes())
    changed = False
    for dev in data.get("AVCaptureDevices", []):
        if not isinstance(dev, dict):
            continue
        is_rear = (
            dev.get("uniqueID") == "com.apple.avfoundation.avcapturedevice.built-in_video:0"
            or (dev.get("mediaType") == "vide" and dev.get("position") == 1)
        )
        if not is_rear:
            continue
        scheme = dev.get("stillImageNoiseReductionAndFusionScheme")
        if not isinstance(scheme, int) or scheme < 1:
            dev["stillImageNoiseReductionAndFusionScheme"] = 1
            changed = True
        if "needsPhotoTNR" not in dev:
            dev["needsPhotoTNR"] = False
            changed = True

    if changed:
        path.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
    return changed


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: patch_cmcapture_plist.py <AVCaptureSession.plist> [...]", file=sys.stderr)
        return 2

    for raw in sys.argv[1:]:
        p = Path(raw)
        if patch_avcapture_plist(p):
            print(f"[+] Patched CMCapture plist for iOS 16 TMBNR: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
