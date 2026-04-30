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
