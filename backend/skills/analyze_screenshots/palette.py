"""Dominant color palette extraction from page screenshots.

Uses PIL's adaptive quantization on downsampled pixels from all chunk
images to find the ~8 most representative colors across the whole page.
Drops near-white, near-black, and near-grey colors so the palette
reflects the brand, not the background/text chrome.
"""
from __future__ import annotations

import logging
import pathlib

logger = logging.getLogger(__name__)


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _is_chromatic(rgb: tuple[int, int, int], min_sat: int = 18) -> bool:
    """Keep colors with enough saturation or mid-range brightness.

    Filters out near-white (>=245), near-black (<=12), and pure greys
    (max-min channel spread < min_sat) since those are usually page
    background / text / borders rather than brand colors.
    """
    r, g, b = rgb
    mx, mn = max(r, g, b), min(r, g, b)
    if mx >= 245 or mx <= 12:
        return False
    if (mx - mn) < min_sat:
        return False
    return True


def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def extract_palette(
    chunk_paths: list[pathlib.Path],
    n_colors: int = 8,
    sample_max_dim: int = 240,
    min_separation: float = 35.0,
) -> dict:
    """Return a tiered palette summarizing the page's brand colors.

    Output:
        {
          "ordered": [hex, hex, ...],        # flat list, most→least used
          "primary": hex or None,            # single top color
          "secondary": [hex, hex],           # next 1-2 most used
          "accent": [hex, ...],              # remaining distinctive colors
          "coverage": {hex: float}           # fraction of pixels, 0-1
        }
    """
    empty = {"ordered": [], "primary": None, "secondary": [], "accent": [], "coverage": {}}
    try:
        from PIL import Image
    except Exception:
        logger.warning("PIL not available — skipping palette extraction")
        return empty

    # Concatenate downsampled versions of every chunk into one tall image,
    # then quantize once. This weights the palette by coverage area across
    # the whole page rather than per-chunk.
    tiles = []
    for p in chunk_paths:
        if not p.exists():
            continue
        try:
            img = Image.open(p).convert("RGB")
            if img.width > sample_max_dim:
                ratio = sample_max_dim / img.width
                img = img.resize((sample_max_dim, max(1, int(img.height * ratio))),
                                 Image.LANCZOS)
            tiles.append(img)
        except Exception:
            continue

    if not tiles:
        return empty

    total_h = sum(t.height for t in tiles)
    stitched = Image.new("RGB", (tiles[0].width, total_h))
    y = 0
    for t in tiles:
        stitched.paste(t, (0, y))
        y += t.height

    # Quantize into a larger palette first (captures accent colors that
    # would otherwise be absorbed into dominant greys), then filter.
    palette_size = 32
    try:
        quant = stitched.quantize(colors=palette_size, method=Image.Quantize.MEDIANCUT)
    except Exception:
        quant = stitched.quantize(colors=palette_size)
    palette = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], key=lambda x: -x[0])

    candidates: list[tuple[int, tuple[int, int, int]]] = []
    for count, idx in counts:
        base = idx * 3
        if base + 3 > len(palette):
            continue
        rgb = (palette[base], palette[base + 1], palette[base + 2])
        if not _is_chromatic(rgb):
            continue
        candidates.append((count, rgb))

    # Greedy pick: keep colors that are sufficiently different from what
    # we've already chosen, so the palette shows distinct hues. Track the
    # pixel count for each so we can tier by usage.
    picked: list[tuple[int, tuple[int, int, int]]] = []
    for cnt, rgb in candidates:
        if all(_color_distance(rgb, p[1]) >= min_separation for p in picked):
            picked.append((cnt, rgb))
        if len(picked) >= n_colors:
            break

    if not picked:
        return empty

    total_pixels = sum(c for c, _ in picked) or 1
    ordered = [_to_hex(rgb) for _, rgb in picked]
    coverage = {_to_hex(rgb): round(c / total_pixels, 4) for c, rgb in picked}

    # Tiering rule: the single dominant brand color is "primary". The next
    # 1-2 heavy hitters form the "secondary" set. Everything else is
    # "accent" — used sparingly for highlights / illustrations.
    primary = ordered[0] if ordered else None
    secondary = ordered[1:3]
    accent = ordered[3:]
    return {
        "ordered": ordered,
        "primary": primary,
        "secondary": secondary,
        "accent": accent,
        "coverage": coverage,
    }
