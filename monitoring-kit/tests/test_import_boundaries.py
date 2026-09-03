import ast
from pathlib import Path


def test_core_does_not_import_adapters_extensions_integrations_or_apps():
    root = Path(__file__).resolve().parents[1] / "src" / "monitoring_kit"
    forbidden_prefixes = ("monitoring_kit.adapters", "monitoring_kit.extensions", "monitoring_kit.integrations", "apps")
    core_files = list((root / "collection").glob("*.py")) + list((root / "history").glob("*.py"))
    for path in core_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(name.startswith(prefix) for name in names for prefix in forbidden_prefixes), path
