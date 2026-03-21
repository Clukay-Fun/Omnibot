---
name: memory
description: Two-layer memory system with grep-based recall.
always: true
---

# Memory

## Structure

- `WORKLOG.md` — Current work state. Use it for active, executable items with a next step.
- `memory/MEMORY.md` — Long-term facts (preferences, project context, relationships). Always loaded into your context as reference memory.
- `memory/HISTORY.md` — Append-only event log. NOT loaded into context. Search it with grep-style tools or in-memory filters. Each entry starts with [YYYY-MM-DD HH:MM].

Use this boundary consistently:
- `WORKLOG.md` = current active items that can advance and finish
- `memory/MEMORY.md` = stable preferences, long-term background, long-lived facts
- `memory/HISTORY.md` = lookup-only event trail

## Search Past Events

Choose the search method based on file size:

- Small `memory/HISTORY.md`: use `read_file`, then search in-memory
- Large or long-lived `memory/HISTORY.md`: use the `exec` tool for targeted search

Examples:
- **Linux/macOS:** `grep -i "keyword" memory/HISTORY.md`
- **Windows:** `findstr /i "keyword" memory\HISTORY.md`
- **Cross-platform Python:** `python -c "from pathlib import Path; text = Path('memory/HISTORY.md').read_text(encoding='utf-8'); print('\n'.join([l for l in text.splitlines() if 'keyword' in l.lower()][-20:]))"`

Prefer targeted command-line search for large history files.

## When to Update MEMORY.md

Write important long-term facts immediately using `edit_file` or `write_file`:
- User preferences ("I prefer dark mode")
- Long-term work background ("The user is building a Feishu bot")
- Relationships ("Alice is the project lead")

Do not put executable tasks, blockers, priorities, or next steps into `memory/MEMORY.md`; those belong in `WORKLOG.md`.
If recent conversation clearly contradicts an older long-term fact, update `memory/MEMORY.md` to match the newer truth.

## Auto-consolidation

Old conversations are automatically summarized and appended to HISTORY.md when the session grows large. Long-term facts are extracted to MEMORY.md. You don't need to manage this.
