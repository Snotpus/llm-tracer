# llm-tracer — agent instructions

## Runtime data files
logs.jsonl, sessions/, and db/ are runtime data files, not source code.
- Never read logs.jsonl or session/ files in full — they can be very large
- Use `head` or `tail` to sample them if needed for context

## Setup & run

pip install -e .            # install from pyproject.toml
python main.py              # start proxy on 0.0.0.0:8000

Or via uvicorn directly:
uvicorn main:app --host 0.0.0.0 --port 8000

## Config

Edit config.json at the repo root. Five keys:
- ollama_base_url — upstream Ollama URL (default http://localhost:11434)
- log_path — where requests are logged (default logs.jsonl)
- log_detail — minimal, detailed, or full
- db_path — DuckDB database path (default db/traces.duckdb, optional)

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

### DB (db.py)

DuckDB persistent storage for traces:

python db.py init              # create DB file with schema
python db.py import             # load JSONL logs into DB
python db.py stats              # row count, date range, model list
python db.py query "SQL..."     # execute and format SQL query as table
python db.py analyze token      # tokens by model over time
python db.py analyze latency    # latency percentile distribution
python db.py analyze errors     # error rate by endpoint
python db.py analyze model      # model comparison
python db.py analyze endpoint   # endpoint distribution

All five subcommands scan `config.json` for `db_path` (default `db/traces.duckdb`).
`import` and `init` both create the `db/` directory if missing. Idempotent via `INSERT OR REPLACE`.

## Architecture

5 flat Python files, no package structure:

main.py     — FastAPI proxy, app entrypoint (uvicorn main:app)
replay.py   — standalone CLI: load JSONL, resend, diff
sessions.py — standalone CLI: list/create/render/audit/export sessions
db.py       — standalone CLI: init/import/analyze DB

No subpackages, no migrations, no generated code.

## Does not exist

No tests, no lint/typecheck/formatter config, no CI.
No .env or secrets — all config is config.json.
*.jsonl, .venv/, .idea/, .opencode/ are gitignored.
