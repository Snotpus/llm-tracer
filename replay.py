"""
Replay a logged Ollama request and diff the old vs new response.

Usage:
    python replay.py <request_id>
    python replay.py <request_id> --full   # show full responses
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

    if entry.get("endpoint") == "/api/generate" or "prompt" in entry:
        # /api/generate endpoint
        url = f"{base_url}/api/generate"
        payload = {"model": entry["model"]}
        if "prompt" in entry:
            payload["prompt"] = entry["prompt"]
        if "messages" in entry:
            payload["messages"] = entry["messages"]
        # Merge request_params into payload
        for k in ["stream", "options", "context", "images", "template", "system"]:
            if k in entry:
                payload[k] = entry[k]
        if "request_params" in entry and entry["request_params"]:
            for k, v in entry["request_params"].items():
                payload[k] = v
        url = f"{base_url}/api/generate"

    elif entry.get("endpoint") == "/api/chat":
        # /api/chat endpoint
        url = f"{base_url}/api/chat"
        payload = {"model": entry["model"], "messages": entry.get("messages", [])}
        for k in ["stream", "options"]:
            if k in entry:
                payload[k] = entry[k]
        if "request_params" in entry and entry["request_params"]:
            for k, v in entry["request_params"].items():
                payload[k] = v

    elif entry.get("endpoint") == "/v1/chat/completions":
        # /v1/chat/completions endpoint
        url = f"{base_url}/v1/chat/completions"
        # Reconstruct OpenAI-style body from request_params
        payload = {
            "model": entry["model"],
            "messages": entry.get("messages", []),
        }
        # Use request_params if available (full or detailed log)
        if "request_params" in entry and entry["request_params"]:
            for k, v in entry["request_params"].items():
                payload[k] = v
        else:
            # Fallback: just model + messages
            pass
            # No way to recover params not in log — warn user
    else:
        # Generic: try request_params if available
        url = f"{base_url}/v1/chat/completions"
        payload = {
            "model": entry["model"],
            "messages": entry.get("messages", []),
        }
        if "request_params" in entry and entry["request_params"]:
            for k, v in entry["request_params"].items():
                payload[k] = v

    with httpx.Client(timeout=120) as client:
        resp = client.post(url, json=payload)
        return resp.json()


def show_diff(old_text: str, new_text: str, title: str = ""):
    if not old_text and not new_text:
        return
    if title:
        print(f"\n=== {title} ===")
    print(f"\n--- original response ({len(old_text)} chars)\n+++ replay response ({len(new_text)} chars)")
    for line in difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile="original",
        tofile="replay",
    ):
        print(line, end="")
    print()


def show_summary(entry: dict, new_response: dict):
    """Print summary stats for the replayed request."""
    new_text = ""
    if "response" in new_response:
        new_text = new_response["response"]
    elif "choices" in new_response and new_response["choices"]:
        choice = new_response["choices"][0]
        new_text = choice.get("message", {}).get("content", "") or choice.get("delta", {}).get("content") or ""

    # Try to get usage from response
    new_usage = None
    if "usage" in new_response:
        new_usage = new_response["usage"]

    print("\n=== REPLAY SUMMARY ===")
    print(f"Model: {new_response.get('model', entry.get('model', 'unknown'))}")

    new_latency = entry.get("latency_ms", 0)
    print(f"Latency: {new_latency:.0f}ms")

    if entry.get("input_tokens"):
        print(f"Input tokens: {entry['input_tokens']}")
    if entry.get("output_tokens"):
        print(f"Output tokens: {entry['output_tokens']}")
    if new_usage:
        output_tokens = new_usage.get("completion_tokens", new_usage.get("output_tokens", 0))
        input_tokens = new_usage.get("prompt_tokens", new_usage.get("input_tokens", 0))
        if input_tokens != entry.get("input_tokens"):
            print(f"Input tokens (server): {input_tokens}")
        if output_tokens != entry.get("output_tokens"):
            print(f"Output tokens (server): {output_tokens}")

    if "usage" in entry and entry["usage"]:
        old_usage = entry["usage"]
        new_tokens = new_usage.get("completion_tokens", 0) if new_usage else 0
        old_tokens = old_usage.get("completion_tokens", old_usage.get("output_tokens", 0))
        print(f"Output tokens: old={old_tokens}, new={new_tokens}")
        old_p = old_usage.get("prompt_tokens", old_usage.get("input_tokens", 0))
        new_p = new_usage.get("prompt_tokens", new_usage.get("input_tokens", 0))
        if old_p != new_p:
            print(f"Input tokens: old={old_p}, new={new_p}")

    finish_reason = None
    if new_response.get("choices") and new_response["choices"]:
        finish_reason = new_response["choices"][0].get("finish_reason")
    if finish_reason:
        print(f"Finish reason: {finish_reason}")


def main():
    parser = argparse.ArgumentParser(description="Replay a logged Ollama request")
    parser.add_argument("request_id", help="The request ID from a previous log entry")
    parser.add_argument("--log-path", default=None, help="Path to the JSONL log file")
    parser.add_argument("--full", action="store_true", help="Show full responses, not truncated")
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
    log_detail = entry.get("_log_detail", "unknown")
    request_params = entry.get("request_params", {})
    response_metadata = entry.get("response_metadata", {})

    print(f"Loaded request: {args.request_id}")
    print(f"Endpoint: {entry.get('endpoint', 'unknown')}")
    print(f"Model: {entry.get('model', 'unknown')}")
    print(f"Log detail: {log_detail}")
    print(f"Original latency: {entry.get('latency_ms', 0):.0f}ms")
    print(f"Original response ({len(old_response)} chars)")
    if old_response and args.full:
        print(old_response)
    else:
        print(old_response[:500] + ".." if len(old_response) > 500 else ("(empty)" if not old_response else old_response))
    print()

    # Show request params if saved
    if request_params:
        print("=== REQUEST PARAMS ===")
        for k, v in request_params.items():
            if k == "messages" and isinstance(v, list):
                print(f"  {k}: [{len(v)} messages]")
                for i, m in enumerate(v):
                    content_preview = str(m.get("content", ""))[:80]
                    print(f"    [{i}] {m.get('role', '?')}: {content_preview}..") if len(str(m.get("content", ""))) > 80 else print(f"     [{i}] {m.get('role', '?')}: {m.get('content', '')}")
            elif k in ("options", "tools", "stream_options") and v:
                print(f"  {k}: {json.dumps(v, indent=8)}")
            elif v:
                print(f"  {k}: {v}")
        print()
    elif log_detail in ("detailed", "full") and not entry.get("request_params"):
        print("(request_params not found — this may be an older log entry)")
        print()

    # Show response metadata if saved
    if response_metadata:
        print("=== RESPONSE METADATA ===")
        if response_metadata.get("usage"):
            print(f"  usage: {json.dumps(response_metadata['usage'])}")
        if response_metadata.get("finish_reason"):
            for c in response_metadata["finish_reason"]:
                print(f"  choices[{c.get('index', '?')}].finish_reason: {c.get('finish_reason')}")
        if response_metadata.get("model"):
            print(f"  response model: {response_metadata['model']}")
        print()

    print("=== RESENDING REQUEST ===")
    new_response = resend_request(entry)

    if new_response.get("error"):
        print(f"Error replaying: {new_response['error']}", file=sys.stderr)
        sys.exit(1)

    new_text = ""
    if "response" in new_response:
        new_text = new_response["response"]
    elif "choices" in new_response and new_response["choices"]:
        choice = new_response["choices"][0]
        new_text = choice.get("message", {}).get("content", "") or choice.get("delta", {}).get("content") or ""

    print(f"Replayed response ({len(new_text)} chars)")
    if new_text and args.full:
        print(new_text)
    else:
        print(new_text[:500] + ".." if len(new_text) > 500 else ("(empty)" if not new_text else new_text))
    print()

    if old_response and new_text:
        show_diff(old_response, new_text)
        show_summary(entry, new_response)
    else:
        print("(Skipping diff — one or both responses are empty)")


if __name__ == "__main__":
    main()
