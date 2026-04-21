"""Single multimodal component-extraction subagent (Sonnet 4.6).

Collapses the old per-category fan-out into ONE call that receives:
- cleaned HTML + computed styles per page (once, not N× duplicated)
- page screenshots (for layout / visual variant detection)

Returns the full taxonomy of components in a single JSON payload.
"""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import re
from typing import Any

from ...shared.llm import SUBAGENT_MODEL, _client, extract_json
from ...shared.schemas import COMPONENT_TAXONOMY
from ...shared.trace import _depth, get_collector, metric, note, traced

logger = logging.getLogger(__name__)


def _taxonomy_block() -> str:
    lines = []
    for group, subs in COMPONENT_TAXONOMY.items():
        lines.append(f"- **{group}**: {', '.join(subs)}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""\
You are a senior design-system engineer. You will analyze one website across \
multiple pages using BOTH the rendered screenshots and the source HTML + \
computed styles, and produce a structured inventory of all UI components \
grouped by category.

## Taxonomy (use these exact category + subtype strings)
{_taxonomy_block()}

## Your task
1. Look at the **screenshots** first — they are the visual ground truth. \
Identify every distinct component *variant* you can SEE rendered on the page.
2. **Only catalog components that are actually VISIBLE in the screenshots.** \
Skip elements that exist in the HTML but are hidden in the page's default \
state: closed dropdown menus, un-triggered modals/dialogs, tooltips that \
aren't hovered, off-screen mobile drawers, `display:none`/`visibility:hidden` \
blocks, Webflow `w-dropdown-list` panels, collapsed accordions, etc. The \
trigger (the button that opens them) IS a visible component and should be \
cataloged — the hidden panel itself is not.
3. Cross-reference with the **HTML + computed styles** to pull real CSS \
values (colors, radii, paddings, shadows, font metrics) for each VISIBLE \
component.
4. Merge visually-identical variants that repeat across pages — return each \
distinct variant ONCE with an accurate `count` of total occurrences.
5. Be thorough — a typical marketing site has 15-40 distinct VISIBLE \
components. Don't collapse different visual variants (primary vs secondary \
button, feature card vs testimonial card) into one entry.
6. For `html_snippet`, follow these rules STRICTLY:
   a. The snippet MUST be valid, self-contained HTML — NEVER use `...` or \
      any other placeholder to abbreviate truncated children. If the full \
      markup won't fit in the budget, pick a SMALLER representative example \
      (e.g. one card instead of three, one nav link instead of the full menu) \
      rather than truncating the middle.
   b. Include enough markup that the component's visual identity is obvious \
      when rendered in isolation — logo src, button label text, card heading \
      + body, etc.
   c. SKIP purely structural wrappers whose appearance depends on parent/ \
      sibling CSS context — e.g. empty `<div class="divider">`, background \
      lines, spacer divs, empty flex containers. These are not UI components.
   d. SKIP single labels or text fragments that belong to a larger component \
      (e.g. a standalone "SOLUTIONS" dropdown-toggle label is part of the \
      navigation bar — catalog the nav bar, not the label on its own).

## Sourcing styles
The "Computed Styles" blocks contain real CSS values from the browser. \
PREFER those over guessing from class names. Only fall back to inline/stylesheet \
values when computed styles are missing.

## Return format
Return ONLY valid JSON (no prose, no fences). Schema:

{{
  "components": [
    {{
      "category": "<one of: {', '.join(COMPONENT_TAXONOMY.keys())}>",
      "subtype": "<specific subtype from taxonomy, or a close variant>",
      "id": "<short stable slug, e.g. primary-button-indigo>",
      "name": "<human-readable label>",
      "description": "<1-2 sentences: appearance, purpose, where used>",
      "html_snippet": "<self-contained valid HTML, <= 800 chars, NO '...' placeholders>",
      "source_url": "<page URL where first seen>",
      "styles": {{
        "background_color": "...", "text_color": "...",
        "border_radius": "...", "padding": "...",
        "font_size": "...", "font_weight": "...", "font_family": "...",
        "border": "...", "box_shadow": "...",
        "extra": {{"notable_prop": "value"}}
      }},
      "count": <int>
    }}
  ]
}}

If a category is not present on this site, just omit it from the components \
list. Aim for completeness over brevity — 20+ components is normal.\
"""


def _load_screenshot_b64(abs_path: pathlib.Path, max_bytes: int = 1_500_000) -> dict | None:
    if not abs_path.exists():
        return None
    raw = abs_path.read_bytes()
    if len(raw) > max_bytes:
        try:
            from PIL import Image
            import io
            img = Image.open(abs_path)
            if img.width > 1280:
                ratio = 1280 / img.width
                img = img.resize((1280, int(img.height * ratio)), Image.LANCZOS)
            if img.height > 1800:
                img = img.crop((0, 0, img.width, 1800))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=55)
            raw = buf.getvalue()
        except Exception:
            return None
    mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return {"media_type": mime, "data": base64.b64encode(raw).decode("ascii")}


@traced
async def extract_all_components(
    per_page_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Single call. Each payload = {url, html, styles, screenshot_path (optional)}."""

    content: list[dict] = []
    # Screenshots first (Claude sees them as visual context for the text below)
    ss_count = 0
    for p in per_page_payloads:
        ss_path = p.get("screenshot_abs_path")
        if ss_path:
            ss = _load_screenshot_b64(pathlib.Path(ss_path))
            if ss:
                content.append({"type": "text", "text": f"Screenshot — {p['url']}"})
                content.append({
                    "type": "image",
                    "source": {"type": "base64", **ss},
                })
                ss_count += 1

    text_sections = []
    for p in per_page_payloads:
        text_sections.append(
            f"### Page: {p['url']}\n\n"
            f"#### HTML (cleaned, truncated)\n```html\n{p['html']}\n```\n\n"
            f"#### Computed Styles\n```\n{p['styles']}\n```"
        )
    content.append({"type": "text", "text": "\n\n---\n\n".join(text_sections)})

    total_text = sum(len(c.get("text", "")) for c in content if c.get("type") == "text")
    metric(
        pages=len(per_page_payloads),
        screenshots_sent=ss_count,
        total_text_chars=total_text,
        est_input_tokens=total_text // 4 + ss_count * 1500,
    )

    MAX_TOKENS = 16_000

    import time as _time
    client = _client()
    col = get_collector()
    call_seq = None
    if col:
        call_seq = col.llm_call(
            _depth.get(), role="subagent", model=SUBAGENT_MODEL,
            system=SYSTEM_PROMPT, user_content=content, max_tokens=MAX_TOKENS,
        )
    _t0 = _time.perf_counter()
    try:
        resp = await client.messages.create(
            model=SUBAGENT_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.error("LLM API call failed: %s", exc)
        metric(failed=True, error=f"api_error: {str(exc)[:120]}")
        return []

    _dt = (_time.perf_counter() - _t0) * 1000
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if col and call_seq is not None:
        col.llm_response(
            _depth.get(), call_seq=call_seq, text=raw,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens, ms=_dt,
        )
    metric(
        stop_reason=resp.stop_reason,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        response_chars=len(raw),
    )
    if resp.stop_reason == "max_tokens":
        note("⚠️  hit max_tokens — response truncated, attempting JSON repair")

    if not raw:
        logger.error("LLM returned empty response (stop_reason=%s)", resp.stop_reason)
        metric(failed=True, error="empty_response")
        return []

    try:
        extracted = extract_json(raw)
        data = json.loads(extracted)
        comps = data.get("components", [])
        if not comps:
            logger.warning(
                "LLM returned valid JSON but 0 components. Keys: %s. "
                "Response length: %d chars",
                list(data.keys()), len(raw),
            )
        metric(components_returned=len(comps))
        return comps
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(
            "Failed to parse LLM response as JSON: %s. "
            "Response (first 500 chars): %s",
            exc, raw[:500],
        )
        metric(failed=True, error=f"json_parse: {str(exc)[:120]}")
        return []
