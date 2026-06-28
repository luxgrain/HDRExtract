"""Android Ultra HDR JPEG -> analysis layers.

Pipeline (each step is best-effort; a failure logs and continues):

    01_base_sdr                 decoded primary / SDR JPEG
    02_gainmap_raw              appended secondary JPEG (the gain map), native res
    03_gainmap_upscaled         gain map resized to base resolution
    04_gainmap_log_boost        gain map -> log2 boost, min/max normalised view
    05_sdr_clipping_mask        where the SDR base is (near) clipped
    06_reconstructed_hdr_preview approximate HDR reconstruction, tonemapped to SDR
    metadata.json               dimensions, hdrgm fields, MPF, GContainer, ExifTool

The gain-map math follows the Ultra HDR / ISO 21496-1 model in a deliberately
simplified form - this is for *seeing* the data, not colour-accurate output.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from . import metadata as md
from .common import (
    LOG,
    begin_saves,
    clean_output_dir,
    flush_saves,
    normalize_to_u8,
    save_image,
    upscale_to,
    write_metadata,
)


# --------------------------------------------------------------------------- #
# sRGB transfer helpers (work on arrays in [0, 1])
# --------------------------------------------------------------------------- #
def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    a = 0.055
    return np.where(x <= 0.04045, x / 12.92, ((x + a) / (1 + a)) ** 2.4)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    a = 0.055
    x = np.clip(x, 0.0, None)
    return np.where(x <= 0.0031308, x * 12.92, (1 + a) * np.power(x, 1 / 2.4) - a)


# --------------------------------------------------------------------------- #
# Gain map extraction
# --------------------------------------------------------------------------- #
def _exiftool_mpimage(exiftool: str, path: str, tag: str = "MPImage2") -> bytes | None:
    """Try to extract an embedded MP image via ExifTool; return JPEG bytes or None."""
    try:
        out = subprocess.run(
            [exiftool, "-b", f"-{tag}", str(path)], capture_output=True, timeout=60
        )
    except Exception as exc:  # noqa: BLE001
        LOG.debug("ExifTool %s extraction failed: %s", tag, exc)
        return None
    blob = out.stdout
    if blob[:2] == b"\xff\xd8":
        LOG.debug("ExifTool %s yielded %d bytes", tag, len(blob))
        return blob
    return None


def _decode_jpeg(blob: bytes) -> Image.Image:
    return Image.open(io.BytesIO(blob))


# --------------------------------------------------------------------------- #
# Gain-map maths
# --------------------------------------------------------------------------- #
def _gain_norm(img: Image.Image) -> np.ndarray:
    """Return gain-map values normalised to [0, 1] with shape (H, W, C)."""
    if img.mode in ("I", "I;16", "I;16B", "F"):
        arr = np.asarray(img, dtype=np.float32) / 65535.0
        arr = arr[..., None]
    else:
        arr = np.asarray(img.convert("RGB") if img.mode not in ("L", "RGB") else img,
                         dtype=np.float32) / 255.0
        if arr.ndim == 2:
            arr = arr[..., None]
    return arr


def _log_boost(gain_norm: np.ndarray, hdrgm: md.HdrgmMeta) -> np.ndarray:
    """Compute the per-pixel log2 boost from normalised gain values.

    log_boost = mix(GainMapMin, GainMapMax, recovery)
    recovery  = gain ** (1 / Gamma)
    """
    h, w, c = gain_norm.shape
    out = np.empty((h, w, c), dtype=np.float32)
    for ch in range(c):
        gamma = float(hdrgm.value("Gamma", ch)) or 1.0
        gmin = float(hdrgm.value("GainMapMin", ch))
        gmax = float(hdrgm.value("GainMapMax", ch))
        recovery = np.power(np.clip(gain_norm[..., ch], 0.0, 1.0), 1.0 / gamma)
        out[..., ch] = gmin + (gmax - gmin) * recovery
    return out


def _reconstruct_hdr(base_rgb: np.ndarray, log_boost: np.ndarray,
                     hdrgm: md.HdrgmMeta) -> np.ndarray:
    """Approximate HDR recovery, returned as a tonemapped sRGB uint8 RGB image.

    HDR_lin = (SDR_lin + OffsetSDR) * 2**log_boost - OffsetHDR
    Then Reinhard tonemap back into the displayable range.
    """
    base_lin = _srgb_to_linear(base_rgb.astype(np.float32) / 255.0)  # (H,W,3)
    # Broadcast a single-channel boost across RGB, or use per-channel boost.
    if log_boost.shape[-1] == 1:
        boost = np.repeat(log_boost, 3, axis=-1)
    elif log_boost.shape[-1] == 3:
        boost = log_boost
    else:
        boost = np.repeat(log_boost[..., :1], 3, axis=-1)

    off_sdr = float(hdrgm.value("OffsetSDR"))
    off_hdr = float(hdrgm.value("OffsetHDR"))
    hdr_lin = (base_lin + off_sdr) * np.exp2(boost) - off_hdr
    hdr_lin = np.clip(hdr_lin, 0.0, None)

    # Reinhard tonemap so recovered highlights stay visible without blowing out.
    display = hdr_lin / (1.0 + hdr_lin)
    srgb = _linear_to_srgb(display)
    return (np.clip(srgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def extract(input_path: Path, outdir: Path, exiftool: str | None = None,
            clip_threshold: int = 250) -> dict[str, Any]:
    """Extract Ultra HDR layers from *input_path* into *outdir*. Returns metadata."""
    input_path = Path(input_path)
    clean_output_dir(outdir)
    begin_saves()
    data = input_path.read_bytes()
    layers: list[str] = []
    notes: list[str] = []

    meta: dict[str, Any] = {
        "tool": "extract_ultrahdr_layers",
        "input": str(input_path),
        "input_bytes": len(data),
        "layers": layers,
        "notes": notes,
    }

    # --- locate the JPEG streams ------------------------------------------- #
    streams = md.iter_jpeg_streams(data)
    meta["jpeg_streams"] = [{"start": s, "end": e, "size": e - s} for s, e in streams]
    LOG.info("found %d top-level JPEG stream(s)", len(streams))
    mpf = md.parse_mpf(data)
    if mpf:
        meta["mpf"] = mpf

    if not streams:
        raise ValueError("No JPEG stream found - is this a JPEG file?")

    # --- base / SDR -------------------------------------------------------- #
    base_img = _decode_jpeg(data[streams[0][0] : streams[0][1]]).convert("RGB")
    save_image(base_img, outdir, "01_base_sdr")
    layers.append("01_base_sdr")
    meta["base_size"] = list(base_img.size)

    # --- gain map: ExifTool MPImage2 first, then SOI/EOI scan fallback ----- #
    gain_blob: bytes | None = None
    gain_source = None
    if exiftool:
        gain_blob = _exiftool_mpimage(exiftool, str(input_path), "MPImage2")
        if gain_blob:
            gain_source = "exiftool:MPImage2"
    if gain_blob is None and len(streams) >= 2:
        s, e = streams[1]
        gain_blob = data[s:e]
        gain_source = "soi_eoi_scan"
    meta["gainmap_source"] = gain_source

    if gain_blob is None:
        notes.append("No secondary/gain-map JPEG found; only base + metadata emitted.")
        LOG.warning("no gain map found - emitting base layer only")
    else:
        try:
            gain_img = _decode_jpeg(gain_blob)
            gain_img.load()
            meta["gainmap_size"] = list(gain_img.size)
            meta["gainmap_mode"] = gain_img.mode
            gain_disp = gain_img if gain_img.mode in ("L", "RGB") else gain_img.convert("L")
            save_image(gain_disp, outdir, "02_gainmap_raw")
            layers.append("02_gainmap_raw")

            # upscaled to base resolution: nearest (faithful to stored samples,
            # analysis) and bilinear (what real renderers do, "bilinear or better").
            gain_up = upscale_to(gain_disp, base_img.size, "nearest")
            save_image(gain_up, outdir, "03_gainmap_upscaled_nearest")
            layers.append("03_gainmap_upscaled_nearest")
            save_image(upscale_to(gain_disp, base_img.size, "bilinear"),
                       outdir, "03_gainmap_upscaled_bilinear")
            layers.append("03_gainmap_upscaled_bilinear")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Gain map decode failed: {exc}")
            LOG.warning("gain map decode failed: %s", exc)
            gain_blob = None

    # --- XMP / hdrgm metadata ---------------------------------------------- #
    # Calibration (GainMapMin/Max, Gamma, Offset*) is stored in the gain-map
    # sub-image XMP in the Google Ultra HDR layout, so parse both and merge.
    xmp = md.extract_xmp(data)
    hdrgm = md.parse_hdrgm(xmp)
    meta["xmp_present"] = {k: bool(v) for k, v in xmp.items()}
    if gain_blob is not None:
        xmp_gain = md.extract_xmp(gain_blob)
        meta["xmp_present"]["gainmap_standard"] = bool(xmp_gain.get("standard"))
        meta["xmp_present"]["gainmap_extended"] = bool(xmp_gain.get("extended"))
        hdrgm_gain = md.parse_hdrgm(xmp_gain)
        if hdrgm_gain.found:
            hdrgm = md.merge_hdrgm(hdrgm, hdrgm_gain)
    meta["hdrgm"] = {
        "found": hdrgm.found,
        "source": hdrgm.source,
        "fields_parsed": hdrgm.fields,
        "fields_present": hdrgm.raw_present,
        "defaults_used": {
            k: md.HDRGM_DEFAULTS[k]
            for k in md.HDRGM_DEFAULTS
            if not hdrgm.raw_present.get(k)
        },
    }
    if hdrgm.gcontainer:
        meta["gcontainer"] = hdrgm.gcontainer
    if not hdrgm.found:
        notes.append("No hdrgm metadata found; log-boost uses spec default values.")

    # --- log boost + clipping mask + reconstruction ------------------------ #
    if gain_blob is not None:
        try:
            gain_norm_up = _gain_norm(gain_up)
            log_boost = _log_boost(gain_norm_up, hdrgm)
            meta["log_boost_range"] = [float(log_boost.min()), float(log_boost.max())]
            vis = log_boost[..., 0] if log_boost.shape[-1] == 1 else log_boost
            u8, vmin, vmax = normalize_to_u8(vis)
            meta["log_boost_vis_mapping"] = {"vmin": vmin, "vmax": vmax}
            save_image(Image.fromarray(u8), outdir, "04_gainmap_log_boost")
            layers.append("04_gainmap_log_boost")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"log boost computation failed: {exc}")
            LOG.warning("log boost failed: %s", exc)
            log_boost = None
    else:
        log_boost = None

    # clipping mask (independent of gain map)
    try:
        base_arr = np.asarray(base_img, dtype=np.uint8)
        clip = (base_arr.max(axis=-1) >= clip_threshold).astype(np.uint8) * 255
        meta["clip_threshold"] = clip_threshold
        meta["clipped_fraction"] = float((clip > 0).mean())
        save_image(Image.fromarray(clip), outdir, "05_sdr_clipping_mask")
        layers.append("05_sdr_clipping_mask")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"clip mask failed: {exc}")
        LOG.warning("clip mask failed: %s", exc)

    # reconstructed HDR preview (optional)
    if log_boost is not None:
        try:
            if hdrgm.value("BaseRenditionIsHDR"):
                notes.append("BaseRenditionIsHDR=true: preview math assumes SDR base "
                             "and may be inverted.")
            recon = _reconstruct_hdr(np.asarray(base_img), log_boost, hdrgm)
            save_image(Image.fromarray(recon), outdir, "06_reconstructed_hdr_preview")
            layers.append("06_reconstructed_hdr_preview")
            notes.append("06_reconstructed_hdr_preview is an approximate, tonemapped "
                         "visualisation - not colour-accurate HDR.")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"HDR reconstruction failed: {exc}")
            LOG.warning("HDR reconstruction failed: %s", exc)

    # --- ExifTool full dump (optional) ------------------------------------- #
    if exiftool:
        dump = md.exiftool_dump(exiftool, str(input_path))
        if dump:
            meta["exiftool"] = dump

    flush_saves()  # wait for all parallel layer writes to finish
    write_metadata(meta, outdir)
    return meta
