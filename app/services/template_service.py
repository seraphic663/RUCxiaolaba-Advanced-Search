"""Minimal template renderer used by the existing static HTML templates."""

from __future__ import annotations

from pathlib import Path


class TemplateService:
    def __init__(self, templates_dir: str | Path):
        self.templates_dir = Path(templates_dir)

    def render(self, name: str, **values) -> str:
        path = self.templates_dir / name
        if not path.exists():
            return (
                "<html><body><h1>Error</h1>"
                f"<p>Template '{name}' not found in {self.templates_dir}</p>"
                "</body></html>"
            )
        content = path.read_text(encoding="utf-8")
        shared_assets = {
            "__SHARED_UI_CSS__": "shared_ui.css",
            "__SHARED_UI_JS__": "shared_ui.js",
            "__ADMIN_TOOLS_CSS__": "admin_tools.css",
            "__ADMIN_REPORTS_JS__": "admin_reports.js",
            "__ADMIN_AI_SEARCH_CSS__": "admin_ai_search.css",
            "__ADMIN_AI_SEARCH_JS__": "admin_ai_search.js",
            "__ADMIN_AI_COMMON_JS__": "admin_ai_common.js",
        }
        for token, shared_name in shared_assets.items():
            if token in content:
                shared_path = self.templates_dir / shared_name
                content = content.replace(
                    token,
                    shared_path.read_text(encoding="utf-8"),
                )
        for key, value in values.items():
            content = content.replace(f"__{key}__", str(value))
        return content
