from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "bug_agent"
DOMAIN_ROOT = PACKAGE_ROOT / "domain"
INFRASTRUCTURE_ROOT = PACKAGE_ROOT / "infrastructure"
PERSISTENCE_ROOT = INFRASTRUCTURE_ROOT / "persistence"
APPLICATION_ROOT = PACKAGE_ROOT / "application"
PRESENTATION_ROOT = PACKAGE_ROOT / "presentation"
AGENT_CORE_ROOT = PACKAGE_ROOT / "agent_core"
INTERFACES_ROOT = PACKAGE_ROOT / "interfaces"
API_ROOT = PACKAGE_ROOT / "api"


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return (node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)))


def test_domain_does_not_depend_on_outer_bug_agent_layers():
    violations = []
    for path in DOMAIN_ROOT.glob("*.py"):
        for node in _imports(path):
            if isinstance(node, ast.ImportFrom):
                if node.level > 1:
                    violations.append(f"{path.name}:{node.lineno} relative import escapes domain")
                if node.level == 0 and node.module and node.module.startswith("bug_agent."):
                    if not node.module.startswith("bug_agent.domain"):
                        violations.append(f"{path.name}:{node.lineno} imports {node.module}")
            else:
                for alias in node.names:
                    if alias.name.startswith("bug_agent.") and not alias.name.startswith(
                        "bug_agent.domain"
                    ):
                        violations.append(f"{path.name}:{node.lineno} imports {alias.name}")

    assert violations == []


def test_infrastructure_adapters_only_depend_on_domain_or_infrastructure():
    violations = []
    for path in INFRASTRUCTURE_ROOT.glob("*.py"):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level > 2:
                violations.append(f"{path.name}:{node.lineno} escapes package boundary")
            if node.level == 2 and node.module and not node.module.startswith(
                ("domain", "agent_core")
            ):
                violations.append(f"{path.name}:{node.lineno} imports outer {node.module}")
            if node.level == 0 and node.module and node.module.startswith("bug_agent."):
                if not node.module.startswith(
                    ("bug_agent.agent_core", "bug_agent.domain", "bug_agent.infrastructure")
                ):
                    violations.append(f"{path.name}:{node.lineno} imports {node.module}")

    assert violations == []


def test_persistence_does_not_depend_on_application_or_interfaces():
    violations = []
    for path in PERSISTENCE_ROOT.glob("*.py"):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level > 3:
                violations.append(f"{path.name}:{node.lineno} escapes package boundary")
            if node.level == 3 and node.module and not node.module.startswith("domain"):
                violations.append(f"{path.name}:{node.lineno} imports outer {node.module}")
            if node.level == 2 and node.module and node.module != "config":
                violations.append(f"{path.name}:{node.lineno} imports infrastructure {node.module}")

    assert violations == []


def test_application_only_uses_declared_lower_level_or_core_modules():
    allowed_outer = {
        "agent_core", "domain", "infrastructure", "presentation",
    }
    violations = []
    for path in APPLICATION_ROOT.glob("*.py"):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level > 2:
                violations.append(f"{path.name}:{node.lineno} escapes package boundary")
            if node.level == 2 and node.module:
                owner = node.module.split(".", 1)[0]
                if owner not in allowed_outer:
                    violations.append(f"{path.name}:{node.lineno} imports outer {node.module}")

    assert violations == []


def test_presentation_does_not_depend_on_interfaces():
    violations = []
    for path in PRESENTATION_ROOT.glob("*.py"):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom) or node.level != 2 or not node.module:
                continue
            owner = node.module.split(".", 1)[0]
            if owner in {"api", "cli"}:
                violations.append(f"{path.name}:{node.lineno} imports interface {node.module}")

    assert violations == []


def test_agent_core_only_depends_on_domain_and_its_own_ports():
    violations = []
    for path in AGENT_CORE_ROOT.glob("*.py"):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level > 2:
                violations.append(f"{path.name}:{node.lineno} escapes package boundary")
            if node.level == 2 and node.module and not node.module.startswith("domain"):
                violations.append(f"{path.name}:{node.lineno} imports outer {node.module}")
            if node.level == 0 and node.module and node.module.startswith("bug_agent."):
                if not node.module.startswith(("bug_agent.agent_core", "bug_agent.domain")):
                    violations.append(f"{path.name}:{node.lineno} imports {node.module}")

    assert violations == []


def test_interface_modules_use_declared_layers_not_root_facades():
    allowed_outer = {"agent_core", "application", "domain", "infrastructure", "presentation"}
    violations = []
    for root in (INTERFACES_ROOT, API_ROOT):
        for path in root.glob("*.py"):
            for node in _imports(path):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.level > 2:
                    violations.append(f"{path.name}:{node.lineno} escapes package boundary")
                if node.level == 2 and node.module:
                    owner = node.module.split(".", 1)[0]
                    if owner not in allowed_outer:
                        violations.append(f"{path.name}:{node.lineno} imports facade {node.module}")

    assert violations == []


def test_package_root_contains_no_flat_implementation_modules():
    root_modules = sorted(path.name for path in PACKAGE_ROOT.glob("*.py"))
    assert root_modules == ["__init__.py"]
