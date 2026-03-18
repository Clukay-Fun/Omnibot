from nanobot.channels.feishu import _extract_post_content
from nanobot.feishu.parser import _extract_interactive_content
from nanobot.feishu.websocket import register_optional_event


def test_extract_post_content_supports_post_wrapper_shape() -> None:
    payload = {
        "post": {
            "zh_cn": {
                "title": "日报",
                "content": [
                    [
                        {"tag": "text", "text": "完成"},
                        {"tag": "img", "image_key": "img_1"},
                    ]
                ],
            }
        }
    }

    text, image_keys = _extract_post_content(payload)

    assert text == "日报 完成"
    assert image_keys == ["img_1"]


def test_extract_post_content_keeps_direct_shape_behavior() -> None:
    payload = {
        "title": "Daily",
        "content": [
            [
                {"tag": "text", "text": "report"},
                {"tag": "img", "image_key": "img_a"},
                {"tag": "img", "image_key": "img_b"},
            ]
        ],
    }

    text, image_keys = _extract_post_content(payload)

    assert text == "Daily report"
    assert image_keys == ["img_a", "img_b"]


def test_register_optional_event_keeps_builder_when_method_missing() -> None:
    class Builder:
        pass

    builder = Builder()
    same = register_optional_event(builder, "missing", object())
    assert same is builder


def test_register_optional_event_calls_supported_method() -> None:
    called = []

    class Builder:
        def register_event(self, handler):
            called.append(handler)
            return self

    builder = Builder()
    handler = object()
    same = register_optional_event(builder, "register_event", handler)

    assert same is builder
    assert called == [handler]


def test_extract_interactive_content_supports_card_v2_body_elements() -> None:
    payload = {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "待办提醒",
            }
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "请尽快处理事项。"},
                {"tag": "markdown", "content": "- 步骤 1\n- 步骤 2"},
            ]
        },
    }

    parts = _extract_interactive_content(payload)

    assert "title: 待办提醒" in parts
    assert "请尽快处理事项。" in parts
    assert "- 步骤 1\n- 步骤 2" in parts
