"""Image-keyword subagent.

Looks at a single component crop and returns Unsplash-style search
keywords describing what image(s) should populate the component's image
slots. Multimodal — sees the crop, not just the name.
"""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import time
from typing import Any

from ...shared.llm import SUBAGENT_MODEL, _client, extract_json
from ...shared.trace import _depth, get_collector, metric, traced

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a visual researcher picking replacement images for UI components.

You will receive ONE tightly-cropped screenshot of a component that \
contains N image slots. Your job: for each slot, produce a short, \
evocative Unsplash-style search query (2–5 words) describing what the \
image in that slot shows.

## Rules
- Describe the SUBJECT and MOOD visible in the crop, not the layout. \
  Prefer concrete nouns plus one adjective. Good: "pink peony bouquet", \
  "couple walking beach sunset", "minimal home office desk". \
  Bad: "hero image", "card picture", "left side photo".
- If the component has multiple distinct image slots (e.g. a gallery or a \
  multi-card feature grid), return one query per slot in visual order \
  (top-left → bottom-right).
- If the original image looks generic (stock placeholder, solid color \
  fill), produce a query that matches the *intent* of the component name \
  & description provided.
- Prefer singular/plural that matches what's visible (one bouquet vs a \
  field of flowers).

## Return format
Return ONLY valid JSON. No prose, no code fences:

{"queries": ["keyword phrase 1", "keyword phrase 2", ...]}

The number of queries should equal the number of image slots you can see. \
If you see zero real images (just text), return `{"queries": []}`.
"""


def _load_image_b64(abs_path: pathlib.Path, max_bytes: int = 1_500_000) -> dict | None:
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
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=75)
            raw = buf.getvalue()
        except Exception:
            return None
    mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return {"media_type": mime, "data": base64.b64encode(raw).decode("ascii")}


@traced
async def suggest_image_keywords(
    crop_abs_path: str | None,
    component_info: dict[str, Any],
    slot_count: int,
) -> list[str]:
    """Return `slot_count` keyword phrases. Multimodal if a crop is given,
    text-only fallback otherwise."""

    content: list[dict] = []
    if crop_abs_path:
        ss = _load_image_b64(pathlib.Path(crop_abs_path))
        if ss:
            content.append({"type": "text", "text": "Component crop:"})
            content.append({"type": "image", "source": {"type": "base64", **ss}})

    user_text = (
        f"Component: {component_info.get('name', '')} "
        f"(type: {component_info.get('type', '')}).\n"
        f"Description: {component_info.get('description', '')}\n"
        f"Image slots in this component: {slot_count}\n\n"
        f"Return exactly {slot_count} search queries, one per slot in "
        f"visual order."
    )
    content.append({"type": "text", "text": user_text})

    client = _client()
    col = get_collector()
    call_seq = None
    if col:
        call_seq = col.llm_call(
            _depth.get(), role="subagent", model=SUBAGENT_MODEL,
            system=SYSTEM_PROMPT, user_content=content, max_tokens=400,
        )
    t0 = time.perf_counter()
    try:
        resp = await client.messages.create(
            model=SUBAGENT_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.error("suggest_image_keywords API call failed: %s", exc)
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

    try:
        data = json.loads(extract_json(raw))
    except Exception as exc:
        logger.error("suggest_image_keywords JSON parse failed: %s; raw=%s", exc, raw[:300])
        return []

    queries = data.get("queries") or []
    cleaned = [str(q).strip() for q in queries if str(q).strip()]
    # Pad / truncate to slot_count so the caller always has enough.
    if len(cleaned) < slot_count and cleaned:
        cleaned = cleaned + [cleaned[-1]] * (slot_count - len(cleaned))
    return cleaned[:slot_count]
