# Skill: Download Website

## Purpose
Given a root URL, produce a local snapshot of **four representative pages** of
the site — the root plus three subpages reached via site navigation — saved as
raw HTML and full-page screenshots for downstream analysis.

## Inputs
- `root_url` (string) — the URL the user pasted in.

## Outputs
- `DownloadResult`: root_url, `pages: [DownloadedPage]` (4 entries when
  possible), `job_dir` (relative path to the job's output folder).
- Each `DownloadedPage` has `url`, `html_path`, `screenshot_path`, `title`.

## Procedure (three steps)

### Step 1 — Pick 3 subpages
1. Enumerate candidate navigation links from the root with the
   `discover_nav_links` tool (Playwright crawl of `<nav>`, `<header>`,
   `<footer>`, `<main>` anchors, same-origin only).
2. Call the **pick_subpages subagent** (Sonnet 4.6) with the full candidate
   list; it chooses 3 URLs that maximise design coverage (variety of page
   types: product, features, pricing, about, blog, docs — not 3 blog posts).
3. If fewer than 3 candidates exist, return whatever was found.

### Step 2 — Download all 4 pages
For the root and each picked subpage, call `download_page`:
- Navigate with Playwright (network idle + 800ms settle).
- Save the rendered HTML to `{job_dir}/html/{slug}.html`.
- Capture the page title.

### Step 3 — Screenshot all 4 pages
`download_page` also captures a full-page PNG and saves it to
`{job_dir}/screenshots/{slug}.png` in the same pass (same Playwright
navigation — don't re-load the page twice).

## Failure modes
- If `capture_page` times out, skip that URL and continue. Return partial
  results rather than failing the whole job.
- If fewer than 4 pages succeed, that's OK — downstream skills handle it.

## Notes for the skill runner
This skill is invoked imperatively by `run_download_skill(root_url, browser,
storage, bus)`. It does NOT use an LLM tool-use loop for the main flow — the
flow is deterministic: enumerate → pick (subagent) → download+screenshot. The
only LLM call is the "pick_subpages" subagent.
