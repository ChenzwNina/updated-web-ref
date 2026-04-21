# Skill 3 — Validate Components

**Purpose:** after `analyze_components` produces the component inventory, some
snippets don't render correctly in isolation because they depend on CSS that
lives on *other pages* or on ancestor stylesheets we didn't capture. This
skill closes that gap.

## Pipeline

1. **Render-test each component's `html_snippet` standalone** in a headless
   Chromium page (no parent context beyond the snippet itself, `<base href>`
   set to the source page so same-origin stylesheets still load if referenced
   inline). Measure `scrollHeight`, visible element count, text length,
   embedded media count.
2. A component is flagged **bad** when the standalone render collapses —
   height < 30px, fewer than 2 visible elements, or no text + no media.
   These are the ones whose visual identity only emerges with external parent
   CSS the snippet doesn't include.
3. Bad components are grouped by `source_url`. For each page, one multimodal
   Sonnet 4.6 call receives:
   - the saved full-page screenshot,
   - the list of bad components (current snippet + name + description + style
     signature).
   The model locates each component in the screenshot and rewrites the
   `html_snippet` as **self-contained HTML with inline styles** so it no
   longer depends on external CSS.
4. Updated snippets are merged back into the `AnalysisResult`. Each component
   gets `validation_status ∈ {ok, regenerated, unrecoverable}` plus a short
   `validation_note`.

## Why a separate skill (and a separate tool call)

- The orchestrator decides when to run it — can be skipped for a quick pass.
- Keeps the initial extraction cheap; regeneration only runs for snippets
  that actually need it.
- Per-page batching keeps Sonnet input token use bounded (one screenshot per
  call, shared across the bad components on that page).

## Outputs

- Updates `analysis.json` in place with the regenerated snippets and
  validation metadata.
- Emits `status` + `skill_end` events so the UI can show regeneration
  progress and counts.
