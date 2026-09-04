from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def test_core_does_not_import_adapters_or_application_code() -> None:
    root = Path(__file__).parents[1] / "src" / "website_collection_kit"
    core_files = [
        root / "collection.py",
        root / "contracts.py",
        root / "errors.py",
        root / "evidence.py",
        root / "interpretation.py",
        root / "ports.py",
        root / "url_policy.py",
    ]
    for path in core_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        for node in imports:
            names = [alias.name for alias in node.names]
            module = (
                node.module if isinstance(node, ast.ImportFrom) and node.module else ""
            )
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            assert not any(
                "adapters" in value or "apps" in value for value in names + [module]
            ), path.name


def test_package_import_does_not_require_optional_network_or_browser_modules() -> None:
    package_root = Path(__file__).parents[1] / "src"
    code = "import sys; sys.path.insert(0, sys.argv[1]); import website_collection_kit; print('ok')"
    completed = subprocess.run(
        [sys.executable, "-S", "-c", code, str(package_root)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "ok"
