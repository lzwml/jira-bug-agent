from __future__ import annotations

from pathlib import Path

import pytest

from bug_agent.skills import SkillRegistry


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


def test_registry_loads_builtin_skill_and_frontmatter():
    item = SkillRegistry(SKILLS_ROOT).load("android-black-screen")
    assert item.name == "android-black-screen"
    assert "SurfaceFlinger" in item.instructions
    assert "black-screen" in item.description


def test_registry_deduplicates_skills_in_stable_order():
    prompt, names = SkillRegistry(SKILLS_ROOT).render([
        "android-log-triage", "android-black-screen", "android-log-triage",
    ])
    assert names == ["android-log-triage", "android-black-screen"]
    assert prompt.index("android-log-triage") < prompt.index("android-black-screen")


@pytest.mark.parametrize("name", ["../secret", "Missing", "not-found"])
def test_registry_rejects_unsafe_or_missing_skill(name):
    with pytest.raises(ValueError):
        SkillRegistry(SKILLS_ROOT).load(name)

