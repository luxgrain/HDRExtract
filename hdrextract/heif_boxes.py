"""Minimal ISO-BMFF / HEIF box reader for the bits pillow-heif does not expose.

We use this only to pull the ``tmap`` (tone-map / gain-map) derived item's
ISO 21496-1 gain-map metadata payload, which carries the calibration values
(gain_map_min/max, gamma, offsets, HDR headroom) needed to turn raw gain-map
pixels into real log2 boost. pillow-heif decodes pixels but does not surface
this metadata.
"""
from __future__ import annotations

import struct
from typing import Any

from .common import LOG


def _boxes(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size = struct.unpack_from(">I", buf, i)[0]
        typ = buf[i + 4:i + 8]
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, i + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        yield typ, i, hdr, i + size
        i += size


def _descend(buf, start, end, typ, full=False):
    """Return (body_start, box_end, box_start, hdr) for the first child *typ*."""
    for t, off, hdr, bend in _boxes(buf, start, end):
        if t == typ:
            return off + hdr + (4 if full else 0), bend, off, hdr
    return None


# --------------------------------------------------------------------------- #
# ISO 21496-1 gain map metadata
# --------------------------------------------------------------------------- #
def parse_iso21496_gainmap(payload: bytes) -> dict[str, Any] | None:
    """Parse an ISO 21496-1 GainMapMetadata blob (the ``tmap`` item payload).

    Layout: u16 minimum_version, u16 writer_version, 2 header bytes, then a
    sequence of (numerator, denominator) 32-bit rational pairs:
    base_hdr_headroom, alternate_hdr_headroom, then per channel
    (gain_map_min, gain_map_max, gamma, base_offset, alternate_offset).
    Channel count (1 or 3) is inferred from the payload length, which is robust
    to header flag-bit ambiguity.
    """
    if len(payload) < 6 + 7 * 8:
        return None
    minv, wrv = struct.unpack_from(">HH", payload, 0)
    body = payload[6:]
    n_pairs = len(body) // 8
    if n_pairs >= 17:
        channels = 3
    elif n_pairs >= 7:
        channels = 1
    else:
        return None

    def rat(idx: int, signed: bool = False) -> float:
        num, den = struct.unpack_from(">iI" if signed else ">II", body, idx * 8)
        return (num / den) if den else 0.0

    out: dict[str, Any] = {
        "minimum_version": minv,
        "writer_version": wrv,
        "channels": channels,
        "base_hdr_headroom": rat(0),
        "alternate_hdr_headroom": rat(1),
    }
    chans = []
    idx = 2
    for _c in range(channels):
        chans.append({
            "gain_map_min": rat(idx, signed=True),
            "gain_map_max": rat(idx + 1),
            "gamma": rat(idx + 2),
            "base_offset": rat(idx + 3, signed=True),
            "alternate_offset": rat(idx + 4, signed=True),
        })
        idx += 5
    out["channels_data"] = chans
    return out


# --------------------------------------------------------------------------- #
# Locate and read the tmap item payload
# --------------------------------------------------------------------------- #
def _item_types(data, ms, me) -> dict[int, str]:
    iinf = _descend(data, ms, me, b"iinf")
    types: dict[int, str] = {}
    if not iinf:
        return types
    s, e, ioff, ihdr = iinf
    ver = data[ioff + ihdr]
    q = ioff + ihdr + 4
    if ver == 0:
        q += 2
    else:
        q += 4
    for t, off, hdr, bend in _boxes(data, q, e):
        if t == b"infe":
            v = data[off + hdr]
            b = off + hdr + 4
            if v >= 2:
                if v == 2:
                    iid = struct.unpack_from(">H", data, b)[0]; b += 2
                else:
                    iid = struct.unpack_from(">I", data, b)[0]; b += 4
                b += 2  # protection index
                types[iid] = data[b:b + 4].decode("latin1")
    return types


def _item_payload(data, ms, me, target: int) -> bytes | None:
    iloc = _descend(data, ms, me, b"iloc")
    idat = _descend(data, ms, me, b"idat")
    idat_start = idat[0] if idat else None
    if not iloc:
        return None
    b0 = iloc[0]
    ver = data[b0]
    q = b0 + 4
    osz = data[q] >> 4
    lsz = data[q] & 0xF
    bosz = data[q + 1] >> 4
    isz = data[q + 1] & 0xF
    q += 2
    item_count = struct.unpack_from(">H" if ver < 2 else ">I", data, q)[0]
    q += 2 if ver < 2 else 4

    def rd(n):
        nonlocal q
        v = int.from_bytes(data[q:q + n], "big")
        q += n
        return v

    for _ in range(item_count):
        iid = rd(2 if ver < 2 else 4)
        cm = 0
        if ver in (1, 2):
            cm = rd(2) & 0xF
        rd(2)  # data_reference_index
        base = rd(bosz)
        ec = rd(2)
        off = ln = 0
        for _e in range(ec):
            if ver in (1, 2) and isz > 0:
                rd(isz)
            off = rd(osz)
            ln = rd(lsz)
        if iid == target:
            if cm == 1 and idat_start is not None:        # idat-relative
                base_off = idat_start + base + off
            else:                                          # file-absolute
                base_off = base + off
            return data[base_off:base_off + ln]
    return None


def extract_gainmap_metadata(data: bytes) -> dict[str, Any] | None:
    """Find the ``tmap`` item and return its parsed ISO 21496-1 gain-map metadata."""
    meta = _descend(data, 0, len(data), b"meta", full=True)
    if not meta:
        return None
    ms, me = meta[0], meta[1]
    types = _item_types(data, ms, me)
    tmap_ids = [i for i, t in types.items() if t == "tmap"]
    for tid in tmap_ids:
        payload = _item_payload(data, ms, me, tid)
        if payload:
            parsed = parse_iso21496_gainmap(payload)
            if parsed:
                parsed["tmap_item_id"] = tid
                parsed["payload_bytes"] = len(payload)
                LOG.debug("tmap item %d: ISO 21496-1 metadata parsed (%d bytes)",
                          tid, len(payload))
                return parsed
    return None
