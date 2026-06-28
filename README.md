# HDRExtract

*Inspect smartphone HDR: Ultra HDR & HEIC gain map / depth / aux items as GIMP layers.*

スマホHDR写真の内部（gain map・depth・auxiliary item・metadata）を解析用レイヤーとして取り出し、GIMPに読み込むツール。

**[English](#english) · [日本語](#日本語)**

---

## English

HDRExtract pulls the hidden contents of smartphone HDR photos — the SDR base, the
**gain map**, **depth/disparity**, **auxiliary items** (semantic mattes, etc.) and the
metadata — out of **Android Ultra HDR JPEG** and **Apple/iPhone HEIC** files, saving
them as **analysis layers (PNG) + `metadata.json`**, and loading them into **GIMP as
aligned layers**.

> This is an **inspector**: it prioritises *seeing what is inside* over colour-accurate
> reproduction. It never modifies the input file.

### What's different

Most gain-map / HEIC open-source tools are **codecs** (libultrahdr), HDR **authoring**
tools (toGainMapHDR), or **single-format extractors** (heic-shenanigans). HDRExtract is
oriented differently:

1. **Unifies Android Ultra HDR and Apple HEIC** in one tool (most do only one).
2. **Surfaces every aux item as a named layer** — depth, gain map, semantic mattes
   (sky / skin / hair / teeth / glasses …), and anything unrecognised as `unknown`.
3. **Loads them into GIMP** scaled to the primary resolution (GIMP's HEIF support only
   exposes the primary image).
4. **Computes a calibrated log2-boost from both formats** — Android `hdrgm` XMP and
   Apple ISO 21496-1 `tmap`, unified into one model.
5. Built for **analysis/visualisation**, not rendering or authoring.

The closest single project, [heic-shenanigans](https://github.com/finnschi/heic-shenanigans),
is HEIC-only and EXR-focused, with no Android support, no semantic-matte inventory, no GIMP
integration, and no calibrated log-boost. See [Prior art](#prior-art--acknowledgments).

### Features

**Android Ultra HDR JPEG → layers** (`scripts/extract_ultrahdr_layers.py`)

| File | Contents |
|---|---|
| `01_base_sdr.png` | decoded primary / SDR base |
| `02_gainmap_raw.png` | appended secondary JPEG (the gain map), native res |
| `03_gainmap_upscaled_nearest.png` | gain map → base res, **nearest** (faithful to samples) |
| `03_gainmap_upscaled_bilinear.png` | gain map → base res, **bilinear** (as real renderers do) |
| `04_gainmap_log_boost.png` | calibrated log2 boost, min/max-normalised view |
| `05_sdr_clipping_mask.png` | where the SDR base is (near) clipped |
| `06_reconstructed_hdr_preview.png` | approximate HDR reconstruction, tonemapped to SDR |
| `aux_00N_<semantic>.png` | extra Google-container items, if present (see below) |
| `metadata.json` | sizes, JPEG streams, MPF, `hdrgm`, GContainer, ExifTool dump |

> Ultra HDR itself is only base + gain map, but the same Google container (GContainer)
> can also carry **Depth**, **Confidence**, or a **MotionPhoto** video (Pixel Portrait /
> Motion modes). HDRExtract slices these out by their directory `Length` and saves them as
> `aux_00N_<semantic>` layers (images) or raw files (video) — Ultra HDR's answer to HEIC aux.

**Apple/iPhone HEIC → layers** (`scripts/extract_heic_aux_layers.py`)

| File | Contents |
|---|---|
| `01_primary.png` | primary / display image (keeps its ICC profile, e.g. Display P3) |
| `aux_00N_<category>_<type>.png` | every decodable auxiliary item |
| `depth_00N_<WxH>.png` | depth / disparity (16-bit preserved where applicable) |
| `gainmap_upscaled_nearest.png` / `_bilinear.png` | gain map → primary res |
| `gainmap_log_boost_calibrated.png` | log2 boost from the `tmap` ISO 21496-1 metadata |
| `metadata.json` | item inventory + `iso21496_gainmap` (min/max/gamma/offset/headroom) |

Aux types (URNs) are classified into `gainmap / depth / disparity / semantic / alpha /
unknown`. Items we cannot classify are still saved so **nothing is lost**.

### Requirements & install

```bash
python -m pip install -r requirements.txt
```

- **Required (pip):** Python 3.10+, Pillow, numpy, lxml, and **pillow-heif** (bundles
  libheif, so no system libheif install is needed for HEIC).
- **Optional (auto-detected if present):**
  - **ExifTool** — richer metadata dump + MPImage-based gain-map extraction.
    `winget install OliverBetz.ExifTool` / `brew install exiftool` /
    `apt install libimage-exiftool-perl`. Pure-Python fallback if absent.
  - **ffmpeg** via `imageio-ffmpeg` — for decoding 10-bit aux items (roadmap).

### Usage (CLI)

```bash
python scripts/extract_ultrahdr_layers.py path/to/image.jpg      # → image_layers/
python scripts/extract_heic_aux_layers.py path/to/image.heic     # → image_layers/
python scripts/extract_ultrahdr_layers.py image.jpg -o out_dir -v
```

Output goes to `<stem>_layers/` next to the input by default. The directory is cleaned
of previous generated files each run.

| Option | Tool | Meaning |
|---|---|---|
| `-o, --outdir DIR` | both | output directory |
| `-v, --verbose` | both | debug logging |
| `--no-exiftool` | both | do not use ExifTool even if present |
| `--clip-threshold N` | ultrahdr | 0–255 channel value treated as clipped (default 250) |
| `--no-thumbnails` | heic | skip embedded thumbnail items |
| `--force-8bit` | heic | convert high-bit-depth images to 8-bit on decode |

### GIMP plug-in (File > Open HDR Aux Layers…)

GIMP's bundled Python cannot import Pillow / pillow-heif, so the plug-in **runs the CLI
in your system Python as a subprocess** and loads the resulting PNGs as layers.
`metadata.json` is attached as an image parasite (`hdrextract-metadata`) + comment.

**Install:**
1. GIMP > **Edit > Preferences > Folders > Plug-ins**
2. Add this repo's **`gimp` folder** (e.g. `C:\path\to\HDRExtract\gimp`)
3. Restart GIMP → **File > Open HDR Aux Layers…** (or **Filters > HDR Aux Layers**)

Works with the Microsoft Store build of GIMP too. If auto-detection fails, set
`HDREXTRACT_PYTHON` (path to a python.exe with the deps) and/or `HDREXTRACT_HOME`
(this repo). The plug-in puts the **primary on top** and scales every other layer to the
primary resolution so they line up.

### How log boost works (Android & Apple, one model)

Gain-map **pixels** encode "how much to brighten, where" normalised to 0..1. Turning them
back into real log2 boost (stops) needs **calibration metadata** (min/max/gamma/offset).
Android and Apple use the **same formula** — only the storage location differs.

```
g          = gain_pixel / maxval                       # normalise (8-bit: /255)
recovery   = g ** (1 / gamma)                           # gamma default 1.0
log_boost  = gain_map_min + (gain_map_max - gain_map_min) * recovery   # log2 stops
linear×    = 2 ** log_boost
```

| | gain-map pixels | calibration metadata |
|---|---|---|
| **Android Ultra HDR JPEG** | appended secondary JPEG | the **gain-map sub-image XMP** (`hdrgm:*`) |
| **Apple/iPhone HEIC** | aux item `hdrgainmap` | the **`tmap` derived item's ISO 21496-1 metadata** |

#### HEIC `tmap` (e.g. item 130) — the key behaviour

In Apple HEIC, the HDR rendition is **not a separate image** but a `tmap` (tone-map)
**derived item**. It has **no coded pixels** (no `hvcC`); its payload is ~62 bytes of
ISO 21496-1 gain-map metadata:

```
dimg: 130 -> [46, 62]     # item 130 = recipe combining base(46) + gainmap(62)
```

- **Item 130 has no pixels of its own.** Its `ispe`/`pixi` match the primary only because
  it declares the *output* HDR dimensions.
- Per-pixel HDR data comes from the **gain-map pixels (item 62, half-res)**; the scaling
  constants come from **item 130's 62 bytes** (`gain_map_max`, `gamma`, `offset`,
  `hdr_headroom`).
- **pillow-heif does not expose this `tmap` metadata**, so HDRExtract parses the HEIF
  boxes directly (`hdrextract/heif_boxes.py`, `construction_method=idat` aware) and stores
  the result in `metadata.json → iso21496_gainmap`
  (e.g. `gain_map_max≈1.469`, `gamma≈0.587`, `alternate_hdr_headroom≈1.469` →
  **+1.47 stops ≈ ×2.77 headroom**).

> Other aux such as `linearthumbnail` / `styledeltamap` are Apple-internal reference
> images and are **not used for HDR display** (the `tmap` recipe references only
> base + gainmap).

#### Reconstructed HDR preview (approximate; currently JPEG only)

`06_reconstructed_hdr_preview.png` applies the boost and tonemaps back for display:

```
HDR_lin = (SDR_lin + offset_sdr) * 2**(log_boost * weight) - offset_hdr
display = HDR_lin / (1 + HDR_lin)       # Reinhard tonemap
```

`weight` depends on display headroom (1 at full HDR). **Not colour-accurate** — it is for
*seeing* where the boost lands. Missing calibration falls back to spec defaults, recorded
in `metadata.json` (`*.defaults_used` / `notes`).

### Gain-map upsampling

Gain maps are stored at lower resolution (measured: **iPhone HEIC = ½ per side**,
**Pixel Ultra HDR = ¼ per side**). Real HDR rendering upsamples with **"bilinear or
better"** (the Ultra HDR spec mandates ≥bilinear; Apple's CoreImage can use the
guided `CIEdgePreserveUpsample`). HDRExtract outputs both:

- **`*_nearest`** — no interpolation, **faithful** to stored samples (blocky).
- **`*_bilinear`** — **matches real rendering**, close to what you actually see.

### Design guarantees

- Never modifies the input file (read-only).
- One failed layer never aborts the run — **whatever was produced is saved**, failures go
  to `metadata.json → notes` and the log.
- Windows-aware path handling; ExifTool location auto-detected.
- Clear errors when a hard dependency is missing.

### Validation

Verified on real devices: a **Pixel Ultra HDR JPEG** (8160×6144; ExifTool MPImage2 path;
`hdrgm` read from the gain-map XMP; SOI/EOI scan fallback also exercised) and an **iPhone
HEIC** (primary + depth + `hdrgainmap` + 6 semantic mattes; `tmap` ISO 21496-1 parsed;
two 10-bit aux recorded as `decode_failed`). No sample files are bundled — pass any file
path.

### Limitations

- `06_reconstructed_hdr_preview` is approximate and tonemapped (no colour accuracy).
- **pillow-heif only decodes 8-bit aux items.** Apple's 10-bit aux
  (`linearthumbnail`, `styledeltamap`) are inventoried with `status:"decode_failed"`
  (pixels not yet rasterised — see roadmap).
- HEIC aux classification is a URN-substring heuristic.
- Ultra HDR `hdrgm` calibration is read preferentially from the gain-map sub-image XMP.

### Roadmap

1–7 ✅ Ultra HDR + HEIC extraction, classification, calibrated log boost (both formats),
reconstructed preview (JPEG), gain-map upscaling, and the GIMP 3.x plug-in are done.
8 ⬜ Decode 10-bit aux (`linearthumbnail` / `styledeltamap`, incl. grid stitching) via
bundled ffmpeg.
9 ✅ Extract extra Ultra HDR GContainer items (Depth / Confidence / MotionPhoto).

### Prior art / acknowledgments

HDRExtract is an independent, clean-room tool — it does **not** copy code from the
projects below. They are credited as prior art and as references for understanding the
formats.

- Codec / spec: [google/libultrahdr](https://github.com/google/libultrahdr),
  [Android Ultra HDR spec](https://developer.android.com/media/platform/hdr-image-format),
  ISO/IEC 21496-1.
- HEIC extraction/conversion:
  [finnschi/heic-shenanigans](https://github.com/finnschi/heic-shenanigans),
  [m13253/heif-hdrgainmap-decode](https://github.com/m13253/heif-hdrgainmap-decode),
  [grapeot/AppleJPEGGainMap](https://github.com/grapeot/AppleJPEGGainMap).
- Gain-map authoring: [chemharuka/toGainMapHDR](https://github.com/chemharuka/toGainMapHDR),
  [vastunghia/HDR2gainmap](https://github.com/vastunghia/HDR2gainmap).
- GIMP HEIF (primary only): [strukturag/heif-gimp-plugin](https://github.com/strukturag/heif-gimp-plugin),
  [novomesk/gimp-heic-avif-plugin](https://github.com/novomesk/gimp-heic-avif-plugin).
- Viewer / list: [Tom94/tev](https://github.com/Tom94/tev),
  [NMoroney/Awesome-Gain-Maps](https://github.com/NMoroney/Awesome-Gain-Maps).

Dependencies: [pillow-heif](https://github.com/bigcat88/pillow_heif) (bundles libheif),
Pillow, numpy, lxml. Optional: [ExifTool](https://exiftool.org/), ffmpeg (via
`imageio-ffmpeg`).

### License

[Apache License 2.0](LICENSE). © 2026 HDRExtract contributors. Third-party dependencies
keep their own licenses and are not bundled in this repository.

### Project layout

```
HDRExtract/
├─ README.md  requirements.txt  LICENSE  NOTICE
├─ hdrextract/                       # importable package
│  ├─ common.py                      # logging / output dir / parallel save / image IO
│  ├─ metadata.py                    # JPEG scan / XMP / hdrgm / MPF / ExifTool
│  ├─ ultrahdr.py                    # Ultra HDR extraction, log boost, reconstruction
│  ├─ heic.py                        # HEIC primary + aux + depth + gain-map calibration
│  └─ heif_boxes.py                  # HEIF box parsing / tmap (ISO 21496-1)
├─ scripts/
│  ├─ extract_ultrahdr_layers.py     # CLI
│  └─ extract_heic_aux_layers.py     # CLI
└─ gimp/
   └─ gimp_open_hdr_aux_layers/      # GIMP 3.x plug-in
      └─ gimp_open_hdr_aux_layers.py
```

---

## 日本語

スマホHDR写真（**Android Ultra HDR JPEG** / **Apple/iPhone HEIC**）の内部構造
— base画像・gain map・depth/disparity・auxiliary item・metadata — を、
**解析用レイヤー（PNG）+ metadata.json** として取り出し、**GIMPに整列レイヤーとして
読み込む** inspector ツールです。

> 色管理や絶対輝度の再現より「**中身を見える化**」を優先します。入力は**一切書き換えません**。

### 既存ツールとの違い（What's different）

gain map / HEIC を扱う既存OSSの多くは **コーデック**（libultrahdr）、HDRを**作る
authoring**（toGainMapHDR）、**HEIC片方だけの抽出**（heic-shenanigans）です。本ツールは:

1. **Android Ultra HDR と Apple HEIC を1つで統一処理**
2. **auxを名前付きで全部レイヤー化**（depth / gainmap / semantic matte、unknownも保存）
3. **GIMPに primary 解像度へ揃えて読み込む**（GIMPのHEIFは primary のみ）
4. **校正済み log2 boost を両形式から算出**（hdrgm XMP / ISO 21496-1 tmap を同一モデル化）
5. **解析・可視化に特化**した inspector（rendering/authoring ではない）

最も近い [heic-shenanigans](https://github.com/finnschi/heic-shenanigans) と比べても
Android対応・semantic matte列挙・GIMP統合・校正log-boost が無く、棲み分けできます。

### 出力レイヤー

**Ultra HDR JPEG** → `01_base_sdr` / `02_gainmap_raw` /
`03_gainmap_upscaled_{nearest,bilinear}` / `04_gainmap_log_boost` /
`05_sdr_clipping_mask` / `06_reconstructed_hdr_preview` / `metadata.json`。
GContainer に **Depth / Confidence / MotionPhoto** 等が含まれていれば（Pixel Portrait /
Motion 撮影）`aux_00N_<semantic>` として追加抽出します（Ultra HDR版の "aux"）。

**HEIC** → `01_primary`（ICC=Display P3等を保持）/ `aux_00N_<category>_<type>` /
`depth_00N` / `gainmap_upscaled_{nearest,bilinear}` / `gainmap_log_boost_calibrated` /
`metadata.json`（`iso21496_gainmap` 含む）。aux は
`gainmap/depth/disparity/semantic/alpha/unknown` に分類、**判別不能でも必ず保存**。

### 依存と導入

```bash
python -m pip install -r requirements.txt
```
- **必須(pip)**: Python 3.10+, Pillow, numpy, lxml, **pillow-heif**（libheif同梱＝HEICに
  システムlibheif不要）
- **任意（あれば自動使用）**: **ExifTool**（metadata詳細＋MPImage抽出、無ければ純Python
  fallback）、ffmpeg（`imageio-ffmpeg` 経由、10-bit aux用＝ロードマップ）

### 使い方（CLI）

```bash
python scripts/extract_ultrahdr_layers.py path/to/image.jpg
python scripts/extract_heic_aux_layers.py path/to/image.heic
python scripts/extract_ultrahdr_layers.py image.jpg -o out_dir -v
```
出力は既定で入力名と同じ `<stem>_layers/`。実行のたびに前回の生成物を自動クリーンします。

| オプション | 対象 | 意味 |
|---|---|---|
| `-o, --outdir DIR` | 両方 | 出力先 |
| `-v, --verbose` | 両方 | デバッグログ |
| `--no-exiftool` | 両方 | ExifToolを使わない |
| `--clip-threshold N` | ultrahdr | クリップ判定閾値（0-255, 既定250） |
| `--no-thumbnails` | heic | サムネイルitemを保存しない |
| `--force-8bit` | heic | 高bit深度を8bitでデコード |

### GIMP プラグイン（File > Open HDR Aux Layers…）

GIMP内Pythonは Pillow/pillow-heif を使えないため、**プラグインがシステムPythonでCLIを
サブプロセス実行**し、出力PNGをレイヤー化します。`metadata.json` は image parasite
（`hdrextract-metadata`）＋コメントに格納。

**導入:**
1. GIMP > **Edit > Preferences > Folders > Plug-ins**
2. このリポジトリの **`gimp` フォルダ**（例 `C:\path\to\HDRExtract\gimp`）を追加
3. GIMP再起動 → **File > Open HDR Aux Layers…**（または **Filters > HDR Aux Layers**）

Microsoft Store版GIMPでも動作。自動検出に失敗したら環境変数 `HDREXTRACT_PYTHON`
（依存入りpython.exe）/ `HDREXTRACT_HOME`（本リポジトリ）を設定。**primaryを最上位**に置き、
他レイヤーをprimary解像度へスケールして整列します。

### log boost の計算（Android/Apple 共通モデル）

gain map の**画素**は「どこを何倍か」を0..1で格納したもので、実 log2 ブーストに戻すには
**校正メタデータ**が必要です。Android/Apple は**保管場所が違うだけで式は同じ**:

```
g          = gain_pixel / maxval
recovery   = g ** (1 / gamma)                            # gamma 既定 1.0
log_boost  = gain_map_min + (gain_map_max - gain_map_min) * recovery   # log2 stops
linear倍率 = 2 ** log_boost
```

| | gain map 画素 | 校正メタデータ |
|---|---|---|
| **Android Ultra HDR JPEG** | 追記された secondary JPEG | **gain map サブ画像の XMP**（`hdrgm:*`） |
| **Apple/iPhone HEIC** | aux item `hdrgainmap`（item 62等） | **`tmap` 派生アイテム（item 130等）の ISO 21496-1** |

#### HEIC の `tmap`（item 130）の挙動 — ここが要点

Apple HEICでは、**HDR版は独立画像ではなく `tmap`（tone-map）"派生アイテム"** です。
符号化画素を持たず（`hvcC`無し）、中身は**62バイト程度の ISO 21496-1 メタデータ**だけ:

```
dimg: 130 -> [46, 62]     # item130 = base(46)+gainmap(62) を合成するレシピ
```

- **item 130 自体に画素は無い**。`ispe`/`pixi` が primary と同寸なのは「出力HDR寸法の宣言」。
- per-pixel HDR は **gainmap画素（item 62, 半解像度）**、係数は **item 130 の62バイト**
  （`gain_map_max`・`gamma`・`offset`・`hdr_headroom`）。
- **pillow-heif はこの tmap メタを公開しない**ため、本ツールは **HEIFボックスを直接パース**
  （`hdrextract/heif_boxes.py`、`construction_method=idat` 対応）して読み、`metadata.json →
  iso21496_gainmap` に格納（例 `gain_map_max≈1.469` / `gamma≈0.587` / headroom≈1.469
  → **+1.47 stops ≈ ×2.77**）。

> linearthumbnail / styledeltamap 等の他 aux は **HDR表示には不使用**の Apple内部参照画像
> です（tmap のレシピは base + gainmap のみ参照）。

#### reconstructed HDR preview（近似・現状 JPEG のみ）

```
HDR_lin = (SDR_lin + offset_sdr) * 2**(log_boost * weight) - offset_hdr
display = HDR_lin / (1 + HDR_lin)       # Reinhard tonemap
```
`weight` は表示ヘッドルーム係数（フルHDRで1）。**色精度は保証しません**。校正メタ欠如時は
spec既定値を使い `metadata.json` に記録。

### gain-map の補完について

ゲインマップは低解像度で格納（実測: **iPhone=½/辺・Pixel=¼/辺**）。実レンダリングは
**「bilinear or better」**で補完（Ultra HDR仕様が最低bilinearを要求、AppleはCoreImageの
`CIEdgePreserveUpsample` も可）。本ツールは両方出力:
- **`*_nearest`** … 補完なし・格納サンプルに**忠実**（ブロック状）
- **`*_bilinear`** … **実レンダリング相当**

### 設計上の約束

- 入力を破壊しない（読み取り専用）
- 1レイヤー失敗でも他は保存、失敗は `metadata.json → notes` とログに記録
- Windowsパス対応、ExifTool自動検出、依存欠如時は明確なエラー

### 検証

実機で確認済み: **Pixel Ultra HDR JPEG**（ExifTool MPImage2／gain map XMPからhdrgm取得／
SOI-EOIスキャンfallbackも）、**iPhone HEIC**（primary+depth+hdrgainmap+semantic matte 6種／
tmap ISO 21496-1 パース／10-bit aux 2種は `decode_failed` 記録）。サンプルは同梱しません。

### 制限

- reconstructed preview は近似・色精度なし
- **pillow-heif は 8-bit aux のみデコード**。Apple の 10-bit aux（linearthumbnail /
  styledeltamap）は `status:"decode_failed"` で item情報のみ記録（ロードマップ）
- aux 分類は URN ヒューリスティック
- hdrgm 校正は gain map サブ画像 XMP を優先

### ロードマップ

1〜7 ✅ Ultra HDR/HEIC 抽出・分類・両形式の校正log boost・再構成プレビュー(JPEG)・
gain map拡大・GIMP 3.xプラグイン まで完了。
8 ⬜ 10-bit aux（linearthumbnail / styledeltamap、grid合成含む）の bundled ffmpeg デコード。
9 ✅ Ultra HDR の GContainer 追加アイテム（Depth / Confidence / MotionPhoto）抽出。

### Prior art / 謝辞 / License

謝辞・依存・ライセンスは上の English 節（[Prior art](#prior-art--acknowledgments) /
[License](#license)）を参照。**Apache License 2.0**, © 2026 HDRExtract contributors。
