# Web Style Reference

**Turn any website into a reusable design system — then generate new sites in the same style. For example, you might find a flower shop website with a product gallery style you like. This agent system can analyze high-level design attributes such as layout, spacing, typography, and color usage, then generate component code that reflects a similar visual feel for your own website.** 

Point it at a URL. It captures the page as viewport chunks, extracts the component library (buttons, heroes, cards, nav…), pulls the brand color palette tiered into Primary / Secondary / Accent, and identifies the typography stack. Then it generates a brand-new site that uses the same visual language — real components, matched Google Fonts, and on-brand Unsplash imagery.

Demo video: https://www.tella.tv/video/speed-up-web-design-with-web-style-reference-8g3i

## Quick start

**You need:** Python 3.11+, Node 20+, an [Anthropic API key](https://console.anthropic.com/), and (optional but recommended) an [Unsplash Access Key](https://unsplash.com/developers). Without an Unsplash key, images fall back to LoremFlickr.

```bash
# API keys
cp .env.example .env
# edit .env: paste ANTHROPIC_API_KEY and UNSPLASH_ACCESS_KEY

# Backend
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium
python -m uvicorn backend.api.app:app --reload --port 8000

# Frontend (second terminal)
cd frontend && npm install && npm run dev
```

Open http://localhost:5173, paste a URL, watch it stream, then click **Generate**.

## How it works

A Claude **Opus** main agent orchestrates five skills in order. The perception work (looking at screenshots, picking image keywords, fixing broken snippets) fans out to Claude **Sonnet** subagents. Everything in between is plain Python — Playwright, PIL, Unsplash's API.

```
URL → ① download → ② analyze → ③ validate → ④ replace_images → ⑤ generate → HTML
```

| Stage | What it does |
|---|---|
| ① `download_website` | Playwright scrolls the page in 1440×900 chunks (better bbox accuracy than one tall screenshot) and reads typography from computed styles. |
| ② `analyze_screenshots` | One Sonnet call per chunk in parallel identifies components. PIL extracts the color palette and tiers it into Primary / Secondary / Accent. Fuzzy dedup merges near-duplicates across chunks. |
| ③ `validate_components` | Renders each component snippet in isolation and checks the pixels (edge density, color spread). A Sonnet fixer rewrites any that render blank. |
| ④ `replace_images` | For every image slot, either uses the existing alt text or asks a Sonnet subagent to pick keywords from the crop. Unsplash resolves each — with per-slot variants so repeats get distinct photos. |
| ⑤ `generate_website` | Opus gets a design-system brief (palette tiers, typography with Google Fonts instructions, component library) + screenshots, and writes one self-contained HTML page. |

All intermediate results are typed Pydantic objects persisted under `output/<job_id>/`, so any stage can be replayed standalone for debugging.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/analyze` | Start analysis. Body: `{url}`. Returns `{job_id}`. |
| GET | `/api/stream/{job_id}?phase=analyze` | SSE progress stream. |
| POST | `/api/generate` | Start generation. Body: `{job_id, site_type, pages[], extra_instructions}`. |
| GET | `/api/stream/{job_id}?phase=generate` | SSE progress stream. |
| GET | `/api/job/{job_id}` | Current job state. |
| GET | `/output/...` | Static artifacts (chunks, HTML, generated sites). |

## Output layout

```
output/<domain>_<timestamp>/
  html/                # raw DOM snapshot
  screenshots/         # viewport chunks (*_chunk00.png, ...)
  components/          # per-component crops
  generated/site.html  # final generated page
  download_result.json # stage ① result
  analysis.json        # stage ②/③/④ result (layered)
```

## Environment

```
ANTHROPIC_API_KEY=sk-ant-...          # required
UNSPLASH_ACCESS_KEY=...               # optional — falls back to LoremFlickr
MAIN_AGENT_MODEL=claude-opus-4-6      # default
SUBAGENT_MODEL=claude-sonnet-4-6      # default
```

---

## Stages in detail

Every skill lives in `backend/skills/<name>/` with `skill.py` (the Python entrypoint), optional `subagent.py` (its Sonnet prompt), and `SKILL.md`.

### ① download_website
Boots headless Chromium, navigates to the URL, and calls `capture_viewport_chunks`. Instead of one tall full-page screenshot (which makes LLM bbox estimates drift tens of pixels), it scrolls the page in ~1440×900 slices with 80px overlap. Sticky headers are auto-suppressed from chunk 2 onward so they don't repeat in every slice. A JS pass walks visible `h1–h3`, `p/li/article`, and `button/a[role=button]` elements and tallies the dominant computed `font-family` + `font-weight` per role — that's the typography data the generator later uses to pick Google Fonts. No LLM calls in this stage.

**Produces:** `DownloadResult { chunks[], html, title, typography }`

### ② analyze_screenshots
Fires **one Sonnet subagent call per chunk in parallel**. Each call sees only that chunk and returns chunk-local bboxes for every UI component it recognizes, plus proposed HTML snippets, declared styles, and a taxonomy tag. After the fan-in:

1. **Crop** — PIL cuts each bbox from the chunk PNG; that's the per-component screenshot shown in the UI.
2. **Enrich** — Playwright loads the saved DOM, finds each LLM-declared element, and replaces its declared styles with the *real* computed values (so padding / radius / font-size match the site exactly).
3. **Dedupe** — token-based fuzzy matching (Jaccard + subset ratio) with a curated stopword list merges near-duplicates across chunks. "White Cookie Consent Bottom Snackbar" and "White Cookie Consent Snackbar" merge, but "Solid Button" and "Outline Button" stay separate.
4. **Palette** — `palette.py` concatenates downsampled chunks, quantizes to 32 colors via PIL median-cut, filters near-white / near-black / grey, greedy-picks distinct hues by RGB distance, and tiers them: dominant = Primary, next 2 = Secondary, rest = Accent.

**Produces:** `AnalysisResult { groups[], design_tokens: { palette_primary, palette_secondary, palette_accent, typography, ... } }`

### ③ validate_components
For each component, Playwright renders the snippet in an isolated page and `measure_snippet_render` runs a pixel check: unique-color count, luma stddev, non-background ratio, edge density. This catches "technically valid HTML that renders as a blank box" — transparent wrappers, white-on-white text, missing parent CSS — that the DOM can't see on its own. Failures trigger a Sonnet fixer subagent with the component screenshot + broken HTML; it returns a rewrite. Each component ends up tagged `ok`, `regenerated`, or `unrecoverable`.

**Produces:** `AnalysisResult` with `validation_status` annotated on each component.

### ④ replace_images
Scans every component snippet for image slots (`<img src>` and inline `background-image: url()`). For slots that already carry a concrete `alt` / `data-alt`, that's the search query — no LLM call needed. For slots without one, a Sonnet subagent looks at the component crop and returns one keyword phrase per slot. Each query is then resolved:

- **With `UNSPLASH_ACCESS_KEY`:** the Unsplash Search API returns 10 results; slot `i` picks `results[i % 10]` so a gallery of 6 slots all tagged "product photo" gets 6 different photos.
- **Without it:** LoremFlickr with a `/lock/<hash ^ variant>` seed — same variant trick to avoid cache repeats.

**Produces:** `AnalysisResult` with real photo URLs baked into every snippet.

### ⑤ generate_website
Builds a design-system brief: the tiered palette with usage rules ("primary for hero CTAs, accents only for small highlights"), typography with a Google Fonts `<link>` instruction (or "pick the closest Google Font" if the family is system / proprietary), the full component library with real computed styles, and reference HTML samples. Loads up to 8 chunk screenshots as base64 and sends everything to Opus in one call, which returns a self-contained HTML page. A deterministic post-pass walks the generated HTML, finds any `PLACEHOLDER_IMAGE` or `placehold.co` tokens, and runs them through the same Unsplash resolver from stage ④.

**Produces:** `GeneratedSite { html, pages_generated[] }`

### How the three actors collaborate

- **Main agent (Opus)** — decides which skill runs next, streams status, never touches pixels.
- **Skills (Python)** — deterministic work: Playwright automation, PIL image ops, HTTP to Unsplash, dedup heuristics, file I/O. Each skill owns its subagent prompt.
- **Subagents (Sonnet)** — focused one-shot calls for perception (what's in this image?) and repair (fix this snippet). Fan out in parallel when there are multiple chunks or components.

Skills never call each other — they exchange typed Pydantic results through the main agent, which is why every stage's output is replayable from disk.

