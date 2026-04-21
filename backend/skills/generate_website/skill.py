"""Skill 3 — Generate Website.

Multimodal Sonnet 4.6 call using the analyzed components + original
HTML/screenshot context. See SKILL.md.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import pathlib
import re

from ...shared.events import EventBus
from ...shared.llm import SUBAGENT_MODEL, _client
from ...shared.schemas import (
    AnalysisResult,
    DownloadResult,
    GeneratedSite,
    GenerateRequest,
)
from ...shared.storage import JobStorage
from ...shared.trace import _depth, get_collector, metric, traced

logger = logging.getLogger(__name__)


GENERATE_PROMPT = """\
You are an expert frontend developer. You will receive:

1. A **design-token table** — colors, fonts, border-radius, spacing extracted \
   from a reference website.
2. A **component library** — deduplicated list of the reference site's \
   buttons, cards, navs, heroes, forms, etc., each with canonical style values.
3. **Reference HTML samples** — truncated snippets so you can see real DOM \
   conventions (class names, structure, composition patterns).
4. **Reference screenshots** — a sequence of viewport-sized slices \
   (≈1440×900 each) captured top-to-bottom from ONE page of the reference \
   site. Stitched together mentally, they show the full page's layout, \
   proportions, whitespace, and aesthetic feel. Use ALL of them (not just \
   the first) to understand section rhythm — hero, feature strips, social \
   proof, footer, etc.
5. A **user brief** — what website to build and what pages/sections to include.

## Your task
Produce ONE complete HTML document that *looks like it belongs to the same \
brand as the reference site*, rebuilt for the user's topic.

## Rules
- Single self-contained HTML file with embedded `<style>`. No CDN links, \
  no external frameworks.
- Use the reference site's **exact design tokens**: colors, fonts, \
  border-radius, spacing. These are the foundation.
- **Recreate the component patterns** from the library — buttons look like \
  the reference's buttons, cards look like the reference's cards, etc. \
  Match padding, border-radius, shadows, font weights.
- Layout proportions should echo the reference (look at the screenshots).
- Make it fully responsive via CSS media queries.
- Realistic placeholder copy for the user's topic — no Lorem ipsum.
- **Images** (critical): for every `<img>` tag, set `src="PLACEHOLDER_IMAGE"` \
  and provide a concrete, evocative `alt="..."` describing the subject \
  (e.g. `alt="barista pouring latte art"`, `alt="minimalist workspace with \
  plants"`). For hero/section backgrounds that need a photo, use \
  `background-image: url(PLACEHOLDER_IMAGE)` inline and put the subject \
  on the element as `data-alt="..."`. A post-processing step swaps every \
  `PLACEHOLDER_IMAGE` for a real Unsplash photo using the alt/data-alt as \
  the search query — so the alt text MUST describe the image contents, \
  not the UI role.
- If the user requested multiple pages, build them as in-page sections with \
  anchor links (`#home`, `#about`, `#contact`) in the nav, unless they \
  explicitly asked for separate files.
- Include basic accessibility: semantic HTML5, alt text on images, aria \
  attributes where appropriate.

## Output
Return ONLY the HTML. Start with `<!DOCTYPE html>`, end with `</html>`. \
No markdown fences, no commentary, no explanations.
"""


def _build_design_system_text(analysis: AnalysisResult) -> str:
    lines: list[str] = []

    tokens = analysis.design_tokens or {}

    # Palette tiers: primary is the dominant brand color — use it for
    # primary CTAs, active states, and any element that should feel "owned"
    # by the brand. Secondary = supporting hues (2nd/3rd buttons, section
    # accents). Accent = sparingly for highlights, tags, illustrations.
    primary = tokens.get("palette_primary") or ""
    secondary = tokens.get("palette_secondary") or []
    accent = tokens.get("palette_accent") or []
    if primary or secondary or accent:
        lines.append("## Color Palette (tiered — respect these tiers)")
        if primary:
            lines.append(f"- **Primary** (dominant brand color): `{primary}`")
        if secondary:
            lines.append(f"- **Secondary** (supporting): {', '.join(f'`{c}`' for c in secondary)}")
        if accent:
            lines.append(f"- **Accent** (use sparingly for highlights): {', '.join(f'`{c}`' for c in accent)}")
        lines.append("")
        lines.append(
            "Use the primary color for hero CTAs, links, and active states. "
            "Secondary colors for section backgrounds, secondary buttons, and "
            "supporting brand moments. Accents only for small highlights "
            "(badges, tags, illustrations) — never as the dominant color."
        )
        lines.append("")

    # Typography: we pulled the *rendered* font-family stacks from the
    # reference site. Instruct the generator to either reuse them verbatim
    # (the generated HTML can link a Google Fonts stylesheet) or pick a
    # visually similar fallback.
    typo = tokens.get("typography") or {}
    if typo:
        lines.append("## Typography (match the reference site)")
        hf = typo.get("heading_family") or ""
        bf = typo.get("body_family") or ""
        btf = typo.get("button_family") or ""
        hw = typo.get("heading_weight") or ""
        bw = typo.get("body_weight") or ""
        hs = typo.get("heading_size") or ""
        bs = typo.get("body_size") or ""
        if hf:
            lines.append(f"- Headings: **{hf}** (weight {hw or 'bold'}, ~{hs}px)")
        if bf:
            lines.append(f"- Body: **{bf}** (weight {bw or '400'}, ~{bs}px)")
        if btf and btf != hf and btf != bf:
            lines.append(f"- Buttons: **{btf}**")
        lines.append("")
        lines.append(
            "Load these via a Google Fonts `<link>` in `<head>` if they're "
            "Google-hosted (Inter, Roboto, Poppins, Montserrat, Playfair "
            "Display, Lora, Merriweather, Nunito, Raleway, Work Sans, DM "
            "Sans, Manrope, Space Grotesk, IBM Plex Sans/Serif, Source Sans "
            "3, Outfit, Figtree, etc.). If the family is a system or "
            "proprietary font (e.g., `-apple-system`, `SF Pro`, custom "
            "brand fonts), pick the closest Google Font substitute that "
            "matches the category (sans/serif/display/mono) and overall "
            "weight feel, then use it consistently."
        )
        lines.append("")

    # Other design tokens (CSS custom properties from the source site, if
    # we scraped any). Filter out the palette/typo keys we already rendered.
    rendered = {
        "palette", "palette_primary", "palette_secondary",
        "palette_accent", "palette_coverage", "typography",
    }
    other = {k: v for k, v in tokens.items() if k not in rendered}
    if other:
        lines.append("## Other Design Tokens")
        for name, values in list(other.items())[:40]:
            if isinstance(values, list):
                lines.append(f"- {name}: {', '.join(str(v) for v in values)}")
            elif isinstance(values, dict):
                lines.append(f"- {name}: " + ", ".join(f"{k}={v}" for k, v in list(values.items())[:8]))
            else:
                lines.append(f"- {name}: {values}")
        lines.append("")

    lines.append("## Component Library")
    for group in analysis.groups:
        lines.append(f"\n### {group.category} ({len(group.components)} variant(s))")
        for c in group.components:
            s = c.styles
            style_str = ", ".join(
                f"{k}: {v}" for k, v in {
                    "bg": s.background_color,
                    "color": s.text_color,
                    "radius": s.border_radius,
                    "padding": s.padding,
                    "font_size": s.font_size,
                    "font_weight": s.font_weight,
                    "border": s.border,
                    "shadow": s.box_shadow,
                }.items() if v
            )
            lines.append(f"- **{c.name}** [{c.type}] ×{c.count} — {c.description}")
            if style_str:
                lines.append(f"  styles: {style_str}")

    if analysis.summary:
        lines.append(f"\n## Summary\n{analysis.summary}")
    return "\n".join(lines)


def _load_html_samples(download: DownloadResult | None, max_pages: int = 2, max_chars: int = 18_000) -> str:
    if not download:
        return ""
    samples = []
    from ...shared.storage import PROJECT_ROOT as _PROJECT_ROOT
    for page in download.pages[:max_pages]:
        abs_path = _PROJECT_ROOT / page.html_path
        if not abs_path.exists():
            continue
        raw = abs_path.read_text(encoding="utf-8", errors="replace")
        truncated = raw[:max_chars]
        if len(raw) > max_chars:
            truncated += "\n<!-- truncated -->"
        samples.append(f"### Reference page: {page.url}\n```html\n{truncated}\n```")
    return "\n\n".join(samples)


def _load_screenshots_b64(
    download: DownloadResult | None,
    max_chunks: int = 8,
) -> list[dict]:
    """Load every viewport chunk of the single downloaded page so the
    generator sees the full top-to-bottom visual of the reference site.

    Before the chunked-capture rewrite this used to load one full-page
    screenshot per page; now we load *all* chunks of the (single) page in
    scroll order. Caps at `max_chunks` to stay within Anthropic's
    per-request image budget — each chunk is ~1440×900 and compressed.
    """
    if not download or not download.pages:
        return []
    from ...shared.storage import PROJECT_ROOT as _PROJECT_ROOT

    out: list[dict] = []
    for page in download.pages:
        chunks = sorted(page.chunks, key=lambda c: c.index)
        for ch in chunks[:max_chunks]:
            abs_path = _PROJECT_ROOT / ch.path
            if not abs_path.exists():
                continue
            raw = abs_path.read_bytes()
            if len(raw) > 1_500_000:
                compressed = _compress(abs_path)
                if compressed:
                    raw = compressed
                else:
                    continue
            mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
            out.append({
                "url": page.url,
                "chunk_index": ch.index,
                "offset_y": ch.offset_y,
                "media_type": mime,
                "data": base64.b64encode(raw).decode("ascii"),
            })
        # We now only analyze a single user-given page, but be defensive:
        # if somehow multiple pages exist, stop once we've hit the cap.
        if len(out) >= max_chunks:
            break
    return out[:max_chunks]


async def _populate_unsplash_images(html: str, bus: EventBus) -> str:
    """Swap every `PLACEHOLDER_IMAGE` (and legacy `placehold.co/…` URLs)
    in the generated HTML for a real Unsplash photo using the element's
    `alt` / `data-alt` as the query.

    Shares the resolver logic with the `replace_images` skill: if
    `UNSPLASH_ACCESS_KEY` is set we use the Unsplash search API, else we
    fall back to LoremFlickr.
    """
    from ..replace_images.skill import _extract_slots, _fallback_url, _resolve_one, _rewrite_snippet

    # Normalize legacy `placehold.co/<WxH>` → PLACEHOLDER_IMAGE so the
    # same pipeline handles both.
    html = re.sub(
        r'https?://(?:www\.)?placehold(?:er)?\.co/[^\s"\'\)]+',
        'PLACEHOLDER_IMAGE', html,
    )

    slots = _extract_slots(html)
    placeholder_slots = [
        s for s in slots
        # Every match we care about — we replace ALL img src & bg urls in
        # the generated doc so the page never ships with a literal
        # `PLACEHOLDER_IMAGE` or a broken relative path.
    ]
    if not placeholder_slots:
        return html

    access_key = os.getenv("UNSPLASH_ACCESS_KEY")
    try:
        import httpx
        http_client = httpx.AsyncClient()
    except Exception:
        http_client = None

    await bus.publish(
        "status",
        message=(
            f"🖼️  Populating {len(placeholder_slots)} image(s) in generated site "
            f"via {'Unsplash' if access_key else 'LoremFlickr'}…"
        ),
    )

    queries = [s["alt"] or "abstract background" for s in placeholder_slots]
    try:
        urls = await asyncio.gather(
            *(
                _resolve_one(q, access_key, http_client, variant=i)
                for i, q in enumerate(queries)
            )
        )
    finally:
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:
                pass

    new_html, _ = _rewrite_snippet(html, urls)
    return new_html


def _compress(path: pathlib.Path) -> bytes | None:
    try:
        from PIL import Image
        import io
        img = Image.open(path)
        if img.width > 1440:
            ratio = 1440 / img.width
            img = img.resize((1440, int(img.height * ratio)), Image.LANCZOS)
        if img.height > 2000:
            img = img.crop((0, 0, img.width, 2000))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=60)
        return buf.getvalue()
    except Exception:
        return None


@traced
async def run_generate_skill(
    request: GenerateRequest,
    analysis: AnalysisResult,
    download: DownloadResult | None,
    storage: JobStorage,
    bus: EventBus,
) -> GeneratedSite:
    await bus.publish(
        "skill_start",
        skill="generate_website",
        message=f"Generating {request.site_type} ({', '.join(request.pages) or 'single-page'})",
    )

    design_system = _build_design_system_text(analysis)
    html_samples = _load_html_samples(download)
    screenshots = _load_screenshots_b64(download)

    text_parts = [f"## Design System\n\n{design_system}"]
    if html_samples:
        text_parts.append(f"## Reference HTML Structure\n\n{html_samples}")
    pages_str = ", ".join(request.pages) if request.pages else "single-page layout"
    text_parts.append(
        "## User Brief\n\n"
        f"- **Website type**: {request.site_type}\n"
        f"- **Pages / sections**: {pages_str}\n"
        f"- **Extra instructions**: {request.extra_instructions or '(none)'}\n"
    )

    content: list[dict] = []
    for ss in screenshots:
        label = (
            f"Reference screenshot — {ss['url']} "
            f"(chunk {ss.get('chunk_index', 0)}, scrollY={ss.get('offset_y', 0)}px)"
        )
        content.append({"type": "text", "text": label})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": ss["media_type"], "data": ss["data"]},
        })
    if screenshots:
        content.append({
            "type": "text",
            "text": (
                f"The {len(screenshots)} screenshots above are sequential "
                f"viewport-sized slices of the same reference page, ordered "
                f"top-to-bottom. Treat them as one continuous design — use "
                f"them together to understand layout, section rhythm, and "
                f"overall aesthetic."
            ),
        })
    content.append({"type": "text", "text": "\n\n".join(text_parts)})

    await bus.publish(
        "status",
        message=f"🖼️  Sending {len(screenshots)} page chunk(s) + design system to Sonnet 4.6…",
    )

    client = _client()
    metric(
        screenshots=len(screenshots),
        design_system_chars=len(design_system),
        html_samples_chars=len(html_samples),
        total_text_chars=sum(len(t) for t in text_parts),
    )
    import time as _time
    col = get_collector()
    call_seq = None
    if col:
        call_seq = col.llm_call(
            _depth.get(), role="subagent", model=SUBAGENT_MODEL,
            system=GENERATE_PROMPT, user_content=content, max_tokens=16384,
        )
    _t0 = _time.perf_counter()
    resp = await client.messages.create(
        model=SUBAGENT_MODEL,
        max_tokens=16384,
        system=GENERATE_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    _dt = (_time.perf_counter() - _t0) * 1000
    html = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if col and call_seq is not None:
        col.llm_response(
            _depth.get(), call_seq=call_seq, text=html,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens, ms=_dt,
        )
    metric(response_chars=len(html), input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
    if html.startswith("```"):
        html = html.split("\n", 1)[1] if "\n" in html else html[3:]
    if html.endswith("```"):
        html = html.rsplit("```", 1)[0]
    html = html.strip()

    # Post-process: replace every `PLACEHOLDER_IMAGE` (and any surviving
    # `placehold.co` URL) with a real Unsplash photo keyed off the
    # element's alt / data-alt. Reuses the replace_images helpers so the
    # resolution logic (Unsplash API if UNSPLASH_ACCESS_KEY set, else
    # LoremFlickr) is identical to the component pipeline.
    html = await _populate_unsplash_images(html, bus)

    saved = storage.save_generated("site.html", html)
    await bus.publish("status", message=f"💾 Saved generated site to {saved} ({len(html):,} chars)")
    await bus.publish("skill_end", skill="generate_website", chars=len(html))

    return GeneratedSite(html=html, pages_generated=request.pages or ["home"])
