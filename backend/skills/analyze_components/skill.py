"""Skill 2 — Analyze Components  (DEPRECATED).

⚠️  No longer wired into the orchestrator. Kept for reference / regression
testing only. Replaced by `analyze_screenshots` which runs visual-only
analysis on page screenshots — that sidesteps the problem of HTML snippets
that look trivial in markup but depend on external CSS (Chakra UI,
Tailwind utility classes, etc.) to render correctly.

If you want to re-enable this path, re-import `run_analyze_skill` in
`backend/agent/main_agent.py` and swap it back into the tool handler.

Original behavior: single multimodal Sonnet call per job receiving HTML +
computed styles + screenshots, returns the full component inventory.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ...shared.browser import BrowserManager
from ...shared.events import EventBus
from ...shared.html_utils import clean_html
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
from .subagent import extract_all_components

logger = logging.getLogger(__name__)


# Per-page HTML budget — keeps the combined input well under the 30k token/min
# rate limit (≈ 4 chars/token → 25k chars × 3 pages ≈ 19k tokens of text).
PER_PAGE_HTML_CHARS = 25_000
PER_PAGE_STYLES_CHARS = 8_000


@traced
async def _prepare_page_payloads(
    download_result: DownloadResult,
    browser: BrowserManager,
    storage: JobStorage,
    bus: EventBus,
) -> list[dict]:
    async def _one(page) -> dict:
        html = storage.read_html(page.html_path)
        raw_len = len(html)
        cleaned = clean_html(html, max_chars=PER_PAGE_HTML_CHARS)
        try:
            styles = await browser.extract_styles_from_html(html)
        except Exception as exc:
            logger.warning("extract_styles_from_html failed for %s: %s", page.url, exc)
            styles = ""
        if len(styles) > PER_PAGE_STYLES_CHARS:
            styles = styles[:PER_PAGE_STYLES_CHARS] + "\n# truncated"
        metric(
            url=page.url,
            raw_html_chars=raw_len,
            cleaned_html_chars=len(cleaned),
            styles_chars=len(styles),
        )
        await bus.publish("status", message=f"🧾 Prepared {page.url} ({len(cleaned):,} chars cleaned)")
        return {
            "url": page.url,
            "html": cleaned,
            "styles": styles,
            "screenshot_abs_path": str(PROJECT_ROOT / page.screenshot_path),
        }

    return await asyncio.gather(*(_one(p) for p in download_result.pages))


def _style_signature(styles: dict) -> str:
    keys = ("background_color", "text_color", "border_radius", "padding",
            "font_size", "font_weight", "border", "box_shadow")
    parts = []
    for k in keys:
        v = (styles.get(k) or "").strip().lower()
        v = re.sub(r"\s+", " ", v)
        parts.append(f"{k}:{v}")
    return "|".join(parts)


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
        html_snippet=str(c.get("html_snippet", ""))[:1000],
        source_url=str(c.get("source_url", "")),
        styles=style,
        count=int(c.get("count") or 1),
    )


def _group_and_dedupe(raw: list[dict]) -> list[ComponentGroup]:
    by_cat: dict[str, dict[str, Component]] = {}
    known_cats = set(COMPONENT_TAXONOMY.keys())
    for i, c in enumerate(raw):
        cat = str(c.get("category") or "Other").strip()
        if cat not in known_cats and cat != "Other":
            # Tolerate case / minor variation
            match = next((k for k in known_cats if k.lower() == cat.lower()), None)
            cat = match or "Other"
        comp = _to_component(c, i)
        sig = f"{comp.type}|" + _style_signature(c.get("styles", {}) or {})
        bucket = by_cat.setdefault(cat, {})
        if sig in bucket:
            bucket[sig].count += comp.count
        else:
            bucket[sig] = comp

    # Preserve taxonomy order, then "Other" at end
    ordered: list[ComponentGroup] = []
    for cat in list(COMPONENT_TAXONOMY.keys()) + ["Other"]:
        comps = list((by_cat.get(cat) or {}).values())
        if comps:
            ordered.append(ComponentGroup(category=cat, components=comps))
    return ordered


def _parse_rgb(s: str | None) -> tuple[int, ...] | None:
    if not s:
        return None
    m = re.search(r"rgb[a]?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", s)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


_TRANSPARENT = {"rgba(0, 0, 0, 0)", "transparent", ""}


def _enrichment_contradicts(llm_styles, enriched: dict) -> bool:
    """Return True if the enriched element is clearly a different component.

    A mismatch is declared when the LLM says the background is a solid colour
    but the enriched element is transparent (or vice-versa).  This catches
    cases where findElement picks a nav-link instead of the CTA button.
    """
    llm_bg = getattr(llm_styles, "background_color", None)
    enr_bg = enriched.get("background_color", "")
    if llm_bg and llm_bg not in _TRANSPARENT and enr_bg in _TRANSPARENT:
        return True
    if llm_bg and enr_bg:
        llm_rgb = _parse_rgb(llm_bg)
        enr_rgb = _parse_rgb(enr_bg)
        if llm_rgb and enr_rgb:
            dist = sum(abs(a - b) for a, b in zip(llm_rgb, enr_rgb))
            if dist > 200:
                return True
    return False


def _parse_snippet_meta(comp: Component) -> dict:
    """Extract tag, classes, href, text hint, and LLM-declared styles from a component."""
    snippet = comp.html_snippet
    if not snippet:
        return {"id": comp.id, "tag": None, "classes": [], "text_hint": comp.name, "href": None, "expected_styles": {}}
    tag_m = re.match(r"<(\w+)", snippet)
    tag = tag_m.group(1).lower() if tag_m else None
    cls_m = re.search(r'class="([^"]*)"', snippet)
    classes = cls_m.group(1).split() if cls_m else []
    href_m = re.search(r'href="([^"]*)"', snippet)
    href = href_m.group(1) if href_m else None
    text = re.sub(r"<[^>]+>", "", snippet).strip()[:60]
    expected = {}
    if comp.styles:
        if comp.styles.background_color:
            expected["background_color"] = comp.styles.background_color
        if comp.styles.font_size:
            expected["font_size"] = comp.styles.font_size
        if comp.styles.font_weight:
            expected["font_weight"] = comp.styles.font_weight
    return {"id": comp.id, "tag": tag, "classes": classes, "text_hint": text or comp.name, "href": href, "expected_styles": expected}


@traced
async def _enrich_component_styles(
    groups: list[ComponentGroup],
    download_result: DownloadResult,
    browser: BrowserManager,
    storage: JobStorage,
    bus: EventBus,
) -> list[ComponentGroup]:
    """Post-processing: resolve real computed styles from the saved HTML via Playwright."""
    page_html: dict[str, str] = {}
    for p in download_result.pages:
        page_html[p.url] = storage.read_html(p.html_path)

    comp_by_page: dict[str, list[tuple[Component, dict]]] = {}
    for group in groups:
        for comp in group.components:
            url = comp.source_url
            if url not in page_html:
                url = download_result.pages[0].url if download_result.pages else ""
            if url in page_html:
                meta = _parse_snippet_meta(comp)
                comp_by_page.setdefault(url, []).append((comp, meta))

    enriched_count = 0
    for url, items in comp_by_page.items():
        html = page_html[url]
        specs = [meta for _, meta in items]
        await bus.publish("status", message=f"🎨 Enriching {len(specs)} components from {url}")
        try:
            results = await browser.enrich_component_styles(html, specs)
        except Exception as exc:
            logger.warning("enrich_component_styles failed for %s: %s", url, exc)
            continue

        by_id = {r["id"]: r for r in results}
        for comp, _ in items:
            enriched = by_id.get(comp.id)
            if not enriched:
                continue
            s = enriched["styles"]
            if _enrichment_contradicts(comp.styles, s):
                logger.info(
                    "Skipping contradictory enrichment for %s (%s): "
                    "LLM bg=%s, enriched bg=%s",
                    comp.id, comp.name,
                    comp.styles.background_color, s.get("background_color"),
                )
                continue
            if s.get("background_color"):
                comp.styles.background_color = s["background_color"]
            if s.get("text_color"):
                comp.styles.text_color = s["text_color"]
            if s.get("border_radius"):
                comp.styles.border_radius = s["border_radius"]
            if s.get("padding"):
                comp.styles.padding = s["padding"]
            if s.get("font_size"):
                comp.styles.font_size = s["font_size"]
            if s.get("font_weight"):
                comp.styles.font_weight = s["font_weight"]
            if s.get("font_family"):
                comp.styles.font_family = s["font_family"]
            if s.get("border"):
                comp.styles.border = s["border"]
            if s.get("box_shadow"):
                comp.styles.box_shadow = s["box_shadow"]
            if enriched.get("enriched_snippet"):
                comp.html_snippet = enriched["enriched_snippet"][:1000]
            enriched_count += 1

    metric(enriched_components=enriched_count)
    note(f"Enriched {enriched_count} components with real computed styles")
    return groups


def _extract_design_tokens(per_page: list[dict]) -> dict[str, list[str]]:
    tokens: dict[str, set[str]] = {}
    pattern = re.compile(r"^\s*(--[a-zA-Z0-9_\-]+):\s*(.+?)\s*$", re.MULTILINE)
    for p in per_page:
        for name, value in pattern.findall(p.get("styles") or ""):
            tokens.setdefault(name, set()).add(value)
    return {k: sorted(v) for k, v in tokens.items()}


@traced
async def run_analyze_skill(
    download_result: DownloadResult,
    browser: BrowserManager,
    storage: JobStorage,
    bus: EventBus,
) -> AnalysisResult:
    await bus.publish(
        "skill_start",
        skill="analyze_components",
        message=f"Analyzing {len(download_result.pages)} pages in a single multimodal pass",
    )

    await bus.publish("status", message="📖 Reading saved HTML + extracting computed styles…")
    payloads = await _prepare_page_payloads(download_result, browser, storage, bus)

    await bus.publish(
        "status",
        message="🧠 Single Sonnet call: screenshots + HTML + styles → full component inventory…",
    )
    raw = await extract_all_components(payloads)
    await bus.publish("status", message=f"  ✓ Extracted {len(raw)} raw components")

    groups = _group_and_dedupe(raw)
    total = sum(len(g.components) for g in groups)
    note(f"Grouped: {len(raw)} raw → {total} unique across {len(groups)} categories")
    for g in groups:
        metric(category=g.category, components=len(g.components))

    await bus.publish("status", message="🎨 Enriching components with real computed styles from saved pages…")
    groups = await _enrich_component_styles(groups, download_result, browser, storage, bus)

    tokens = _extract_design_tokens(payloads)
    summary = (
        f"Found {total} distinct components across {len(groups)} categories "
        f"from {len(download_result.pages)} pages."
    )
    result = AnalysisResult(
        root_url=download_result.root_url,
        groups=groups,
        design_tokens=tokens,
        summary=summary,
    )
    storage.save_json("analysis", result.model_dump())
    await bus.publish("analysis", result=result.model_dump())
    await bus.publish("skill_end", skill="analyze_components", components=total, categories=len(groups))
    return result
