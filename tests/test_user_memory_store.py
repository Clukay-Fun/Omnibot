"""描述:
主要功能:
    - 验证用户记忆存储路径和更新逻辑。
"""

import importlib
import importlib.util

from nanobot.agent.user_state.user_memory import UserMemoryStore


#region 用户记忆测试


def test_user_memory_path_and_rw(tmp_path) -> None:
    """用处，参数

    功能:
        - 校验用户记忆文件路径和读写行为。
    """
    store = UserMemoryStore(tmp_path)

    path = store.path_for("slack", "U123")
    assert path == tmp_path / "memory" / "users" / "slack__U123.json"

    store.write("slack", "U123", {"name": "Ada"})
    loaded = store.read("slack", "U123")

    assert loaded == {"name": "Ada"}


def test_user_memory_update_merges_payload(tmp_path) -> None:
    """用处，参数

    功能:
        - 校验更新操作会合并并覆盖字段。
    """
    store = UserMemoryStore(tmp_path)

    store.write("telegram", "42", {"city": "Shenzhen", "lang": "en"})
    updated = store.update("telegram", "42", {"lang": "zh", "team": "ops"})

    assert updated == {"city": "Shenzhen", "lang": "zh", "team": "ops"}


def test_legacy_skill_runtime_package_is_removed() -> None:
    assert importlib.util.find_spec("nanobot.agent.skill_runtime") is None


def test_rehomed_runtime_namespaces_are_available() -> None:
    assert importlib.import_module("nanobot.agent.reminders.reminder_runtime")
    assert importlib.import_module("nanobot.agent.reminders.bitable_reminder_engine")
    assert importlib.import_module("nanobot.agent.documents.document_extractor")
    assert importlib.import_module("nanobot.agent.documents.document_classifier")
    assert importlib.import_module("nanobot.agent.documents.document_pipeline")
    assert importlib.import_module("nanobot.agent.documents.mineru_client")
    assert importlib.import_module("nanobot.agent.table_runtime.table_registry")
    assert importlib.import_module("nanobot.agent.table_runtime.table_profile_cache")
    assert importlib.import_module("nanobot.agent.table_runtime.table_profile_synthesizer")
    assert importlib.import_module("nanobot.agent.user_state.user_memory")
#endregion
