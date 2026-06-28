"""Shared helpers: logging, output directories, dependency detection, image IO.

Everything here is deliberately defensive: a missing optional dependency or a
single failed layer must never abort the whole extraction. The guiding rule is
"save whatever layers we managed to produce".
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image

LOG = logging.getLogger("hdrextract")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = False) -> None:
    """Configure the package logger once, writing human-readable lines to stderr."""
    level = logging.DEBUG if verbose else logging.INFO
    if not LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(level)


# --------------------------------------------------------------------------- #
# Dependency detection
# --------------------------------------------------------------------------- #
def find_exiftool() -> str | None:
    """Locate an ExifTool executable, or return None.

    Checks PATH first, then the common Windows install locations used by the
    winget OliverBetz.ExifTool package.
    """
    for name in ("exiftool", "exiftool.exe", "ExifTool.exe"):
        found = shutil.which(name)
        if found:
            return found

    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    pf = os.environ.get("ProgramFiles")
    if local:
        candidates.append(Path(local) / "Programs" / "ExifTool" / "ExifTool.exe")
        candidates.append(Path(local) / "Programs" / "ExifTool" / "exiftool.exe")
    if pf:
        candidates.append(Path(pf) / "ExifTool" / "exiftool.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def have_pillow_heif() -> bool:
    try:
        import pillow_heif  # noqa: F401

        return True
    except Exception:
        return False


class DependencyError(RuntimeError):
    """Raised when a hard dependency for a given operation is missing."""


# --------------------------------------------------------------------------- #
# Output directory
# --------------------------------------------------------------------------- #
def make_output_dir(input_path: Path, outdir: Path | None) -> Path:
    """Return (and create) the output directory for *input_path*.

    Default is ``<input_stem>_layers`` next to the input file. Never touches or
    overwrites the input file itself.
    """
    input_path = Path(input_path)
    if outdir is None:
        outdir = input_path.parent / f"{input_path.stem}_layers"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir


# --------------------------------------------------------------------------- #
# Image / data IO
# --------------------------------------------------------------------------- #
def clean_output_dir(outdir: Path) -> int:
    """Remove previously generated layer files (*.png, metadata.json) so the
    directory always reflects the current run. Returns the count removed.

    Only touches tool-generated artifacts in the ``<stem>_layers`` directory;
    leaves any other files the user may have placed there.
    """
    outdir = Path(outdir)
    removed = 0
    for p in list(outdir.glob("*.png")) + list(outdir.glob("metadata.json")):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        LOG.info("cleaned %d stale output file(s) in %s", removed, outdir.name)
    return removed


# --------------------------------------------------------------------------- #
# Parallel image writing
# --------------------------------------------------------------------------- #
# PNG encoding (zlib) releases the GIL, so a thread pool gives real parallelism.
# Images are computed in the main thread and only the encode+write is deferred;
# callers must not mutate an image after handing it to save_image().
PNG_COMPRESS_LEVEL = 1  # ~40% faster than level 6, only ~5% larger - fine for analysis
_SAVE_POOL: "ThreadPoolExecutor | None" = None
_SAVE_FUTURES: list = []


def begin_saves() -> None:
    """Reset the deferred-save queue at the start of an extraction."""
    global _SAVE_FUTURES
    _SAVE_FUTURES = []


def flush_saves() -> None:
    """Wait for all queued saves to finish; re-raise the first failure."""
    global _SAVE_FUTURES
    pending, _SAVE_FUTURES = _SAVE_FUTURES, []
    first_error = None
    for fut in pending:
        try:
            fut.result()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("deferred save failed: %s", exc)
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _ensure_pool() -> "ThreadPoolExecutor":
    global _SAVE_POOL
    if _SAVE_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _SAVE_POOL = ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4)))
    return _SAVE_POOL


def _encode_write(img: Image.Image, path: Path, kwargs: dict) -> None:
    img.save(path, **kwargs)
    LOG.info("wrote layer  %s  (%dx%d, %s)", path.name, img.width, img.height, img.mode)


def save_image(img: Image.Image, outdir: Path, name: str, fmt: str = "png") -> Path:
    """Queue a PIL image to be saved as ``<name>.<fmt>``; return its path.

    The actual encode+write runs on a background thread pool (see flush_saves).
    Preserves any embedded ICC profile (e.g. Display P3).
    """
    path = Path(outdir) / f"{name}.{fmt}"
    kwargs: dict = {}
    if fmt == "png":
        kwargs["compress_level"] = PNG_COMPRESS_LEVEL
        icc = img.info.get("icc_profile")
        if icc:
            kwargs["icc_profile"] = icc
    _SAVE_FUTURES.append(_ensure_pool().submit(_encode_write, img, path, kwargs))
    return path


def save_array_png(arr: np.ndarray, outdir: Path, name: str) -> Path:
    """Save a numpy array as a PNG, choosing 8- or 16-bit based on dtype.

    Accepts uint8 / uint16 / float arrays. Float arrays are assumed to already
    be in the correct integer-ish range and are cast; normalise beforehand if
    you want a visualisation.
    """
    if arr.dtype == np.uint16:
        # PIL writes single-channel uint16 as mode "I;16".
        if arr.ndim == 2:
            img = Image.fromarray(arr, mode="I;16")
        else:
            # 16-bit RGB is not natively supported by PNG in PIL; downscale.
            img = Image.fromarray((arr >> 8).astype(np.uint8))
    elif arr.dtype == np.uint8:
        img = Image.fromarray(arr)
    else:
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    return save_image(img, outdir, name)


def normalize_to_u8(arr: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Min/max normalise a float array to uint8 for visualisation.

    Returns ``(u8_array, vmin, vmax)`` so the caller can record the mapping in
    metadata. A flat array maps to all-zero.
    """
    arr = arr.astype(np.float64)
    vmin = float(np.nanmin(arr))
    vmax = float(np.nanmax(arr))
    if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        return np.zeros(arr.shape, dtype=np.uint8), vmin, vmax
    scaled = (arr - vmin) / (vmax - vmin)
    return (scaled * 255.0 + 0.5).astype(np.uint8), vmin, vmax


def to_grayscale_float(img: Image.Image) -> np.ndarray:
    """Return a float32 single-channel array from a PIL image (luma if RGB)."""
    if img.mode in ("L", "I", "I;16", "F"):
        return np.asarray(img.convert("F"), dtype=np.float32)
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
    return rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def write_metadata(meta: dict[str, Any], outdir: Path) -> Path:
    """Write metadata.json (UTF-8, pretty, non-ASCII preserved)."""
    path = Path(outdir) / "metadata.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False, default=_json_default)
    LOG.info("wrote %s", path.name)
    return path


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def upscale_to(img: Image.Image, size: tuple[int, int], method: str = "nearest") -> Image.Image:
    """Resize *img* to *size* (w, h). method = 'nearest' | 'bilinear'.

    'nearest' is faithful to the stored gain-map samples (1:1, blocky) and is
    the analysis default. 'bilinear' matches what real HDR renderers do
    (the Ultra HDR spec mandates "bilinear or better").
    """
    resample = Image.NEAREST if method == "nearest" else Image.BILINEAR
    if tuple(img.size) == tuple(size):
        return img
    return img.resize(size, resample)


def gainmap_log_boost(gain_norm: np.ndarray, gmin: float, gmax: float,
                      gamma: float) -> np.ndarray:
    """ISO 21496-1 / Ultra HDR decode: stored gain (0..1) -> log2 boost (stops).

    log_recovery = gain ** (1 / gamma)
    log_boost    = gmin * (1 - log_recovery) + gmax * log_recovery
    """
    gamma = gamma or 1.0
    recovery = np.power(np.clip(gain_norm, 0.0, 1.0), 1.0 / gamma)
    return gmin * (1.0 - recovery) + gmax * recovery


def slugify(text: str, maxlen: int = 40) -> str:
    """Make a filesystem-safe slug from an arbitrary string (e.g. an aux URN)."""
    keep = []
    for ch in text:
        if ch.isalnum():
            keep.append(ch.lower())
        elif ch in ("-", "_"):
            keep.append(ch)
        else:
            keep.append("_")
    slug = "".join(keep).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug[:maxlen] or "unknown"
