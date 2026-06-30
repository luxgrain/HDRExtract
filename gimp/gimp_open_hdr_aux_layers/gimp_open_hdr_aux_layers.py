#!/usr/bin/env python3
"""GIMP 3.x plug-in: File > Open HDR Aux Layers...

Runs the hdrextract CLI (in the *system* Python, since GIMP's bundled Python
cannot import Pillow / numpy / pillow-heif) on a chosen Ultra HDR JPEG or HEIC
file, then loads every produced PNG as a layer in a single new image and stores
metadata.json as an image parasite.

Install: GIMP > Edit > Preferences > Folders > Plug-ins, add the repository's
``gimp`` folder, then restart GIMP. (See README.)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("GimpUi", "3.0")
from gi.repository import Gimp, GimpUi, GObject, GLib, Gio  # noqa: E402

PROC = "python-fu-open-hdr-aux-layers"
PARASITE_PERSISTENT = 1  # GIMP_PARASITE_PERSISTENT

HEIC_EXT = {".heic", ".heif", ".hif", ".avif"}
JPEG_EXT = {".jpg", ".jpeg", ".jpe"}


# --------------------------------------------------------------------------- #
# Plain-Python orchestration (no GIMP API here, so it is unit-testable)
# --------------------------------------------------------------------------- #
# This plug-in is self-contained: the hdrextract code is bundled next to it and
# dependencies are installed into a plugin-local _vendor dir on first run, so a
# drop-in deployment needs no repo clone and no environment variables.
PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PLUGIN_DIR / "_vendor"            # auto-installed dependencies
RUN_EXTRACT = PLUGIN_DIR / "_run_extract.py"   # subprocess entry point

DEPS = ["pillow", "numpy", "lxml", "pillow-heif"]
DEP_IMPORTS = "import PIL, numpy, lxml, pillow_heif"


def _clean_env() -> dict:
    """os.environ without GIMP's bundled-Python variables, so a *system* Python
    launched as a subprocess uses its own environment."""
    return {k: v for k, v in os.environ.items()
            if k not in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP")}


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


def _python_has_deps(cmd: list[str]) -> bool:
    """True if *cmd* can import the deps (checking the bundled _vendor dir too)."""
    code = f"import sys; sys.path.insert(0, r'{VENDOR_DIR}'); {DEP_IMPORTS}"
    try:
        r = subprocess.run([*cmd, "-c", code], capture_output=True,
                           env=_clean_env(), timeout=60, **_no_window())
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _pip_install_vendor(cmd: list[str]) -> tuple[bool, str]:
    """Install the dependencies into the plugin-local _vendor dir using *cmd*."""
    try:
        VENDOR_DIR.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [*cmd, "-m", "pip", "install", "--upgrade", "--target", str(VENDOR_DIR), *DEPS],
            capture_output=True, text=True, env=_clean_env(), timeout=900, **_no_window())
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _python_candidates() -> list[list[str]]:
    cands: list[list[str]] = []
    env = os.environ.get("HDREXTRACT_PYTHON")  # optional override, not required
    if env and Path(env).exists():
        cands.append([env])
    local = os.environ.get("LOCALAPPDATA", "")
    for v in ("Python313", "Python312", "Python311", "Python310"):
        fb = Path(local) / "Programs" / "Python" / v / "python.exe"
        if fb.exists():
            cands.append([str(fb)])
    for name in ("python", "python3"):
        w = shutil.which(name)
        if w and "WindowsApps" not in w:   # skip the Store alias stub
            cands.append([w])
    py = shutil.which("py")
    if py:
        cands.append([py, "-3"])
    seen, unique = set(), []
    for c in cands:
        if tuple(c) not in seen:
            seen.add(tuple(c))
            unique.append(c)
    return unique


def find_python(allow_install: bool = True) -> tuple[list[str] | None, str]:
    """Return (python_cmd, status). Probes for a Python that can import the deps
    (natively or via the bundled _vendor dir); if none and *allow_install*, pip
    installs them into _vendor on first run. No repo clone / env vars needed."""
    candidates = _python_candidates()
    if not candidates:
        return None, "No system Python 3 found on this machine (install Python 3)."
    for c in candidates:
        if _python_has_deps(c):
            return c, "ok"
    if not allow_install:
        return None, "No Python with the required packages."
    last = ""
    for c in candidates:
        ok, log = _pip_install_vendor(c)
        last = log
        if ok and _python_has_deps(c):
            return c, "installed"
    return None, (
        "Could not install dependencies automatically (offline / proxy?).\n"
        f"Run once with any Python:\n  python -m pip install --target \"{VENDOR_DIR}\" "
        f"{' '.join(DEPS)}\n\n{last[-500:]}")


def layer_priority(fname: str) -> int:
    """Stacking order: lower number => higher in the layer stack (on top)."""
    f = fname.lower()
    order = (
        "01_primary", "01_base_sdr", "primary", "base_sdr",
        "log_boost", "logboost",
        "reconstruct",
        "gainmap_upscaled_nearest", "03_gainmap_upscaled_nearest",
        "gainmap_upscaled_bilinear", "03_gainmap_upscaled_bilinear",
        "02_gainmap_raw", "gainmap_raw", "gainmap",
        "depth",
        "clip", "05_sdr_clipping",
        "aux_", "image_", "thumb_",
    )
    for i, key in enumerate(order):
        if key in f:
            return i
    return len(order)


def run_extractor(python: list[str], input_file: Path) -> tuple[int, str, Path]:
    """Run the bundled extractor dispatcher. Returns (rc, combined_log, outdir)."""
    outdir = input_file.parent / f"{input_file.stem}_layers"
    cmd = [*python, str(RUN_EXTRACT), str(input_file), "-o", str(outdir), "-v"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=_clean_env(), **_no_window())
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), outdir


# --------------------------------------------------------------------------- #
# GIMP-side image building
# --------------------------------------------------------------------------- #
def build_image(outdir: Path):
    """Load every PNG in *outdir* as a layer of one image; return the image.

    The first (highest-priority) layer is the primary; we use *its* loaded image
    as the base so its colour profile (e.g. Display P3) is preserved instead of
    pasting the pixels into a blank sRGB image. Every other layer is scaled up to
    the primary's resolution so all layers line up pixel-for-pixel.
    """
    pngs = sorted(outdir.glob("*.png"), key=lambda p: (layer_priority(p.name), p.name))
    if not pngs:
        return None

    Gimp.context_push()
    # bilinear when scaling the lower-resolution data layers to canvas size
    Gimp.context_set_interpolation(Gimp.InterpolationType.LINEAR)

    image = None
    canvas_w = canvas_h = 0
    for pos, png in enumerate(pngs):
        try:
            loaded = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE,
                                    Gio.File.new_for_path(str(png)))
        except Exception as exc:  # noqa: BLE001
            Gimp.message(f"hdrextract: failed to load {png.name}: {exc}")
            continue
        src_layers = loaded.get_layers()
        if not src_layers:
            loaded.delete()
            continue

        if image is None:
            # Primary: keep its own image (and ICC profile) as the canvas.
            image = loaded
            canvas_w, canvas_h = image.get_width(), image.get_height()
            image.get_layers()[0].set_name(f"{png.stem} [{canvas_w}x{canvas_h}]")
            continue

        w, h = loaded.get_width(), loaded.get_height()
        new_layer = Gimp.Layer.new_from_drawable(src_layers[0], image)
        new_layer.set_name(f"{png.stem} [{w}x{h}]")
        image.insert_layer(new_layer, None, pos)
        if w != canvas_w or h != canvas_h:
            new_layer.scale(canvas_w, canvas_h, False)
        loaded.delete()

    Gimp.context_pop()
    if image is not None:
        _attach_metadata(image, outdir)
    return image


def _attach_metadata(image, outdir: Path):
    meta_path = outdir / "metadata.json"
    if not meta_path.is_file():
        return
    raw = meta_path.read_bytes()
    image.attach_parasite(
        Gimp.Parasite.new("hdrextract-metadata", PARASITE_PERSISTENT, raw))
    # short human comment
    try:
        meta = json.loads(raw.decode("utf-8"))
        summary = _summarize(meta)
        image.attach_parasite(
            Gimp.Parasite.new("gimp-comment", PARASITE_PERSISTENT,
                              (summary + "\x00").encode("utf-8")))
    except Exception:  # noqa: BLE001
        pass


def _summarize(meta: dict) -> str:
    lines = [f"hdrextract: {meta.get('tool', '')}", f"input: {meta.get('input', '')}"]
    if "hdrgm" in meta:
        f = meta["hdrgm"].get("fields_parsed", {})
        lines.append("hdrgm: " + ", ".join(f"{k}={v}" for k, v in f.items()))
    if "iso21496_gainmap" in meta:
        g = meta["iso21496_gainmap"]
        ch = (g.get("channels_data") or [{}])[0]
        lines.append("ISO21496-1 gain map: "
                     f"max={ch.get('gain_map_max')}, gamma={ch.get('gamma')}, "
                     f"headroom={g.get('alternate_hdr_headroom')}")
    if "counts" in meta:
        lines.append("counts: " + json.dumps(meta["counts"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Plug-in registration
# --------------------------------------------------------------------------- #
class HdrAuxLayers(Gimp.PlugIn):
    # Disable i18n for this plug-in. If do_set_i18n is not implemented (or does
    # not return False), GIMP 3.x can fail to register the procedures silently.
    def do_set_i18n(self, procname):
        return False

    def do_query_procedures(self):
        return [PROC]

    def do_create_procedure(self, name):
        # An ImageProcedure (not a plain Procedure) is required for the entry to
        # actually appear in GIMP's menus. We allow it to run with no image open.
        proc = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN,
                                       self.run, None)
        proc.set_image_types("*")
        proc.set_menu_label("Open HDR Aux Layers...")
        proc.add_menu_path("<Image>/File/[Export]")
        proc.add_menu_path("<Image>/Filters/HDR Aux Layers")
        # Keep the item enabled even when no image is open.
        try:
            m = Gimp.ProcedureSensitivityMask
            proc.set_sensitivity_mask(
                m.NO_IMAGE | m.DRAWABLE | m.DRAWABLES | m.NO_DRAWABLES)
        except Exception:  # noqa: BLE001
            pass
        proc.set_documentation(
            "Open smartphone HDR gain map / depth / aux items as layers",
            "Runs the hdrextract CLI on an Ultra HDR JPEG or HEIC and loads the "
            "resulting PNGs as layers, with metadata.json as an image parasite.",
            name)
        proc.set_attribution("hdrextract", "hdrextract", "2026")
        proc.add_file_argument(
            "input-file", "_Input file",
            "Android Ultra HDR JPEG or Apple/iPhone HEIC",
            Gimp.FileChooserAction.OPEN, False, None, GObject.ParamFlags.READWRITE)
        return proc

    def run(self, procedure, run_mode, image, drawables, config, run_data):
        if run_mode == Gimp.RunMode.INTERACTIVE:
            GimpUi.init(PROC)
            dialog = GimpUi.ProcedureDialog.new(procedure, config,
                                                "Open HDR Aux Layers")
            dialog.fill(None)
            if not dialog.run():
                dialog.destroy()
                return procedure.new_return_values(Gimp.PDBStatusType.CANCEL,
                                                   GLib.Error())
            dialog.destroy()

        gfile = config.get_property("input-file")
        if gfile is None or gfile.get_path() is None:
            return self._fail(procedure, "No input file selected.")
        input_file = Path(gfile.get_path())
        if not input_file.is_file():
            return self._fail(procedure, f"File not found: {input_file}")

        if not RUN_EXTRACT.is_file():
            return self._fail(procedure,
                              f"Plug-in package is incomplete: {RUN_EXTRACT.name} is "
                              "missing next to the plug-in.")
        Gimp.progress_init("Preparing HDRExtract (first run may install dependencies)…")
        try:
            python, status = find_python()
        finally:
            Gimp.progress_end()
        if python is None:
            return self._fail(procedure, status)

        Gimp.progress_init(f"Extracting layers from {input_file.name} …")
        try:
            rc, log, outdir = run_extractor(python, input_file)
        except Exception as exc:  # noqa: BLE001
            return self._fail(procedure, f"Extractor failed to launch: {exc}")
        finally:
            Gimp.progress_end()

        if rc != 0:
            return self._fail(procedure,
                              f"Extractor exited with code {rc}.\n{log[-1500:]}")

        image = build_image(outdir)
        if image is None:
            return self._fail(procedure, f"No layers were produced in {outdir}.")

        Gimp.Display.new(image)
        Gimp.displays_flush()
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    @staticmethod
    def _fail(procedure, message: str):
        Gimp.message(f"Open HDR Aux Layers: {message}")
        error = GLib.Error.new_literal(GLib.quark_from_string("hdrextract"), message, 0)
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, error)


Gimp.main(HdrAuxLayers.__gtype__, sys.argv)
