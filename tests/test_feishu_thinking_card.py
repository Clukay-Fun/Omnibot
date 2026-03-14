from nanobot.feishu.thinking_card import (
    build_completed,
    build_initial,
    build_minimal,
    build_progress,
)


def test_build_initial_returns_neutral_placeholder() -> None:
    assert build_initial() == {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "note", "elements": [{"tag": "plain_text", "content": "▏ …"}]}],
    }


def test_build_progress_renders_latest_six_entries_as_note_lines() -> None:
    card = build_progress([f"entry-{i}" for i in range(8)])

    assert card["elements"] == [
        {"tag": "note", "elements": [{"tag": "plain_text", "content": f"▏ entry-{i}"}]}
        for i in range(2, 8)
    ]


def test_build_completed_appends_completion_marker() -> None:
    card = build_completed(["思考中…", "正在搜索网络：AI"])

    assert card["elements"][-1]["elements"][0]["content"] == "▏ 思考完成"
    assert {"tag": "note", "elements": [{"tag": "plain_text", "content": "▏ 正在搜索网络：AI"}]} in card["elements"]


def test_build_minimal_returns_note_with_zero_width_space() -> None:
    assert build_minimal() == {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "note", "elements": [{"tag": "plain_text", "content": "\u200b"}]}],
    }
