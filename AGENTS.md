# llm-tracer — agent instructions

## Setup & run

pip install -e .            # install from pyproject.toml
python main.py              # start proxy on 0.0.0.0:8000

Or via uvicorn directly:
uvicorn main:app --host 0.0.0.0 --port 8000

## Config

Edit config.json at the repo root. Three keys:
- ollama_base_url — upstream Ollama URL (default http://localhost:11434)
- log_path — where requests are logged (default logs.jsonl)
- log_detail — minimal, detailed, or full

Log detail controls what extra fields are written per request (request params,
response metadata, raw body). full is the most replay-faithful.

## Commands

### Proxy (main.py)

Proxy passes through three endpoint families to Ollama, logging each:
- /api/generate — Ollama native generate
- /api/chat — Ollama native chat
- /v1/chat/completions — OpenAI-compatible chat (forwards to Ollama's /v1/chat/completions)

### Sessions (sessions.py)

Capture conversations from logs.jsonl:

python sessions.py list [--log-path logs.jsonl]           # interactive select
python sessions.py create --range "2026-04-30T10:00 2026-04-30T10:30"
python sessions.py create --ids <uuid1> <uuid2> ...
python sessions.py render <session_file>                  # markdown preview
python sessions.py audit <session_file>                   # chronological chain
python sessions.py export <session_file> --format jsonl   # portable JSONL
python sessions.py export <session_file> --format openai  # per-file JSONL

Session files go to sessions/ — JSONL with metadata header + one line per entry.

### Replay (replay.py)

Resend a logged request to Ollama, diff against the original:

python replay.py <request_id>
python replay.py <request_id> --full   # untruncated responses
python replay.py <request_id> --log-path /path/to/logs.jsonl

Older log entries (no request_params) replay with only model + messages.

## Architecture

4 flat Python files, no package structure:

main.py     — FastAPI proxy, app entrypoint (uvicorn main:app)
replay.py   — standalone CLI: load JSONL, resend, diff
sessions.py — standalone CLI: list/create/render/audit/export sessions

No subpackages, no migrations, no generated code.

## Does not exist

No tests, no lint/typecheck/formatter config, no CI.
No .env or secrets — all config is config.json.
*.jsonl, .venv/, .idea/, .opencode/ are gitignored.
