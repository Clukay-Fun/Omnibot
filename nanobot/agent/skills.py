"""
描述: 智能体能力的动态挂载与描述生成器。
主要功能:
    - 从工作区和内置技能树（skills/）读取 SKILL.md。
    - 生成包含环境与依赖限制的供大模型阅读的能力目录清单。
"""

import json
import os
import re
import shutil
from pathlib import Path

# 默认的内置技能目录（相对于当前文件）
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills" / "builtin"


# region [技能加载核心类]

class SkillsLoader:
    """
    用处: 运行时技能扫描与内容提供者。

    功能:
        - 查找工作区或内置区定义的外部应用拓展配置（如飞书数据接入等指令）。
        - 组装带缺失依赖警告（requires/env）的结构化 XML 或 Markdown 挂载信息。
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        用处: 收集可供挂载的目录树节点。

        功能:
            - 遍历搜索默认内置和用户工作区的拓展，按可用性标记加载状态。

        Args:
            filter_unavailable: 如果为 True，则过滤掉不满足要求的技能。

        Returns:
            包含 'name'、'path'、'source' 的技能信息字典列表。
        """
        skills = []

        # 工作区技能（最高优先级）
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # 内置技能
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # 按照要求进行过滤
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        用处: 定点获取技能的 Markdown 全文。

        功能:
            - 解析原始文件内容供后续正则提纯和组装所用。

        Args:
            name: 技能名称（目录名）。

        Returns:
            技能内容，如果未找到则返回 None。
        """
        # 首先检查工作区
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # 检查内置技能
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        用处: 将特定开启的技能数组转为 LLM 阅读的 Prompt 文本块。

        功能:
            - 提取技能具体上下文逻辑进行拼装（已过滤去头部配置数据）。

        Args:
            skill_names: 要加载的技能名称列表。

        Returns:
            格式化后的技能内容。
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        用处: 构建 XML 格式的全局导航目录。

        功能:
            - 常用于渐进式加载 —— 智能体可以在此预览大纲，并在需要时再使用 `read_file` 深入阅读技能细则。

        Returns:
            XML 格式的技能摘要。
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # 对于不可用的技能显示缺失的要求
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    # endregion

    # region [内部辅助方法]

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """获取缺失要求的描述。"""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """从前置元数据（frontmatter）中获取技能的描述。"""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # 回退到技能名称

    def _strip_frontmatter(self, content: str) -> str:
        """从 Markdown 内容中移除 YAML 前置元数据。"""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: str) -> dict:
        """解析前置元数据中的技能 JSON 元数据（支持 nanobot 和 openclaw 键）。"""
        try:
            data = json.loads(raw)
            return data.get("nanobot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """检查是否满足技能要求（二进制文件、环境变量）。"""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """获取技能的 nanobot 元数据（缓存在前置元数据中）。"""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """获取标记为 always=true 且满足要求的技能。"""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        从技能的前置元数据中获取元数据字典。

        Args:
            name: 技能名称。

        Returns:
            元数据字典，或者 None。
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # 简单的 YAML 解析
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None

    # endregion
