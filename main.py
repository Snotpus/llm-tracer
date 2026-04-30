import json
import sys
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="LLM Tracer Proxy", version="0.2.0")

_config = {}
_config_path = Path("config.json")
if _config_path.exists():
    _config = json.loads(_config_path.read_text())


def get_ollama_base() -> str:
    return _config.get("ollama_base_url", "http://localhost:11434")


def get_log_path() -> str:
    return _config.get("log_path", "logs.jsonl")


def estimate_tokens(text: str) -> int:
    return len(text.split()) if text else 0


def write_log(entry: dict):
    log_path = get_log_path()
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def pretty_console_log(entry: dict):
    status = "ERR" if entry.get("error") else "OK"
    print(
        f"\n[{status}] req={entry.get('id')} model={entry.get('model')} "
        f"latency={entry.get('latency_ms', 0):.0f}ms "
        f"input_tokens={entry.get('input_tokens', 0)} "
        f"output_tokens={entry.get('output_tokens', 0)}",
        file=sys.stderr,
    )
    if entry.get("error"):
        print(f"  error: {entry['error']}", file=sys.stderr)
    print(file=sys.stderr)


# ── Ollama native endpoints (passthrough) ────────────────────────────────────

@app.post("/api/generate")
async def api_generate(request: Request):
    """Passthrough to Ollama /api/generate with logging."""
    req_id = str(uuid.uuid4())
    start = time.time()
    body = await request.json()

    log_entry = {
        "id": req_id,
        "endpoint": "/api/generate",
        "timestamp": time.time(),
        "model": body.get("model", "unknown"),
        "prompt": body.get("prompt", ""),
        "response": "",
        "latency_ms": 0,
        "input_tokens": estimate_tokens(body.get("prompt", "")),
        "output_tokens": 0,
        "error": None,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{get_ollama_base()}/api/generate", json=body)
            data = resp.json()

        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["response"] = data.get("response", "")
        log_entry["output_tokens"] = estimate_tokens(data.get("response", ""))
        write_log(log_entry)
        pretty_console_log(log_entry)
        return data

    except Exception as e:
        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["error"] = str(e)
        write_log(log_entry)
        pretty_console_log(log_entry)
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/api/chat")
async def api_chat(request: Request):
    """Passthrough to Ollama /api/chat with logging."""
    req_id = str(uuid.uuid4())
    start = time.time()
    body = await request.json()
    messages = body.get("messages", [])

    log_entry = {
        "id": req_id,
        "endpoint": "/api/chat",
        "timestamp": time.time(),
        "model": body.get("model", "unknown"),
        "messages": messages,
        "response": "",
        "latency_ms": 0,
        "input_tokens": sum(estimate_tokens(m.get("content", "")) for m in messages),
        "output_tokens": 0,
        "error": None,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{get_ollama_base()}/api/chat", json=body)
            data = resp.json()

        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["response"] = data.get("message", {}).get("content", "")
        log_entry["output_tokens"] = estimate_tokens(log_entry["response"])
        write_log(log_entry)
        pretty_console_log(log_entry)
        return data

    except Exception as e:
        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["error"] = str(e)
        write_log(log_entry)
        pretty_console_log(log_entry)
        return JSONResponse(status_code=502, content={"error": str(e)})


# ── OpenAI-compatible endpoints ───────────────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    """Proxy Ollama's model list in OpenAI format."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{get_ollama_base()}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                return {
                    "object": "list",
                    "data": [
                        {
                            "id": m["name"],
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "ollama",
                        }
                        for m in models
                    ],
                }
    except Exception:
        pass
    return {"object": "list", "data": []}


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """
    Forward OpenAI-compatible requests directly to Ollama's own
    /v1/chat/completions endpoint. This preserves tools, streaming,
    stream_options, and every other OpenAI field without translation.
    """
    req_id = str(uuid.uuid4())
    start = time.time()
    body = await request.json()
    model = body.get("model", "unknown")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    print(
        f"[DEBUG] /v1/chat/completions stream={stream} model={model} "
        f"tools={len(body.get('tools', []))} tool_choice={body.get('tool_choice')}",
        file=sys.stderr,
    )

    log_entry = {
        "id": req_id,
        "endpoint": "/v1/chat/completions",
        "timestamp": time.time(),
        "model": model,
        "messages": messages,
        "response": "",
        "latency_ms": 0,
        "input_tokens": sum(
            estimate_tokens(m.get("content", "") if isinstance(m.get("content"), str) else "")
            for m in messages
        ),
        "output_tokens": 0,
        "error": None,
    }

    ollama_url = f"{get_ollama_base()}/v1/chat/completions"

    if stream:
        async def generate_stream():
            accumulated_content = ""
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream(
                        "POST",
                        ollama_url,
                        json=body,
                        headers={"Content-Type": "application/json"},
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            # Forward the line as-is — Ollama already speaks SSE
                            yield f"{line}\n\n"

                            # Accumulate content for logging
                            if line.startswith("data: ") and line != "data: [DONE]":
                                try:
                                    chunk = json.loads(line[6:])
                                    for choice in chunk.get("choices", []):
                                        accumulated_content += (
                                            choice.get("delta", {}).get("content") or ""
                                        )
                                except Exception:
                                    pass

                log_entry["latency_ms"] = (time.time() - start) * 1000
                log_entry["response"] = accumulated_content
                log_entry["output_tokens"] = estimate_tokens(accumulated_content)
                write_log(log_entry)
                pretty_console_log(log_entry)

            except Exception as e:
                log_entry["latency_ms"] = (time.time() - start) * 1000
                log_entry["error"] = str(e)
                write_log(log_entry)
                pretty_console_log(log_entry)
                error_data = json.dumps({"error": {"message": str(e), "type": "proxy_error"}})
                yield f"data: {error_data}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    else:
        # Non-streaming: forward and return directly
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    ollama_url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
                data = resp.json()

            log_entry["latency_ms"] = (time.time() - start) * 1000
            content = ""
            for choice in data.get("choices", []):
                content += choice.get("message", {}).get("content") or ""
            log_entry["response"] = content
            log_entry["output_tokens"] = estimate_tokens(content)
            write_log(log_entry)
            pretty_console_log(log_entry)
            return data

        except Exception as e:
            log_entry["latency_ms"] = (time.time() - start) * 1000
            log_entry["error"] = str(e)
            write_log(log_entry)
            pretty_console_log(log_entry)
            return JSONResponse(
                status_code=502,
                content={"error": {"message": str(e), "type": "proxy_error"}},
            )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)