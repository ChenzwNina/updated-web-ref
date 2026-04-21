"""Screenshot-only component extractor (Sonnet 4.6, multimodal).

One call per viewport *chunk*: receives a ~1440×900 slice of the page and
returns a JSON inventory of the distinct visually-reusable UI components
visible in that chunk, each with a self-contained inline-styled HTML
snippet and a chunk-local bbox.

Chunk-local bboxes mean crops are accurate: the model's coordinates live
in a small image, not a 10000px-tall full-page screenshot where small
errors blow up.
"""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import time
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
You are a senior design-system engineer. You will look at ONE viewport-sized \
screenshot (about 1440×900) that is a slice of a webpage at a known scroll \
position, and produce a structured inventory of every distinct, \
visually-reusable UI component you can SEE in that slice.

You will not be given any HTML or CSS — the screenshot is the ground truth.

## Taxonomy (use these exact category + subtype strings)
{_taxonomy_block()}

## Your task
1. Scan the screenshot. Identify each *distinct visual variant* of a UI \
   component (primary vs. secondary button, feature card vs. testimonial \
   card, dark nav vs. light footer, etc.).
2. **Only include components that are visibly rendered in this slice.** \
   Skip anything that is clearly hidden, cut off so severely you can't tell \
   what it is, or merely a background strip with no content.
3. For each component, return:
   - `category` + `subtype` from the taxonomy above.
   - `id`: a short stable slug (e.g. `primary-button-dark-green`).
   - `name`: a **style-descriptive** label that encodes the *visual look*, \
     NOT a generic type name. Include the dominant color, fill style \
     (solid/outline/ghost), and shape/size if relevant. Good examples: \
     "Dark Green Primary Button", "White Outline Button", "Black Pill \
     CTA", "Peach Feature Card", "Black Dismiss Snackbar". \
     **Bad** examples that you MUST avoid: "Button", "Card", "Snackbar", \
     "Cookie Consent Snackbar" — these are generic types, not styles. \
     If you use the same name for two components, they will be treated as \
     duplicates — so use different names when they look visibly different.
   - `description`: 1–2 sentences describing function + look.
   - `bbox`: approximate `[x, y, width, height]` in pixels, **relative to \
     the top-left of THIS screenshot slice** (not the full page). Be tight \
     around the component — exclude surrounding whitespace. If the \
     component is cut off at the top or bottom edge of this slice, still \
     use the edge as the bbox boundary (we will stitch it with the \
     neighbouring chunk later); set the relevant corner to 0 or \
     chunk-height accordingly.
   - `html_snippet`: **self-contained HTML with inline `style="..."` \
     attributes only**. NO external classes, NO `...` placeholders, NO \
     reliance on parent CSS. The snippet MUST be a FAITHFUL reproduction \
     of what is in the screenshot:
     * Match the background color exactly — sample the pixel, don't \
       paraphrase (if you see `#2e6b5e`, don't write `darkgreen`).
     * Match text color, font weight, letter-spacing/text-transform, \
       border radius, border width + color, padding, and any shadow.
     * Match the label text verbatim (same wording, same case).
     * If you are unsure of an exact hex, pick the closest visible \
       approximation rather than a generic CSS name.
     Keep under 1200 characters; for large components pick one \
     representative example (one card, one nav link).
   - `styles`: the inferred key CSS values (`background_color` as a hex \
     whenever possible, `text_color`, `border_radius`, `padding`, \
     `font_size`, `font_weight`, `font_family`, `border`, `box_shadow`, \
     and an `extra` dict for notable props like `text_transform`, \
     `letter_spacing`).
   - `count`: how many instances of this exact variant you can see in this \
     slice (merge visually-identical repeats).
4. Skip pure structural wrappers (empty dividers, spacers, background \
   strokes) — those aren't components.
5. **Image handling — CRITICAL.** Whenever the component visibly contains \
   a photograph or illustration (anything in the Media category: image, \
   gallery, carousel, hero; OR a card that shows a photo; OR a hero/split \
   section with a background photo), the snippet MUST include a real \
   `<img src="PLACEHOLDER_IMAGE" alt="<short description of what the \
   image depicts>" style="...">` tag for each image slot. Use the literal \
   string `PLACEHOLDER_IMAGE` as the src — a later step swaps it for a \
   real Unsplash photo using your `alt` text as the search query, so the \
   `alt` MUST concretely describe the subject (e.g. `alt="pink peony \
   bouquet"`, not `alt="card image"`). For galleries/grids, emit one \
   `<img>` per visible image slot. For hero/card backgrounds, use \
   `background-image: url(PLACEHOLDER_IMAGE)` inline and still include an \
   `alt`-equivalent via a `data-alt="..."` attribute on the element so \
   the replacer knows what to search for. Never leave a visible image \
   region empty or filled with a solid color block.

## Return format
Return ONLY valid JSON (no prose, no code fences). Schema:

{{
  "components": [
    {{
      "category": "<taxonomy category>",
      "subtype": "<taxonomy subtype>",
      "id": "<short slug>",
      "name": "<label>",
      "description": "<1-2 sentences>",
      "bbox": [x, y, width, height],
      "html_snippet": "<self-contained inline-styled HTML, <= 1200 chars>",
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
"""


def _load_screenshot_b64(abs_path: pathlib.Path, max_bytes: int = 2_000_000) -> dict | None:
    if not abs_path.exists():
        return None
    raw = abs_path.read_bytes()
    if len(raw) > max_bytes:
        try:
            from PIL import Image
            import io
            img = Image.open(abs_path)
            if img.width > 1440:
                ratio = 1440 / img.width
                img = img.resize((1440, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            raw = buf.getvalue()
        except Exception:
            return None
    mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return {"media_type": mime, "data": base64.b64encode(raw).decode("ascii")}


@traced
async def extract_components_from_chunk(
    chunk_abs_path: str,
    page_url: str,
    chunk_index: int,
    offset_y: int,
) -> list[dict[str, Any]]:
    """One multimodal Sonnet call per viewport chunk.

    Returns raw component dicts. bboxes are chunk-local (NOT full-page).
    Each dict is tagged with `source_url`, `chunk_index`, and `offset_y`.
    """

    ss = _load_screenshot_b64(pathlib.Path(chunk_abs_path))
    if not ss:
        logger.warning("Chunk screenshot unreadable: %s", chunk_abs_path)
        return []

    content = [
        {"type": "text", "text": (
            f"Viewport chunk #{chunk_index} of {page_url} "
            f"(captured at scrollY={offset_y}px). Bboxes must be "
            f"relative to this image's top-left corner."
        )},
        {"type": "image", "source": {"type": "base64", **ss}},
        {"type": "text", "text": (
            "Identify every distinct, visually-reusable UI component in this "
            "viewport chunk. For each, return a self-contained inline-styled "
            "HTML snippet that reproduces it when rendered on a blank page, "
            "and a tight chunk-local bbox."
        )},
    ]

    MAX_TOKENS = 12_000

    client = _client()
    col = get_collector()
    call_seq = None
    if col:
        call_seq = col.llm_call(
            _depth.get(), role="subagent", model=SUBAGENT_MODEL,
            system=SYSTEM_PROMPT, user_content=content, max_tokens=MAX_TOKENS,
        )
    t0 = time.perf_counter()
    try:
        resp = await client.messages.create(
            model=SUBAGENT_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.error("analyze_screenshots API call failed: %s", exc)
        metric(failed=True, error=f"api_error: {str(exc)[:120]}")
        return []

    dt = (time.perf_counter() - t0) * 1000
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if col and call_seq is not None:
        col.llm_response(
            _depth.get(), call_seq=call_seq, text=raw,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens, ms=dt,
        )
    metric(
        chunk_index=chunk_index,
        stop_reason=resp.stop_reason,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        response_chars=len(raw),
    )
    if resp.stop_reason == "max_tokens":
        note(f"⚠️  chunk {chunk_index} hit max_tokens — may be truncated")

    if not raw:
        logger.error("Empty response (stop_reason=%s)", resp.stop_reason)
        metric(failed=True, error="empty_response")
        return []

    try:
        data = json.loads(extract_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("JSON parse failed: %s; raw[:500]=%s", exc, raw[:500])
        metric(failed=True, error=f"json_parse: {str(exc)[:120]}")
        return []

    comps = data.get("components", [])
    for c in comps:
        c.setdefault("source_url", page_url)
        c["chunk_index"] = chunk_index
        c["offset_y"] = offset_y
    metric(components_returned=len(comps))
    return comps
