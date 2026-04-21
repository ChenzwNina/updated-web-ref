"""Pick-subpages subagent (Sonnet 4.6).

Given a list of candidate nav links from the root page, choose up to N
subpages that maximise design coverage.
"""
from __future__ import annotations

import json
import logging

from ...shared.llm import extract_json, subagent_call
from ...shared.schemas import NavLink
from ...shared.trace import traced

logger = logging.getLogger(__name__)


PICK_SUBPAGES_PROMPT = """\
You are a web design researcher. You will receive a list of candidate \
navigation links from a website's root page. Your task is to pick up to \
THREE subpages that together maximise *design coverage* — i.e. show the \
widest variety of components and layouts.

Prefer pages that are likely to differ visually from the homepage. Good \
picks include: pricing, features/product, about, blog/articles, \
contact, docs, dashboard, gallery, case studies.

AVOID:
- Multiple pages of the same type (don't pick 3 blog posts).
- Login / sign-up / cart / search / legal / terms / privacy.
- Anchors within the current page (#fragments).
- Duplicate or near-duplicate URLs.

Return ONLY valid JSON (no prose, no markdown fences):

{
  "picks": [
    {"label": "<nav label>", "href": "<absolute URL>", "reason": "<one short sentence>"},
    ...
  ]
}

Return 0–3 picks (usually exactly 3). If fewer than 3 distinct page types \
are available, return fewer.\
"""


@traced
async def pick_subpages(root_url: str, candidates: list[NavLink], k: int = 3) -> list[NavLink]:
    if not candidates:
        return []
    if len(candidates) <= k:
        return candidates

    cand_text = "\n".join(f"- [{c.label}]({c.href})" for c in candidates)
    user = (
        f"Root URL: {root_url}\n\n"
        f"Candidate navigation links:\n{cand_text}\n\n"
        f"Pick up to {k} subpages that maximise design coverage."
    )
    try:
        raw = await subagent_call(
            system=PICK_SUBPAGES_PROMPT,
            user_content=user,
            max_tokens=1024,
        )
        data = json.loads(extract_json(raw))
        picks_raw = data.get("picks", [])
    except Exception as exc:
        logger.warning("pick_subpages subagent failed: %s — falling back to first %d", exc, k)
        return candidates[:k]

    picked: list[NavLink] = []
    by_href = {c.href: c for c in candidates}
    for p in picks_raw[:k]:
        href = p.get("href")
        if href and href in by_href:
            picked.append(by_href[href])
    if not picked:
        return candidates[:k]
    return picked
