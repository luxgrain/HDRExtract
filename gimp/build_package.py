#!/usr/bin/env python3
"""Build a self-contained, drop-in GIMP plug-in package.

Produces ``dist/gimp_open_hdr_aux_layers/`` (and a matching .zip) containing the
plug-in, the dispatcher, and a bundled copy of the ``hdrextract`` package.
Deploy by extracting the zip into a folder GIMP scans for plug-ins
(Edit > Preferences > Folders > Plug-ins). Dependencies are installed into a
plugin-local ``_vendor`` dir automatically on first run.

Usage:  python gimp/build_package.py
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC_PLUGIN = REPO / "gimp" / "gimp_open_hdr_aux_layers"
HDREXTRACT = REPO / "hdrextract"
DIST = REPO / "dist"
PKG_NAME = "gimp_open_hdr_aux_layers"
PKG = DIST / PKG_NAME

PLUGIN_FILES = ("gimp_open_hdr_aux_layers.py", "_run_extract.py")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "_vendor")


def main() -> None:
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)

    for fname in PLUGIN_FILES:
        shutil.copy2(SRC_PLUGIN / fname, PKG / fname)
    shutil.copytree(HDREXTRACT, PKG / "hdrextract", ignore=IGNORE)

    zip_path = DIST / f"{PKG_NAME}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(PKG.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(DIST))

    n = sum(1 for _ in PKG.rglob("*") if _.is_file())
    print(f"Built package: {PKG}  ({n} files)")
    print(f"Built zip:     {zip_path}")
    print("\nDeploy: extract the zip into a GIMP plug-ins search folder, then "
          "restart GIMP.\nFirst run installs dependencies into _vendor/ automatically.")


if __name__ == "__main__":
    main()
