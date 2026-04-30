"""
Replay a logged Ollama request and diff the old vs new response.

Usage:
    python replay.py <request_id>
"""
import argparse
import difflib
import json
import sys
from pathlib import Path


def load_log(path: Path, request_id: str):
    for line in path.read_text().strip().splitlines():
        entry = json.loads(line)
        if entry.get("id") == request_id:
            return entry
    return None


def resend_request(entry: dict):
    """Resend the original request to Ollama and return the new response."""
    import httpx

    base_url = entry.get("_config", {}).get("ollama_base_url", "http://localhost:11434")
    params = entry.get("request_params", {})

    if "prompt" in entry:
        # /api/generate endpoint
        payload = {"model": entry["model"], "prompt": entry.get("prompt", "")}
        for k in ["stream", "options"]:
            if k in params:
                payload[k] = params[k]
        url = f"{base_url}/api/generate"
    else:
        # /api/chat endpoint
        payload = {"model": entry["model"], "messages": entry.get("messages", [])}
        for k in ["stream", "options"]:
            if k in params:
                payload[k] = params[k]
        url = f"{base_url}/api/chat"

    with httpx.Client(timeout=120) as client:
        resp = client.post(url, json=payload)
        return resp.json()


def show_diff(old_text: str, new_text: str):
    print("\n=== OLD RESPONSE ===")
    for line in difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile="original",
        tofile="replay",
    ):
        print(line, end="")
    print("\n")


def main():
    parser = argparse.ArgumentParser(description="Replay a logged Ollama request")
    parser.add_argument("request_id", help="The request ID from a previous log entry")
    parser.add_argument("--log-path", default=None, help="Path to the JSONL log file")
    args = parser.parse_args()

    # Determine log path
    log_file = None
    for candidate in [
        args.log_path,
        "logs.jsonl",
        "log.jsonl",
    ]:
        if candidate and Path(candidate).exists():
            log_file = Path(candidate)
            break

    if not log_file:
        print("Error: no log file found. Try --log-path /path/to/logs.jsonl", file=sys.stderr)
        sys.exit(1)

    entry = load_log(log_file, args.request_id)
    if not entry:
        print(f"Error: request {args.request_id} not found in {log_file}", file=sys.stderr)
        sys.exit(1)

    old_response = entry.get("response", "")
    print(f"Loaded request: {args.request_id}")
    print(f"Model: {entry.get('model')}")
    print(f"Original latency: {entry.get('latency_ms', 0):.0f}ms")
    print(f"Original response ({len(old_response)} chars):")
    if old_response:
        print(old_response[:500] + "..." if len(old_response) > 500 else old_response)
    print()

    new_response = resend_request(entry)
    new_text = ""
    if "response" in new_response:
        new_text = new_response["response"]
    elif "choices" in new_response:
        new_text = new_response["choices"][0]["message"]["content"]

    print(f"Replayed response ({len(new_text)} chars):")
    if new_text:
        print(new_text[:500] + "..." if len(new_text) > 500 else new_text)
    print()

    if old_response and new_text:
        show_diff(old_response, new_text)
    else:
        print("(Skipping diff — one or both responses are empty)")


if __name__ == "__main__":
    main()
