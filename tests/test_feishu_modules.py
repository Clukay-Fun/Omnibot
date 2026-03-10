from nanobot.feishu.client import FeishuClient
from nanobot.feishu.renderer import FeishuRenderer
from nanobot.feishu.websocket import register_optional_event


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def _table(label: str) -> dict:
    return {
        "tag": "table",
        "columns": [{"tag": "column", "name": "c0", "display_name": label, "width": "auto"}],
        "rows": [{"c0": label}],
        "page_size": 2,
    }


def test_renderer_detects_text_and_interactive_content() -> None:
    assert FeishuRenderer.detect_msg_format("hello") == "text"
    assert FeishuRenderer.detect_msg_format("# Heading") == "interactive"


def test_renderer_splits_multiple_tables() -> None:
    chunks = FeishuRenderer.split_elements_by_table_limit([
        _md("intro"),
        _table("A"),
        _md("between"),
        _table("B"),
    ])

    assert len(chunks) == 2
    assert chunks[0] == [_md("intro"), _table("A"), _md("between")]
    assert chunks[1] == [_table("B")]


def test_register_optional_event_keeps_builder_when_missing() -> None:
    class Builder:
        pass

    builder = Builder()
    assert register_optional_event(builder, "missing", object()) is builder


def test_feishu_client_resolves_receive_id_type() -> None:
    assert FeishuClient.resolve_receive_id_type("oc_123") == "chat_id"
    assert FeishuClient.resolve_receive_id_type("ou_123") == "open_id"
