from pathlib import Path

from nanobot.agent.worklog import WorklogStore


def test_build_snapshot_limits_visible_sections_and_skips_completed(tmp_path: Path) -> None:
    worklog = tmp_path / "WORKLOG.md"
    worklog.write_text(
        "# WORKLOG.md - 当前工作面板\n\n"
        "## 进行中\n\n"
        + "\n\n".join(
            f"### 进行中事项 {i}\n- 优先级：高\n- 状态/下一步：继续推进 {i}"
            for i in range(1, 7)
        )
        + "\n\n## 待处理\n\n"
        + "\n\n".join(
            f"### 待处理事项 {i}\n- 优先级：中\n- 状态/下一步：稍后处理 {i}"
            for i in range(1, 5)
        )
        + "\n\n## 已完成\n\n"
        + "\n\n".join(
            f"### 已完成事项 {i}\n- 优先级：低\n- 状态/下一步：已完成"
            for i in range(1, 4)
        ),
        encoding="utf-8",
    )

    snapshot = WorklogStore(tmp_path).build_snapshot()

    assert "进行中事项 1" in snapshot
    assert "进行中事项 5" in snapshot
    assert "进行中事项 6" not in snapshot
    assert "待处理事项 1" in snapshot
    assert "待处理事项 2" not in snapshot
    assert "已完成事项" not in snapshot


def test_build_snapshot_falls_back_to_raw_prefix_when_worklog_is_malformed(tmp_path: Path) -> None:
    worklog = tmp_path / "WORKLOG.md"
    raw = "not a structured worklog\n" + ("x" * 2600)
    worklog.write_text(raw, encoding="utf-8")

    snapshot = WorklogStore(tmp_path).build_snapshot()

    assert snapshot.startswith("not a structured worklog")
    assert snapshot.endswith("[truncated]")
    assert len(snapshot) <= 2500


def test_normalize_content_rewrites_legacy_numbered_worklog_to_canonical_schema() -> None:
    legacy = """# WORKLOG.md - 当前工作面板

## 进行中

1. 把 WORKLOG 的格式约束再压硬

   - 优先级：高
   - 状态/下一步：在 system prompt 中明确禁止使用“阻塞/进展”等字段
   - 进展：模板注释已加

## 待处理

1. 控制 system prompt 膨胀

   - 状态/下一步：尚未开始（待制定切入点）
   - 优先级：高
   - 阻塞：无

## 已完成

1. WORKLOG 快照裁剪
"""

    normalized = WorklogStore.normalize_content(legacy)

    assert "### 把 WORKLOG 的格式约束再压硬" in normalized
    assert "1. 把 WORKLOG 的格式约束再压硬" not in normalized
    assert "- 优先级：高" in normalized
    assert "- 状态/下一步：在 system prompt 中明确禁止使用“阻塞/进展”等字段；进展：模板注释已加" in normalized
    assert "- 阻塞：" not in normalized
    assert "- 进展：" not in normalized


def test_normalize_content_drops_template_placeholder_items() -> None:
    content = """# WORKLOG.md - 当前工作面板

## 进行中

### 真正事项
- 优先级：高
- 状态/下一步：继续推进

## 待处理

## 已完成

### 事项标题
- 优先级：高
- 状态/下一步：已完成；如有必要，可补一句收尾说明
"""

    normalized = WorklogStore.normalize_content(content)

    assert "### 真正事项" in normalized
    assert "\n\n### 事项标题\n- 优先级：高\n- 状态/下一步：已完成；如有必要，可补一句收尾说明" not in normalized
