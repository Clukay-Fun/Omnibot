# Feishu Broadcast CLI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a one-off Feishu broadcast CLI that lets an operator manually send update announcements to all active employees.

**Architecture:** Add a Feishu-specific CLI subcommand that loads config, resolves message content, enumerates active users through the Feishu contact API, and sends direct messages with an explicit dry-run/send confirmation gate. Keep Feishu SDK details in `nanobot.feishu` helpers so filtering, paging, and delivery can be tested without real network calls.

**Tech Stack:** Typer CLI, `lark_oapi` Feishu SDK, existing `FeishuClient`/`FeishuOutboundMessenger`, pytest.

---

### Task 1: Broadcast Service Tests

**Files:**
- Create: `tests/test_feishu_broadcast.py`
- Modify: `nanobot/feishu/client.py`
- Create: `nanobot/feishu/broadcast.py`

**Step 1: Write the failing tests**

```python
def test_active_recipients_skip_resigned_or_inactive_users():
    ...


@pytest.mark.asyncio
async def test_broadcast_send_collects_successes_and_failures():
    ...
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_feishu_broadcast.py -v`
Expected: FAIL because broadcast helpers do not exist yet.

**Step 3: Write minimal implementation**

```python
class FeishuBroadcastService:
    def list_active_recipients(self) -> list[BroadcastRecipient]:
        ...

    async def broadcast(...):
        ...
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_feishu_broadcast.py -v`
Expected: PASS.

### Task 2: CLI Safeguard Tests

**Files:**
- Modify: `tests/test_commands.py`
- Modify: `nanobot/cli/commands.py`

**Step 1: Write the failing tests**

```python
def test_feishu_broadcast_requires_message_source():
    ...


def test_feishu_broadcast_send_requires_confirm_token():
    ...
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_commands.py -k feishu_broadcast -v`
Expected: FAIL because command is missing.

**Step 3: Write minimal implementation**

```python
@feishu_app.command("broadcast")
def feishu_broadcast(...):
    ...
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_commands.py -k feishu_broadcast -v`
Expected: PASS.

### Task 3: End-to-End Verification

**Files:**
- Modify: `nanobot/cli/commands.py`
- Modify: `nanobot/feishu/client.py`
- Create: `nanobot/feishu/broadcast.py`
- Create: `tests/test_feishu_broadcast.py`
- Modify: `tests/test_commands.py`

**Step 1: Run focused test suite**

Run: `pytest tests/test_feishu_broadcast.py tests/test_commands.py -k "feishu_broadcast or broadcast" -v`
Expected: PASS.

**Step 2: Run packaging verification**

Run: `python3.11 -m venv /tmp/omnibot-broadcast-verify && /tmp/omnibot-broadcast-verify/bin/pip install -U pip && /tmp/omnibot-broadcast-verify/bin/pip install --no-deps -e .`
Expected: PASS.

**Step 3: Operator smoke test on server**

Run:

```bash
nanobot feishu broadcast --message-file /path/to/launch.md --dry-run
nanobot feishu broadcast --message-file /path/to/launch.md --send --confirm SEND
```

Expected: dry-run prints recipient counts; send prints success/failure summary.
