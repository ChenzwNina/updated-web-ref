"""Skill 2b — Analyze From Screenshots (viewport-chunked).

Fans out one multimodal Sonnet call *per viewport chunk* of the single
downloaded page. bboxes returned by the model are chunk-local, so crops
stay accurate (small image → small error). After crops are generated we
dedupe across chunks by (subtype + style signature) so repeated components
across neighbouring chunks collapse to one entry.

See SKILL.md.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ...shared.events import EventBus
from ...shared.schemas import (
    COMPONENT_TAXONOMY,
    AnalysisResult,
    Component,
    ComponentGroup,
    ComponentStyle,
    DownloadResult,
)
from ...shared.storage import JobStorage, PROJECT_ROOT
from ...shared.trace import metric, note, traced
from .palette import extract_palette
from .subagent import extract_components_from_chunk

logger = logging.getLogger(__name__)


# Cap parallelism so we don't trip Anthropic's per-minute token budget.
# Each chunk is one Sonnet call; a tall page can produce 8–12 chunks.
CHUNK_CONCURRENCY = 3


# Generic words that appear in nearly every component name of a given
# subtype and therefore don't carry signal for dedup. We strip these before
# token-set comparisons so "White Cookie Consent Snackbar" and
# "White Bottom Cookie Consent Banner" both reduce to {white, cookie,
# consent} and collapse together.
_NAME_STOPWORDS = {
    # subtype / structural tags
    "button", "btn", "cta", "snackbar", "banner", "bar", "dialog", "modal",
    "card", "hero", "footer", "header", "icon", "chip", "badge", "row",
    "tab", "section", "panel", "image", "nav", "navigation", "menu",
    "toast", "sheet", "drawer", "link", "item",
    # connectors & articles
    "with", "and", "the", "a", "an", "of", "for", "on", "in", "to",
    # position (component placement, not a visual differentiator)
    "bottom", "top", "left", "right",
    # size modifiers
    "full", "width", "wide", "large", "small", "medium", "sm", "lg", "md",
    # generic hierarchical labels
    "primary", "secondary", "default", "standard",
    # Kept OUT of stopwords (they DO differentiate looks):
    #   solid, outline, filled, ghost, text (fill style)
    #   square, pill, round, rounded, rectangular (shape)
}


def _name_tokens(name: str) -> set[str]:
    """Significant lowercase tokens — drops stopwords + very short words."""
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {t for t in toks if t not in _NAME_STOPWORDS and len(t) >= 3}


def _normalize_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    n = re.sub(r"[^\w\s]", "", n)
    return n


def _similar(a: set[str], b: set[str]) -> bool:
    """Token-set similarity: merge if jaccard ≥ 0.5 OR one ⊆ the other.

    Small token sets (1-2 tokens) require full overlap to avoid merging
    unrelated components that happen to share one descriptor like 'green'.
    """
    if not a or not b:
        return False
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return False
    jaccard = inter / union
    smaller = min(len(a), len(b))
    subset_ratio = inter / smaller if smaller else 0
    if smaller <= 2:
        # With only 1-2 signal tokens, require near-full overlap.
        return subset_ratio >= 1.0
    return jaccard >= 0.5 or subset_ratio >= 0.75


def _to_component(c: dict, i: int) -> Component:
    styles_dict = c.get("styles", {}) or {}
    extra = styles_dict.pop("extra", {}) if isinstance(styles_dict, dict) else {}
    style = ComponentStyle(
        background_color=str(styles_dict.get("background_color", "") or ""),
        text_color=str(styles_dict.get("text_color", "") or ""),
        border_radius=str(styles_dict.get("border_radius", "") or ""),
        padding=str(styles_dict.get("padding", "") or ""),
        font_size=str(styles_dict.get("font_size", "") or ""),
        font_weight=str(styles_dict.get("font_weight", "") or ""),
        font_family=str(styles_dict.get("font_family", "") or ""),
        border=str(styles_dict.get("border", "") or ""),
        box_shadow=str(styles_dict.get("box_shadow", "") or ""),
        extra={str(k): str(v) for k, v in (extra or {}).items()} if isinstance(extra, dict) else {},
    )
    cid = c.get("id") or f"c{i}"
    return Component(
        id=cid,
        type=str(c.get("subtype") or c.get("type") or "unknown"),
        name=str(c.get("name", cid)),
        description=str(c.get("description", "")),
        html_snippet=str(c.get("html_snippet", ""))[:1500],
        source_url=str(c.get("source_url", "")),
        styles=style,
        count=int(c.get("count") or 1),
    )


def _group_and_dedupe(raw: list[dict]) -> list[ComponentGroup]:
    """Dedup within each (category, subtype) by fuzzy token overlap.

    The subagent is told to produce style-descriptive names, but across
    chunks it wordsmiths them slightly differently ("White Cookie Consent
    Snackbar" vs "White Bottom Cookie Consent Banner" vs "White Cookie
    Consent Bar"). Exact-name dedup misses those; token-set similarity
    (after stripping structural/generic stopwords) merges them while
    keeping genuinely different looks ("Dark Green Button" vs "White
    Outline Button") separate.
    """
    # by_cat[cat] is a list of (token_set, subtype, Component) buckets.
    by_cat: dict[str, list[tuple[set[str], str, Component]]] = {}
    known_cats = set(COMPONENT_TAXONOMY.keys())

    for i, c in enumerate(raw):
        cat = str(c.get("category") or "Other").strip()
        if cat not in known_cats and cat != "Other":
            match = next((k for k in known_cats if k.lower() == cat.lower()), None)
            cat = match or "Other"
        comp = _to_component(c, i)
        tokens = _name_tokens(comp.name)
        buckets = by_cat.setdefault(cat, [])

        merged = False
        for j, (bt, btype, bcomp) in enumerate(buckets):
            if btype != comp.type:
                continue
            if _similar(tokens, bt):
                # Merge: bump count, union tokens (name stays as first-seen).
                bcomp.count += comp.count
                buckets[j] = (bt | tokens, btype, bcomp)
                merged = True
                break
        if not merged:
            buckets.append((tokens, comp.type, comp))

    ordered: list[ComponentGroup] = []
    for cat in list(COMPONENT_TAXONOMY.keys()) + ["Other"]:
        comps = [b[2] for b in (by_cat.get(cat) or [])]
        if comps:
            ordered.append(ComponentGroup(category=cat, components=comps))
    return ordered


EDGE_THRESH = 8  # px — if bbox y is within this of chunk top/bottom, treat as straddler
EDGE_EXTEND = 600  # px to extend into the neighbour chunk when stitching


def _crop_screenshots(
    raw: list[dict],
    download: DownloadResult,
    storage: JobStorage,
) -> None:
    """Crop each component's chunk-local bbox out of its chunk PNG.

    If a bbox touches the top or bottom edge of its chunk, stitch the
    neighbouring chunk on (accounting for overlap) and extend the crop so
    the full component is captured — fixes components that got sliced in
    half by the viewport chunking. Attaches the relative path to the dict
    as `screenshot_crop`.
    """
    try:
        from PIL import Image
    except Exception:
        return

    # Map (url, chunk_index) -> (PIL image, (W, H), chunk_model)
    chunk_map: dict[tuple[str, int], tuple] = {}
    for p in download.pages:
        for ch in p.chunks:
            abs_path = PROJECT_ROOT / ch.path
            if not abs_path.exists():
                continue
            try:
                img = Image.open(abs_path).convert("RGB")
                chunk_map[(p.url, ch.index)] = (img, img.size, ch)
            except Exception:
                continue

    def _stitch(url: str, idx: int, direction: str):
        """Return (stitched_img, y_offset_of_primary_in_stitched).

        direction ∈ {"prev", "next"}. Stitches primary chunk with its
        neighbour, accounting for the overlap between adjacent chunks.
        """
        primary = chunk_map.get((url, idx))
        neighbour = chunk_map.get((url, idx + (-1 if direction == "prev" else 1)))
        if not primary or not neighbour:
            return None, 0
        p_img, _, p_ch = primary
        n_img, _, n_ch = neighbour
        if direction == "prev":
            # neighbour sits above primary; overlap = n_ch offset+h - p_ch offset
            overlap = max(0, (n_ch.offset_y + n_img.height) - p_ch.offset_y)
            trim = min(overlap, n_img.height)
            top_part = n_img.crop((0, 0, n_img.width, n_img.height - trim))
            canvas = Image.new("RGB", (max(p_img.width, top_part.width),
                                       top_part.height + p_img.height))
            canvas.paste(top_part, (0, 0))
            canvas.paste(p_img, (0, top_part.height))
            return canvas, top_part.height
        else:
            overlap = max(0, (p_ch.offset_y + p_img.height) - n_ch.offset_y)
            trim = min(overlap, n_img.height)
            bot_part = n_img.crop((0, trim, n_img.width, n_img.height))
            canvas = Image.new("RGB", (max(p_img.width, bot_part.width),
                                       p_img.height + bot_part.height))
            canvas.paste(p_img, (0, 0))
            canvas.paste(bot_part, (0, p_img.height))
            return canvas, 0

    import io

    for c in raw:
        url = c.get("source_url")
        idx = int(c.get("chunk_index", 0))
        bbox = c.get("bbox") or []
        key = (url, idx)
        if key not in chunk_map or not (isinstance(bbox, list) and len(bbox) == 4):
            continue
        img, (W, H), _ch = chunk_map[key]
        try:
            x, y, w, h = (int(v) for v in bbox)
        except Exception:
            continue
        x = max(0, min(x, W - 1))
        y = max(0, min(y, H - 1))
        w = max(1, min(w, W - x))
        h = max(1, min(h, H - y))
        if w < 8 or h < 8:
            continue

        touches_top = y <= EDGE_THRESH and idx > 0
        touches_bottom = (y + h) >= (H - EDGE_THRESH) and (url, idx + 1) in chunk_map

        src_img = img
        sx, sy, sw, sh = x, y, w, h

        if touches_top and touches_bottom:
            # Spans full chunk — both neighbours might hold the rest.
            touches_bottom = False  # prefer extending upward only

        if touches_top:
            stitched, y_off = _stitch(url, idx, "prev")
            if stitched is not None:
                src_img = stitched
                # bbox in primary → stitched coords: add y_off
                sy_stitched = y + y_off
                # Extend upward into neighbour
                new_y = max(0, sy_stitched - EDGE_EXTEND)
                sh = (sy_stitched + h) - new_y
                sy = new_y
                sx, sw = x, w
                c["stitched_with"] = "prev"
        elif touches_bottom:
            stitched, _y_off = _stitch(url, idx, "next")
            if stitched is not None:
                src_img = stitched
                # Primary is at top of stitched, bbox unchanged
                sy = y
                # Extend downward into neighbour
                max_h = stitched.height - sy
                sh = min(h + EDGE_EXTEND, max_h)
                sx, sw = x, w
                c["stitched_with"] = "next"

        # Clamp to the source (stitched or primary) size
        SW, SH = src_img.size
        sx = max(0, min(sx, SW - 1))
        sy = max(0, min(sy, SH - 1))
        sw = max(1, min(sw, SW - sx))
        sh = max(1, min(sh, SH - sy))

        try:
            crop = src_img.crop((sx, sy, sx + sw, sy + sh))
            cid = c.get("id") or f"c{hash((key, sx, sy, sw, sh)) & 0xffff:x}"
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            rel = storage.save_screenshot_crop(cid, buf.getvalue())
            c["screenshot_crop"] = rel
        except Exception:
            continue


def _apply_crops_to_components(
    raw: list[dict], groups: list[ComponentGroup]
) -> None:
    """Match crops back onto the deduped Component objects (by id)."""
    crops_by_id = {c.get("id"): c.get("screenshot_crop") for c in raw if c.get("screenshot_crop")}
    for g in groups:
        for comp in g.components:
            path = crops_by_id.get(comp.id)
            if path:
                comp.screenshot_crop = path


@traced
async def run_analyze_screenshots_skill(
    download_result: DownloadResult,
    storage: JobStorage,
    bus: EventBus,
) -> AnalysisResult:
    # Build a flat list of (page, chunk) tasks — usually a single page with
    # N chunks, but the code handles multi-page defensively.
    tasks: list[tuple] = []
    for p in download_result.pages:
        for ch in p.chunks:
            tasks.append((p, ch))

    await bus.publish(
        "skill_start",
        skill="analyze_screenshots",
        message=f"Analyzing {len(tasks)} viewport chunk(s) visually",
    )

    sem = asyncio.Semaphore(CHUNK_CONCURRENCY)

    async def _one(page, chunk) -> list[dict]:
        async with sem:
            ss_abs = str(PROJECT_ROOT / chunk.path)
            await bus.publish(
                "status",
                message=f"👁️  Visual analysis → {page.url} chunk {chunk.index}",
            )
            comps = await extract_components_from_chunk(
                ss_abs, page.url, chunk.index, chunk.offset_y,
            )
            await bus.publish(
                "status",
                message=f"  ✓ chunk {chunk.index}: {len(comps)} components found",
            )
            return comps

    per_chunk = await asyncio.gather(*(_one(p, ch) for p, ch in tasks))
    raw: list[dict] = [c for lst in per_chunk for c in lst]

    # Save crops to disk (best-effort — requires PIL + valid bbox values).
    _crop_screenshots(raw, download_result, storage)

    groups = _group_and_dedupe(raw)
    _apply_crops_to_components(raw, groups)

    total = sum(len(g.components) for g in groups)
    note(f"Grouped: {len(raw)} raw → {total} unique across {len(groups)} categories")
    for g in groups:
        metric(category=g.category, components=len(g.components))

    # Brand-color palette extracted from the actual chunk pixels —
    # surfaced alongside components for users to sample into their site.
    palette: dict = {}
    try:
        chunk_paths = [
            PROJECT_ROOT / ch.path
            for p in download_result.pages for ch in p.chunks
        ]
        palette = extract_palette(chunk_paths, n_colors=8)
        ordered = palette.get("ordered", [])
        if ordered:
            await bus.publish(
                "status",
                message=(
                    f"🎨 Palette: primary={palette.get('primary')} "
                    f"secondary={palette.get('secondary')} "
                    f"accent={palette.get('accent')}"
                ),
            )
    except Exception as exc:
        note(f"palette extraction failed: {exc}")

    # Typography was extracted by the download skill via computed styles.
    # Use whichever page has non-empty typography (usually the root).
    typography: dict = {}
    for p in download_result.pages:
        if p.typography:
            typography = dict(p.typography)
            break
    if typography:
        await bus.publish(
            "status",
            message=(
                f"🔤 Typography: heading={typography.get('heading_family')} "
                f"body={typography.get('body_family')} "
                f"button={typography.get('button_family')}"
            ),
        )

    design_tokens: dict = {}
    if palette.get("ordered"):
        # Flat list kept for any downstream consumer that expected the old shape.
        design_tokens["palette"] = palette["ordered"]
        design_tokens["palette_primary"] = palette.get("primary") or ""
        design_tokens["palette_secondary"] = palette.get("secondary", [])
        design_tokens["palette_accent"] = palette.get("accent", [])
        design_tokens["palette_coverage"] = palette.get("coverage", {})
    if typography:
        design_tokens["typography"] = typography

    summary = (
        f"Found {total} distinct components across {len(groups)} categories "
        f"from {len(tasks)} viewport chunk(s) (visual analysis)."
    )
    result = AnalysisResult(
        root_url=download_result.root_url,
        groups=groups,
        design_tokens=design_tokens,
        summary=summary,
    )
    storage.save_json("analysis", result.model_dump())
    await bus.publish("analysis", result=result.model_dump())
    await bus.publish(
        "skill_end",
        skill="analyze_screenshots",
        components=total,
        categories=len(groups),
    )
    return result
