from __future__ import annotations

from pathlib import Path

import pytest

from bug_agent.skills import SkillRegistry


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


def test_registry_loads_builtin_skill_and_frontmatter():
    item = SkillRegistry(SKILLS_ROOT).load("android-black-screen")
    assert item.name == "android-black-screen"
    assert item.category == "symptom"
    assert "SurfaceFlinger" in item.instructions
    assert "black-screen" in item.description


def test_registry_discovers_categorized_skill_catalog():
    catalog = SkillRegistry(SKILLS_ROOT).discover()

    assert {item.name for item in catalog} >= {
        "android-log-triage", "android-black-screen", "mtk-ivi-log-analysis",
    }
    categories = {item.name: item.category for item in catalog}
    assert categories["android-log-triage"] == "base"
    assert categories["mtk-ivi-log-analysis"] == "platform"


def test_registry_deduplicates_skills_in_stable_order():
    prompt, names = SkillRegistry(SKILLS_ROOT).render([
        "android-log-triage", "android-black-screen", "android-log-triage",
    ])
    assert names == ["android-log-triage", "android-black-screen"]
    assert prompt.index("android-log-triage") < prompt.index("android-black-screen")


def test_registry_rejects_multiple_preconfigured_symptom_skills():
    with pytest.raises(ValueError, match="一个主要症状"):
        SkillRegistry(SKILLS_ROOT).render([
            "android-black-screen", "android-anr-ui-freeze",
        ])


def test_mtk_ivi_skill_keeps_route_selection_in_the_runtime_entrypoint():
    item = SkillRegistry(SKILLS_ROOT).load("mtk-ivi-log-analysis")

    # The current Worker injects SKILL.md only. These investigation invariants
    # must therefore remain in the entrypoint instead of living solely in refs.
    assert "android-anr-ui-freeze" in item.instructions
    assert "system-reboot-watchdog" in item.instructions
    assert "can-mcu-signal-analysis" in item.instructions
    assert "Respect clock domains" in item.instructions
    assert "insufficient_evidence" in item.instructions
    assert "inspect_archive" in item.instructions
    assert "extract_archive_members" in item.instructions
    assert "prepare_case" in item.instructions


def test_mtk_ivi_skill_references_are_present():
    reference_root = SKILLS_ROOT / "mtk-ivi-log-analysis" / "references"
    expected = {
        "android-vm.md",
        "linux-vm.md",
            "clock-domains.md",
                "archive-safety.md",
                "aplog-archive-selection.md",
                "sos-archive-selection.md",
                "symptom-routing.md",
    }

    assert {path.name for path in reference_root.glob("*.md")} == expected


@pytest.mark.parametrize("name", [
    "android-anr-ui-freeze",
    "android-native-crash",
    "system-reboot-watchdog",
    "linux-virtualization-failure",
    "can-mcu-signal-analysis",
    "ota-pki-connectivity",
])
def test_symptom_route_skills_are_independently_loadable(name):
    item = SkillRegistry(SKILLS_ROOT).load(name)

    assert item.description
    assert "open_case" in item.instructions
    assert "inspect_case" in item.instructions


@pytest.mark.parametrize("name", ["../secret", "Missing", "not-found"])
def test_registry_rejects_unsafe_or_missing_skill(name):
    with pytest.raises(ValueError):
        SkillRegistry(SKILLS_ROOT).load(name)
