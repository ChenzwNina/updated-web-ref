"""Skill 1 — Download Website (single-page + chunked screenshots).

Imperative runner invoked by the main agent.

Focus mode: only the URL the user provided is captured. We no longer pick
representative subpages — users asked us to concentrate on the one page
they gave us so component detection is more accurate.

Capture mode: instead of one full-page screenshot (which makes LLM bbox
estimates drift wildly on tall pages), we take a sequence of
viewport-sized chunks (~1440×900 each) by scrolling the page top-to-bottom.
Analyze-skill then runs one Sonnet call per chunk, so bbox output is in
chunk-local coordinates and crops are precise.

Also saves the raw DOM (one pass at page-load) for reference — useful for
future skills that want to cross-reference markup with the pixel analysis.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ...shared.browser import BrowserManager
from ...shared.events import EventBus
from ...shared.schemas import DownloadedPage, DownloadResult, ScreenshotChunk
from ...shared.storage import JobStorage
from ...shared.trace import metric, traced

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


@traced
async def run_download_skill(
    root_url: str,
    browser: BrowserManager,
    storage: JobStorage,
    bus: EventBus,
) -> DownloadResult:
    root_url = _normalize_url(root_url)

    await bus.publish(
        "skill_start",
        skill="download_website",
        message=f"Downloading {root_url} as viewport chunks",
    )
    await bus.publish("status", message=f"📷 Capturing {root_url} as viewport chunks…")

    try:
        chunks = await browser.capture_viewport_chunks(
            root_url, chunk_height=900, overlap=80, max_chunks=12,
        )
    except Exception as exc:
        logger.warning("capture_viewport_chunks failed for %s: %s", root_url, exc)
        await bus.publish("status", message=f"⚠️  Could not load {root_url}: {exc}")
        raise RuntimeError(
            f"Could not load {root_url}: {exc}"
        ) from exc

    if not chunks:
        raise RuntimeError(f"Could not load {root_url}: no screenshots captured")

    # Save the full DOM (captured with the page in initial state) for any
    # downstream skill that wants it.
    first = chunks[0]
    html = first.get("html", "")
    title = first.get("title", "") or urlparse(root_url).path or root_url
    html_path = storage.save_html(root_url, html) if html else ""

    # Persist each chunk screenshot.
    chunk_models: list[ScreenshotChunk] = []
    for c in chunks:
        rel = storage.save_screenshot_chunk(root_url, c["index"], c["screenshot"])
        chunk_models.append(ScreenshotChunk(
            index=c["index"],
            path=rel,
            offset_y=c["offset_y"],
            width=c["width"],
            height=c["height"],
        ))

    # Also save chunk 0 under the canonical "page screenshot" slot so any
    # older code that reads `DownloadedPage.screenshot_path` still works.
    first_ss_path = chunk_models[0].path if chunk_models else ""

    typography = first.get("typography", {}) or {}

    page = DownloadedPage(
        url=root_url,
        html_path=html_path,
        screenshot_path=first_ss_path,
        title=title,
        chunks=chunk_models,
        typography=typography,
    )

    metric(
        url=root_url,
        chunks=len(chunk_models),
        full_height=first.get("full_height", 0),
        html_chars=len(html),
    )
    await bus.publish(
        "status",
        message=f"✅ Captured {len(chunk_models)} viewport chunk(s) of {root_url}",
    )

    result = DownloadResult(
        root_url=root_url,
        pages=[page],
        job_dir=str(storage.base_dir.relative_to(storage.base_dir.parent.parent)).replace("\\", "/"),
    )
    storage.save_json("download_result", result.model_dump())
    await bus.publish("skill_end", skill="download_website", pages=1, chunks=len(chunk_models))
    return result
