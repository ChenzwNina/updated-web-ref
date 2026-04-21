# Skill: Analyze Components

## Purpose
Turn the 4 downloaded pages into a de-duplicated, categorised library of the
site's UI components — what a designer would call a "component inventory".

## Inputs
- `DownloadResult` from Skill 1 (4 pages of HTML + screenshots + URLs).
- Access to the shared browser (for computed-style extraction).

## Outputs
- `AnalysisResult`: per-category component groups, design tokens, summary.
- Saved JSON: `{job_dir}/analysis.json`.

## Procedure

### Step 1 — Read saved frontend code
For each of the 4 downloaded pages, load the saved HTML from disk and extract
computed styles via Playwright (loading saved HTML into a blank page rather
than re-navigating — faster, no network).

### Step 2 — Extract components in parallel, *by category*
We fan out one Sonnet 4.6 subagent *per component category*, running in
parallel. Categories come from `COMPONENT_CATEGORIES`:

    header · navigation · hero_banner · button · card · form ·
    layout_section · footer · media · feedback

Each subagent receives:
- Its category (e.g. "button")
- All 4 pages' cleaned HTML + computed styles
- Instructions to extract *only* components of its category

This gives us parallelism + focused attention per category.

### Step 3 — Dedupe and group
Each subagent returns components with a stable signature (type + canonical
styles). Within a category, collapse components whose key style values are
identical or nearly identical — keep the first, bump `count`.

### Step 4 — Harvest design tokens
Take the `:root` CSS custom properties from the style-extraction pass and
publish as `design_tokens`.

### Step 5 — Emit to the frontend
Publish an `analysis` event with the full `AnalysisResult` so the UI can
render the component gallery immediately.

## Notes
- We do NOT screenshot-crop individual components (too complex for v1 — the
  full-page screenshots already tell the visual story). A future revision
  could add per-component crops via DOM `getBoundingClientRect()`.
- This skill is invoked as `run_analyze_skill(download_result, browser,
  storage, bus) -> AnalysisResult`.
