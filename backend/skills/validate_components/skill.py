"""Skill 3 — Validate Components.

Render-test each component's html_snippet standalone. For those that come
out blank/broken, batch them per source page and ask Sonnet to regenerate
self-contained HTML from the page screenshot. See SKILL.md.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from ...shared.browser import BrowserManager
from ...shared.events import EventBus
from ...shared.schemas import AnalysisResult, Component, DownloadResult
from ...shared.storage import JobStorage, PROJECT_ROOT
from ...shared.trace import metric, note, traced
from .subagent import regenerate_from_crop, regenerate_from_screenshot

logger = logging.getLogger(__name__)


# Cap concurrent render-tests — Chromium is the bottleneck, not Claude here.
RENDER_CONCURRENCY = 4
# Don't fan-out beyond this many components per page to one regeneration call.
REGEN_BATCH_SIZE = 12


async def _render_test_one(
    browser: BrowserManager, comp: Component, base_href: str | None
) -> tuple[Component, dict]:
    try:
        result = await browser.measure_snippet_render(comp.html_snippet, base_href)
    except Exception as exc:
        logger.warning("render test failed for %s: %s", comp.id, exc)
        result = {"blank": True, "reason": f"render_error: {exc}"}
    return comp, result


def _screenshot_path_for(download: DownloadResult, url: str) -> str | None:
    for p in download.pages:
        if p.url == url:
            return str(PROJECT_ROOT / p.screenshot_path)
    # Fallback: first page
    if download.pages:
        return str(PROJECT_ROOT / download.pages[0].screenshot_path)
    return None


@traced
async def run_validate_skill(
    analysis: AnalysisResult,
    download: DownloadResult,
    browser: BrowserManager,
    storage: JobStorage,
    bus: EventBus,
) -> AnalysisResult:
    total = sum(len(g.components) for g in analysis.groups)
    await bus.publish(
        "skill_start",
        skill="validate_components",
        message=f"Render-testing {total} components…",
    )

    # ── 1. Standalone render test for every component ──────────────────
    sem = asyncio.Semaphore(RENDER_CONCURRENCY)
    results: list[tuple[Component, dict]] = []

    async def _guarded(comp: Component) -> None:
        async with sem:
            base_href = comp.source_url or analysis.root_url
            res = await _render_test_one(browser, comp, base_href)
            results.append(res)

    all_components: list[Component] = [c for g in analysis.groups for c in g.components]
    await asyncio.gather(*(_guarded(c) for c in all_components))

    ok_count = 0
    bad: list[Component] = []
    for comp, r in results:
        reason = r.get("reason", "")
        if r.get("blank"):
            comp.validation_status = "unrecoverable"  # preliminary; may be overwritten
            comp.validation_note = f"blank render: {reason}"
            bad.append(comp)
        else:
            comp.validation_status = "ok"
            comp.validation_note = (
                f"h={r.get('height')} colors={r.get('unique_colors')} "
                f"stddev={r.get('stddev')} edges={r.get('edge_density')}"
            )
            ok_count += 1

    metric(total=total, ok=ok_count, bad=len(bad))
    await bus.publish(
        "status",
        message=f"🧪 Render test: {ok_count}/{total} rendered cleanly · {len(bad)} need regeneration",
    )

    if not bad:
        note("No components needed regeneration — all rendered cleanly in isolation")
        analysis.summary = (
            analysis.summary + " All components validated."
            if analysis.summary else "All components validated."
        )
        storage.save_json("analysis", analysis.model_dump())
        await bus.publish("analysis", result=analysis.model_dump())
        await bus.publish(
            "skill_end", skill="validate_components",
            validated=ok_count, regenerated=0, unrecoverable=0,
        )
        return analysis

    # ── 2. Regenerate. Prefer per-component CROP when available (far more
    #      faithful). Fall back to page-screenshot batch regen otherwise.
    regenerated = 0
    unrecoverable = 0

    with_crop = [c for c in bad if c.screenshot_crop]
    without_crop = [c for c in bad if not c.screenshot_crop]

    if with_crop:
        await bus.publish(
            "status",
            message=f"🎯 Regenerating {len(with_crop)} snippets from tight component crops…",
        )

    crop_sem = asyncio.Semaphore(RENDER_CONCURRENCY)

    async def _regen_one_crop(c: Component) -> None:
        nonlocal regenerated, unrecoverable
        async with crop_sem:
            crop_abs = str(PROJECT_ROOT / c.screenshot_crop)
            payload = {
                "name": c.name,
                "type": c.type,
                "description": c.description,
                "styles": c.styles.model_dump(),
            }
            entry = await regenerate_from_crop(crop_abs, payload)
            if not entry.get("html_snippet") or entry.get("unrecoverable"):
                c.validation_status = "unrecoverable"
                c.validation_note = (c.validation_note + "; crop_regen_failed").strip("; ")
                unrecoverable += 1
                return
            new_snippet = entry["html_snippet"][:2000]
            try:
                verify = await browser.measure_snippet_render(
                    new_snippet, c.source_url or analysis.root_url
                )
            except Exception as exc:
                verify = {"blank": True, "reason": f"verify_error: {exc}"}
            if verify.get("blank"):
                c.validation_status = "unrecoverable"
                c.validation_note = f"crop_regen_still_blank: {verify.get('reason', '')}"
                unrecoverable += 1
            else:
                c.html_snippet = new_snippet
                c.validation_status = "regenerated"
                c.validation_note = (
                    f"crop_regen h={verify.get('height')} "
                    f"colors={verify.get('unique_colors')} "
                    f"edges={verify.get('edge_density')}"
                )
                regenerated += 1

    await asyncio.gather(*(_regen_one_crop(c) for c in with_crop))

    # Fallback: components without a saved crop go through page-screenshot regen.
    by_page: dict[str, list[Component]] = defaultdict(list)
    for c in without_crop:
        key = c.source_url or analysis.root_url
        by_page[key].append(c)

    if by_page:
        await bus.publish(
            "status",
            message=f"🎯 Regenerating {len(without_crop)} remaining snippets from {len(by_page)} page screenshot(s)…",
        )

    for page_url, comps in by_page.items():
        ss_path = _screenshot_path_for(download, page_url)
        if not ss_path:
            for c in comps:
                c.validation_status = "unrecoverable"
                c.validation_note = (c.validation_note + "; no_screenshot").strip("; ")
                unrecoverable += 1
            continue

        # Process in reasonably-sized batches so each call stays focused.
        for i in range(0, len(comps), REGEN_BATCH_SIZE):
            batch = comps[i : i + REGEN_BATCH_SIZE]
            payload = [
                {
                    "id": c.id,
                    "name": c.name,
                    "type": c.type,
                    "description": c.description,
                    "html_snippet": c.html_snippet,
                    "styles": c.styles.model_dump(),
                }
                for c in batch
            ]
            await bus.publish(
                "status",
                message=f"  → Regenerating {len(batch)} on {page_url}…",
            )
            results_by_id = await regenerate_from_screenshot(ss_path, page_url, payload)

            for c in batch:
                entry = results_by_id.get(c.id)
                if not entry:
                    c.validation_status = "unrecoverable"
                    c.validation_note = (c.validation_note + "; no_regen_result").strip("; ")
                    unrecoverable += 1
                    continue
                if entry["unrecoverable"] or not entry["html_snippet"]:
                    c.validation_status = "unrecoverable"
                    c.validation_note = (c.validation_note + "; llm_unrecoverable").strip("; ")
                    unrecoverable += 1
                    continue

                # Re-test the regenerated snippet so we don't silently keep a
                # still-broken replacement.
                new_snippet = entry["html_snippet"][:2000]
                try:
                    verify = await browser.measure_snippet_render(
                        new_snippet, c.source_url or analysis.root_url
                    )
                except Exception as exc:
                    verify = {"blank": True, "reason": f"verify_error: {exc}"}

                if verify.get("blank"):
                    c.validation_status = "unrecoverable"
                    c.validation_note = (
                        f"regen_still_blank: {verify.get('reason', '')}"
                    )
                    unrecoverable += 1
                else:
                    c.html_snippet = new_snippet
                    c.validation_status = "regenerated"
                    c.validation_note = (
                        f"regen h={verify.get('height')} "
                        f"colors={verify.get('unique_colors')} "
                        f"edges={verify.get('edge_density')}"
                    )
                    regenerated += 1

    metric(
        regenerated=regenerated,
        unrecoverable=unrecoverable,
        ok=ok_count,
        total=total,
    )
    await bus.publish(
        "status",
        message=(
            f"✅ Validation complete: {ok_count} ok · {regenerated} regenerated · "
            f"{unrecoverable} unrecoverable"
        ),
    )

    analysis.summary = (
        (analysis.summary + " " if analysis.summary else "")
        + f"Validated: {ok_count} rendered cleanly, {regenerated} regenerated from "
        f"screenshots, {unrecoverable} unrecoverable."
    )
    storage.save_json("analysis", analysis.model_dump())
    await bus.publish("analysis", result=analysis.model_dump())
    await bus.publish(
        "skill_end",
        skill="validate_components",
        validated=ok_count,
        regenerated=regenerated,
        unrecoverable=unrecoverable,
    )
    return analysis
