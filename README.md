# llm-tracer

Lightweight LLM observability proxy for local Ollama.

- Proxies Ollama requests (`/api/generate`, `/api/chat`)
- Logs payloads, token counts, latency, and responses to JSONL
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
    "log_path": "logs.jsonl"
}
```

## Replay requests

```bash
python replay.py <request_id>
```

Resends a logged request to Ollama and prints a unified diff between the
original and replayed responses. Use `--log-path` if your log file is not in
the default location.



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

### See active network connections from opencode
```bash
lsof -i -P -n | grep opencode
```

### Or watch connections in real time
```bash
watch -n 1 "lsof -i -P -n | grep opencode"
```

### Is your proxy responding at all?
```bash
curl -s http://localhost:8000/  # or whatever port you're using
curl -s http://localhost:8000/health
```

### Test if ollama itself is reachable directly
```bash
curl -s http://localhost:11434/api/tags
```


### Run your proxy with output visible
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --log-level debug
```

### Watch:
```bash
tail -f /tmp/llmproxy.log    # watch logs live
lsof -i :8000                # is it actually listening?
ps aux | grep uvicorn        # is the process alive?
```

## launchd + plist example:
`~/Library/LaunchAgents/com.yourname.llmproxy.plist`
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

## `nohup` example
Run with 
```bash
nohup uvicorn main:app --host 0.0.0.0 --port 8000 --log-level debug \
  > /tmp/llmproxy.log 2>&1 &

# It prints a PID (process ID), save it
echo $! > /tmp/llmproxy.pid
```

### Check if it's running
ps aux | grep uvicorn | grep -v grep

### Kill it (use the PID from before)
kill $(cat /tmp/llmproxy.pid)

### Watch logs
tail -f /tmp/llmproxy.log