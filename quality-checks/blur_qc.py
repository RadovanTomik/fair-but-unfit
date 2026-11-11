"""Quality check: Out-of-focus tissue in WSI slides.

Executable port of RationAI's ``blur_score_piqe``
(https://github.com/RationAI/quality-control/blob/master/rationai/qc/blur/blur_score_piqe.py)
to whole-slide images read with OpenSlide (.mrxs, .svs, .ndpi, ...). Blur is
detected per 16x16 px block with the PIQE perception approach: after median
filtering (kernel derived from the pixel size) the image is MSCN-normalized
and blocks whose variance stays below the activity threshold count as
blurred. Like the original, the scalar PIQE score is discarded and only the
PIQE activity mask is kept. Areas without tissue are suppressed with a simple
foreground mask, so background does not inflate the result. A slide is
flagged when blurred blocks cover at least ``BLUR_COVERAGE_THRESHOLD`` of its
sampled tissue; out-of-focus tissue is useless for pathologists and
algorithms alike, so such slides may need re-scanning.

Sampling: a WSI at 40x is far too large to analyze as a whole. A coarse
whole-slide thumbnail localizes the tissue, and up to ``MAX_PROBED_TILES``
tiles of ``TILE_SIZE`` px are probed at the pyramid level closest to
~0.44 um/px (where the score is most accurate). Probe tiles are spread
spatially: the tissue bounding box is split into a grid of bins and one tile
is drawn per occupied bin (seeded, so runs are reproducible); leftover budget
is filled at random from the remaining tissue tiles. Blur coverage is
aggregated across the probed tiles, weighted by tissue pixels.

Known limits — read before acting on the flags:
  * The sample is small (16 tiles ~ 3 mm^2 of tissue). Global defocus is
    caught reliably; localized defocus (one scan stripe, a fold) may be
    missed or diluted below the threshold.
  * "Low PIQE activity" is not the same as "blurred". Low-texture tissue that
    is in focus (fat septa, mucin, necrosis, blood, homogeneous stroma, faint
    IHC) and pen marks / dust count as blurred, causing false positives.
  * ACTIVITY_THRESHOLD is PIQE's default for natural images and
    BLUR_COVERAGE_THRESHOLD is not calibrated. Validate both against
    pathologist focus labels (e.g. via a MAX_SLIDES pilot) before using the
    flags to trigger re-scans.

Corpus layout: every single-file slide OpenSlide can read is a candidate.
A Mirax scan consists of an ``<SCAN>.mrxs`` index file and a companion
directory ``<SCAN>/`` holding the tile data, with ``SCAN`` typically of the
form ``P<patient>-S<scan>`` (e.g. ``P00040-S00064``). The check reports the
scan identifier — the slide path relative to the data dir, without the file
extension — so one id identifies the patient-scan and points at both the
.mrxs file and its companion directory.

Unscorable scans (cannot be opened, missing pixel-spacing metadata, read
errors, no tissue found) never fail the corpus. They are listed on stderr
with the reason and, if ``FLAG_UNSCORABLE`` is True, also reported as
affected. Set ``MAX_SLIDES`` to analyze only the first N scans (sorted by
scan id, so the subset is deterministic) — handy for pilot runs.

Result contract: a single JSON object printed as the LAST line of stdout:
    count (int):       number of affected scans
    entityType (str):  always "file" (a scan is a .mrxs file + companion dir)
    ids (list[str]):   scan identifiers of the affected slides (see above)
    error (str|null):  description of a check-level error, else null

Run inside the sandbox with:
    curl -s -X POST --data-binary @examples/wsi_blurred_tissue.py http://localhost:8080/run
Timing: roughly ~20-30 s per slide per worker with the defaults (16 probed
tiles x ~1.4 s decode+analysis plus the thumbnail pass), i.e. about
1-1.5 h for 600 slides on 3 worker cores; size OMICSBOX_TIMEOUT (default
60 s) accordingly. /run is a single synchronous HTTP call — for very large
corpora, prefer batching the mount into several smaller runs over one
multi-hour request. Memory: ~1.5 GB total for 3 workers.
"""

import json
import math
import os
import sys

import cv2
import numpy as np
import openslide
from skimage.color import rgb2gray
from skimage.filters import median
from skimage.morphology import area_opening, erosion

# --- Tunables ---------------------------------------------------------------

OPTIMAL_PIXEL_SIZE_UM = 0.44  # PIQE is most accurate around 40x magnification
BLOCK_SIZE = 16  # PIQE activity blocks (px)
ACTIVITY_THRESHOLD = 0.1  # min MSCN block variance of a "sharp" block (PIQE default)
FOREGROUND_THRESHOLD = 0.92  # ~235/255: gray values below count as tissue
TILE_SIZE = 1024  # probe tiles, multiple of BLOCK_SIZE
MAX_PROBED_TILES = 16  # per-slide probe budget, spread spatially over tissue tiles
MIN_FOREGROUND_FRACTION = 0.25  # grid positions below this tissue share are ignored
BLUR_COVERAGE_THRESHOLD = 0.2  # flag scans with >= 20% sampled tissue blurred
THUMB_TILE_PIXELS = 8  # thumbnail resolution per candidate probe tile
SAMPLING_SEED = 0  # fixed seed: probe selection is reproducible across runs
FLAG_UNSCORABLE = False  # also report scans that could not be analyzed
WORKERS = 3  # worker processes analyzing slides in parallel (~1 CPU core each)
MAX_SLIDES = 10  # analyze only the first N scans (sorted by scan id); None = all

SLIDE_SUFFIXES = (".mrxs", ".svs", ".ndpi", ".vms", ".vmu", ".scn", ".bif")


class Unscorable(Exception):
    """The slide cannot be scored; the message says why."""


# --- Blur scoring (self-contained equivalent of rationai.qc.blur) -----------


def _calculate_mscn(image):
    """MSCN coefficients, as in the PIQE reference implementation."""
    image = image.astype(np.float32)
    mu = cv2.GaussianBlur(image, (7, 7), 7 / 6)
    sigma_sq = cv2.GaussianBlur(image**2, (7, 7), 7 / 6) - mu * mu
    sigma = np.sqrt(np.abs(sigma_sq))
    return (image - mu) / (1 + sigma)


def _activity_mask(image_255):
    """Per-pixel mask of PIQE "high spatial activity" blocks (True = sharp).

    Vectorized equivalent of PIQE's ActivityMask: the image is edge-padded to
    a multiple of BLOCK_SIZE (before MSCN, as in PIQE) and every 16x16 block
    with variance above the activity threshold is marked active. The mask is
    cropped back to the exact image shape; probed tiles are multiples of
    BLOCK_SIZE, so there this matches the reference implementation exactly.
    """
    height, width = image_255.shape
    pad_h = (-height) % BLOCK_SIZE
    pad_w = (-width) % BLOCK_SIZE
    padded = np.pad(image_255, ((0, pad_h), (0, pad_w)), mode="edge")
    mscn = _calculate_mscn(padded)
    rows, cols = mscn.shape[0] // BLOCK_SIZE, mscn.shape[1] // BLOCK_SIZE
    blocks = mscn.reshape(rows, BLOCK_SIZE, cols, BLOCK_SIZE)
    active_blocks = blocks.var(axis=(1, 3)) > ACTIVITY_THRESHOLD
    mask = np.repeat(np.repeat(active_blocks, BLOCK_SIZE, axis=0), BLOCK_SIZE, axis=1)
    return mask[:height, :width]


def _foreground_mask(gray):
    """Binary tissue mask: dark enough, de-speckled, eroded — as in the original."""
    foreground = gray < FOREGROUND_THRESHOLD
    foreground = area_opening(foreground)
    return erosion(foreground, footprint=np.ones((5, 5))).astype(bool)


def _rgb_on_white(region):
    """RGB array of a PIL region with transparent pixels composited onto white.

    OpenSlide renders areas without data (e.g. around Mirax scan strips) as
    transparent; compositing onto white treats them as background instead of
    dark, activity-free "tissue".
    """
    rgba = np.asarray(region.convert("RGBA")).astype(np.uint16)
    alpha = rgba[..., 3:4]
    return ((rgba[..., :3] * alpha + 255 * (255 - alpha)) // 255).astype(np.uint8)


def _tile_blur_and_tissue(tile, pixel_size_um):
    """(blurred tissue px, tissue px) of one RGB tile, or None if unusable."""
    height, width = tile.shape[:2]
    if height < BLOCK_SIZE or width < BLOCK_SIZE:
        return None
    kernel_size = 2 * round(1 / pixel_size_um * OPTIMAL_PIXEL_SIZE_UM + 1e-9) + 1
    gray = median(rgb2gray(tile), footprint=np.ones((kernel_size, kernel_size), dtype=bool))
    foreground = _foreground_mask(gray)
    if not foreground.any():
        return None
    sharp = _activity_mask(gray * 255)
    return int((~sharp & foreground).sum()), int(foreground.sum())


# --- Choosing probe tiles ---------------------------------------------------


def _closest_level(spacings_um):
    """Index of the level whose pixel size is closest to the optimal one (log scale)."""
    return min(
        range(len(spacings_um)),
        key=lambda i: abs(math.log(spacings_um[i] / OPTIMAL_PIXEL_SIZE_UM)),
    )


def _tissue_tile_positions(slide, width, height):
    """Grid positions (level coords) of TILE_SIZE tiles with enough estimated tissue.

    The estimate comes from a coarse whole-slide thumbnail. The thumbnail
    covers the full level extent, while the tile grid drops the partial strip
    at the right and bottom edges, so the thumbnail is cropped to the gridded
    extent before it is mapped onto the grid.
    """
    cols = list(range(0, width - TILE_SIZE + 1, TILE_SIZE))
    rows = list(range(0, height - TILE_SIZE + 1, TILE_SIZE))
    if not cols or not rows:
        return []
    thumb = _rgb_on_white(
        slide.get_thumbnail((len(cols) * THUMB_TILE_PIXELS, len(rows) * THUMB_TILE_PIXELS))
    )
    foreground = rgb2gray(thumb) < FOREGROUND_THRESHOLD
    foreground = area_opening(foreground).astype(np.float32)
    thumb_h, thumb_w = foreground.shape
    crop_h = max(1, round(thumb_h * len(rows) * TILE_SIZE / height))
    crop_w = max(1, round(thumb_w * len(cols) * TILE_SIZE / width))
    foreground = foreground[:crop_h, :crop_w]
    per_tile = cv2.resize(foreground, (len(cols), len(rows)), interpolation=cv2.INTER_AREA)
    return [
        (x, y)
        for j, y in enumerate(rows)
        for i, x in enumerate(cols)
        if per_tile[j, i] >= MIN_FOREGROUND_FRACTION
    ]


def _spread_positions(positions, budget):
    """Up to `budget` positions, spread spatially over the tissue.

    The tissue bounding box is split into a g x g grid of bins
    (g = ceil(sqrt(budget))); one position is drawn per occupied bin, and
    leftover budget is filled at random from the remaining positions. Unlike
    taking every k-th position in reading order, this cannot alias into a
    single column of the slide. Seeded, so the selection is reproducible.
    """
    if len(positions) <= budget:
        return positions
    rng = np.random.default_rng(SAMPLING_SEED)
    xs = np.array([x for x, _ in positions])
    ys = np.array([y for _, y in positions])
    g = math.ceil(math.sqrt(budget))
    span_x = xs.max() - xs.min() + TILE_SIZE
    span_y = ys.max() - ys.min() + TILE_SIZE
    bin_x = np.minimum((xs - xs.min()) * g // span_x, g - 1)
    bin_y = np.minimum((ys - ys.min()) * g // span_y, g - 1)
    bins = {}
    for k, key in enumerate(zip(bin_y.tolist(), bin_x.tolist())):
        bins.setdefault(key, []).append(k)
    chosen = [members[rng.integers(len(members))] for _, members in sorted(bins.items())]
    if len(chosen) > budget:
        chosen = rng.choice(chosen, budget, replace=False).tolist()
    elif len(chosen) < budget:
        rest = sorted(set(range(len(positions))) - set(chosen))
        chosen += rng.choice(rest, budget - len(chosen), replace=False).tolist()
    return [positions[k] for k in sorted(chosen)]


# --- Scoring one slide ------------------------------------------------------


def _blur_coverage(slide, scan_id):
    """(blur coverage 0.0-1.0, number of tiles scored); raises Unscorable."""
    base_mpp = float(slide.properties.get("openslide.mpp-x") or 0)
    if base_mpp <= 0:
        raise Unscorable("missing openslide.mpp-x")
    spacings_um = [base_mpp * slide.level_downsamples[i] for i in range(slide.level_count)]
    level = _closest_level(spacings_um)
    pixel_size_um = spacings_um[level]
    level_downsample = slide.level_downsamples[level]
    width, height = slide.level_dimensions[level]
    if width < TILE_SIZE or height < TILE_SIZE:
        probes = [(0, 0, width, height)]
    else:
        positions = _tissue_tile_positions(slide, width, height)
        probes = [
            (x, y, TILE_SIZE, TILE_SIZE)
            for x, y in _spread_positions(positions, MAX_PROBED_TILES)
        ]
    blurred = tissue = scored = 0
    for x, y, w, h in probes:
        # OpenSlide locations are level-0 coordinates; downsamples may be non-integer
        location = (int(x * level_downsample), int(y * level_downsample))
        try:
            region = slide.read_region(location, level, (w, h))
        except openslide.OpenSlideError as e:
            # OpenSlide objects stay in an error state after a read failure,
            # so every later read would fail too: give up on this slide.
            raise Unscorable(f"read error at tile ({x},{y}): {e}") from e
        result = _tile_blur_and_tissue(_rgb_on_white(region), pixel_size_um)
        if result is None:
            continue
        tile_blurred, tile_tissue = result
        if tile_tissue < MIN_FOREGROUND_FRACTION * w * h:
            continue  # thumbnail estimate was off (e.g. faint or folded tissue)
        blurred += tile_blurred
        tissue += tile_tissue
        scored += 1
    if not tissue:
        raise Unscorable("no tissue found in probe tiles")
    return blurred / tissue, scored


def _scan_coverage(scan):
    """(scan_id, coverage, tiles scored, reason unscorable); runs in a worker.

    Never raises: any failure on one slide becomes an "unscorable" result,
    so a single bad scan cannot abort the corpus run.
    """
    scan_id, path = scan
    cv2.setNumThreads(1)  # N workers, one core each: no thread oversubscription
    try:
        slide = openslide.OpenSlide(path)
    except Exception as e:
        return scan_id, None, 0, f"cannot be opened ({e})"
    try:
        coverage, scored = _blur_coverage(slide, scan_id)
        return scan_id, coverage, scored, None
    except Unscorable as e:
        return scan_id, None, 0, str(e)
    except Exception as e:
        return scan_id, None, 0, f"analysis failed ({type(e).__name__}: {e})"
    finally:
        slide.close()


# --- Scan discovery and main ------------------------------------------------


def _iter_scans(data_dir):
    """Yield (scan_id, path) for every slide file under data_dir.

    ``scan_id`` is the path relative to the data dir without the file
    extension (e.g. ``mirax/P00040-S00064``); for Mirax it identifies both
    the .mrxs index file and the companion tile directory of the same name.
    """
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            if name.lower().endswith(SLIDE_SUFFIXES):
                path = os.path.join(root, name)
                yield os.path.splitext(os.path.relpath(path, data_dir))[0], path


def _run():
    """Analyze the corpus; returns the sorted ids of affected scans."""
    import multiprocessing

    data_dir = os.getcwd()  # the sandbox runs scripts with cwd = data dir
    scans = sorted(_iter_scans(data_dir))
    if MAX_SLIDES is not None:
        scans = scans[:MAX_SLIDES]
    if WORKERS > 1 and len(scans) > 1:
        # "fork" workers inherit this script; no re-import, no exec from the
        # noexec tmpfs. pool.map keeps input order, so output is deterministic.
        with multiprocessing.get_context("fork").Pool(WORKERS) as pool:
            results = pool.map(_scan_coverage, scans)
    else:
        results = [_scan_coverage(scan) for scan in scans]

    flagged, unscorable = [], []
    for scan_id, coverage, scored, reason in results:
        if coverage is None:
            print(f"{scan_id}: UNSCORABLE, {reason}", file=sys.stderr)
            unscorable.append(scan_id)
            continue
        print(
            f"{scan_id}: blur coverage {coverage:.1%} of sampled tissue ({scored} tiles)",
            file=sys.stderr,
        )
        if coverage >= BLUR_COVERAGE_THRESHOLD:
            flagged.append(scan_id)
    print(
        f"summary: {len(scans)} scans, {len(flagged)} out of focus, "
        f"{len(unscorable)} unscorable"
        + (" (reported as affected)" if FLAG_UNSCORABLE and unscorable else ""),
        file=sys.stderr,
    )
    if FLAG_UNSCORABLE:
        flagged += unscorable
    return sorted(flagged)


def main() -> None:
    try:
        ids = _run()
        result = {"count": len(ids), "entityType": "file", "ids": ids, "error": None}
    except Exception as e:
        result = {
            "count": 0,
            "entityType": "file",
            "ids": [],
            "error": f"{type(e).__name__}: {e}",
        }
    sys.stdout.flush()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
