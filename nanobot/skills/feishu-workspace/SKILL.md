---
name: feishu-workspace
description: Operate Feishu bitable, calendar, docs, wiki, and drive resources via bundled scripts. Use for deterministic Feishu workspace operations that require app-level API access, structured JSON output, and explicit action boundaries.
metadata: {"nanobot":{"emoji":"🪶","requires":{"bins":["bash"]}}}
---

# Feishu Workspace

Use this skill when the user wants to inspect or change Feishu workspace resources through app APIs:

- Bitable: app/table/view/field/record discovery and CRUD
- Calendar: calendar discovery and event CRUD
- Docs / Wiki / Drive: document creation and text extraction, wiki space/node operations, drive file listing and deletion

This skill is script-first. Do not handcraft API requests in the prompt. Use the bundled shell wrappers:

```bash
bash "{baseDir}/scripts/bitable.sh" ...
bash "{baseDir}/scripts/calendar.sh" ...
bash "{baseDir}/scripts/docs.sh" ...
```

## Important Boundaries

- v1 only supports `tenant_access_token`.
- Do not promise access to a user's private calendar, private drive files, or other resources not shared with the app.
- If the API returns a permission error, explain the boundary and stop. Do not keep retrying the same inaccessible target.
- Deletion is entity-level only. Do not attempt container-level deletion such as removing an entire bitable app/table, calendar, or wiki space.
- For docs, write operations are limited to appending plain-text paragraphs. Do not attempt rich block editing or full document replacement.
- For any request about the current state of bitable tables, records, calendars, documents, wiki nodes, or drive files, do not answer from prior conversation memory. Always run a fresh list/get/read/check command to verify the current state before answering.

## Workflow

1. Start with `check` for the relevant module to verify auth and permissions.
2. Read the matching reference file before non-trivial operations:
   - Bitable: `{baseDir}/references/bitable.md`
   - Calendar: `{baseDir}/references/calendar.md`
   - Docs / Wiki / Drive: `{baseDir}/references/docs.md`
3. Run the wrapper script with explicit IDs, URLs, or JSON payloads.
4. Read the JSON result and summarize it for the user.

## Typical Commands

Check auth and module reachability:

```bash
bash "{baseDir}/scripts/bitable.sh" check
bash "{baseDir}/scripts/calendar.sh" check
bash "{baseDir}/scripts/docs.sh" check
```

List bitable records:

```bash
bash "{baseDir}/scripts/bitable.sh" record list --app-token app_token --table-id tbl_id
```

Create a calendar event:

```bash
bash "{baseDir}/scripts/calendar.sh" event create --calendar-id cal_id --data-json '{"summary":"Demo","start_time":"2026-03-11T10:00:00+08:00","end_time":"2026-03-11T11:00:00+08:00"}'
```

Read document text:

```bash
bash "{baseDir}/scripts/docs.sh" doc read_text --document-id doc_id
```

Append plain text to a document:

```bash
bash "{baseDir}/scripts/docs.sh" doc append_text --document-id doc_id --text 'Follow-up notes'
```

## When Not To Use

- Do not use this skill for ordinary chat, greetings, or general knowledge.
- Do not use it when the user has not identified a Feishu resource and no relevant lookup should happen.
- Do not use it for unsupported private-resource access or unsupported rich-doc editing.
