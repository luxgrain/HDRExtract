#!/usr/bin/env python3
"""CLI: extract Apple/iPhone HEIC primary + auxiliary/depth layers.

    python scripts/extract_heic_aux_layers.py image.heic [-o OUTDIR] [-v]

Produces <stem>_layers/ containing the primary image, every auxiliary item
(gain map / depth / disparity / semantic matte / unknown), depth images, and
metadata.json. Never modifies the input file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hdrextract import heic  # noqa: E402
from hdrextract.common import (  # noqa: E402
    DependencyError,
    LOG,
    find_exiftool,
    make_output_dir,
    setup_logging,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract HEIC primary + auxiliary layers.")
    p.add_argument("input", type=Path, help="HEIC/HEIF file")
    p.add_argument("-o", "--outdir", type=Path, default=None,
                   help="output directory (default: <stem>_layers next to input)")
    p.add_argument("--no-exiftool", action="store_true",
                   help="do not use ExifTool even if available")
    p.add_argument("--no-thumbnails", action="store_true",
                   help="skip embedded thumbnail items")
    p.add_argument("--force-8bit", action="store_true",
                   help="convert high-bit-depth images to 8-bit on decode")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = p.parse_args(argv)

    setup_logging(args.verbose)

    if not args.input.is_file():
        LOG.error("input file not found: %s", args.input)
        return 2

    exiftool = None if args.no_exiftool else find_exiftool()
    if exiftool:
        LOG.info("using ExifTool: %s", exiftool)

    outdir = make_output_dir(args.input, args.outdir)
    LOG.info("output dir: %s", outdir)

    try:
        meta = heic.extract(
            args.input, outdir, exiftool=exiftool,
            save_thumbnails=not args.no_thumbnails,
            keep_hdr_bit_depth=not args.force_8bit,
        )
    except DependencyError as exc:
        LOG.error("%s", exc)
        return 3
    except Exception as exc:  # noqa: BLE001
        LOG.error("extraction failed: %s", exc)
        return 1

    LOG.info("done: %d layer record(s) -> %s", len(meta.get("layers", [])), outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
