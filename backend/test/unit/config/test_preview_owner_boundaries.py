"""Preview Owner 的静态依赖方向。"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "yuxi"


def _imports(relative_path: str) -> set[str]:
    tree = ast.parse((PACKAGE_ROOT / relative_path).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_common_filepreview_does_not_import_domain_storage_or_http() -> None:
    imports = _imports("infrastructure/document_preview.py")

    assert not any(
        module.startswith(("yuxi.modules.workspace", "yuxi.modules.knowledge", "yuxi.storage", "fastapi", "starlette"))
        for module in imports
    )


def test_knowledge_and_artifact_do_not_import_workspace_preview() -> None:
    for relative_path in (
        "modules/knowledge/base.py",
        "modules/knowledge/preview.py",
        "modules/agents/services/artifacts.py",
    ):
        assert "yuxi.modules.workspace.preview" not in _imports(relative_path)
