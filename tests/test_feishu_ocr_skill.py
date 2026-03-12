from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from nanobot.agent.skills import SkillsLoader

SKILL_DIR = Path("nanobot/skills/feishu-ocr").resolve()
QUICK_VALIDATE_PATH = Path("nanobot/skills/skill-creator/scripts/quick_validate.py").resolve()


quick_validate_spec = importlib.util.spec_from_file_location("feishu_ocr_quick_validate", QUICK_VALIDATE_PATH)
quick_validate = importlib.util.module_from_spec(quick_validate_spec)
assert quick_validate_spec and quick_validate_spec.loader
sys.modules["feishu_ocr_quick_validate"] = quick_validate
quick_validate_spec.loader.exec_module(quick_validate)


def test_feishu_ocr_skill_is_valid_and_discoverable(tmp_path: Path) -> None:
    valid, message = quick_validate.validate_skill(SKILL_DIR)
    assert valid, message

    skills = SkillsLoader(tmp_path).list_skills(filter_unavailable=False)
    assert any(skill["name"] == "feishu-ocr" for skill in skills)


def test_feishu_ocr_skill_contains_required_boundaries() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "If the current message has no image, stop and ask the user to send or resend the image." in skill_md
    assert "only process the first image" in skill_md
    assert "Do not invent unreadable or missing text." in skill_md
    assert "low confidence" in skill_md
    assert "Do not use web search or external lookup to verify company authenticity." in skill_md


def test_feishu_ocr_references_exist_and_define_output_templates() -> None:
    general = (SKILL_DIR / "references" / "mode-general.md").read_text(encoding="utf-8")
    enterprise = (SKILL_DIR / "references" / "mode-enterprise.md").read_text(encoding="utf-8")

    assert "## Output Template" in general
    assert "### 原文转写" in general
    assert "### 结构化要点" in general
    assert "### 不确定项" in general

    assert "### 企业信息字段表" in enterprise
    assert "### 原文摘录" in enterprise
    assert "### 不确定项" in enterprise
