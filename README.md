# llm-tracer

Lightweight LLM observability proxy for local Ollama.

- Proxies Ollama requests (`/api/generate`, `/api/chat`, `/v1/chat/completions`)
- Logs payloads, token counts, latency, and responses to JSONL with configurable detail
- Replay logged requests and diff against new responses

## Install

```bash
pip install -e .
```

## Run proxy

```bash
python main.py
```

Proxy listens on `0.0.0.0:8000`. Point your Ollama client to it instead of
`http://localhost:11434`.

## Config

Edit `config.json`:

```json
{
    "ollama_base_url": "http://127.0.0.1:11434",
    "log_path": "logs.jsonl",
    "log_detail": "detailed"
}
```

### Log detail levels

- **`minimal`** — basic fields only: id, model, latency, raw prompt/response, token counts
- **`detailed`** (default) — all of the above plus `request_params` (temperature, max_tokens, tools, stream, etc.) and `response_metadata` (usage tokens, finish_reason)
- **`full`** — everything in `detailed` plus the full request body for perfect replay fidelity

## Sessions (Conversation Recreation)

Capture, export, render, and audit conversations from the raw request log.

```bash
# List all log entries available for capture
python sessions.py list [--log-path logs.jsonl]

# Create a new session by time range
python sessions.py create --range "2026-04-30T10:00 2026-04-30T10:30"

# Create a session from specific request IDs
python sessions.py create --ids <uuid1> <uuid2> ...

# Render a session as markdown
python sessions.py render <session_file>

# Audit: request → response chain with timing
python sessions.py audit <session_file>

# Export to portable format
python sessions.py export <session_file> --format jsonl
python sessions.py export <session_file> --format openai
```

Sessions are stored as portable JSONL files in `sessions/`. Each session file contains a metadata header line followed by one entry per request, with full message history, responses, timing, and token counts.

The `openai` export format writes individual files per entry in OpenAI assistant import format.

## Replay requests

```bash
python replay.py <request_id>
python replay.py <request_id> --full   # show complete (untruncated) responses
python replay.py <request_id> --log-path /path/to/logs.jsonl
```

Resends a logged request to Ollama using the original parameters and prints a unified diff between the original and replayed responses. The output includes a summary with latency, token counts, and finish reason. Use `--log-path` if your log file is not in the default location.

Older log entries (from before `log_detail` was added) will show a warning but replay will still attempt using whatever fields are available.

## Troubleshooting

### Find any python processes running fastapi/uvicorn
```bash
ps aux | grep -E "(uvicorn|fastapi|proxy)" | grep -v grep
```

### Or more broadly, python processes
```bash
ps aux | grep python | grep -v grep
```

### See all listening ports with the process name
```bash
lsof -i -P -n | grep LISTEN
```

### Check a specific port (e.g. 8000, 11434 for ollama)
```bash
lsof -i :8000
lsof -i :11434
```

### See active network connections
```bash
lsof -i -P -n | grep LISTEN
```

### Or watch connections in real time
```bash
watch -n 1 "lsof -i -P -n | grep LISTEN"
```

### Is your proxy responding at all?
```bash
curl -s http://localhost:8000/
curl -s http://localhost:8000/v1/models
```

### Test if ollama itself is reachable directly
```bash
curl -s http://localhost:11434/api/tags
```

### Run your proxy with output visible
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --log-level debug
```

### Watch logs live
```bash
tail -f /tmp/llmproxy.log
lsof -i :8000                # is it actually listening?
ps aux | grep uvicorn        # is the process alive?
```

## Persistent deployment

### launchd + plist example

Create `~/Library/LaunchAgents/com.yourname.llmproxy.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.yourname.llmproxy</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>python3</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8000</string>
        <string>--log-level</string>
        <string>debug</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/your/proxy/folder</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/tmp/llmproxy.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/llmproxy.err</string>
</dict>
</plist>
```

### Manage with:
```bash
# Load and start it
launchctl load ~/Library/LaunchAgents/com.yourname.llmproxy.plist

# Stop and unload
launchctl unload ~/Library/LaunchAgents/com.yourname.llmproxy.plist

# Check if it's running
launchctl list | grep llmproxy

# Watch the logs live
tail -f /tmp/llmproxy.log
tail -f /tmp/llmproxy.err
```

### nohup example
```bash
nohup uvicorn main:app --host 0.0.0.0 --port 8000 --log-level debug \
  > /tmp/llmproxy.log 2>&1 &

# It prints a PID (process ID), save it
echo $! > /tmp/llmproxy.pid
```

### Check if it's running
```bash
ps aux | grep uvicorn | grep -v grep
```

### Kill it (use the PID from before)
```bash
kill $(cat /tmp/llmproxy.pid)
```

### Watch logs
```bash
tail -f /tmp/llmproxy.log
```

## Config Locations:
- OpenCode: ~/.config/opencode/config.json
- Proxy (this project): ~/Projects/llm-tracer/config.json
- ollama plist (not used atm...): ~/Library/LaunchAgents/ollama.keepalive.plist
- 