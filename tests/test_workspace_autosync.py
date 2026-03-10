from importlib.resources import files
from typing import Any, cast

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import MemoryStore
from nanobot.agent.documents.document_extractor import load_extract_templates
from nanobot.agent.table_runtime.table_registry import TableRegistry
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel
from nanobot.config.schema import FeishuConfig, SkillSpecConfig
from nanobot.providers.base import LLMResponse
from nanobot.utils.helpers import sync_workspace_templates


class _DummyProvider:
    async def chat(self, **kwargs):
        _ = kwargs
        return LLMResponse(content="ok", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


def test_agent_loop_auto_syncs_missing_workspace_templates(tmp_path) -> None:
    AgentLoop(
        bus=MessageBus(),
        provider=cast(Any, _DummyProvider()),
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    assert (tmp_path / "BOOTSTRAP.md").exists()
    assert (tmp_path / "SOUL.md").exists()
    assert (tmp_path / "USER.md").exists()
    assert (tmp_path / "MEMORY.md").exists()
    assert (tmp_path / "runtime_texts.yaml").exists()
    assert (tmp_path / "memory" / "HISTORY.md").exists()


def test_feishu_channel_auto_syncs_missing_workspace_templates(tmp_path) -> None:
    FeishuChannel(FeishuConfig(), MessageBus(), workspace=tmp_path)

    assert (tmp_path / "BOOTSTRAP.md").exists()
    assert (tmp_path / "SOUL.md").exists()
    assert (tmp_path / "USER.md").exists()
    assert (tmp_path / "MEMORY.md").exists()
    assert (tmp_path / "runtime_texts.yaml").exists()
    assert (tmp_path / "memory" / "feishu" / "users").exists()


def test_packaged_workspace_templates_are_grouped_by_domain() -> None:
    templates = files("nanobot") / "templates"

    assert (templates / "workspace" / "BOOTSTRAP.md").is_file()
    assert (templates / "workspace" / "runtime_texts.yaml").is_file()
    assert (templates / "feishu" / "bitable_rules.yaml").is_file()
    assert (templates / "memory" / "MEMORY.md").is_file()


def test_sync_workspace_templates_keeps_external_layout_unchanged(tmp_path) -> None:
    templates = files("nanobot") / "templates"
    expected_workspace_files = {
        "AGENTS.md": templates / "workspace" / "AGENTS.md",
        "BOOTSTRAP.md": templates / "workspace" / "BOOTSTRAP.md",
        "HEARTBEAT.md": templates / "workspace" / "HEARTBEAT.md",
        "IDENTITY.md": templates / "workspace" / "IDENTITY.md",
        "MEMORY.md": templates / "memory" / "MEMORY.md",
        "SOUL.md": templates / "workspace" / "SOUL.md",
        "TOOLS.md": templates / "workspace" / "TOOLS.md",
        "USER.md": templates / "workspace" / "USER.md",
        "runtime_texts.yaml": templates / "workspace" / "runtime_texts.yaml",
        "memory/MEMORY.md": templates / "memory" / "MEMORY.md",
        "feishu/bitable_rules.yaml": templates / "feishu" / "bitable_rules.yaml",
        "extract/invoice.yaml": files("nanobot") / "skills" / "extract" / "invoice.yaml",
        "skills/table_registry.yaml": files("nanobot") / "skills" / "registry" / "table_registry.yaml",
    }
    added = set(sync_workspace_templates(tmp_path, silent=True))

    assert added >= set(expected_workspace_files) | {"memory/HISTORY.md"}
    for relative_path, source in expected_workspace_files.items():
        target = tmp_path / relative_path
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    history_path = tmp_path / "memory" / "HISTORY.md"
    assert history_path.is_file()
    assert history_path.read_text(encoding="utf-8") == ""
    assert not (tmp_path / "workspace").exists()


def test_agent_loop_reads_packaged_workspace_template_when_workspace_copy_missing(tmp_path) -> None:
    packaged = (files("nanobot") / "templates" / "workspace" / "BOOTSTRAP.md").read_text(encoding="utf-8")
    loop = AgentLoop(
        bus=MessageBus(),
        provider=cast(Any, _DummyProvider()),
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )
    (tmp_path / "BOOTSTRAP.md").unlink()

    assert loop._read_workspace_or_template_file("BOOTSTRAP.md") == packaged


def test_memory_store_bootstraps_private_persona_from_packaged_templates_when_workspace_copy_missing(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory = MemoryStore(workspace)

    expected_soul = (files("nanobot") / "templates" / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    expected_memory = (files("nanobot") / "templates" / "memory" / "MEMORY.md").read_text(encoding="utf-8")

    soul_path = memory.ensure_feishu_user_persona_file("ou_user", "SOUL.md")
    memory_path = memory.ensure_feishu_user_persona_file("ou_user", "MEMORY.md")

    assert soul_path.read_text(encoding="utf-8") == expected_soul
    assert memory_path.read_text(encoding="utf-8") == expected_memory


def test_document_extractor_ignores_repo_root_style_extract_duplicates(tmp_path, monkeypatch) -> None:
    (tmp_path / "extract").mkdir()
    (tmp_path / "extract" / "invoice.yaml").write_text(
        "\n".join(
            [
                "id: repo_root_duplicate",
                "document_type: invoice",
                "fields:",
                "  - name: invoice_number",
                "    required: true",
                "    patterns:",
                "      - 'Duplicate[: ]+([A-Z0-9-]+)'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    templates = load_extract_templates()

    assert templates["invoice"].template_id == "invoice_minimal"


def test_table_registry_ignores_repo_root_style_skills_duplicates(tmp_path, monkeypatch) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "table_registry.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "tables:",
                "  case_registry:",
                "    display_name: repo-root-duplicate",
                "    field_aliases:",
                "      status: duplicate-status",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    registry = TableRegistry(workspace=tmp_path / "workspace")

    assert registry.map_field("case_registry", "status") == "案件状态"
