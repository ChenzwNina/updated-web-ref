"""Main orchestrator — Claude Opus 4.6 with tool-use.

Two entry points:
- `run_analysis_phase(url, ...)` — agent decides when to download & analyze.
- `run_generate_phase(request, analysis, download, ...)` — agent decides
  how to call the generate skill.

Each phase runs a Claude Opus 4.6 tool-use loop. Tools delegate to skills.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..shared.browser import BrowserManager
from ..shared.events import EventBus
from ..shared.llm import MAIN_AGENT_MODEL, ToolSpec, agent_loop
from ..shared.schemas import (
    AnalysisResult,
    DownloadResult,
    GeneratedSite,
    GenerateRequest,
)
from ..shared.storage import JobStorage
from ..shared.trace import metric, note, traced
# NOTE: `analyze_components` (HTML-based) is deprecated — replaced by
# `analyze_screenshots` (screenshot-only, visual analysis). The old import is
# left commented out so it's easy to swap back for experiments.
# from ..skills.analyze_components import run_analyze_skill  # deprecated
from ..skills.analyze_screenshots import run_analyze_screenshots_skill
from ..skills.download_website import run_download_skill
from ..skills.generate_website import run_generate_skill
from ..skills.replace_images import run_replace_images_skill
from ..skills.validate_components import run_validate_skill

logger = logging.getLogger(__name__)


ANALYSIS_SYSTEM = """\
You are the orchestrator for a web-style-reference tool. A user pasted a URL \
and wants their site's visual design captured so they can later generate a \
new site in the same style.

You have four skills available via tools:

1. `download_website(root_url)` — Captures ONLY the single URL the user \
   provided (no subpage crawling), split into viewport-sized screenshot \
   chunks (~1440×900 each) by scrolling the page top-to-bottom. Also saves \
   the initial DOM. Returns a DownloadResult whose one page has a list of \
   chunks.
2. `analyze_screenshots()` — Visual-only multimodal Sonnet analysis: one \
   call per viewport chunk, producing distinct UI components with \
   self-contained inline-styled HTML snippets and chunk-local bboxes. \
   Results are deduped across chunks. No HTML is consulted.
3. `validate_components()` — Render-tests every extracted component's HTML \
   snippet in a headless browser. Any that collapse to blank get \
   regenerated from the original page screenshot.
4. `replace_images()` — For every component with image slots (<img> or \
   background-image), a multimodal subagent looks at the component crop, \
   picks search keywords describing the image content, and rewrites the \
   snippet to use a real Unsplash (or LoremFlickr fallback) URL so the \
   component renders correctly when exported.

Your job for this phase: call `download_website` exactly once with the \
user's URL, then `analyze_screenshots` exactly once, then \
`validate_components` exactly once, then `replace_images` exactly once. \
Then STOP — reply with one short sentence confirming the analysis is \
ready. The user will request generation in a separate step.

Do NOT invent tools. Always run the four in order: download → \
analyze_screenshots → validate → replace_images.\
"""

GENERATE_SYSTEM = """\
You are the orchestrator for a web-style-reference tool. The user has \
already had their reference site downloaded and analyzed. They have now \
submitted a generation request — a site type, a list of pages, and any \
extra instructions.

You have ONE skill available via a tool:

- `generate_website(site_type, pages, extra_instructions)` — Produces a \
  complete single-file HTML website that matches the reference site's design.

Call this tool exactly once with the user's request, then STOP and reply \
with one short sentence confirming the site was generated.\
"""


@dataclass
class AnalysisPhaseState:
    download_result: DownloadResult | None = None
    analysis_result: AnalysisResult | None = None


@traced
async def run_analysis_phase(
    url: str,
    browser: BrowserManager,
    storage: JobStorage,
    bus: EventBus,
) -> tuple[DownloadResult, AnalysisResult]:
    """Kick off the main agent to download + analyze the URL.

    The agent is an Opus 4.6 tool-use loop with two tools — one per skill.
    Skill invocations update `state` and publish events on the bus.
    """
    state = AnalysisPhaseState()

    async def _download(args: dict) -> str:
        note(f"main_agent tool_call: download_website(root_url={args.get('root_url')!r})")
        root = args.get("root_url") or url
        result = await run_download_skill(root, browser, storage, bus)
        state.download_result = result
        metric(skill="download_website", pages=len(result.pages))
        return json.dumps({
            "pages": [{"url": p.url, "title": p.title} for p in result.pages],
            "job_dir": result.job_dir,
            "message": f"Downloaded {len(result.pages)} pages",
        })

    async def _analyze(args: dict) -> str:
        note("main_agent tool_call: analyze_screenshots()")
        if state.download_result is None:
            return "Error: call download_website first."
        result = await run_analyze_screenshots_skill(
            state.download_result, storage, bus,
        )
        state.analysis_result = result
        metric(
            skill="analyze_screenshots",
            categories=len(result.groups),
            total_components=sum(len(g.components) for g in result.groups),
        )
        return json.dumps({
            "categories": [g.category for g in result.groups],
            "total_components": sum(len(g.components) for g in result.groups),
            "summary": result.summary,
        })

    async def _validate(args: dict) -> str:
        note("main_agent tool_call: validate_components()")
        if state.analysis_result is None or state.download_result is None:
            return "Error: call download_website and analyze_components first."
        result = await run_validate_skill(
            state.analysis_result, state.download_result, browser, storage, bus,
        )
        state.analysis_result = result
        ok = sum(1 for g in result.groups for c in g.components
                 if c.validation_status == "ok")
        regen = sum(1 for g in result.groups for c in g.components
                    if c.validation_status == "regenerated")
        unrec = sum(1 for g in result.groups for c in g.components
                    if c.validation_status == "unrecoverable")
        metric(skill="validate_components", ok=ok, regenerated=regen, unrecoverable=unrec)
        return json.dumps({
            "ok": ok,
            "regenerated": regen,
            "unrecoverable": unrec,
            "message": f"{ok} rendered cleanly, {regen} regenerated from screenshot, {unrec} unrecoverable.",
        })

    async def _replace_images(args: dict) -> str:
        note("main_agent tool_call: replace_images()")
        if state.analysis_result is None:
            return "Error: call analyze_screenshots and validate_components first."
        result = await run_replace_images_skill(state.analysis_result, storage, bus)
        state.analysis_result = result
        # Count components whose notes mention image replacement.
        replaced_components = sum(
            1 for g in result.groups for c in g.components
            if "images replaced" in (c.validation_note or "")
        )
        metric(skill="replace_images", replaced_components=replaced_components)
        return json.dumps({
            "replaced_components": replaced_components,
            "message": f"Replaced images in {replaced_components} component(s).",
        })

    tools = [
        ToolSpec(
            name="download_website",
            description="Capture the single given URL as a sequence of viewport-sized (~1440x900) screenshot chunks by scrolling. Also saves the initial DOM. No subpage crawling.",
            input_schema={
                "type": "object",
                "properties": {
                    "root_url": {"type": "string", "description": "The URL to analyze."},
                },
                "required": ["root_url"],
            },
            handler=_download,
        ),
        ToolSpec(
            name="analyze_screenshots",
            description="Visually analyze each viewport screenshot chunk (no HTML) and return a deduped component library with self-contained inline-styled HTML snippets and accurate chunk-local bboxes.",
            input_schema={"type": "object", "properties": {}},
            handler=_analyze,
        ),
        ToolSpec(
            name="validate_components",
            description="Render-test each extracted component's HTML snippet; regenerate any that render blank in isolation from the original page screenshot so they become self-contained.",
            input_schema={"type": "object", "properties": {}},
            handler=_validate,
        ),
        ToolSpec(
            name="replace_images",
            description="Replace every component's image placeholders with real Unsplash photos (LoremFlickr fallback). A multimodal subagent inspects each component's crop to pick image-search keywords that match the original screenshot's subject.",
            input_schema={"type": "object", "properties": {}},
            handler=_replace_images,
        ),
    ]

    await bus.publish("agent_start", phase="analysis", model=MAIN_AGENT_MODEL)

    async def _on_event(event: str, data: dict) -> None:
        if event == "tool_use":
            await bus.publish("status", message=f"🤖 Main agent → calling `{data.get('name')}`")

    final_text = await agent_loop(
        model=MAIN_AGENT_MODEL,
        system=ANALYSIS_SYSTEM,
        user_prompt=f"The user wants their web-style reference extracted from: {url}\n\nProceed.",
        tools=tools,
        max_tokens=2048,
        max_turns=10,
        on_event=_on_event,
    )

    if state.download_result is None or state.analysis_result is None:
        raise RuntimeError(
            f"Main agent did not complete both skills. Final reply: {final_text!r}"
        )

    await bus.publish("agent_end", phase="analysis", reply=final_text)
    return state.download_result, state.analysis_result


@traced
async def run_generate_phase(
    request: GenerateRequest,
    analysis: AnalysisResult,
    download: DownloadResult | None,
    storage: JobStorage,
    bus: EventBus,
) -> GeneratedSite:
    """Run the main agent for the generate phase. Returns GeneratedSite."""
    captured: dict[str, GeneratedSite] = {}

    async def _generate(args: dict) -> str:
        req = GenerateRequest(
            site_type=args.get("site_type") or request.site_type,
            pages=args.get("pages") or request.pages,
            extra_instructions=args.get("extra_instructions") or request.extra_instructions,
        )
        result = await run_generate_skill(req, analysis, download, storage, bus)
        captured["site"] = result
        return json.dumps({
            "chars": len(result.html),
            "pages_generated": result.pages_generated,
            "message": "Site generated.",
        })

    tools = [
        ToolSpec(
            name="generate_website",
            description="Produce a complete single-file HTML website that matches the reference site's design.",
            input_schema={
                "type": "object",
                "properties": {
                    "site_type": {"type": "string", "description": "The kind of website to build (e.g. 'personal portfolio', 'SaaS landing page')."},
                    "pages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Pages or sections to include (e.g. ['home', 'about', 'contact']).",
                    },
                    "extra_instructions": {"type": "string", "description": "Any extra guidance from the user."},
                },
                "required": ["site_type"],
            },
            handler=_generate,
        ),
    ]

    await bus.publish("agent_start", phase="generate", model=MAIN_AGENT_MODEL)

    async def _on_event(event: str, data: dict) -> None:
        if event == "tool_use":
            await bus.publish("status", message=f"🤖 Main agent → calling `{data.get('name')}`")

    pages_str = ", ".join(request.pages) if request.pages else "single-page"
    user_prompt = (
        f"User's generation request:\n"
        f"- site_type: {request.site_type}\n"
        f"- pages: {pages_str}\n"
        f"- extra_instructions: {request.extra_instructions or '(none)'}\n\n"
        f"Call generate_website with these values."
    )
    final_text = await agent_loop(
        model=MAIN_AGENT_MODEL,
        system=GENERATE_SYSTEM,
        user_prompt=user_prompt,
        tools=tools,
        max_tokens=1024,
        max_turns=4,
        on_event=_on_event,
    )

    if "site" not in captured:
        raise RuntimeError(f"Main agent did not produce a generated site. Final reply: {final_text!r}")

    await bus.publish("agent_end", phase="generate", reply=final_text)
    return captured["site"]
