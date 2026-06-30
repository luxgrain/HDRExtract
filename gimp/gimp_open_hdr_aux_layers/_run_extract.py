#!/usr/bin/env python3
"""Subprocess entry point for the GIMP plug-in.

Run by the plug-in in the *system* Python (not GIMP's). It puts the bundled
``hdrextract`` package and the auto-installed ``_vendor`` dependencies on
sys.path, then dispatches to the right extractor by file extension. This is what
makes the plug-in a self-contained, drop-in package (no repo clone, no env vars).
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Search order: bundled deps, the plug-in dir (bundled hdrextract/), the repo
# root (dev checkout: <repo>/gimp/<plugin>/.. -> <repo>), and an optional override.
for _p in (
    os.path.join(_HERE, "_vendor"),
    _HERE,
    os.path.abspath(os.path.join(_HERE, "..", "..")),
    os.environ.get("HDREXTRACT_HOME", ""),
):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from pathlib import Path  # noqa: E402

from hdrextract import heic, ultrahdr  # noqa: E402
from hdrextract.common import LOG, find_exiftool, make_output_dir, setup_logging  # noqa: E402

HEIC_EXT = {".heic", ".heif", ".hif", ".avif"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HDRExtract dispatcher (bundled).")
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--outdir", type=Path, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    if not args.input.is_file():
        LOG.error("input file not found: %s", args.input)
        return 2

    outdir = make_output_dir(args.input, args.outdir)
    exiftool = find_exiftool()
    try:
        if args.input.suffix.lower() in HEIC_EXT:
            heic.extract(args.input, outdir, exiftool=exiftool)
        else:
            ultrahdr.extract(args.input, outdir, exiftool=exiftool)
    except Exception as exc:  # noqa: BLE001
        LOG.error("extraction failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
