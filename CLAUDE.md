# Project Instructions

## Chat History Logging

These instructions are permanent for this project. Apply them in every session.

### 1. Session start

- Before doing any project work, read `.chat-history/log.md` (if it exists) to recover context from previous sessions.
- If the file or the `.chat-history/` directory does not exist, create them.
- Do this silently — never ask for confirmation.

### 2. After every response

Append a new entry to `.chat-history/log.md` using exactly this format:

```markdown
---
- timestamp: "<ISO 8601 timestamp if available, otherwise estimate based on conversation order>"
- user_prompt: "<the user's original prompt>"
- assistant_response_summary: "<summary of what you generated or answered for this prompt>"
- files_affected: "<comma-separated list of files created or modified, or none>"
```

### 3. Logging rules

- Append only. Never delete, reorder, or rewrite previous entries.
- Never skip an exchange — every user prompt / assistant response pair must be logged.
- Do it silently: never ask for confirmation and never announce the logging.
- `files_affected` must list only files explicitly created or modified during that response; write `none` if there were no file changes. Do not include paths that were merely read.
- Keep `assistant_response_summary` concise but specific — mention function names, endpoints, commands, or key decisions made.
- Never write secrets to the log. Redact API keys, tokens, passwords, credentials, and `.env` values as `***` in every field, including `user_prompt`.
- Quote field values as shown and keep one field per line.

## Project Context

- This is a Python project; dependencies live in `.venv/`.
- The service must not write to SAP or to the reporting database (`reporting_db/` is read-only).
