import json
import sys
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="LLM Tracer Proxy", version="0.1.0")

# Load config from config.json if it exists
_config = {}
_config_path = Path("config.json")
if _config_path.exists():
    _config = json.loads(_config_path.read_text())


def estimate_tokens(text: str) -> int:
    """Very rough token count: one token per whitespace-separated word."""
    if not text:
        return 0
    return len(text.split())


def estimate_response_tokens(data: dict) -> int:
    """Count tokens in the response text from Ollama."""
    if "response" in data:
        return estimate_tokens(data["response"])
    for choice in data.get("choices", []):
        delta = choice.get("message", {})
        if delta.get("content"):
            return estimate_tokens(delta["content"])
    return 0


def get_log_path():
    return _config.get("log_path", "logs.jsonl")


def write_log(entry: dict):
    """Append a log entry to the JSONL file."""
    log_path = get_log_path()
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def pretty_console_log(entry: dict):
    """Print summary to stderr without affecting proxy responses."""
    model = entry.get("model", "?")
    latency_ms = entry.get("latency_ms", 0)
    input_tokens = entry.get("input_tokens", 0)
    output_tokens = entry.get("output_tokens", 0)
    req_id = entry.get("id")
    error = entry.get("error")
    status = "ERR" if error else "OK"

    print(f"\n[{status}] req={req_id} model={model} latency={latency_ms:.0f}ms "
          f"input_tokens={input_tokens} output_tokens={output_tokens}", file=sys.stderr)
    if err := entry.get("error"):
        print(f"  error: {err}", file=sys.stderr)
    print(file=sys.stderr)


@app.post("/api/generate")
async def generate(request: Request):
    req_id = str(uuid.uuid4())
    start = time.time()
    body = await request.json()
    model = body.get("model", "unknown")
    prompt = body.get("prompt", "")

    log_entry = {
        "id": req_id,
        "timestamp": time.time(),
        "model": model,
        "prompt": prompt,
        "response": "",
        "latency_ms": 0,
        "request_params": body,
        "error": None,
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": 0,
        "_config": _config,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{_config.get('ollama_base_url', 'http://localhost:11434')}/api/generate",
                json=body,
            )
            data = resp.json()

        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["response"] = data.get("response", "")
        log_entry["output_tokens"] = estimate_response_tokens(data)

        write_log(log_entry)
        pretty_console_log(log_entry)
        return resp.json()

    except Exception as e:
        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["error"] = str(e)
        write_log(log_entry)
        pretty_console_log(log_entry)
        return JSONResponse(
            status_code=502,
            content={"error": "Ollama proxy error", "details": str(e)},
        )


@app.post("/api/chat")
async def chat(request: Request):
    req_id = str(uuid.uuid4())
    start = time.time()
    body = await request.json()
    model = body.get("model", "unknown")
    messages = body.get("messages", [])

    log_entry = {
        "id": req_id,
        "timestamp": time.time(),
        "model": model,
        "messages": messages,
        "response": "",
        "latency_ms": 0,
        "request_params": body,
        "error": None,
        "input_tokens": sum(estimate_tokens(m.get("content", "")) for m in messages),
        "output_tokens": 0,
        "_config": _config,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{_config.get('ollama_base_url', 'http://localhost:11434')}/api/chat",
                json=body,
            )
            data = resp.json()

        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["response"] = data.get("message", {}).get("content", "")
        log_entry["output_tokens"] = estimate_response_tokens(data)

        write_log(log_entry)
        pretty_console_log(log_entry)
        return resp.json()

    except Exception as e:
        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["error"] = str(e)
        write_log(log_entry)
        pretty_console_log(log_entry)
        return JSONResponse(
            status_code=502,
            content={"error": "Ollama proxy error", "details": str(e)},
        )



# ── OpenAI-compatible API endpoints (for SDKs like @ai-sdk/openai-compatible) ──


def _ollama_to_openai_chat(choice, usage):
    """Convert an Ollama chat response body to OpenAI format."""
    message = choice.get("message", {})
    return {
        "index": 0,
        "message": {
            "role": message.get("role", "assistant"),
            "content": message.get("content", ""),
        },
        "finish_reason": choice.get("finish_reason", "stop"),
    }


def _ollama_to_openai_usage(ollama_resp, request_body):
    """Extract token usage from an Ollama response. Fallback to query size if no usage."""
    prompt_tokens = 0
    completion_tokens = 0
    if "prompt_eval_count" in ollama_resp:
        prompt_tokens = ollama_resp["prompt_eval_count"]
    if "eval_count" in ollama_resp:
        completion_tokens = ollama_resp["eval_count"]
    if prompt_tokens == 0:
        # Estimate from the request messages
        for msg in request_body.get("messages", []):
            prompt_tokens += estimate_tokens(msg.get("content", ""))
    if completion_tokens == 0:
        completion_tokens = estimate_tokens(ollama_resp.get("message", {}).get("content", ""))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _convert_openai_messages_to_ollama(openai_messages):
    """Extract messages from OpenAI format into Ollama-compatible list."""
    messages = []
    for msg in openai_messages:
        messages.append({"role": msg["role"], "content": msg.get("content", "")})
    return messages


@app.get("/v1/models")
async def list_models():
    """Proxy Ollama's /api/tags to return available models in OpenAI format."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_config.get('ollama_base_url', 'http://localhost:11434')}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("models", [])
                openai_models = []
                for m in models:
                    openai_models.append({
                        "id": m["name"],
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "ollama",
                    })
                return {"data": openai_models}
            return {"data": []}
    except Exception:
        return {"data": []}


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """Translate OpenAI /v1/chat/completions → Ollama /api/chat."""
    req_id = str(uuid.uuid4())
    start = time.time()
    body = await request.json()
    model = body.get("model", "unknown")
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    ollama_messages = _convert_openai_messages_to_ollama(messages)

    # Build Ollama-compatible request body
    ollama_body = {
        "model": model,
        "messages": ollama_messages,
    }
    if "temperature" in body:
        ollama_body["options"] = {"temperature": body["temperature"]}

    log_entry = {
        "id": req_id,
        "timestamp": time.time(),
        "model": model,
        "messages": messages,
        "response": "",
        "latency_ms": 0,
        "request_params": body,
    }

    if not stream:
        try:
            # Ollama streams /api/chat even when stream=False
            # Collect all lines and use the last line before "done":true
            ollama_resp = {}
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{_config.get('ollama_base_url', 'http://localhost:11434')}/api/chat",
                    json=ollama_body,
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if "done" in chunk and chunk["done"]:
                            break
                        ollama_resp = chunk

            log_entry["latency_ms"] = (time.time() - start) * 1000
            log_entry["response"] = ollama_resp.get("message", {}).get("content", "")
            log_entry["output_tokens"] = estimate_response_tokens(ollama_resp)
            log_entry["error"] = None
            write_log(log_entry)
            pretty_console_log(log_entry)
        except Exception as e:
            log_entry["latency_ms"] = (time.time() - start) * 1000
            log_entry["error"] = str(e)
            write_log(log_entry)
            pretty_console_log(log_entry)
            usage = _ollama_to_openai_usage(ollama_resp, body)
            return JSONResponse(
                status_code=502,
                content={
                    "error": {"message": str(e), "type": "proxy_error"},
                    "usage": usage,
                    "choices": [],
                },
            )

        # Convert to OpenAI response format
        usage = _ollama_to_openai_usage(ollama_resp, body)
        return {
            "id": f"chatcmpl-{req_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [_ollama_to_openai_chat(ollama_resp, {})],
            "usage": usage,
        }
    else:
        async def generate_stream():
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream(
                        "POST",
                        f"{_config.get('ollama_base_url', 'http://localhost:11434')}/api/chat",
                        json=ollama_body,
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            chunk = json.loads(line)
                            msg_content = chunk.get("message", {}).get("content", "")
                            if not msg_content:
                                continue
                            delta = {"role": "assistant", "content": msg_content}
                            sse = f'data: {json.dumps({"id": f"chatcmpl-{req_id}", "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})}\n\n'
                            yield sse

                # Send final chunk with finish_reason
                final = json.dumps({
                    "id": f"chatcmpl-{req_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
                })
                yield f"data: {final}\n\n"
                yield "data: [DONE]\n\n"

                log_entry["latency_ms"] = (time.time() - start) * 1000
                log_entry["error"] = None
                write_log(log_entry)
                pretty_console_log(log_entry)
            except Exception as e:
                log_entry["latency_ms"] = (time.time() - start) * 1000
                log_entry["error"] = str(e)
                write_log(log_entry)
                pretty_console_log(log_entry)
                error_data = json.dumps({
                    "error": {"message": str(e), "type": "proxy_error"},
                })
                yield f"data: {error_data}\n\n"
                yield "data: [DONE]\n\n"

        async def error_stream(e):
            error_data = json.dumps({"error": {"message": str(e), "type": "proxy_error"}})
            yield f"data: {error_data}\n\n"
            yield "data: [DONE]\n\n"

        try:
            return StreamingResponse(generate_stream(), media_type="text/event-stream")
        except Exception as e:
            return StreamingResponse(error_stream(e), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
