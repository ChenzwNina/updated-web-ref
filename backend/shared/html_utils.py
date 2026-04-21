"""HTML cleaning/compaction helpers shared by the analyze skill."""
from __future__ import annotations

import hashlib
import re

_TRACKING = re.compile(
    r'(pixel\.gif|bat\.bing|facebook\.net|googletagmanager|google-analytics|'
    r'hotjar|doubleclick|adform|licdn)',
    re.IGNORECASE,
)


def clean_html(html: str, max_chars: int = 60_000) -> str:
    h = html
    h = re.sub(r"<script[\s>].*?</script>", "", h, flags=re.DOTALL | re.IGNORECASE)
    h = re.sub(r"<noscript[\s>].*?</noscript>", "", h, flags=re.DOTALL | re.IGNORECASE)
    h = re.sub(r"<iframe[\s>].*?</iframe>", "", h, flags=re.DOTALL | re.IGNORECASE)
    h = re.sub(r"<iframe\b[^>]*/?>", "", h, flags=re.IGNORECASE)
    # Strip inline <style> blocks -- they can be enormous (CSS frameworks) and
    # the LLM gets computed styles separately. Keep <link rel="stylesheet"> refs.
    h = re.sub(r"<style[\s>].*?</style>", "", h, flags=re.DOTALL | re.IGNORECASE)

    def _img(m: re.Match) -> str:
        tag = m.group(0)
        if _TRACKING.search(tag):
            return ""
        if re.search(r'(?:width|height)\s*=\s*"[01]"', tag):
            return ""
        return re.sub(r'src="data:[^"]*"', 'src="[data-uri]"', tag)

    h = re.sub(r"<img\b[^>]*/?>", _img, h, flags=re.IGNORECASE)

    def _svg(m: re.Match) -> str:
        cls_m = re.search(r'class="([^"]*)"', m.group(0))
        cls = cls_m.group(1) if cls_m else ""
        return f'<svg class="{cls}"/>'

    h = re.sub(r"<svg\b[^>]*>.*?</svg>", _svg, h, flags=re.DOTALL | re.IGNORECASE)
    h = re.sub(r'url\(data:[^)]{100,}\)', 'url([data-uri])', h)
    h = re.sub(r'src="data:[^"]{100,}"', 'src="[data-uri]"', h)
    h = re.sub(r"\s{2,}", " ", h)
    h = re.sub(r">\s+<", ">\n<", h)
    h = _dedupe(h)
    if len(h) > max_chars:
        h = h[:max_chars] + "\n<!-- truncated -->"
    return h.strip()


def _dedupe(html: str) -> str:
    lines = html.split("\n")
    if len(lines) < 10:
        return html
    seen: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        if len(line) > 100:
            k = hashlib.md5(line.encode(), usedforsecurity=False).hexdigest()
            seen.setdefault(k, []).append(i)
    remove: set[int] = set()
    annotate: dict[int, int] = {}
    for indices in seen.values():
        if len(indices) > 1:
            annotate[indices[0]] = len(indices)
            for i in indices[1:]:
                remove.add(i)
    if not remove:
        return html
    out: list[str] = []
    for i, line in enumerate(lines):
        if i in remove:
            continue
        if i in annotate:
            out.append(f"<!-- repeated {annotate[i]}x -->")
        out.append(line)
    return "\n".join(out)
