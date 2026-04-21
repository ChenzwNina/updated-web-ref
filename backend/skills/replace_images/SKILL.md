# Skill — Replace Images (Unsplash)

Runs after `validate_components`. Components that contain `<img>` tags or
`background-image: url(...)` references usually carry broken/local URLs
from the original page. We replace each with a relevant Unsplash photo so
the exported component renders correctly in isolation and on the user's
project.

## Pipeline

1. Walk every component in the `AnalysisResult`.
2. For each component whose `html_snippet` contains image references:
   - Ask a multimodal subagent to look at the component's `screenshot_crop`
     and return 2–4 short keywords describing what the image should depict
     (e.g. `["pink peony bouquet", "peach flowers"]`). If the component
     holds multiple distinct images, the subagent returns one keyword set
     per slot.
3. For each keyword set, resolve to a concrete image URL:
   - If `UNSPLASH_ACCESS_KEY` is set, call Unsplash's search API and pick
     the top result's `urls.regular`.
   - Otherwise fall back to `https://loremflickr.com/{w}/{h}/{keyword}`
     which returns keyword-matched CC-licensed images without an API key.
4. Rewrite the `html_snippet`'s `src="..."` attributes (and
   `background-image: url(...)`) in source order. Original URLs are
   preserved in `validation_note` for debugging.

## Why a subagent

The image content should match what the original screenshot shows (peach
flowers, a couple on the beach, a laptop on a desk…). A pure-text keyword
heuristic from the component name misses nuance; the multimodal call sees
the actual crop and picks specific, evocative keywords.
