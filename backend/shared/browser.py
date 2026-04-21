"""Shared Playwright browser manager.

One BrowserManager per job. Opens a headless Chromium, exposes helpers for
navigation, screenshotting, nav-link discovery, and computed-style extraction.
Adapted from the prior web_style_ref project.
"""
from __future__ import annotations

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .schemas import NavLink
from .trace import metric, traced


_TYPOGRAPHY_JS = """
() => {
    const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width < 10 || r.height < 10) return false;
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') return false;
        if (parseFloat(cs.opacity) < 0.1) return false;
        return true;
    };
    // First font listed in a computed font-family stack — that's what
    // actually renders if present. Strips quotes.
    const firstFont = (stack) => {
        if (!stack) return '';
        const first = stack.split(',')[0].trim();
        return first.replace(/^["']|["']$/g, '');
    };
    const tally = (selector, limit = 40) => {
        const fonts = {};
        const weights = {};
        const sizes = [];
        let n = 0;
        for (const el of document.querySelectorAll(selector)) {
            if (n >= limit) break;
            if (!isVisible(el)) continue;
            const text = (el.textContent || '').trim();
            if (text.length < 2) continue;
            const cs = getComputedStyle(el);
            const f = firstFont(cs.fontFamily);
            if (!f) continue;
            fonts[f] = (fonts[f] || 0) + 1;
            const w = cs.fontWeight || '400';
            weights[w] = (weights[w] || 0) + 1;
            const s = parseFloat(cs.fontSize);
            if (!isNaN(s)) sizes.push(s);
            n++;
        }
        const top = (obj) => {
            const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]);
            return entries.length ? entries[0][0] : '';
        };
        return {
            family: top(fonts),
            weight: top(weights),
            // median font size for the role
            size: sizes.length
                ? Math.round(sizes.sort((a, b) => a - b)[Math.floor(sizes.length / 2)])
                : 0,
        };
    };
    const heading = tally('h1, h2, h3');
    const body = tally('p, li, article, main, section > div');
    const button = tally('button, a.btn, [role="button"], [class*="btn"], [class*="button"]');
    const bodyEl = document.body;
    const rootFamily = bodyEl
        ? firstFont(getComputedStyle(bodyEl).fontFamily)
        : '';
    const rootSize = bodyEl
        ? Math.round(parseFloat(getComputedStyle(bodyEl).fontSize) || 16)
        : 16;
    return {
        heading_family: heading.family,
        heading_weight: heading.weight,
        heading_size: heading.size,
        body_family: body.family || rootFamily,
        body_weight: body.weight,
        body_size: body.size || rootSize,
        button_family: button.family,
        button_weight: button.weight,
        base_family: rootFamily,
        base_size: rootSize,
    };
}
"""


async def _extract_typography(page: Page) -> dict:
    """Sample visible headings/body/buttons, return dominant font family + weight per role."""
    try:
        return await page.evaluate(_TYPOGRAPHY_JS) or {}
    except Exception:
        return {}


def _analyze_render_pixels(png_bytes: bytes) -> dict:
    """Pixel-level visual inspection of a rendered snippet.

    Returns:
        unique_colors: number of distinct RGB values (≤ 3 ≈ blank)
        stddev:        population std-dev of luma across pixels
        non_bg_ratio:  fraction of pixels that differ from the dominant bg
        edge_density:  fraction of pixels that are part of a luma edge
    """
    try:
        from PIL import Image, ImageFilter, ImageStat
        import io
    except Exception:
        return {"unique_colors": 0, "stddev": 0, "non_bg_ratio": 0.0, "edge_density": 0.0}

    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception:
        return {"unique_colors": 0, "stddev": 0, "non_bg_ratio": 0.0, "edge_density": 0.0}

    # Downsample for speed; keep enough resolution to see real content.
    max_w = 400
    if img.width > max_w:
        ratio = max_w / img.width
        img = img.resize((max_w, max(1, int(img.height * ratio))))

    total = img.width * img.height or 1

    # 1. Unique colors — blank renders collapse to the page bg (optionally plus
    #    a couple of anti-alias fringe colors).
    colors = img.getcolors(maxcolors=4096) or []
    unique_colors = len(colors)

    # 2. Luma stddev — flat images have stddev ≈ 0.
    gray = img.convert("L")
    stddev = float(ImageStat.Stat(gray).stddev[0])

    # 3. Non-background ratio — find the dominant color and count pixels that
    #    are far from it.
    if colors:
        dom_count, dom_color = max(colors, key=lambda c: c[0])
        dr, dg, db = dom_color
        px = img.load()
        threshold = 22  # Manhattan distance
        non_bg = 0
        step = 2  # stride for speed
        sampled = 0
        for y in range(0, img.height, step):
            for x in range(0, img.width, step):
                r, g, b = px[x, y]
                if abs(r - dr) + abs(g - dg) + abs(b - db) > threshold:
                    non_bg += 1
                sampled += 1
        non_bg_ratio = non_bg / max(sampled, 1)
    else:
        non_bg_ratio = 0.0

    # 4. Edge density — how much structural detail is there?
    try:
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_hist = edges.histogram()
        edge_pixels = sum(edge_hist[25:])  # pixels above low-intensity cutoff
        edge_density = edge_pixels / max(total, 1)
    except Exception:
        edge_density = 0.0

    return {
        "unique_colors": unique_colors,
        "stddev": round(stddev, 2),
        "non_bg_ratio": round(non_bg_ratio, 4),
        "edge_density": round(edge_density, 5),
    }


class BrowserManager:
    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def launch(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )

    async def new_page(self) -> Page:
        assert self._context, "Browser not launched"
        return await self._context.new_page()

    @traced
    async def capture_page(self, url: str) -> tuple[bytes, str, str]:
        """Navigate, return (screenshot_bytes, html, title)."""
        page = await self.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=20_000)
            except Exception:
                # Heavy sites (SPAs, analytics-heavy) may never reach networkidle.
                # Fall back to domcontentloaded which fires once HTML is parsed.
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(3_000)
            await page.wait_for_timeout(800)
            screenshot = await page.screenshot(full_page=True)
            html = await page.content()
            title = await page.title()
            metric(url=url, screenshot_bytes=len(screenshot), html_chars=len(html))
            return screenshot, html, title
        finally:
            await page.close()

    @traced
    async def capture_viewport_chunks(
        self,
        url: str,
        chunk_height: int = 900,
        max_chunks: int = 12,
        overlap: int = 80,
    ) -> list[dict]:
        """Navigate to *url* and capture a sequence of viewport-sized
        screenshots that together cover the page top-to-bottom.

        Why chunks: LLM coordinate accuracy on very tall full-page
        screenshots is poor — bboxes drift by tens to hundreds of pixels. By
        splitting the page into ~1440×900 slices (one laptop viewport each),
        the model sees one chunk at a time and its bbox is in
        chunk-local coordinates, which crops very cleanly.

        Returns a list of dicts:
            [{index, screenshot, offset_y, width, height, html, title, full_height}, ...]
        The `offset_y` is the scrollY at capture time so callers can map
        chunk-local coords back to full-page coords if needed.
        """
        page = await self.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=25_000)
            except Exception:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                await page.wait_for_timeout(3_000)
            await page.wait_for_timeout(800)

            html = await page.content()
            title = await page.title()
            typography = await _extract_typography(page)

            viewport = page.viewport_size or {"width": 1440, "height": 900}
            vp_w = viewport["width"]
            # Allow caller to override the slice height (smaller slices =
            # better bbox accuracy, more calls).
            vp_h = chunk_height
            # Resize the viewport so screenshots match the chunk height.
            if vp_h != viewport["height"]:
                await page.set_viewport_size({"width": vp_w, "height": vp_h})

            # Dismiss a fixed/sticky header on subsequent chunks by hiding
            # its fixed-positioning styles so it doesn't appear in every
            # slice. The first chunk keeps it (it's a real visual component).
            full_height = await page.evaluate(
                "() => Math.max(document.documentElement.scrollHeight, "
                "document.body?.scrollHeight || 0)"
            )

            chunks: list[dict] = []
            offset = 0
            idx = 0
            step = max(200, vp_h - overlap)
            while offset < full_height and idx < max_chunks:
                # Hide sticky/fixed layers from chunk 2 onward so the same
                # header isn't duplicated across slices and doesn't cover
                # content behind it.
                if idx == 1:
                    await page.add_style_tag(content="""
                        *[style*="position: fixed"], *[style*="position:fixed"],
                        *[style*="position: sticky"], *[style*="position:sticky"],
                        header, nav.fixed, nav.sticky {
                            position: static !important;
                        }
                    """)
                await page.evaluate(f"window.scrollTo(0, {offset})")
                await page.wait_for_timeout(250)
                try:
                    shot = await page.screenshot(full_page=False, clip={
                        "x": 0, "y": 0, "width": vp_w, "height": vp_h,
                    })
                except Exception:
                    shot = await page.screenshot(full_page=False)
                chunks.append({
                    "index": idx,
                    "screenshot": shot,
                    "offset_y": offset,
                    "width": vp_w,
                    "height": vp_h,
                })
                idx += 1
                offset += step

            metric(
                url=url, chunks=len(chunks),
                full_height=full_height, viewport_h=vp_h,
            )
            return [
                {
                    **c,
                    "html": html if c["index"] == 0 else "",  # only need once
                    "title": title,
                    "full_height": full_height,
                    "typography": typography if c["index"] == 0 else {},
                }
                for c in chunks
            ]
        finally:
            await page.close()

    @traced
    async def find_nav_links(self, url: str, max_links: int = 15) -> list[NavLink]:
        """Heuristically enumerate same-origin navigation links on *url*.

        Returns candidate subpages for a subagent to pick from.
        """
        page = await self.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=20_000)
            except Exception:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(3_000)
            await page.wait_for_timeout(500)
            js = """
            () => {
                const origin = location.origin;
                const here = location.pathname;
                const seen = new Set();
                const out = [];
                const sels = [
                    'nav a[href]', 'header a[href]', '[role="navigation"] a[href]',
                    'footer a[href]', 'main a[href]'
                ];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        let href = el.href;
                        if (!href || href.startsWith('javascript:') || href === '#') continue;
                        try {
                            const u = new URL(href, origin);
                            if (u.origin !== origin) continue;
                            if (u.pathname === here && !u.hash) continue;
                            href = u.origin + u.pathname + u.search;
                        } catch { continue; }
                        if (seen.has(href)) continue;
                        seen.add(href);
                        const label = (el.textContent || '').trim().replace(/\\s+/g,' ').substring(0, 80);
                        if (!label) continue;
                        out.push({ label, href });
                    }
                }
                return out;
            }
            """
            raw = await page.evaluate(js)
            metric(url=url, candidates_found=len(raw), returned=min(len(raw), max_links))
            return [NavLink(**x) for x in raw[:max_links]]
        finally:
            await page.close()

    async def extract_computed_styles(self, url: str) -> str:
        """Open *url*, extract computed styles via in-browser JS. Returns text summary."""
        page = await self.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=20_000)
            except Exception:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(3_000)
            await page.wait_for_timeout(500)
            return await self._run_style_extraction(page)
        finally:
            await page.close()

    @traced
    async def extract_styles_from_html(self, html: str) -> str:
        """Load raw HTML into a page, extract computed styles."""
        page = await self.new_page()
        try:
            await page.set_content(html, wait_until="networkidle", timeout=15_000)
            await page.wait_for_timeout(400)
            return await self._run_style_extraction(page)
        finally:
            await page.close()

    async def _run_style_extraction(self, page: Page) -> str:
        js = """
        () => {
            const PROPS = [
                'color','background-color','font-family','font-size','font-weight',
                'line-height','letter-spacing','border','border-radius','padding',
                'margin','box-shadow','text-transform','display','gap','max-width',
                'width','height','flex-direction','justify-content','align-items',
                'grid-template-columns'
            ];
            const DEFAULTS = new Set([
                'none','normal','0px','auto','rgba(0, 0, 0, 0)','transparent',
                'start','stretch','visible','static','inline','baseline'
            ]);
            const getStyles = (el) => {
                const cs = getComputedStyle(el);
                const o = {};
                for (const p of PROPS) {
                    const v = cs.getPropertyValue(p);
                    if (v && !DEFAULTS.has(v)) o[p] = v;
                }
                return o;
            };
            const label = (el) => {
                const tag = el.tagName.toLowerCase();
                const cls = (el.className?.toString?.() || '').substring(0, 60);
                const id = el.id ? '#' + el.id : '';
                return tag + id + (cls ? '.' + cls.split(/\\s+/).slice(0,2).join('.') : '');
            };
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };

            const SELECTORS = [
                { k: 'buttons', s: 'button, a.btn, [role="button"], [class*="button"], [class*="btn"], [class*="cta"]' },
                { k: 'inputs', s: 'input:not([type="hidden"]), textarea, select' },
                { k: 'nav', s: 'nav, header, [role="navigation"], [class*="navbar"], [class*="menu"]' },
                { k: 'headings', s: 'h1, h2, h3, h4' },
                { k: 'sections', s: 'section, main, article, footer, [class*="hero"], [class*="banner"]' },
                { k: 'cards', s: '[class*="card"], [class*="Card"]' },
                { k: 'body', s: 'body' }
            ];
            const out = {};
            for (const { k, s } of SELECTORS) {
                const items = [];
                const seen = new Set();
                for (const el of document.querySelectorAll(s)) {
                    if (items.length >= 5) break;
                    if (!visible(el)) continue;
                    const styles = getStyles(el);
                    const key = JSON.stringify(styles);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const text = (el.textContent || '').trim().substring(0, 40);
                    items.push({ el: label(el), text, styles });
                }
                if (items.length) out[k] = items;
            }

            // Design tokens from :root
            const tokens = {};
            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules || []) {
                        if (rule.selectorText === ':root' || rule.selectorText === 'html') {
                            for (const prop of rule.style) {
                                if (prop.startsWith('--')) {
                                    tokens[prop] = rule.style.getPropertyValue(prop).trim();
                                }
                            }
                        }
                    }
                } catch (e) { /* cross-origin */ }
            }
            if (Object.keys(tokens).length) out['css_custom_properties'] = tokens;
            return out;
        }
        """
        try:
            data = await page.evaluate(js)
        except Exception:
            return ""
        lines = ["## Computed Styles"]
        tokens = data.pop("css_custom_properties", None)
        if tokens:
            lines.append("\n### Design Tokens (CSS Custom Properties)")
            for k, v in list(tokens.items())[:30]:
                lines.append(f"  {k}: {v}")
        for cat, items in data.items():
            lines.append(f"\n### {cat}")
            for it in items:
                text = it.get("text") or ""
                lines.append(f"  {it['el']}" + (f' "{text}"' if text else ""))
                for p, v in it["styles"].items():
                    lines.append(f"    {p}: {v}")
        return "\n".join(lines)

    @traced
    async def enrich_component_styles(
        self, html: str, components: list[dict],
    ) -> list[dict]:
        """Load saved page HTML, locate each component, return real computed styles.

        Each entry in `components` must have:
            {id, tag, classes, text_hint, href, expected_styles}.
        `expected_styles` carries the LLM-declared values (background_color,
        font_size, font_weight) used to disambiguate visually similar elements.
        Returns a list of {id, styles: {...}, enriched_snippet: "..."} for
        components that were successfully located.
        """
        page = await self.new_page()
        try:
            await page.set_content(html, wait_until="networkidle", timeout=20_000)
            await page.wait_for_timeout(600)
            return await page.evaluate("""(components) => {
                const PROPS = [
                    'color','background-color','font-family','font-size','font-weight',
                    'line-height','letter-spacing','border','border-radius','padding',
                    'box-shadow','text-transform','display','width','height',
                    'gap','max-width'
                ];
                const DEFAULTS = new Set([
                    'none','normal','0px','auto','rgba(0, 0, 0, 0)','transparent',
                    'start','stretch','visible','static','inline','baseline',''
                ]);

                function parseRgb(s) {
                    if (!s) return null;
                    const m = s.match(/rgb[a]?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
                    return m ? [+m[1], +m[2], +m[3]] : null;
                }

                function colorDist(a, b) {
                    if (!a || !b) return 999;
                    return Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1]) + Math.abs(a[2]-b[2]);
                }

                function styleScore(el, expected) {
                    if (!expected || Object.keys(expected).length === 0) return 0;
                    const cs = getComputedStyle(el);
                    let score = 0;
                    if (expected.background_color) {
                        const expBg = parseRgb(expected.background_color);
                        const actBg = parseRgb(cs.backgroundColor);
                        const dist = colorDist(expBg, actBg);
                        if (dist < 30) score += 10;
                        else if (dist > 200) score -= 5;
                    }
                    if (expected.font_size) {
                        const expFs = parseFloat(expected.font_size);
                        const actFs = parseFloat(cs.fontSize);
                        if (!isNaN(expFs) && !isNaN(actFs)) {
                            if (Math.abs(expFs - actFs) < 2) score += 3;
                        }
                    }
                    if (expected.font_weight) {
                        const expFw = parseInt(expected.font_weight, 10);
                        const actFw = parseInt(cs.fontWeight, 10);
                        if (!isNaN(expFw) && !isNaN(actFw)) {
                            if (expFw === actFw) score += 2;
                        }
                    }
                    return score;
                }

                function findElement(spec) {
                    let candidates = [];

                    // Strategy 1: class-based selector
                    if (spec.classes && spec.classes.length > 0) {
                        const tag = spec.tag || '*';
                        const full = tag + '.' + spec.classes.map(c => CSS.escape(c)).join('.');
                        candidates = Array.from(document.querySelectorAll(full));
                        if (candidates.length === 0 && spec.classes.length > 1) {
                            const loose = tag + '.' + CSS.escape(spec.classes[0]);
                            candidates = Array.from(document.querySelectorAll(loose));
                        }
                    }

                    // Strategy 2: href-based for links/buttons —
                    // collect ALL href matches but don't stop; later strategies
                    // may add more candidates if needed.
                    if (spec.href) {
                        const sel = spec.tag
                            ? spec.tag + '[href="' + CSS.escape(spec.href) + '"]'
                            : '[href="' + CSS.escape(spec.href) + '"]';
                        const hrefMatches = Array.from(document.querySelectorAll(sel));
                        for (const h of hrefMatches) {
                            if (!candidates.includes(h)) candidates.push(h);
                        }
                    }

                    // Strategy 3: tag-only fallback
                    if (candidates.length === 0 && spec.tag) {
                        candidates = Array.from(document.querySelectorAll(spec.tag)).slice(0, 50);
                    }

                    if (candidates.length === 0) return null;

                    const hint = (spec.text_hint || '').trim().toLowerCase().slice(0, 60);
                    const expected = spec.expected_styles || {};
                    let best = candidates[0];
                    let bestScore = -Infinity;
                    for (const el of candidates) {
                        const t = (el.textContent || '').trim().toLowerCase().slice(0, 80);
                        let score = 0;
                        if (hint && t.includes(hint.slice(0, 20))) score += 3;
                        if (hint && t === hint) score += 5;
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) score += 2;
                        score += styleScore(el, expected);
                        if (score > bestScore) { bestScore = score; best = el; }
                    }
                    return best;
                }

                function getComputedProps(el) {
                    const cs = getComputedStyle(el);
                    const out = {};
                    for (const p of PROPS) {
                        const v = cs.getPropertyValue(p);
                        if (v && !DEFAULTS.has(v)) out[p] = v;
                    }
                    return out;
                }

                function buildInlineSnippet(el, computed) {
                    const clone = el.cloneNode(true);
                    const inlineParts = [];
                    for (const [prop, val] of Object.entries(computed)) {
                        inlineParts.push(prop + ':' + val);
                    }
                    const existing = clone.getAttribute('style') || '';
                    const merged = existing
                        ? existing.replace(/;?$/, ';') + inlineParts.join(';')
                        : inlineParts.join(';');
                    clone.setAttribute('style', merged);
                    return clone.outerHTML.substring(0, 1200);
                }

                const results = [];
                for (const spec of components) {
                    const el = findElement(spec);
                    if (!el) continue;
                    const computed = getComputedProps(el);
                    results.push({
                        id: spec.id,
                        styles: {
                            background_color: computed['background-color'] || '',
                            text_color: computed['color'] || '',
                            border_radius: computed['border-radius'] || '',
                            padding: computed['padding'] || '',
                            font_size: computed['font-size'] || '',
                            font_weight: computed['font-weight'] || '',
                            font_family: computed['font-family'] || '',
                            border: computed['border'] || '',
                            box_shadow: computed['box-shadow'] || '',
                        },
                        enriched_snippet: buildInlineSnippet(el, computed),
                    });
                }
                return results;
            }""", components)
        except Exception as exc:
            metric(enrich_failed=True, error=str(exc)[:120])
            return []
        finally:
            await page.close()

    @traced
    async def measure_snippet_render(
        self, snippet: str, base_href: str | None = None
    ) -> dict:
        """Render `snippet` in isolation and visually inspect the result.

        Takes a real screenshot of the standalone render and runs pixel-level
        analysis (unique color count, stddev, non-background ratio, edge
        density). A snippet is "blank" if the rendered image is essentially
        one flat color, has almost no non-background pixels, or has no edge
        structure — which covers cases the DOM heuristic misses (transparent
        wrappers, white-on-white text, elements hidden by missing parent CSS).

        Also returns dom-level counters and the raw screenshot bytes so
        callers can show the render in the UI or hand it to an LLM.
        """
        page = await self.new_page()
        try:
            base_tag = f'<base href="{base_href}">' if base_href else ""
            # Neutral light-grey page background so transparent snippets are
            # visibly distinguishable from a rendered component with its own
            # background.
            doc = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"{base_tag}"
                "<style>html,body{margin:0;padding:0;"
                "font-family:system-ui,sans-serif;background:#f4f4f5}</style>"
                f"</head><body>{snippet}</body></html>"
            )
            try:
                await page.set_content(doc, wait_until="networkidle", timeout=8_000)
            except Exception:
                await page.set_content(doc, wait_until="domcontentloaded", timeout=8_000)
            await page.wait_for_timeout(400)

            try:
                dom = await page.evaluate(
                    """
                    () => {
                        const body = document.body;
                        const rect = body.getBoundingClientRect();
                        const text = (body.innerText || '').trim();
                        const media = body.querySelectorAll('img, svg, video, canvas, picture').length;
                        let visible = 0;
                        for (const el of body.querySelectorAll('*')) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 4 && r.height > 4) visible++;
                            if (visible > 500) break;
                        }
                        return {
                            height: body.scrollHeight,
                            width: body.scrollWidth,
                            rect_height: Math.round(rect.height),
                            text_chars: text.length,
                            media_count: media,
                            visible_elements: visible,
                        };
                    }
                    """
                )
            except Exception as exc:
                return {
                    "height": 0, "width": 0, "text_chars": 0,
                    "media_count": 0, "visible_elements": 0,
                    "blank": True, "reason": f"eval_failed: {exc}",
                    "screenshot": None,
                }

            # Cap screenshot height so huge broken layouts don't dominate.
            clip_h = max(60, min(int(dom.get("height", 0)) or 400, 1200))
            try:
                img_bytes = await page.screenshot(
                    clip={"x": 0, "y": 0, "width": 1440, "height": clip_h}
                )
            except Exception:
                img_bytes = None

            pixel = _analyze_render_pixels(img_bytes) if img_bytes else {
                "unique_colors": 0, "stddev": 0, "non_bg_ratio": 0.0,
                "edge_density": 0.0,
            }

            blank = False
            reasons: list[str] = []

            # DOM collapse — definitely blank.
            if dom.get("height", 0) < 30:
                blank = True
                reasons.append(f"dom_height<30 ({dom.get('height')})")

            # Pixel-level visual inspection — authoritative.
            if img_bytes:
                if pixel["unique_colors"] <= 3:
                    blank = True
                    reasons.append(f"flat_image (unique_colors={pixel['unique_colors']})")
                if pixel["stddev"] < 4.0:
                    blank = True
                    reasons.append(f"low_variance (stddev={pixel['stddev']:.1f})")
                if pixel["non_bg_ratio"] < 0.01:
                    blank = True
                    reasons.append(f"no_content (non_bg={pixel['non_bg_ratio']:.3f})")
                if pixel["edge_density"] < 0.002:
                    blank = True
                    reasons.append(f"no_edges (density={pixel['edge_density']:.4f})")

            return {
                **dom,
                **pixel,
                "blank": blank,
                "reason": ",".join(reasons) if reasons else "ok",
                "screenshot": img_bytes,
            }
        finally:
            await page.close()

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
