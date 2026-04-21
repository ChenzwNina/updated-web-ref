from __future__ import annotations

from pydantic import BaseModel


class NavLink(BaseModel):
    label: str
    href: str


class ScreenshotChunk(BaseModel):
    """One viewport-sized slice of a page, saved to disk.

    `offset_y` is the scrollY at capture time so chunk-local bboxes can be
    mapped back to full-page coordinates.
    """
    index: int
    path: str
    offset_y: int = 0
    width: int = 1440
    height: int = 900


class DownloadedPage(BaseModel):
    url: str
    html_path: str
    screenshot_path: str
    title: str = ""
    chunks: list[ScreenshotChunk] = []
    # Dominant font families / weights by role. Keys include
    # heading_family, heading_weight, body_family, body_weight,
    # button_family, button_weight, base_family, base_size (px).
    typography: dict[str, str | int] = {}


class DownloadResult(BaseModel):
    root_url: str
    pages: list[DownloadedPage]
    job_dir: str


class ComponentStyle(BaseModel):
    background_color: str = ""
    text_color: str = ""
    border_radius: str = ""
    padding: str = ""
    font_size: str = ""
    font_weight: str = ""
    font_family: str = ""
    border: str = ""
    box_shadow: str = ""
    extra: dict[str, str] = {}


class Component(BaseModel):
    id: str
    type: str
    name: str
    description: str
    html_snippet: str = ""
    source_url: str = ""
    screenshot_crop: str | None = None
    styles: ComponentStyle
    count: int = 1
    # Populated by the validate_components skill.
    # status ∈ {"unchecked", "ok", "regenerated", "unrecoverable"}.
    validation_status: str = "unchecked"
    validation_note: str = ""


class ComponentGroup(BaseModel):
    category: str
    components: list[Component]


class AnalysisResult(BaseModel):
    root_url: str
    groups: list[ComponentGroup]
    # Loose dict so tokens can hold lists (palette), nested dicts (palette
    # coverage / typography), or scalars (primary color hex).
    design_tokens: dict = {}
    summary: str = ""


class GenerateRequest(BaseModel):
    site_type: str
    pages: list[str] = []
    extra_instructions: str = ""


class GeneratedSite(BaseModel):
    html: str
    pages_generated: list[str] = []


COMPONENT_TAXONOMY: dict[str, list[str]] = {
    "Actions": [
        "standard_button", "button_group", "segmented_button",
        "fab", "extended_fab", "fab_menu", "icon_button",
    ],
    "Communication": [
        "badge", "progress_linear", "progress_circular", "snackbar",
    ],
    "Containment": [
        "card", "dialog", "bottom_sheet", "side_sheet", "divider", "tooltip",
    ],
    "Navigation": [
        "top_app_bar", "bottom_app_bar", "navigation_bar",
        "navigation_drawer", "navigation_rail", "tabs", "breadcrumbs", "pagination",
    ],
    # Selection category removed per user request — chips/switches/checkboxes
    # are rarely distinct-enough style variants and tended to pollute galleries.
    "Inputs": [
        "text_field", "textarea", "select", "date_picker", "time_picker", "search_bar",
    ],
    "Layout": [
        "hero", "feature_grid", "split_section", "bento_grid",
        "logo_cloud", "testimonial_section", "pricing_table", "footer",
    ],
    "Media": [
        "image", "gallery", "carousel", "video_player", "avatar",
    ],
}

COMPONENT_CATEGORIES = list(COMPONENT_TAXONOMY.keys())
COMPONENT_SUBTYPES = [sub for subs in COMPONENT_TAXONOMY.values() for sub in subs]
