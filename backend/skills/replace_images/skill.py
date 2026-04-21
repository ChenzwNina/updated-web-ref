"""Skill 4 — Replace Images with Unsplash photos.

For every component whose html_snippet contains image slots (<img src="...">
or inline background-image: url(...)), we ask a multimodal subagent to
look at the crop and suggest Unsplash-style keywords, then resolve each
keyword to a real photo URL and rewrite the snippet. See SKILL.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any
from urllib.parse import quote_plus

from ...shared.events import EventBus
from ...shared.schemas import AnalysisResult
from ...shared.storage import JobStorage, PROJECT_ROOT
from ...shared.trace import metric, note, traced
from .subagent import suggest_image_keywords

logger = logging.getLogger(__name__)


# Match both <img ...> tags and `background-image: url(...)`.
# For <img> we capture the full tag so we can inspect `alt` / `data-alt`
# for a concrete subject before falling back to the crop-keyword subagent.
_IMG_TAG_RE = re.compile(r'<img\b[^>]*?>', re.IGNORECASE)
_IMG_SRC_RE = re.compile(r'(\bsrc\s*=\s*)(["\'])([^"\']*)\2', re.IGNORECASE)
_IMG_ALT_RE = re.compile(r'\balt\s*=\s*(["\'])([^"\']*)\1', re.IGNORECASE)
_IMG_DATA_ALT_RE = re.compile(r'\bdata-alt\s*=\s*(["\'])([^"\']*)\1', re.IGNORECASE)
_BG_IMG_RE = re.compile(
    r'(background(?:-image)?\s*:\s*url\()(["\']?)([^"\')]+)\2(\))',
    re.IGNORECASE,
)
# Elements that carry `data-alt="..."` (used for background-image slots
# whose subject the analyzer captured out-of-band).
_DATA_ALT_ELEM_RE = re.compile(
    r'<[^>]*\bdata-alt\s*=\s*(["\'])([^"\']*)\1[^>]*>', re.IGNORECASE,
)

# Tuning: cap how many components we process in parallel so we don't thrash
# the LLM or Unsplash API.
CONCURRENCY = 3


def _extract_slots(snippet: str) -> list[dict]:
    """Walk the snippet in source order and return one dict per image slot.

    Each dict: {"kind": "img"|"bg", "alt": str | None, "match": re.Match}
    The `alt` is lifted from the tag's alt / data-alt attribute when
    present, so the replacer can skip the subagent keyword call for those
    slots (a concrete alt is usually better than re-deriving keywords).
    """
    slots: list[dict] = []
    for m in _IMG_TAG_RE.finditer(snippet):
        tag = m.group(0)
        # Skip tags with no src at all (nothing to replace).
        if not _IMG_SRC_RE.search(tag):
            continue
        alt_m = _IMG_ALT_RE.search(tag) or _IMG_DATA_ALT_RE.search(tag)
        alt = alt_m.group(2).strip() if alt_m else None
        slots.append({"kind": "img", "alt": alt, "start": m.start(), "end": m.end()})
    for m in _BG_IMG_RE.finditer(snippet):
        # Try to find a data-alt on the containing element (search backward
        # from the match for the nearest opening tag).
        prefix = snippet[:m.start()]
        last_lt = prefix.rfind("<")
        alt = None
        if last_lt != -1:
            tag_end = snippet.find(">", last_lt)
            if tag_end != -1:
                tag = snippet[last_lt:tag_end + 1]
                a = _IMG_DATA_ALT_RE.search(tag)
                if a:
                    alt = a.group(2).strip()
        slots.append({"kind": "bg", "alt": alt, "start": m.start(), "end": m.end()})
    slots.sort(key=lambda s: s["start"])
    return slots


def _count_image_slots(snippet: str) -> int:
    return len(_extract_slots(snippet))


# ─────────────────────────── URL resolution ──────────────────────────

_UNSPLASH_BASE = "https://api.unsplash.com"


async def _unsplash_search(
    query: str, access_key: str, client, variant: int = 0,
) -> str | None:
    """Hit Unsplash's search API and return a distinct result per variant.

    `variant` is the slot index within a component — we fetch a page of 10
    results and pick `variant % len(results)` so a gallery of 5 slots with
    the same keyword maps to 5 different photos.
    """
    try:
        r = await client.get(
            f"{_UNSPLASH_BASE}/search/photos",
            params={"query": query, "per_page": 10, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {access_key}"},
            timeout=10.0,
        )
        if r.status_code != 200:
            logger.warning("unsplash search %r → HTTP %s", query, r.status_code)
            return None
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None
        pick = results[variant % len(results)]
        return pick.get("urls", {}).get("regular")
    except Exception as exc:
        logger.warning("unsplash search %r failed: %s", query, exc)
        return None


def _fallback_url(
    query: str, width: int = 1200, height: int = 800, variant: int = 0,
) -> str:
    """Keyword-matched fallback when no Unsplash key is configured.

    Appends `/lock/<variant>` — LoremFlickr honors this as a deterministic
    photo seed, so identical-keyword slots with different variant indices
    get different photos instead of the cached one.
    """
    slug = quote_plus(query.strip() or "abstract")
    lock = (hash(query) ^ variant) & 0xFFFF
    return f"https://loremflickr.com/{width}/{height}/{slug}/lock/{lock}"


async def _resolve_one(
    query: str, access_key: str | None, http_client, variant: int = 0,
) -> str:
    if access_key and http_client is not None:
        url = await _unsplash_search(query, access_key, http_client, variant=variant)
        if url:
            return url
    return _fallback_url(query, variant=variant)


# ─────────────────────── snippet rewriting ───────────────────────────


def _rewrite_snippet(snippet: str, urls: list[str]) -> tuple[str, list[str]]:
    """Replace <img src="..."> and background-image url(...) in source
    order with the provided URLs. Returns (new_snippet, original_urls)."""
    originals: list[str] = []
    url_iter = iter(urls)

    def _img_sub(m: re.Match) -> str:
        try:
            new = next(url_iter)
        except StopIteration:
            return m.group(0)
        originals.append(m.group(3))
        return f'{m.group(1)}{m.group(2)}{new}{m.group(2)}'

    def _bg_sub(m: re.Match) -> str:
        try:
            new = next(url_iter)
        except StopIteration:
            return m.group(0)
        originals.append(m.group(3))
        return f'{m.group(1)}{m.group(2)}{new}{m.group(2)}{m.group(4)}'

    out = _IMG_SRC_RE.sub(_img_sub, snippet)
    out = _BG_IMG_RE.sub(_bg_sub, out)
    return out, originals


# ──────────────────────────── the skill ──────────────────────────────


@traced
async def run_replace_images_skill(
    analysis: AnalysisResult,
    storage: JobStorage,
    bus: EventBus,
) -> AnalysisResult:
    all_components = [c for g in analysis.groups for c in g.components]
    targets = [c for c in all_components if _count_image_slots(c.html_snippet) > 0]

    await bus.publish(
        "skill_start",
        skill="replace_images",
        message=f"Checking {len(all_components)} components for image slots…",
    )

    if not targets:
        note("No components with image slots — skipping Unsplash replacement")
        await bus.publish(
            "status", message="🖼️  No image slots found — nothing to replace"
        )
        await bus.publish(
            "skill_end", skill="replace_images", replaced=0, components=0,
        )
        return analysis

    access_key = os.getenv("UNSPLASH_ACCESS_KEY")
    if access_key:
        await bus.publish(
            "status",
            message=f"🖼️  Replacing images in {len(targets)} component(s) via Unsplash…",
        )
    else:
        note("UNSPLASH_ACCESS_KEY not set — falling back to LoremFlickr (keyword-matched CC photos)")
        await bus.publish(
            "status",
            message=f"🖼️  Replacing images in {len(targets)} component(s) (no Unsplash key — using LoremFlickr)…",
        )

    # Create a single httpx client for the whole run. Import lazy so we
    # don't pay the cost on jobs with no image slots.
    try:
        import httpx
        http_client = httpx.AsyncClient()
    except Exception:
        http_client = None

    sem = asyncio.Semaphore(CONCURRENCY)
    replaced_total = 0
    failed_total = 0

    async def _process(comp) -> None:
        nonlocal replaced_total, failed_total
        async with sem:
            slots = _extract_slots(comp.html_snippet)
            if not slots:
                return

            # Slots with a concrete alt / data-alt already describe their
            # subject. Only call the crop-keyword subagent for slots
            # missing alt text. This also means media components that
            # carry PLACEHOLDER_IMAGE + alt="pink peony bouquet" skip the
            # extra LLM call entirely.
            missing = [i for i, s in enumerate(slots) if not s["alt"]]
            subagent_queries: list[str] = []
            if missing:
                crop_abs = (
                    str(PROJECT_ROOT / comp.screenshot_crop)
                    if comp.screenshot_crop else None
                )
                subagent_queries = await suggest_image_keywords(
                    crop_abs,
                    {"name": comp.name, "type": comp.type, "description": comp.description},
                    len(missing),
                )
                if not subagent_queries:
                    subagent_queries = [comp.name or "abstract"] * len(missing)

            # Weave alts + subagent-keywords back in source order.
            queries: list[str] = []
            q_iter = iter(subagent_queries)
            for s in slots:
                if s["alt"]:
                    queries.append(s["alt"])
                else:
                    queries.append(next(q_iter, comp.name or "abstract"))

            # Resolve each query concurrently. `variant=i` guarantees
            # distinct images even when multiple slots share a keyword
            # (common for galleries where every slot is "flower photo").
            urls: list[str] = await asyncio.gather(
                *(
                    _resolve_one(q, access_key, http_client, variant=i)
                    for i, q in enumerate(queries)
                )
            )

            new_snippet, originals = _rewrite_snippet(comp.html_snippet, urls)
            if new_snippet != comp.html_snippet:
                comp.html_snippet = new_snippet[:2000]
                replaced_total += len(urls)
                metric(
                    component=comp.id,
                    queries=queries,
                    replaced=len(urls),
                )
                if originals:
                    tag = "; ".join(f"slot{i}={o[:80]}" for i, o in enumerate(originals))
                    comp.validation_note = (
                        (comp.validation_note + "; " if comp.validation_note else "")
                        + f"images replaced ({len(urls)}): {queries} | originals: {tag}"
                    )[:500]
                await bus.publish(
                    "status",
                    message=f"  ✓ {comp.name}: {len(urls)} image(s) → {queries}",
                )
            else:
                failed_total += 1

    try:
        await asyncio.gather(*(_process(c) for c in targets))
    finally:
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:
                pass

    analysis.summary = (
        (analysis.summary + " " if analysis.summary else "")
        + f"Replaced {replaced_total} image(s) across {len(targets)} components "
        f"({'Unsplash' if access_key else 'LoremFlickr'})."
    )
    storage.save_json("analysis", analysis.model_dump())
    await bus.publish("analysis", result=analysis.model_dump())
    await bus.publish(
        "skill_end",
        skill="replace_images",
        components=len(targets),
        replaced=replaced_total,
        failed=failed_total,
    )
    return analysis
