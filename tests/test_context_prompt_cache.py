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

    assert "Reply directly when the user is making small talk, greeting you, acknowledging something, asking who you are, or making a conversational remark that does not ask for information or action." in prompt
    assert "Use tools when the user is asking you to obtain current, external, or workspace-specific information, or to perform an action that requires tools." in prompt
    assert "If the user's intent is to get up-to-date facts, such as today's weather, latest news, or current prices, proactively use relevant tools." in prompt
    assert "Do not use tools just because topics like weather, news, or prices are mentioned in casual conversation." in prompt
    assert "On emoji-capable chat platforms" not in prompt
    assert "materially help answer the user's request" not in prompt


def test_system_prompt_includes_explicitly_requested_skills(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(skill_names=["feishu-ocr"])

    assert "# Active Skills" in prompt
    assert "# Feishu OCR" in prompt
    assert "only process the first image" in prompt
    assert "Do not use scripts or external OCR tools for v1." in prompt


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
    assert f"User-scoped long-term memory: {overlay}/memory/MEMORY.md" in prompt


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
