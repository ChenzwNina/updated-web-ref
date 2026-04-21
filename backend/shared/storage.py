"""Per-job disk storage: output/{domain}_{timestamp}/{html,screenshots,components,generated}."""
from __future__ import annotations

import json
import pathlib
import re
from urllib.parse import urlparse

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_OUTPUT_ROOT = PROJECT_ROOT / "output"


def _slug(url: str, max_len: int = 60) -> str:
    p = urlparse(url)
    host = p.netloc.replace(":", "_") or "page"
    path = p.path.strip("/").replace("/", "_") or "index"
    raw = f"{host}_{path}" if host not in path else path
    return re.sub(r"[^a-zA-Z0-9_\-]", "", raw)[:max_len]


class JobStorage:
    def __init__(self, root_url: str, timestamp: str):
        domain = urlparse(root_url).netloc.replace(":", "_") or "job"
        self.root_url = root_url
        self.base_dir = _OUTPUT_ROOT / f"{domain}_{timestamp}"
        self.html_dir = self.base_dir / "html"
        self.screenshots_dir = self.base_dir / "screenshots"
        self.components_dir = self.base_dir / "components"
        self.generated_dir = self.base_dir / "generated"
        for d in (self.html_dir, self.screenshots_dir, self.components_dir, self.generated_dir):
            d.mkdir(parents=True, exist_ok=True)

    def rel(self, path: pathlib.Path) -> str:
        """Path relative to the output root (for serving via /output/...)."""
        return str(path.relative_to(_OUTPUT_ROOT.parent)).replace("\\", "/")

    def save_html(self, url: str, html: str) -> str:
        path = self.html_dir / f"{_slug(url)}.html"
        path.write_text(html, encoding="utf-8")
        return self.rel(path)

    def save_screenshot(self, url: str, data: bytes) -> str:
        path = self.screenshots_dir / f"{_slug(url)}.png"
        path.write_bytes(data)
        return self.rel(path)

    def save_screenshot_chunk(self, url: str, index: int, data: bytes) -> str:
        path = self.screenshots_dir / f"{_slug(url)}_chunk{index:02d}.png"
        path.write_bytes(data)
        return self.rel(path)

    def save_screenshot_crop(self, component_id: str, data: bytes) -> str:
        path = self.components_dir / f"{component_id}.png"
        path.write_bytes(data)
        return self.rel(path)

    def save_json(self, name: str, data: dict) -> str:
        path = self.base_dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.rel(path)

    def save_generated(self, filename: str, html: str) -> str:
        path = self.generated_dir / filename
        path.write_text(html, encoding="utf-8")
        return self.rel(path)

    def read_html(self, rel_path: str) -> str:
        abs_path = PROJECT_ROOT / rel_path
        return abs_path.read_text(encoding="utf-8", errors="replace")

    def read_bytes(self, rel_path: str) -> bytes:
        abs_path = PROJECT_ROOT / rel_path
        return abs_path.read_bytes()


def find_job_dir(job_dir_rel: str) -> pathlib.Path:
    return PROJECT_ROOT / job_dir_rel
