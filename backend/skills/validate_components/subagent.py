"""Regeneration subagent for validate_components.

Given the source page screenshot and a list of components whose standalone
render came out blank, returns self-contained replacement HTML snippets with
inline styles (so no parent CSS is required).
"""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import time
from typing import Any

from ...shared.llm import SUBAGENT_MODEL, _client, extract_json
from ...shared.trace import _depth, get_collector, metric, note, traced

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a senior UI engineer. You will receive ONE screenshot of a website \
page and a list of components that we extracted from that page. Our \
standalone rendering of each component's current HTML came out BLANK or \
broken — the snippet depends on parent/stylesheet CSS we don't have.

Your job: for each component, look at the screenshot, locate the component \
visually, and return a REPLACEMENT `html_snippet` that renders the same \
component correctly WITHOUT any external CSS.

## Rules
1. Use **inline `style="..."` attributes** on every element that needs them. \
Do NOT rely on class selectors from an external stylesheet — those won't \
exist when the snippet is rendered in isolation.
2. **FAITHFUL STYLE REPRODUCTION is the priority.** When rendered on a \
blank page the snippet must look like the component in the screenshot:
   - Match the `background-color` exactly. Sample the pixel in the \
     screenshot — don't use generic CSS keywords like `darkgreen` when the \
     real color is `#2e6b5e`.
   - Match text color, font weight, letter-spacing, text-transform.
   - Match border radius, border width + color, padding, box-shadow.
   - Match the visible label/copy verbatim (same wording + case).
3. Keep the snippet self-contained and under 1200 characters. NEVER use \
`...` placeholders. If the real component is too large, pick a smaller \
representative example (one card, one row).
4. Keep the same outer tag/semantics when reasonable (e.g. a `<button>` \
stays a `<button>`; a nav bar stays a `<nav>`).
5. When a known style value was provided for the component, prefer that \
over guessing, unless the screenshot clearly contradicts it.
6. If you genuinely cannot locate the component in the screenshot (e.g. it's \
a hidden panel that isn't visible), set `"unrecoverable": true` for that \
component and leave `html_snippet` empty.

## Return format
Return ONLY valid JSON. No prose, no code fences. Schema:

{
  "components": [
    {
      "id": "<the id you were given>",
      "html_snippet": "<self-contained HTML with inline styles, <= 1200 chars>",
      "unrecoverable": false
    }
  ]
}
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
            if img.height > 2400:
                img = img.crop((0, 0, img.width, 2400))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=60)
            raw = buf.getvalue()
        except Exception:
            return None
    mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return {"media_type": mime, "data": base64.b64encode(raw).decode("ascii")}


def _format_component_brief(c: dict[str, Any]) -> str:
    st = c.get("styles") or {}
    style_hint = ", ".join(
        f"{k}: {v}" for k, v in st.items()
        if k in ("background_color", "text_color", "border_radius", "padding",
                 "font_size", "font_weight", "border", "box_shadow") and v
    )
    return (
        f"- id: {c['id']}\n"
        f"  name: {c.get('name', '')}\n"
        f"  type: {c.get('type', '')}\n"
        f"  description: {c.get('description', '')}\n"
        f"  current_snippet (broken):\n    {c.get('html_snippet', '')[:400]}\n"
        f"  known_styles: {style_hint or '(none)'}"
    )


@traced
async def regenerate_from_screenshot(
    screenshot_abs_path: str,
    page_url: str,
    components: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """One multimodal call per page. Returns {component_id: {html_snippet, unrecoverable}}."""

    ss = _load_screenshot_b64(pathlib.Path(screenshot_abs_path))
    if not ss:
        logger.warning("Screenshot not loadable: %s", screenshot_abs_path)
        return {}

    brief = "\n\n".join(_format_component_brief(c) for c in components)
    user_text = (
        f"Page URL: {page_url}\n\n"
        f"Components to regenerate ({len(components)}):\n\n{brief}\n\n"
        f"For each id above, return a self-contained `html_snippet` with \n"
        f"inline styles that matches what you see in the screenshot."
    )
    content = [
        {"type": "text", "text": f"Page screenshot — {page_url}"},
        {"type": "image", "source": {"type": "base64", **ss}},
        {"type": "text", "text": user_text},
    ]

    metric(
        page_url=page_url,
        bad_components=len(components),
        user_text_chars=len(user_text),
    )

    client = _client()
    col = get_collector()
    call_seq = None
    if col:
        call_seq = col.llm_call(
            _depth.get(), role="subagent", model=SUBAGENT_MODEL,
            system=SYSTEM_PROMPT, user_content=content, max_tokens=6_000,
        )
    t0 = time.perf_counter()
    try:
        resp = await client.messages.create(
            model=SUBAGENT_MODEL,
            max_tokens=6_000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.error("regenerate_from_screenshot API call failed: %s", exc)
        metric(failed=True, error=f"api_error: {str(exc)[:120]}")
        return {}

    dt = (time.perf_counter() - t0) * 1000
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if col and call_seq is not None:
        col.llm_response(
            _depth.get(), call_seq=call_seq, text=raw,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens, ms=dt,
        )
    if resp.stop_reason == "max_tokens":
        note("⚠️  validate_components hit max_tokens — some regenerations may be truncated")

    try:
        data = json.loads(extract_json(raw))
    except Exception as exc:
        logger.error("regenerate_from_screenshot JSON parse failed: %s; raw[:300]=%s",
                     exc, raw[:300])
        metric(failed=True, error=f"json_parse: {str(exc)[:120]}")
        return {}

    out: dict[str, dict[str, Any]] = {}
    for entry in data.get("components", []):
        cid = entry.get("id")
        if not cid:
            continue
        out[cid] = {
            "html_snippet": str(entry.get("html_snippet") or ""),
            "unrecoverable": bool(entry.get("unrecoverable")),
        }
    metric(regenerated=sum(1 for v in out.values() if v["html_snippet"] and not v["unrecoverable"]),
           unrecoverable=sum(1 for v in out.values() if v["unrecoverable"]),
           returned=len(out))
    return out


CROP_SYSTEM_PROMPT = """\
You are a senior UI engineer. You will receive ONE tightly-cropped \
screenshot of a single UI component. Return a self-contained HTML snippet \
with inline `style="..."` attributes that, when rendered on a blank page, \
reproduces the component FAITHFULLY.

## Fidelity rules (highest priority)
- Match the `background-color` exactly by sampling the pixel in the crop. \
  Don't use generic CSS keywords when the real color is a specific hex.
- Match text color, font weight, letter-spacing, text-transform, \
  border radius, border width + color, padding, and shadow.
- Match the label text verbatim (same wording, same case).
- Preserve the component's visible structure (e.g. icon + text layout).

## Output rules
- Inline styles only — no external classes, no placeholders (`...`).
- Keep under 1200 characters; pick one representative example if needed.
- Same outer tag/semantics when reasonable (`<button>` stays `<button>`).

Return ONLY JSON (no prose, no fences):

{"html_snippet": "<self-contained HTML with inline styles>",
 "unrecoverable": false}

Set `unrecoverable: true` only if the crop is clearly blank/broken and you \
cannot identify the component.
"""


@traced
async def regenerate_from_crop(
    crop_abs_path: str,
    component: dict[str, Any],
) -> dict[str, Any]:
    """Regenerate one component using its tight crop image.

    Much higher fidelity than page-level regeneration because the crop is
    exactly the component — no searching, no ambiguous neighbours.
    Returns {html_snippet, unrecoverable}.
    """
    ss = _load_screenshot_b64(pathlib.Path(crop_abs_path))
    if not ss:
        logger.warning("Crop not loadable: %s", crop_abs_path)
        return {"html_snippet": "", "unrecoverable": True}

    st = component.get("styles") or {}
    style_hint = ", ".join(
        f"{k}: {v}" for k, v in st.items()
        if k in ("background_color", "text_color", "border_radius", "padding",
                 "font_size", "font_weight", "border", "box_shadow") and v
    )
    user_text = (
        f"Component: {component.get('name', '')} "
        f"(type: {component.get('type', '')}).\n"
        f"Description: {component.get('description', '')}\n"
        f"Known styles: {style_hint or '(none)'}\n\n"
        f"Reproduce this component FAITHFULLY with a self-contained "
        f"inline-styled HTML snippet. Match the background color, text, "
        f"and layout exactly as shown in the crop."
    )
    content = [
        {"type": "text", "text": "Component crop:"},
        {"type": "image", "source": {"type": "base64", **ss}},
        {"type": "text", "text": user_text},
    ]

    client = _client()
    col = get_collector()
    call_seq = None
    if col:
        call_seq = col.llm_call(
            _depth.get(), role="subagent", model=SUBAGENT_MODEL,
            system=CROP_SYSTEM_PROMPT, user_content=content, max_tokens=2000,
        )
    t0 = time.perf_counter()
    try:
        resp = await client.messages.create(
            model=SUBAGENT_MODEL,
            max_tokens=2000,
            system=CROP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.error("regenerate_from_crop API call failed: %s", exc)
        metric(failed=True, error=f"api_error: {str(exc)[:120]}")
        return {"html_snippet": "", "unrecoverable": True}

    dt = (time.perf_counter() - t0) * 1000
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if col and call_seq is not None:
        col.llm_response(
            _depth.get(), call_seq=call_seq, text=raw,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens, ms=dt,
        )

    try:
        data = json.loads(extract_json(raw))
    except Exception as exc:
        logger.error("regenerate_from_crop JSON parse failed: %s; raw[:300]=%s",
                     exc, raw[:300])
        return {"html_snippet": "", "unrecoverable": True}

    return {
        "html_snippet": str(data.get("html_snippet") or "")[:2000],
        "unrecoverable": bool(data.get("unrecoverable")),
    }
