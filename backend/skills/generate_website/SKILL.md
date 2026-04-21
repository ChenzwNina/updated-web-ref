# Skill: Generate Website

## Purpose
Produce a new, standalone HTML website that **looks like it belongs to the
reference brand** — using the component library + design tokens extracted by
Skill 2, and (optionally) the raw reference HTML + screenshots from Skill 1.

## Inputs
- `AnalysisResult` from Skill 2 (component library + tokens + summary).
- `DownloadResult` from Skill 1 (saved HTML + screenshots, available if the
  generator wants to reach back for extra fidelity).
- `GenerateRequest` from the user:
    - `site_type` — e.g. "personal portfolio", "SaaS landing page", "coffee shop".
    - `pages` — list of pages to generate, e.g. ["home", "about", "contact"].
    - `extra_instructions` — free text.

## Outputs
- `GeneratedSite` with a single-file HTML string (all pages stitched together
  as sections, OR one file per page — decided below).
- For v1 we produce ONE combined single-file HTML with anchor navigation
  between the requested pages. Saves to `{job_dir}/generated/site.html`.

## Procedure

### Step 1 — Tell user analysis is ready, collect the request
This step happens at the API layer (the frontend renders the component
gallery and the generate form, which then calls `/api/generate`).

### Step 2 — Build the generation prompt
Assemble:
1. Design-tokens table (colors, fonts, border-radius, spacing).
2. Component library (one entry per deduped component with its styles).
3. Up to 2 reference HTML samples (truncated) so the model sees real DOM structure.
4. Up to 3 reference screenshots (base64) as multimodal input.
5. The user's site_type + pages + extra_instructions.

### Step 3 — Call Sonnet 4.6 (multimodal)
Single call, system prompt = `GENERATE_PROMPT`, expected output = a full
HTML document (`<!DOCTYPE html> … </html>`). Strip markdown fences if present.

### Step 4 — Save + return
Save to `{job_dir}/generated/site.html`, return the HTML string to the
frontend, which displays it in an iframe.

## Notes
- Using Sonnet 4.6 (not Opus) for generation keeps costs bounded and the
  speed high; quality is sufficient for design-matched static pages.
- A future revision could break this into per-page subagents (one subagent
  per requested page) and stitch, but v1 keeps it as one pass.
