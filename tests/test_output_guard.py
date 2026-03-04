from nanobot.agent.skill_runtime.output_guard import ContinuationCache, OutputGuard


def test_output_guard_truncates_text_and_recovers_continuation() -> None:
    guard = OutputGuard(ContinuationCache(ttl_seconds=60))

    result = guard.guard_text("abcdefgh", max_chars=3)
    assert result.truncated is True
    assert result.content == "abc"
    assert result.remaining_chars == 5
    assert result.continuation_token is not None

    remaining = guard.continue_from(result.continuation_token)
    assert remaining == "defgh"
    assert guard.continue_from(result.continuation_token) is None


def test_output_guard_items_and_expired_continuation() -> None:
    now = {"t": 10.0}

    def _now() -> float:
        return now["t"]

    cache = ContinuationCache(ttl_seconds=2, now_fn=_now)
    guard = OutputGuard(cache)

    result = guard.guard_items([1, 2, 3, 4], max_items=2)
    assert result.content == [1, 2]
    assert result.remaining_items == 2
    assert result.continuation_token is not None

    now["t"] = 20.0
    assert guard.continue_from(result.continuation_token) is None
