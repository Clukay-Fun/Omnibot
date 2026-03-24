from __future__ import annotations

from types import SimpleNamespace

from nanobot.feishu.payload import read_path


def test_read_path_supports_mixed_dict_and_object_payloads() -> None:
    payload = {
        "event": SimpleNamespace(
            message={"message_id": "om_1"},
            sender=SimpleNamespace(sender_id={"open_id": "ou_user_1"}),
        )
    }

    assert read_path(payload, "event", "message", "message_id") == "om_1"
    assert read_path(payload, "event", "sender", "sender_id", "open_id") == "ou_user_1"
    assert read_path(payload, "event", "missing") is None
