"""Tests for cache-friendly prompt construction."""

from __future__ import annotations

import base64
import datetime as datetime_module
import os
from datetime import datetime as real_datetime
from importlib.resources import files as pkg_files
from io import BytesIO
from pathlib import Path

from PIL import Image

from nanobot.agent.context import ContextBuilder


class _FakeDatetime(real_datetime):
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_bootstrap_files_are_backed_by_templates() -> None:
    template_dir = pkg_files("nanobot") / "templates"

    for filename in ContextBuilder.BOOTSTRAP_FILES:
        assert (template_dir / filename).is_file(), f"missing bootstrap template: {filename}"


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """System prompt should not change just because wall clock minute changes."""
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_system_prompt_instructs_model_to_avoid_tools_for_small_talk(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "# 规则层（必须遵守）" in prompt
    assert "## 运行契约" in prompt
    assert "`规则层 > WORKLOG > MEMORY`" in prompt
    assert "# 当前工作层（操作面板）" not in prompt
    assert "# 参考记忆层（可引用但不是硬规则）" not in prompt
    assert "Reply directly when the user is making small talk, greeting you, acknowledging something, asking who you are, or making a conversational remark that does not ask for information or action." in prompt
    assert "Use tools when the user is asking you to obtain current, external, or workspace-specific information, or to perform an action that requires tools." in prompt
    assert "If the user's intent is to get up-to-date facts, such as today's weather, latest news, or current prices, proactively use relevant tools." in prompt
    assert "Do not use tools just because topics like weather, news, or prices are mentioned in casual conversation." in prompt
    assert "The system information above already includes user profile, long-term memory, any available worklog snapshot, and any available Feishu integration context." in prompt
    assert "WORKLOG.md is the source of truth for current work items." in prompt
    assert "`memory/MEMORY.md` 可能过时" in prompt
    assert "普通对话不要预加载 `memory/HISTORY.md` 或 `HEARTBEAT.md`" in prompt
    assert "`memory/HISTORY.md` 只用于明确的历史回查" in prompt
    assert "回复用户优先。不要为了维护文件而延迟正常答复。" in prompt
    assert "如果本轮暴露了稳定偏好或长期背景，在同一轮更新 `USER.md` 或 `memory/MEMORY.md`。" in prompt
    assert "“记住”就意味着同一轮写入对应文件；不要只在回复里说“已记住”而不落盘。" in prompt
    assert "如果你说了“已记住”却没有更新 `USER.md` 或 `memory/MEMORY.md`" in prompt
    assert "不要把“正在处理”“稍后给你结果”这类占位话术当作正式回复正文发给用户" in prompt
    assert "如果 `WORKLOG.md` 不存在、为空，或没有可用 snapshot，就直接跳过“当前工作层”" in prompt
    assert "When updating WORKLOG.md, follow the format defined in the file exactly." in prompt
    assert "Do not add extra fields to WORKLOG.md such as `阻塞`, `进展`, `截止日期`, `负责人`, `标签`, or numbered list prefixes." in prompt
    assert "If WORKLOG.md is currently in a legacy or malformed format, rewrite it into the canonical three-field schema while updating it." in prompt
    assert "If the user says a previously recorded next step is now complete, update the parent item's `状态/下一步` or move the parent item to `已完成` when the whole item is done." in prompt
    assert "Do not update WORKLOG.md for casual chat, one-off Q&A, or replies that do not change ongoing work state." in prompt
    assert "Only read USER.md, BOOTSTRAP.md, MEMORY.md, HISTORY.md, or WORKLOG.md when the user explicitly asks to inspect or modify those files" in prompt
    assert "For mutable workspace or external state, such as Feishu tables, records, calendars, documents, files, or other resources that may have changed since earlier turns, do not answer from memory or prior tool results." in prompt
    assert "On emoji-capable chat platforms" not in prompt
    assert "materially help answer the user's request" not in prompt


def test_system_prompt_tells_model_to_answer_from_skills_summary(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "# Skills" in prompt
    assert "If the user asks what skills or built-in capabilities you currently have, answer directly from this skills summary." in prompt
    assert "Do not claim that you cannot see your own skills, default skills, or built-in capabilities when they are listed here." in prompt
    assert "feishu-workspace" in prompt
    assert "feishu-ocr" not in prompt
    assert "feishu-weekly-report" not in prompt


def test_system_prompt_includes_explicitly_requested_skills(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(skill_names=["feishu-ocr"])

    assert "# Active Skills" in prompt
    assert "# Feishu OCR Compatibility Shim" in prompt
    assert "perception.ocr" in prompt
    assert "多张图片只处理第一张" in prompt


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """Runtime metadata should be merged with the user message."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    # Runtime context is now merged with user message into a single message
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in user_content
    assert "Current Time:" in user_content
    assert "Channel: cli" in user_content
    assert "Chat ID: direct" in user_content
    assert "Return exactly: OK" in user_content


def test_runtime_context_includes_selected_feishu_metadata(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Add me as collaborator",
        channel="feishu",
        chat_id="ou_user_1",
        runtime_metadata={
            "user_open_id": "ou_user_1",
            "tenant_key": "tenant-1",
            "chat_type": "p2p",
            "content_json": {"text": "ignored"},
        },
    )

    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert "Feishu User Open ID: ou_user_1" in user_content
    assert "Feishu Tenant Key: tenant-1" in user_content
    assert "Feishu Chat Type: p2p" in user_content
    assert "content_json" not in user_content


def test_multimodal_user_message_places_text_before_images(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jx7kAAAAASUVORK5CYII="))

    messages = builder.build_messages(
        history=[],
        current_message="Describe this image.",
        media=[str(image_path)],
        channel="feishu",
        chat_id="direct",
    )

    user_content = messages[-1]["content"]
    assert isinstance(user_content, list)
    assert [item["type"] for item in user_content] == ["text", "text", "image_url"]
    assert user_content[1]["text"] == "Describe this image."


def test_prepare_image_for_model_reencodes_oversized_images() -> None:
    raw_image = Image.frombytes("RGB", (512, 512), os.urandom(512 * 512 * 3))
    image_path = BytesIO()
    raw_image.save(image_path, format="PNG")
    original = image_path.getvalue()

    processed, mime = ContextBuilder._prepare_image_for_model(original, "image/png")

    assert len(original) > ContextBuilder._MODEL_IMAGE_MAX_BYTES
    assert mime == "image/jpeg"
    assert len(processed) < len(original)
    assert len(processed) <= ContextBuilder._MODEL_IMAGE_MAX_BYTES


def test_extra_context_is_merged_into_user_message(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Answer briefly",
        channel="feishu",
        chat_id="oc_chat_1",
        extra_context=["Profile: likes coffee", "Summary: discussed billing"],
    )

    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert "[Extra Context" in user_content
    assert "Profile: likes coffee" in user_content
    assert "Summary: discussed billing" in user_content
    assert "Answer briefly" in user_content


def test_extra_system_messages_are_inserted_after_primary_system_prompt(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[{"role": "assistant", "content": "recent raw turn"}],
        current_message="Answer briefly",
        channel="feishu",
        chat_id="oc_chat_1",
        extra_system_messages=["Earlier summary goes here.", "Heartbeat execution note."],
    )

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "system", "content": "Earlier summary goes here."}
    assert messages[2] == {"role": "system", "content": "Heartbeat execution note."}
    assert messages[3] == {"role": "assistant", "content": "recent raw turn"}
    assert messages[-1]["role"] == "user"


def test_system_prompt_uses_overlay_files_for_dm_persona(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("global agents", encoding="utf-8")
    (workspace / "SOUL.md").write_text("global soul", encoding="utf-8")
    (workspace / "USER.md").write_text("global user", encoding="utf-8")
    (workspace / "BOOTSTRAP.md").write_text("global bootstrap", encoding="utf-8")
    (workspace / "TOOLS.md").write_text("global tools", encoding="utf-8")

    overlay = workspace / "users" / "feishu" / "tenant-1" / "ou_user_1"
    overlay.mkdir(parents=True)
    (overlay / "USER.md").write_text("overlay user", encoding="utf-8")
    (overlay / "BOOTSTRAP.md").write_text("overlay bootstrap", encoding="utf-8")

    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt(system_overlay_root=overlay, system_overlay_bootstrap=True)

    assert "global agents" in prompt
    assert "global soul" in prompt
    assert "global tools" in prompt
    assert "overlay user" in prompt
    assert "overlay bootstrap" in prompt
    assert "global user" not in prompt
    assert "global bootstrap" not in prompt
    assert f"Common prompt files: {workspace}/AGENTS.md, {workspace}/SOUL.md, {workspace}/TOOLS.md" in prompt
    assert f"User-scoped prompt files: {overlay}/USER.md, {overlay}/BOOTSTRAP.md" in prompt
    assert f"User-scoped worklog: {overlay}/WORKLOG.md" in prompt
    assert f"User-scoped long-term memory: {overlay}/memory/MEMORY.md" in prompt


def test_system_prompt_includes_worklog_and_memory_in_explicit_layers(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "WORKLOG.md").write_text(
        "## 进行中\n\n### 收紧运行契约\n- 优先级：高\n- 状态/下一步：调整 context\n",
        encoding="utf-8",
    )
    (workspace / "memory").mkdir()
    (workspace / "memory" / "MEMORY.md").write_text("长期背景：正在做 Feishu bot", encoding="utf-8")

    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt()

    assert "# 规则层（必须遵守）" in prompt
    assert "# 当前工作层（操作面板）" in prompt
    assert "## Worklog Snapshot" in prompt
    assert "收紧运行契约" in prompt
    assert "# 参考记忆层（可引用但不是硬规则）" in prompt
    assert "长期背景：正在做 Feishu bot" in prompt
    assert prompt.index("# 规则层（必须遵守）") < prompt.index("# 当前工作层（操作面板）")
    assert prompt.index("# 当前工作层（操作面板）") < prompt.index("# 参考记忆层（可引用但不是硬规则）")


def test_system_prompt_skips_bootstrap_when_overlay_is_initialized(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("global agents", encoding="utf-8")
    (workspace / "SOUL.md").write_text("global soul", encoding="utf-8")
    (workspace / "USER.md").write_text("global user", encoding="utf-8")
    (workspace / "BOOTSTRAP.md").write_text("global bootstrap", encoding="utf-8")
    (workspace / "TOOLS.md").write_text("global tools", encoding="utf-8")

    overlay = workspace / "users" / "feishu" / "tenant-1" / "ou_user_1"
    overlay.mkdir(parents=True)
    (overlay / "USER.md").write_text("overlay user", encoding="utf-8")
    (overlay / "BOOTSTRAP.md").write_text("overlay bootstrap", encoding="utf-8")

    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt(system_overlay_root=overlay, system_overlay_bootstrap=False)

    assert "overlay user" in prompt
    assert "overlay bootstrap" not in prompt
    assert "global bootstrap" not in prompt


def test_system_prompt_includes_worklog_snapshot_without_completed_items(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "WORKLOG.md").write_text(
        "# WORKLOG.md - 当前工作面板\n\n"
        "## 进行中\n\n"
        "### 补 per-user worklog\n- 优先级：高\n- 状态/下一步：更新 prompt\n\n"
        "## 待处理\n\n"
        "### 清理飞书 summary 注入\n- 优先级：中\n- 状态/下一步：加 handler 测试\n\n"
        "## 已完成\n\n"
        "### 画出 v0 边界\n- 优先级：高\n- 状态/下一步：已完成\n",
        encoding="utf-8",
    )

    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt()

    assert "# Worklog Snapshot" in prompt
    assert "补 per-user worklog" in prompt
    assert "清理飞书 summary 注入" in prompt
    assert "画出 v0 边界" not in prompt
