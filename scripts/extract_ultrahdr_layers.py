#!/usr/bin/env python3
"""CLI: extract Android Ultra HDR JPEG layers.

    python scripts/extract_ultrahdr_layers.py image.jpg [-o OUTDIR] [-v]

Produces <stem>_layers/ containing the base SDR, gain map (raw + upscaled),
log-boost visualisation, clipping mask, an approximate HDR preview, and
metadata.json. Never modifies the input file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the hdrextract package importable when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hdrextract import ultrahdr  # noqa: E402
from hdrextract.common import (  # noqa: E402
    LOG,
    find_exiftool,
    make_output_dir,
    setup_logging,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract Ultra HDR JPEG analysis layers.")
    p.add_argument("input", type=Path, help="Ultra HDR JPEG file")
    p.add_argument("-o", "--outdir", type=Path, default=None,
                   help="output directory (default: <stem>_layers next to input)")
    p.add_argument("--no-exiftool", action="store_true",
                   help="do not use ExifTool even if available")
    p.add_argument("--clip-threshold", type=int, default=250,
                   help="0-255 channel value treated as clipped (default 250)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = p.parse_args(argv)

    setup_logging(args.verbose)

    if not args.input.is_file():
        LOG.error("input file not found: %s", args.input)
        return 2

    exiftool = None if args.no_exiftool else find_exiftool()
    if exiftool:
        LOG.info("using ExifTool: %s", exiftool)
    else:
        LOG.info("ExifTool not used (pure-Python parsing)")

    outdir = make_output_dir(args.input, args.outdir)
    LOG.info("output dir: %s", outdir)

    try:
        meta = ultrahdr.extract(args.input, outdir, exiftool=exiftool,
                                clip_threshold=args.clip_threshold)
    except Exception as exc:  # noqa: BLE001
        LOG.error("extraction failed: %s", exc)
        return 1

    LOG.info("done: %d layer(s) -> %s", len(meta.get("layers", [])), outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
