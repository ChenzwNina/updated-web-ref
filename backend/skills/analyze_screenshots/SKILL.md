# Skill 2b — Analyze From Screenshots

**Purpose:** extract the component inventory using *only the page
screenshots*, not the HTML. The code-based approach (`analyze_components`)
struggled with sites whose markup depends on external stylesheets — e.g.
Chakra UI / `chakra-link` + `mega-menu__*` utility classes where the raw HTML
looks trivial but the rendered component is a styled nav trigger. Screenshots
are the visual ground truth, so we hand them directly to Sonnet.

## Pipeline

1. For each downloaded page, load the full-page screenshot and send it to a
   Sonnet 4.6 multimodal call with a strict instruction: identify every
   *distinct, visually-reusable* UI component and return, per component:
   - taxonomy category + subtype,
   - id / name / description,
   - approximate bounding box `[x, y, w, h]` in pixels,
   - a **self-contained HTML snippet with inline styles** that visually
     reproduces the component (no external CSS, no `...` placeholders),
   - the usual style keys (`background_color`, `text_color`, `padding`, …).
2. Optionally crop the bbox from the page screenshot and save it as the
   component's `screenshot_crop` so the UI can show "this is what it
   actually looks like" next to the rendered snippet.
3. Deduplicate across pages by `subtype + style signature`, summing `count`.
4. Return the same `AnalysisResult` shape as the old skill, so
   `validate_components` and `generate_website` can consume it unchanged.

## Why this is better for style-heavy sites

- The model never sees external stylesheets, so it can't be misled into
  emitting a "snippet" that only looks right inside the page's CSS cascade.
- It has to synthesize inline styles from the pixels, which naturally yields
  standalone-renderable HTML — exactly what the frontend preview needs.
- Screenshots show the true visual variants (primary button vs. ghost
  button) even when the underlying markup uses the same class.

## Tradeoffs

- Slightly less precise on exact CSS values (no computed styles to crib
  from). Partially mitigated by the taxonomy + the `styles` object, which
  Sonnet estimates from the pixels.
- Bounding boxes from LLM output are approximate — used for cropping, not
  for pixel-perfect measurement.
