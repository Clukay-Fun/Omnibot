from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from nanobot.agent.skills import SkillsLoader

SKILL_DIR = Path("nanobot/skills/feishu-weekly-report").resolve()
QUICK_VALIDATE_PATH = Path("nanobot/skills/skill-creator/scripts/quick_validate.py").resolve()


quick_validate_spec = importlib.util.spec_from_file_location("feishu_weekly_report_quick_validate", QUICK_VALIDATE_PATH)
quick_validate = importlib.util.module_from_spec(quick_validate_spec)
assert quick_validate_spec and quick_validate_spec.loader
sys.modules["feishu_weekly_report_quick_validate"] = quick_validate
quick_validate_spec.loader.exec_module(quick_validate)


def test_feishu_weekly_report_skill_is_valid_and_discoverable(tmp_path: Path) -> None:
    valid, message = quick_validate.validate_skill(SKILL_DIR)
    assert valid, message

    skills = SkillsLoader(tmp_path).list_skills(filter_unavailable=False)
    skill = next(skill for skill in skills if skill["name"] == "feishu-weekly-report")
    assert skill["deprecated"] is True


def test_feishu_weekly_report_skill_contains_required_boundaries() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "旧入口兼容壳" in skill_md
    assert "workflows.weekly_report" in skill_md
    assert "v1 每次只处理一张表" in skill_md
    assert "受控 `doc create_blocks`" in skill_md
    assert "固定使用 `edit`" in skill_md


def test_feishu_weekly_report_reference_locks_template_sections() -> None:
    template = (SKILL_DIR / "references" / "report-template.md").read_text(encoding="utf-8")

    for heading in ("本周概览", "重点进展", "风险/阻塞", "下周计划", "附录/原始条目摘要"):
        assert heading in template

    assert "不要使用：" in template
    assert "代码块" in template
