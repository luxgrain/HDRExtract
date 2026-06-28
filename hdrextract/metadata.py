"""Metadata parsing for Ultra HDR JPEGs.

Three concerns live here, all pure-Python (no hard external dependency):

1. Marker-aware scanning of a JPEG byte stream to locate every *top-level*
   JPEG (the SDR base + the appended gain-map image). This is robust against
   embedded EXIF/MPF thumbnails because length-prefixed APPn segments are
   skipped wholesale rather than scanned for byte patterns.
2. Extraction of standard + extended XMP packets.
3. Parsing of the ``hdrgm`` (gain-map) and Google ``GContainer`` namespaces.

ExifTool, if available, is used only for an additional rich metadata dump.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from .common import LOG

# --------------------------------------------------------------------------- #
# JPEG marker walking
# --------------------------------------------------------------------------- #
SOI = b"\xff\xd8"


def _scan_one_jpeg(data: bytes, start: int) -> int:
    """Return the end offset (exclusive) of the JPEG stream starting at *start*.

    *start* must point at an SOI (0xFFD8). Walks markers, treating SOS entropy
    data specially, and stops just after the EOI (0xFFD9). If no EOI is found,
    returns len(data).
    """
    n = len(data)
    i = start + 2
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xD9:  # EOI
            return i + 2
        if marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            # stuffed byte / TEM / restart markers carry no length
            i += 2
            continue
        if i + 3 >= n:
            break
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if marker == 0xDA:  # SOS - entropy-coded data follows the header
            i += 2 + seg_len
            while i < n - 1:
                if data[i] == 0xFF:
                    m = data[i + 1]
                    if m == 0x00 or (0xD0 <= m <= 0xD7):
                        i += 2  # byte stuffing or restart marker
                        continue
                    if m == 0xFF:
                        i += 1
                        continue
                    break  # a real marker (DHT, another SOS, or EOI)
                i += 1
            continue
        i += 2 + seg_len
    return n


def iter_jpeg_streams(data: bytes) -> list[tuple[int, int]]:
    """Return [(start, end), ...] byte ranges of every top-level JPEG stream."""
    streams: list[tuple[int, int]] = []
    i = 0
    n = len(data)
    while i < n - 1:
        if data[i] == 0xFF and data[i + 1] == 0xD8:
            end = _scan_one_jpeg(data, i)
            streams.append((i, end))
            i = max(end, i + 2)
        else:
            i += 1
    return streams


# --------------------------------------------------------------------------- #
# APPn / XMP / MPF segment parsing
# --------------------------------------------------------------------------- #
XMP_STD_HDR = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXT_HDR = b"http://ns.adobe.com/xmp/extension/\x00"
MPF_HDR = b"MPF\x00"


def _iter_app_segments(data: bytes):
    """Yield (marker_byte, payload_bytes, file_offset_of_payload) for APP0-15.

    Only walks the first JPEG stream's header segments (stops at SOS/EOI).
    """
    n = len(data)
    i = 0
    if not (n >= 2 and data[0] == 0xFF and data[1] == 0xD8):
        return
    i = 2
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD9, 0xDA):  # EOI or SOS -> header section is over
            return
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 3 >= n:
            return
        seg_len = (data[i + 2] << 8) | data[i + 3]
        payload_start = i + 4
        payload = data[payload_start : i + 2 + seg_len]
        if 0xE0 <= marker <= 0xEF:
            yield marker, payload, payload_start
        i += 2 + seg_len


def extract_xmp(data: bytes) -> dict[str, str | None]:
    """Extract standard and reassembled extended XMP from a JPEG."""
    standard: bytes | None = None
    ext_chunks: dict[str, dict[int, bytes]] = {}
    ext_lengths: dict[str, int] = {}

    for marker, payload, _off in _iter_app_segments(data):
        if marker != 0xE1:
            continue
        if payload.startswith(XMP_STD_HDR):
            standard = payload[len(XMP_STD_HDR) :]
        elif payload.startswith(XMP_EXT_HDR):
            body = payload[len(XMP_EXT_HDR) :]
            # 32-byte ASCII GUID, 4-byte total length, 4-byte chunk offset
            if len(body) < 40:
                continue
            guid = body[:32].decode("ascii", "replace")
            total = int.from_bytes(body[32:36], "big")
            offset = int.from_bytes(body[36:40], "big")
            ext_chunks.setdefault(guid, {})[offset] = body[40:]
            ext_lengths[guid] = total

    extended: bytes | None = None
    if ext_chunks:
        # Reassemble the largest GUID group in offset order.
        guid = max(ext_chunks, key=lambda g: ext_lengths.get(g, 0))
        parts = ext_chunks[guid]
        extended = b"".join(parts[o] for o in sorted(parts))

    return {
        "standard": standard.decode("utf-8", "replace") if standard else None,
        "extended": extended.decode("utf-8", "replace") if extended else None,
    }


def parse_mpf(data: bytes) -> dict[str, Any] | None:
    """Parse the MPF (Multi-Picture Format) APP2 index, if present.

    Returns a dict with per-image attribute/size/offset entries. Offsets are
    recorded as stored (relative to the MPF TIFF header) plus an absolute file
    offset best-effort. Extraction does not rely on this - the marker scan is
    authoritative - but it is recorded in metadata for inspection.
    """
    for marker, payload, payload_off in _iter_app_segments(data):
        if marker != 0xE2 or not payload.startswith(MPF_HDR):
            continue
        tiff = payload[len(MPF_HDR) :]
        tiff_file_off = payload_off + len(MPF_HDR)
        if len(tiff) < 8:
            return None
        endian = "<" if tiff[:2] == b"II" else ">"
        import struct

        first_ifd = struct.unpack_from(endian + "I", tiff, 4)[0]
        if first_ifd + 2 > len(tiff):
            return None
        count = struct.unpack_from(endian + "H", tiff, first_ifd)[0]
        entries = {}
        mp_entry_off = None
        num_images = None
        pos = first_ifd + 2
        for _ in range(count):
            if pos + 12 > len(tiff):
                break
            tag, typ, cnt = struct.unpack_from(endian + "HHI", tiff, pos)
            val_off = pos + 8
            if tag == 0xB001:  # NumberOfImages
                num_images = struct.unpack_from(endian + "I", tiff, val_off)[0]
            elif tag == 0xB002:  # MP Entry
                data_offset = struct.unpack_from(endian + "I", tiff, val_off)[0]
                mp_entry_off = data_offset
            entries[hex(tag)] = {"type": typ, "count": cnt}
            pos += 12

        images = []
        if mp_entry_off is not None and num_images:
            for k in range(num_images):
                eoff = mp_entry_off + k * 16
                if eoff + 16 > len(tiff):
                    break
                attr, size, off = struct.unpack_from(endian + "III", tiff, eoff)
                images.append(
                    {
                        "index": k,
                        "attribute": hex(attr),
                        "size": size,
                        "stored_offset": off,
                        "abs_offset": (0 if off == 0 else tiff_file_off + off),
                    }
                )
        return {"num_images": num_images, "images": images, "tags": entries}
    return None


# --------------------------------------------------------------------------- #
# hdrgm / GContainer namespace parsing
# --------------------------------------------------------------------------- #
HDRGM_NS = "http://ns.adobe.com/hdr-gain-map/1.0/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
GCONTAINER_NS = "http://ns.google.com/photos/1.0/container/"
GCONTAINER_ITEM_NS = "http://ns.google.com/photos/1.0/container/item/"

HDRGM_FIELDS = [
    "Version",
    "BaseRenditionIsHDR",
    "GainMapMin",
    "GainMapMax",
    "Gamma",
    "OffsetSDR",
    "OffsetHDR",
    "HDRCapacityMin",
    "HDRCapacityMax",
]

# Spec defaults applied when a field is absent (single-channel).
HDRGM_DEFAULTS = {
    "BaseRenditionIsHDR": False,
    "GainMapMin": 0.0,
    "GainMapMax": 1.0,
    "Gamma": 1.0,
    "OffsetSDR": 1.0 / 64.0,
    "OffsetHDR": 1.0 / 64.0,
    "HDRCapacityMin": 0.0,
    "HDRCapacityMax": 1.0,
}


@dataclass
class HdrgmMeta:
    found: bool = False
    fields: dict[str, Any] = field(default_factory=dict)        # parsed values
    raw_present: dict[str, bool] = field(default_factory=dict)  # was it in XMP?
    gcontainer: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None  # "standard" / "extended" / None

    def value(self, name: str, channel: int = 0) -> Any:
        """Return a field value (channel-aware) or its spec default."""
        v = self.fields.get(name, HDRGM_DEFAULTS.get(name))
        if isinstance(v, list):
            return v[channel] if channel < len(v) else v[-1]
        return v


def _strip_to_xmpmeta(xmp: str) -> bytes | None:
    m = re.search(r"<x:xmpmeta.*?</x:xmpmeta>", xmp, re.DOTALL)
    if not m:
        m = re.search(r"<rdf:RDF.*?</rdf:RDF>", xmp, re.DOTALL)
    return m.group(0).encode("utf-8") if m else None


def _parse_value(text: str) -> Any:
    text = text.strip()
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        if any(c in text for c in ".eE"):
            return float(text)
        return int(text)
    except ValueError:
        return text


def _read_field(root: etree._Element, local_name: str) -> tuple[Any, bool]:
    """Find an hdrgm field as attribute or element; return (value, present)."""
    qname = "{%s}%s" % (HDRGM_NS, local_name)
    # Attribute form on any element.
    for el in root.iter():
        if qname in el.attrib:
            return _parse_value(el.attrib[qname]), True
    # Element form (text or rdf:Seq for per-channel values).
    for el in root.iter(qname):
        seq = el.find("{%s}Seq" % RDF_NS)
        if seq is not None:
            vals = [
                _parse_value(li.text or "")
                for li in seq.findall("{%s}li" % RDF_NS)
                if li.text is not None
            ]
            if vals:
                return (vals if len(vals) > 1 else vals[0]), True
        if el.text and el.text.strip():
            return _parse_value(el.text), True
    return None, False


def _read_gcontainer(root: etree._Element) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # Items live in a Container:Directory rdf:Seq of Container:Item descriptions.
    for el in root.iter():
        tag = etree.QName(el).localname if el.tag is not etree.Comment else ""
        if tag != "Item":
            continue
        entry = {}
        for k, v in el.attrib.items():
            q = etree.QName(k)
            if q.namespace == GCONTAINER_ITEM_NS:
                entry[q.localname] = v
        # Also handle element-form sub-properties.
        for child in el:
            q = etree.QName(child)
            if q.namespace == GCONTAINER_ITEM_NS and child.text:
                entry.setdefault(q.localname, child.text.strip())
        if entry:
            items.append(entry)
    return items


def merge_hdrgm(primary: HdrgmMeta, secondary: HdrgmMeta) -> HdrgmMeta:
    """Merge two hdrgm parses.

    In the Google Ultra HDR layout the calibration fields (GainMapMin/Max,
    Gamma, Offset*, HDRCapacity*) live in the *gain-map sub-image* XMP, while
    the primary carries only hdrgm:Version + the GContainer directory. So the
    *secondary* (gain-map) parse wins for any field it actually provides.
    """
    out = HdrgmMeta()
    out.fields = dict(primary.fields)
    out.raw_present = dict(primary.raw_present)
    for k, v in secondary.fields.items():
        out.fields[k] = v
    for k, present in secondary.raw_present.items():
        out.raw_present[k] = bool(out.raw_present.get(k)) or present
    out.gcontainer = primary.gcontainer or secondary.gcontainer
    out.found = primary.found or secondary.found
    srcs = []
    if primary.source:
        srcs.append(primary.source)
    if secondary.source:
        srcs.append(f"gainmap:{secondary.source}")
    out.source = "+".join(srcs) if srcs else None
    return out


def parse_hdrgm(xmp_packets: dict[str, str | None]) -> HdrgmMeta:
    """Parse hdrgm + GContainer metadata from the available XMP packets."""
    meta = HdrgmMeta()
    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    for source in ("extended", "standard"):
        xmp = xmp_packets.get(source)
        if not xmp:
            continue
        chunk = _strip_to_xmpmeta(xmp)
        if not chunk:
            continue
        try:
            root = etree.fromstring(chunk, parser=parser)
        except etree.XMLSyntaxError as exc:
            LOG.debug("XMP parse failed for %s packet: %s", source, exc)
            continue
        if root is None:
            continue

        got_any = False
        for name in HDRGM_FIELDS:
            val, present = _read_field(root, name)
            meta.raw_present[name] = present
            if present:
                meta.fields[name] = val
                got_any = True
        gc = _read_gcontainer(root)
        if gc and not meta.gcontainer:
            meta.gcontainer = gc
        if got_any and not meta.found:
            meta.found = True
            meta.source = source
    return meta


# --------------------------------------------------------------------------- #
# ExifTool dump (optional)
# --------------------------------------------------------------------------- #
def exiftool_dump(exiftool: str, path: str) -> dict[str, Any] | None:
    """Run ExifTool and return a parsed metadata dict, or None on failure."""
    try:
        out = subprocess.run(
            [exiftool, "-j", "-a", "-G1", "-struct", "-n", str(path)],
            capture_output=True,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("ExifTool invocation failed: %s", exc)
        return None
    if out.returncode != 0:
        LOG.warning("ExifTool returned %d: %s", out.returncode, out.stderr.decode("utf-8", "replace")[:200])
    try:
        parsed = json.loads(out.stdout.decode("utf-8", "replace"))
        return parsed[0] if isinstance(parsed, list) and parsed else parsed
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Could not parse ExifTool JSON: %s", exc)
        return None
