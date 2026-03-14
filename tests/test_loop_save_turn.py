from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.session.manager import Session, SessionManager


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._TOOL_RESULT_MAX_CHARS = 500
    return loop


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_prefers_session_user_content_over_injected_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:session-user-content")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"
    extra = ContextBuilder._EXTRA_CONTEXT_TAG + "\nProfile: likes coffee\n\nSummary: discussed billing"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": f"{runtime}\n\n{extra}\n\n您好",
            ContextBuilder._SESSION_USER_CONTENT_KEY: "您好",
        }],
        skip=0,
    )

    assert session.messages[0]["content"] == "您好"


def test_session_manager_cleans_legacy_injected_context_on_reload(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = Session(key="test:legacy-context")
    session.messages = [{
        "role": "user",
        "content": (
            f"{ContextBuilder._RUNTIME_CONTEXT_TAG}\nCurrent Time: now (UTC)\n\n"
            f"{ContextBuilder._EXTRA_CONTEXT_TAG}\n"
            "Profile: likes coffee\n\n"
            "Summary: discussed billing\n\n"
            "您好"
        ),
    }]
    manager.save(session)
    manager.invalidate(session.key)

    reloaded = manager.get_or_create("test:legacy-context")

    assert reloaded.messages[0]["content"] == "您好"
