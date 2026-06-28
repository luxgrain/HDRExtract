"""Apple/iPhone HEIC -> analysis layers (primary + auxiliary + depth items).

Uses pillow-heif (which bundles libheif). Strategy:

    01_primary                  the primary/display image
    aux_00N_<category>          every auxiliary item libheif exposes
    depth_00N_<...>             depth / disparity images
    thumb_00N                   embedded thumbnails (optional)
    metadata.json               item inventory with ids, sizes, bit depths, types

We decode *everything* libheif gives us. Items whose semantic type we cannot
recognise are still saved and named ``aux_00N_unknown`` so nothing is lost.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from . import heif_boxes, metadata as md
from .common import (
    DependencyError,
    LOG,
    begin_saves,
    clean_output_dir,
    flush_saves,
    gainmap_log_boost,
    normalize_to_u8,
    save_image,
    slugify,
    upscale_to,
    write_metadata,
)


# Known Apple / MPEG auxiliary-image URNs -> friendly category.
def classify_aux(urn: str) -> str:
    u = (urn or "").lower()
    if "hdrgain" in u or "gainmap" in u:
        return "gainmap"
    if "disparity" in u:
        return "disparity"
    if "depth" in u:
        return "depth"
    if "segmentation" in u or "matte" in u or "semantic" in u:
        return "semantic"
    if "alpha" in u or u.endswith("auxid:1"):
        return "alpha"
    return "unknown"


def _save_layer(img: Image.Image, outdir: Path, name: str) -> str:
    """Save a layer, preserving bit depth where possible, else 8-bit RGB."""
    try:
        save_image(img, outdir, name)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("native save of %s failed (%s); falling back to 8-bit RGB", name, exc)
        save_image(img.convert("RGB"), outdir, name)
    return name


def _emit_gainmap_layers(gainmap: Image.Image, primary_size: tuple[int, int],
                         gm_meta: dict[str, Any] | None, outdir: Path,
                         notes: list[str]) -> list[dict[str, Any]]:
    """Upscale the gain map to primary resolution (nearest + bilinear) and, if
    ISO 21496-1 metadata is available, emit a calibrated log2-boost layer."""
    out: list[dict[str, Any]] = []
    gm_l = gainmap if gainmap.mode in ("L", "RGB") else gainmap.convert("L")

    # nearest = faithful to stored samples (analysis); bilinear = as real renderers do
    for method in ("nearest", "bilinear"):
        up = upscale_to(gm_l, primary_size, method)
        name = f"gainmap_upscaled_{method}"
        save_image(up, outdir, name)
        out.append({
            "file": f"{name}.png", "role": "gainmap_upscaled", "method": method,
            "size": [up.width, up.height], "source_size": [gm_l.width, gm_l.height],
        })

    # calibrated log boost using tmap metadata (channel 0)
    if gm_meta and gm_meta.get("channels_data"):
        ch = gm_meta["channels_data"][0]
        up_n = upscale_to(gm_l, primary_size, "nearest")
        g = np.asarray(up_n.convert("L"), np.float64) / 255.0
        lb = gainmap_log_boost(g, ch["gain_map_min"], ch["gain_map_max"], ch["gamma"])
        u8, vmin, vmax = normalize_to_u8(lb)
        save_image(Image.fromarray(u8), outdir, "gainmap_log_boost_calibrated")
        out.append({
            "file": "gainmap_log_boost_calibrated.png", "role": "gainmap_log_boost",
            "size": list(primary_size), "unit": "log2_stops",
            "vis_mapping": {"vmin": vmin, "vmax": vmax},
            "boost_stops": {"min": float(lb.min()), "max": float(lb.max()),
                            "mean": float(lb.mean())},
            "linear_peak": float(2 ** lb.max()),
            "gain_map_min": ch["gain_map_min"], "gain_map_max": ch["gain_map_max"],
            "gamma": ch["gamma"],
        })
        LOG.info("gain map calibrated: peak +%.3f stops (x%.2f)", lb.max(), 2 ** lb.max())
    else:
        notes.append("Gain map upscaled, but no ISO 21496-1 tmap metadata found; "
                     "calibrated log-boost skipped (raw gain map only).")
    return out


def extract(input_path: Path, outdir: Path, exiftool: str | None = None,
            save_thumbnails: bool = True, keep_hdr_bit_depth: bool = True) -> dict[str, Any]:
    """Extract HEIC primary + auxiliary/depth layers. Returns metadata dict."""
    try:
        import pillow_heif
    except Exception as exc:  # noqa: BLE001
        raise DependencyError(
            "pillow-heif is required for HEIC extraction. "
            "Install it with: python -m pip install pillow-heif"
        ) from exc

    pillow_heif.options.AUX_IMAGES = True
    pillow_heif.options.DEPTH_IMAGES = True
    pillow_heif.options.THUMBNAILS = True

    input_path = Path(input_path)
    clean_output_dir(outdir)
    begin_saves()
    layers: list[dict[str, Any]] = []
    notes: list[str] = []
    meta: dict[str, Any] = {
        "tool": "extract_heic_aux_layers",
        "input": str(input_path),
        "pillow_heif": pillow_heif.__version__,
        "libheif": str(pillow_heif.libheif_version()),
        "layers": layers,
        "notes": notes,
    }

    heif = pillow_heif.open_heif(str(input_path), convert_hdr_to_8bit=not keep_hdr_bit_depth)
    meta["top_level_images"] = len(heif)
    primary_index = getattr(heif, "primary_index", 0)
    meta["primary_index"] = primary_index

    aux_counter = 0
    aux_failed = 0
    depth_counter = 0
    thumb_counter = 0
    seen_aux_ids: set[int] = set()
    primary_pil: Image.Image | None = None
    primary_size: tuple[int, int] | None = None
    gainmap_pil: Image.Image | None = None

    for idx in range(len(heif)):
        himg = heif[idx]
        info = himg.info
        bit_depth = int(info.get("bit_depth", 8))
        is_primary = bool(info.get("primary", idx == primary_index))

        # --- primary / top-level image ------------------------------------ #
        try:
            pil = himg.to_pillow()
            if is_primary:
                name = "01_primary"
                primary_pil = pil
                primary_size = (pil.width, pil.height)
            else:
                name = f"image_{idx:02d}"
            _save_layer(pil, outdir, name)
            layers.append({
                "file": f"{name}.png",
                "role": "primary" if is_primary else "top_level_image",
                "index": idx,
                "size": [pil.width, pil.height],
                "mode": pil.mode,
                "bit_depth": bit_depth,
            })
        except Exception as exc:  # noqa: BLE001
            notes.append(f"top-level image {idx} decode failed: {exc}")
            LOG.warning("top-level image %d decode failed: %s", idx, exc)

        # --- depth images ------------------------------------------------- #
        for d in info.get("depth_images", []) or []:
            depth_counter += 1
            try:
                dpil = d.to_pillow()
                dmeta = (d.info or {}).get("metadata", {})
                name = f"depth_{depth_counter:03d}_{dpil.width}x{dpil.height}"
                _save_layer(dpil, outdir, name)
                layers.append({
                    "file": f"{name}.png",
                    "role": "depth",
                    "attached_to_image": idx,
                    "size": [dpil.width, dpil.height],
                    "mode": dpil.mode,
                    "depth_metadata": dmeta,
                })
            except Exception as exc:  # noqa: BLE001
                notes.append(f"depth image on item {idx} failed: {exc}")
                LOG.warning("depth image decode failed: %s", exc)

        # --- auxiliary images --------------------------------------------- #
        aux_map = info.get("aux", {}) or {}
        for aux_type, ids in aux_map.items():
            category = classify_aux(aux_type)
            for aux_id in ids:
                if aux_id in seen_aux_ids:
                    continue
                seen_aux_ids.add(aux_id)
                slug = slugify(aux_type.split(":")[-1] if aux_type else category)
                try:
                    apil = himg.get_aux_image(aux_id).to_pillow()
                    aux_counter += 1  # only number files we actually saved
                    name = f"aux_{aux_counter:03d}_{category}_{slug}"
                    if category == "gainmap" and gainmap_pil is None:
                        gainmap_pil = apil
                    _save_layer(apil, outdir, name)
                    layers.append({
                        "file": f"{name}.png",
                        "role": "auxiliary",
                        "status": "ok",
                        "category": category,
                        "aux_type": aux_type,
                        "aux_id": aux_id,
                        "attached_to_image": idx,
                        "size": [apil.width, apil.height],
                        "mode": apil.mode,
                    })
                except Exception as exc:  # noqa: BLE001
                    # Record the item in the inventory even when libheif/pillow-heif
                    # cannot decode it (e.g. 10-bit aux items are unsupported), so
                    # nothing is silently lost from metadata.json.
                    aux_failed += 1
                    layers.append({
                        "file": None,
                        "role": "auxiliary",
                        "status": "decode_failed",
                        "category": category,
                        "aux_type": aux_type,
                        "aux_id": aux_id,
                        "attached_to_image": idx,
                        "reason": str(exc),
                    })
                    notes.append(f"aux item {aux_id} ({aux_type}) failed: {exc}")
                    LOG.warning("aux item %s (%s) failed: %s", aux_id, aux_type, exc)

        # --- thumbnails (optional) ---------------------------------------- #
        if save_thumbnails:
            for t in info.get("thumbnails", []) or []:
                thumb_counter += 1
                try:
                    # thumbnails may be ints (ids) or image objects depending on version
                    tpil = t.to_pillow() if hasattr(t, "to_pillow") else None
                    if tpil is None:
                        continue
                    name = f"thumb_{thumb_counter:03d}_{tpil.width}x{tpil.height}"
                    _save_layer(tpil, outdir, name)
                    layers.append({
                        "file": f"{name}.png",
                        "role": "thumbnail",
                        "attached_to_image": idx,
                        "size": [tpil.width, tpil.height],
                    })
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("thumbnail skip: %s", exc)

    # --- tmap (ISO 21496-1) gain-map metadata ----------------------------- #
    gm_meta = None
    try:
        gm_meta = heif_boxes.extract_gainmap_metadata(input_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        LOG.debug("tmap metadata parse failed: %s", exc)
    if gm_meta:
        meta["iso21496_gainmap"] = gm_meta

    # --- gain map upscaled to primary resolution + calibrated log boost ---- #
    if gainmap_pil is not None and primary_size is not None:
        gm_layers = _emit_gainmap_layers(gainmap_pil, primary_size, gm_meta, outdir, notes)
        layers.extend(gm_layers)
    elif gainmap_pil is not None:
        notes.append("Gain map present but primary size unknown; upscale skipped.")

    if aux_counter == 0 and depth_counter == 0:
        notes.append("No auxiliary or depth items found (this HEIC may be a plain "
                     "single image).")

    # record presence of EXIF/XMP on the primary
    pinfo = heif[primary_index].info if len(heif) else {}
    meta["primary_has_exif"] = bool(pinfo.get("exif"))
    meta["primary_has_xmp"] = bool(pinfo.get("xmp"))
    meta["counts"] = {
        "top_level": len(heif),
        "auxiliary": aux_counter,
        "auxiliary_failed": aux_failed,
        "depth": depth_counter,
        "thumbnails": thumb_counter,
    }

    if exiftool:
        dump = md.exiftool_dump(exiftool, str(input_path))
        if dump:
            meta["exiftool"] = dump

    flush_saves()  # wait for all parallel layer writes to finish
    write_metadata(meta, outdir)
    LOG.info("HEIC: %d top-level, %d aux, %d depth, %d thumbnails",
             len(heif), aux_counter, depth_counter, thumb_counter)
    return meta
