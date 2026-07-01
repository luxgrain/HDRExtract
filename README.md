# HDRExtract

*Inspect smartphone HDR: Ultra HDR & HEIC gain map / depth / aux items as GIMP layers.*
スマホHDR写真の中身（gain map・depth・aux item・metadata）を解析用レイヤーとして取り出し、GIMPで開く inspector。

**[English](#english) · [日本語](#日本語) · [Technical references](#technical-references-specs)**

📦 GIMP plug-in download: **[Releases](../../releases/latest)**

---

## English

Extracts the hidden contents of **Android Ultra HDR JPEG** and **Apple/iPhone HEIC** photos —
SDR base, **gain map**, **depth**, **auxiliary items** (semantic mattes, …), metadata — as
**analysis layers (PNG) + `metadata.json`**, and loads them into **GIMP as aligned layers**.
It is an **inspector**: it prioritises *seeing what's inside* over colour accuracy, and never
modifies the input.

### What's different

Most gain-map / HEIC tools are **codecs** (libultrahdr), HDR **authoring** tools, or
**single-format extractors**. HDRExtract instead:

1. **Unifies Android Ultra HDR + Apple HEIC** in one tool.
2. **Surfaces every aux item as a named layer** (depth, gain map, semantic mattes; `unknown` if unrecognised).
3. **Loads them into GIMP** scaled to the primary resolution (GIMP's HEIF opens only the primary).
4. **Computes a calibrated log2 boost from both formats** ([one model](#how-it-works-hdr-internals)).
5. Is for **analysis**, not rendering/authoring.

The closest project, [heic-shenanigans](https://github.com/finnschi/heic-shenanigans), is
HEIC-only/EXR-focused with no Android, no matte inventory, no GIMP, no calibrated log-boost.

### Output layers

**Ultra HDR JPEG** (`scripts/extract_ultrahdr_layers.py`)

| File | Contents |
|---|---|
| `01_base_sdr` | decoded SDR base |
| `02_gainmap_raw` | appended gain-map JPEG, native res |
| `03_gainmap_upscaled_nearest` / `_bilinear` | gain map → base res ([why two](#how-it-works-hdr-internals)) |
| `04_gainmap_log_boost` | calibrated log2 boost, normalised view |
| `05_sdr_clipping_mask` | where the SDR base is (near) clipped |
| `06_reconstructed_hdr_preview` | approximate HDR reconstruction, tonemapped |
| `aux_00N_<semantic>` | extra GContainer items (Depth / Confidence / MotionPhoto), if present |
| `metadata.json` | streams, MPF, `hdrgm`, GContainer, ExifTool dump |

**HEIC** (`scripts/extract_heic_aux_layers.py`)

| File | Contents |
|---|---|
| `01_primary` | primary image (keeps its ICC profile, e.g. Display P3) |
| `aux_00N_<category>_<type>` | every decodable aux item (`gainmap/depth/semantic/alpha/unknown`) |
| `depth_00N_<WxH>` | depth / disparity (16-bit where applicable) |
| `gainmap_upscaled_nearest` / `_bilinear` | gain map → primary res |
| `gainmap_log_boost_calibrated` | log2 boost from the `tmap` ISO 21496-1 metadata |
| `metadata.json` | item inventory + `iso21496_gainmap` (min/max/gamma/offset/headroom) |

Unclassifiable items are still saved, so nothing is lost.

### Install & usage

```bash
python -m pip install -r requirements.txt          # Pillow, numpy, lxml, pillow-heif
python scripts/extract_ultrahdr_layers.py image.jpg    # → image_layers/
python scripts/extract_heic_aux_layers.py image.heic   # → image_layers/
```

**pillow-heif** bundles libheif (no system libheif needed). Optional, auto-detected:
**ExifTool** (richer metadata + MPImage gain-map extraction; pure-Python fallback otherwise).
CLI flags: `-o/--outdir`, `-v`, `--no-exiftool`, `--clip-threshold N` (ultrahdr),
`--no-thumbnails` / `--force-8bit` (heic).

**GIMP plug-in — `File > Open HDR Aux Layers…`** (also under `Filters`). It runs the
extractor in a system Python and loads the PNGs as layers (primary on top, others scaled to
match), with `metadata.json` attached as an image parasite.

> **Drop-in install (no clone, no env vars):** grab the zip from
> [Releases](../../releases/latest) → extract into a GIMP plug-ins search folder
> (*Edit > Preferences > Folders > Plug-ins*) → restart GIMP. The package bundles the code
> and, on first run, installs its Python deps into a plug-in-local `_vendor/` folder. The
> only requirement is a **Python 3** interpreter. (Build it yourself: `python gimp/build_package.py`.)

### How it works (HDR internals)

A gain map stores, per pixel, *how much to brighten* (normalised 0–1). Recovering real log2
boost needs **calibration metadata** (min/max/gamma/offset). **Android and Apple use the same
math — only the storage location differs** (see [references](#technical-references-specs)):

```
g          = gain_pixel / maxval                                   # 8-bit: /255
recovery   = g ** (1 / gamma)                                      # gamma default 1.0
log_boost  = gain_map_min + (gain_map_max - gain_map_min) * recovery   # log2 stops
linear×    = 2 ** log_boost
```

| | gain-map pixels | calibration metadata |
|---|---|---|
| **Ultra HDR JPEG** | appended secondary JPEG | gain-map sub-image XMP (`hdrgm:*`) |
| **HEIC** | aux item `hdrgainmap` | the **`tmap` derived item** (ISO 21496-1) |

**HEIC `tmap` (e.g. item 130).** Apple's HDR rendition is not a separate image but a `tmap`
tone-map **derived item** with **no coded pixels** — just ~62 bytes of ISO 21496-1 metadata
plus `dimg → [base, gainmap]` (a recipe). Per-pixel HDR comes from the half-res gain map; the
scalars (`gain_map_max`, `gamma`, offsets, headroom) come from the `tmap`. pillow-heif does
**not** expose it, so HDRExtract parses the HEIF boxes directly
([`heif_boxes.py`](hdrextract/heif_boxes.py), `construction_method=idat` aware) into
`metadata.json → iso21496_gainmap` (e.g. `gain_map_max≈1.469`, `gamma≈0.587` → **+1.47 stops ≈ ×2.77**).

**Gain-map upsampling.** Gain maps are stored low-res (measured: iPhone ½/side, Pixel ¼/side).
Real rendering uses **“bilinear or better”** (Ultra HDR spec; Apple can use guided
`CIEdgePreserveUpsample`), so we emit `*_nearest` (faithful to samples) **and** `*_bilinear`
(what you actually see).

**Reconstructed preview** (JPEG only, approximate): `HDR = (SDR+offset_sdr)·2^(log_boost·w) −
offset_hdr`, then Reinhard tonemap. Not colour-accurate. **ICC** profiles (e.g. Display P3) are
preserved end-to-end; per-item NCLX (`colr`) is read per ITU-T H.273.

### Technical references (specs)

The format/behaviour claims above trace to:

- **[Android Ultra HDR Image Format](https://developer.android.com/media/platform/hdr-image-format)** — `hdrgm` metadata, GContainer directory, reconstruction formula, “bilinear or better” upsampling.
- **ISO/IEC 21496-1** *Gain map metadata for image conversion* — the `tmap` payload fields & gain-map math (paywalled).
- **Adobe Gain Map specification** — the `hdrgm:*` XMP namespace used by Ultra HDR JPEG.
- **ISO/IEC 23008-12** *HEIF* — HEIC container: image items, `iloc`/`iref`/`iprp`(`ipco`/`ipma`), `grid` & `tmap` derived items, auxiliary images (paywalled).
- **Google Dynamic Depth / Container** — GContainer `Depth` / `Confidence` / `MotionPhoto` items.
- **[ITU-T H.273](https://www.itu.int/rec/T-REC-H.273)** (CICP) — colour primaries / transfer / matrix codes used to read NCLX.
- **[Apple ImageIO — auxiliary data](https://developer.apple.com/documentation/imageio/cgimageauxiliarydatatype)** — depth / disparity / matte / HDR gain map item types.
- **CIPA DC-007 (MPF)** — the Multi-Picture Format index used to locate the appended gain map.

### Limitations

- `06_reconstructed_hdr_preview` is approximate/tonemapped (no colour accuracy).
- **pillow-heif decodes only 8-bit aux**; Apple 10-bit aux (`linearthumbnail`, `styledeltamap`) are inventoried as `decode_failed` ([roadmap](#roadmap)).
- HEIC aux classification is a URN heuristic; `hdrgm` calibration is read from the gain-map sub-image XMP.

### Roadmap

Done: Ultra HDR + HEIC extraction & classification, calibrated log boost (both formats),
reconstructed preview (JPEG), gain-map upscaling, GContainer extras (Depth/Confidence/MotionPhoto),
GIMP 3.x drop-in plug-in. ⬜ **Next:** decode 10-bit aux (`linearthumbnail`/`styledeltamap`,
incl. grid stitching) via bundled ffmpeg.

### Prior art (tools)

Independent, clean-room — no code copied from these:
[libultrahdr](https://github.com/google/libultrahdr),
[heic-shenanigans](https://github.com/finnschi/heic-shenanigans),
[heif-hdrgainmap-decode](https://github.com/m13253/heif-hdrgainmap-decode),
[AppleJPEGGainMap](https://github.com/grapeot/AppleJPEGGainMap),
[toGainMapHDR](https://github.com/chemharuka/toGainMapHDR),
[HDR2gainmap](https://github.com/vastunghia/HDR2gainmap),
[heif-gimp-plugin](https://github.com/strukturag/heif-gimp-plugin),
[gimp-heic-avif-plugin](https://github.com/novomesk/gimp-heic-avif-plugin),
[tev](https://github.com/Tom94/tev),
[Awesome-Gain-Maps](https://github.com/NMoroney/Awesome-Gain-Maps).
Deps: [pillow-heif](https://github.com/bigcat88/pillow_heif), Pillow, numpy, lxml; optional [ExifTool](https://exiftool.org/), ffmpeg.

### License & layout

[Apache-2.0](LICENSE). © 2026 HDRExtract contributors. Deps keep their own licenses (not bundled).

```
hdrextract/  common.py metadata.py ultrahdr.py heic.py heif_boxes.py   # importable package
scripts/     extract_ultrahdr_layers.py  extract_heic_aux_layers.py    # CLIs
gimp/        gimp_open_hdr_aux_layers/  build_package.py               # GIMP 3.x plug-in + packager
```

---

## 日本語

**Android Ultra HDR JPEG** と **Apple/iPhone HEIC** の内部（SDR base・**gain map**・**depth**・
**auxiliary item**・metadata）を **解析用レイヤー(PNG) + `metadata.json`** として取り出し、**GIMPに
整列レイヤーとして**読み込む inspector。色再現より「中身を見える化」優先、入力は書き換えません。

### 既存ツールとの違い
gain map / HEIC の既存OSSは大抵 **codec**（libultrahdr）・**authoring**・**片方だけの抽出**。本ツールは:
**①Ultra HDR と HEIC を統一処理 ②全auxを名前付きレイヤー化（unknown含む） ③GIMPにprimary解像度で
整列読込 ④両形式から校正済みlog2 boost算出（[同一モデル](#how-it-works-hdr-internals)） ⑤解析特化**。

### 出力レイヤー
- **Ultra HDR JPEG**: `01_base_sdr` / `02_gainmap_raw` / `03_gainmap_upscaled_{nearest,bilinear}` /
  `04_gainmap_log_boost` / `05_sdr_clipping_mask` / `06_reconstructed_hdr_preview` / `metadata.json`。
  GContainerに **Depth/Confidence/MotionPhoto** があれば `aux_00N_<semantic>` として追加抽出。
- **HEIC**: `01_primary`（ICC=Display P3等を保持）/ `aux_00N_<category>_<type>`（gainmap/depth/
  semantic/alpha/unknownに分類・不明も保存）/ `depth_00N` / `gainmap_upscaled_{nearest,bilinear}` /
  `gainmap_log_boost_calibrated` / `metadata.json`（`iso21496_gainmap`含む）。

### 導入と使い方
```bash
python -m pip install -r requirements.txt
python scripts/extract_ultrahdr_layers.py image.jpg     # → image_layers/
python scripts/extract_heic_aux_layers.py image.heic
```
pillow-heif が libheif 同梱（システムlibheif不要）。**ExifTool** は任意（あれば詳細metadata＋
MPImage抽出、無ければ純Python fallback）。フラグ: `-o`,`-v`,`--no-exiftool`,`--clip-threshold`(jpeg),
`--no-thumbnails`/`--force-8bit`(heic)。

**GIMPプラグイン — `File > Open HDR Aux Layers…`**（`Filters` にも）。システムPythonで抽出を実行し
PNG群をレイヤー化（primary最上位・他はprimary解像度へスケール）、`metadata.json` を parasite に格納。

> **ワンパッケージ配置（クローン/環境変数 不要）:** [Releases](../../releases/latest) の zip を
> GIMPのプラグイン検索フォルダ（*Edit > Preferences > Folders > Plug-ins*）に展開して再起動。
> コード同梱で、初回起動時に依存を `_vendor/` へ自動install。必要なのは **Python 3** だけ。
> （自前ビルド: `python gimp/build_package.py`）

### HDRの仕組み（要点）
gain mapの画素は「どこを何倍か」(0–1)。実log2ブーストに戻すには**校正メタ**(min/max/gamma/offset)が
必要で、**Android/Appleは式が同じ・保管場所だけ違う**（[参照元](#technical-references-specs)）:
```
recovery  = (gain/maxval) ** (1/gamma)
log_boost = gain_map_min + (gain_map_max - gain_map_min) * recovery   # log2 stops
```
校正メタの在り処: **Ultra HDR** = gain mapサブ画像のXMP(`hdrgm:*`) / **HEIC** = **`tmap`派生アイテム**
(ISO 21496-1)。

**HEICの`tmap`（item 130等）** は独立画像でなく**画素を持たない派生アイテム**で、中身は~62バイトの
ISO 21496-1メタ + `dimg→[base, gainmap]`（＝合成レシピ）。per-pixel HDRは半解像度のgain map、係数は
tmap由来。pillow-heifは非公開のため**HEIFボックスを直接パース**（[`heif_boxes.py`](hdrextract/heif_boxes.py)、
`construction_method=idat`対応）→ `metadata.json → iso21496_gainmap`（例 max≈1.469/γ≈0.587 → **+1.47 stops ≈ ×2.77**）。

**補完**: gain mapは低解像度格納（iPhone ½/辺, Pixel ¼/辺）。実レンダは**「bilinear or better」**なので
`*_nearest`（サンプル忠実）と `*_bilinear`（実表示相当）を両方出力。**ICC(Display P3等)は保持**、
各itemのNCLX(`colr`)は ITU-T H.273 で解釈。

技術参照元の一覧は英語版 **[Technical references](#technical-references-specs)** を参照。

### 制限 / ロードマップ
- reconstructed preview は近似・色精度なし。**pillow-heifは8-bit auxのみ**デコード（Apple 10-bit aux
  `linearthumbnail`/`styledeltamap` は `decode_failed` 記録）。aux分類はURNヒューリスティック。
- ⬜ 次: 10-bit aux（grid合成含む）の bundled ffmpeg デコード。

### Prior art / License
謝辞・依存・参照元・ライセンスは英語版（[Prior art](#prior-art-tools) /
[Technical references](#technical-references-specs) / [License](#license--layout)）参照。**Apache-2.0**, © 2026 HDRExtract contributors。
