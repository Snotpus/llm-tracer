import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="LLM Tracer Proxy", version="0.2.0")

@app.on_event("startup")
async def startup():
    app.state.httpx_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        follow_redirects=True,
    )

@app.on_event("shutdown")
async def shutdown():
    await app.state.httpx_client.aclose()

def _iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"

_config = {}
_config_path = Path("config.json")
if _config_path.exists():
    _config = json.loads(_config_path.read_text())

_LOG_DETAIL = _config.get("log_detail", "detailed")


def get_ollama_base() -> str:
    return _config.get("ollama_base_url", "http://localhost:11434")


def get_log_path() -> str:
    return _config.get("log_path", "logs.jsonl")


def get_db_path() -> str | None:
    db = _config.get("db_path", "")
    if db and db.endswith(".duckdb"):
        p = Path(db)
        if not p.is_absolute():
            p = Path.cwd() / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    return None


def write_db(log_entry: dict):
    db_path = get_db_path()
    if not db_path:
        return
    import duckdb
    from datetime import datetime

    conn = duckdb.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS traces (id TEXT PRIMARY KEY, endpoint TEXT, model TEXT, timestamp TIMESTAMP, latency_ms DOUBLE, input_tokens BIGINT, output_tokens BIGINT, messages JSON, prompt JSON, response TEXT, error TEXT, request_params JSON, response_metadata JSON, raw_body JSON, loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_endpoint ON traces(endpoint)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_model ON traces(model)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON traces(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_error ON traces(error)")

        ts = log_entry.get("timestamp")
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts)

        conn.execute(
            """INSERT OR REPLACE INTO traces
               (id, endpoint, model, timestamp, latency_ms, input_tokens,
                output_tokens, messages, prompt, response, error,
                request_params, response_metadata, raw_body)
               VALUES (?,?,?,?,?, ?,?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                log_entry.get("id"),
                log_entry.get("endpoint"),
                log_entry.get("model"),
                ts,
                log_entry.get("latency_ms"),
                log_entry.get("input_tokens"),
                log_entry.get("output_tokens"),
                json.dumps(log_entry.get("messages")) if log_entry.get("messages") is not None else None,
                json.dumps(log_entry.get("prompt")) if log_entry.get("prompt") is not None else None,
                log_entry.get("response"),
                log_entry.get("error"),
                json.dumps(log_entry.get("request_params")) if log_entry.get("request_params") is not None else None,
                json.dumps(log_entry.get("response_metadata")) if log_entry.get("response_metadata") is not None else None,
                log_entry.get("raw_body"),
            ],
        )
    finally:
        conn.close()


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
        f"\n[{_iso()}] [{status}] req={entry.get('id')} model={entry.get('model')} "
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
        "_log_detail": _LOG_DETAIL,
    }

    if _LOG_DETAIL in ("detailed", "full"):
        params = {}
        if "stream" in body:
            params["stream"] = body["stream"]
        if "options" in body:
            params["options"] = body["options"]
        if "template" in body:
            params["template"] = body["template"]
        if "system" in body:
            params["system"] = body["system"]
        if "context" in body:
            params["context"] = body["context"]
        if "images" in body:
            params["images"] = body["images"]
        if "tools" in body:
            params["tools"] = body["tools"]
        log_entry["request_params"] = params

    try:
        start_external = time.time()
        resp = await app.state.httpx_client.post(f"{get_ollama_base()}/api/generate", json=body)
        data = resp.json()

        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["upstream_latency_ms"] = (time.time() - start_external) * 1000
        log_entry["response"] = data.get("response", "")
        log_entry["output_tokens"] = estimate_tokens(data.get("response", ""))

        if _LOG_DETAIL in ("detailed", "full"):
            metadata = {}
            if "usage" in data:
                metadata["usage"] = data["usage"]
            if "done" in data:
                metadata["done"] = data["done"]
            if "done_reason" in data:
                metadata["done_reason"] = data["done_reason"]
            if "model" in data and data["model"] != log_entry["model"]:
                metadata["response_model"] = data["model"]
            log_entry["response_metadata"] = metadata

        write_log(log_entry)
        write_db(log_entry)
        pretty_console_log(log_entry)
        return data

    except Exception as e:
        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["error"] = str(e)
        write_log(log_entry)
        write_db(log_entry)
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
        "_log_detail": _LOG_DETAIL,
    }

    if _LOG_DETAIL in ("detailed", "full"):
        params = {}
        if "stream" in body:
            params["stream"] = body["stream"]
        if "options" in body:
            params["options"] = body["options"]
        if "tools" in body:
            params["tools"] = body["tools"]
        if "tool_choice" in body:
            params["tool_choice"] = body["tool_choice"]
        log_entry["request_params"] = params

    try:
        start_external = time.time()
        resp = await app.state.httpx_client.post(f"{get_ollama_base()}/api/chat", json=body)
        data = resp.json()

        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["upstream_latency_ms"] = (time.time() - start_external) * 1000
        log_entry["response"] = data.get("message", {}).get("content", "")
        log_entry["output_tokens"] = estimate_tokens(log_entry["response"])

        if _LOG_DETAIL in ("detailed", "full"):
            metadata = {}
            if "usage" in data:
                metadata["usage"] = data["usage"]
            if "done" in data:
                metadata["done"] = data["done"]
            if "done_reason" in data:
                metadata["done_reason"] = data["done_reason"]
            log_entry["response_metadata"] = metadata

        write_log(log_entry)
        write_db(log_entry)
        pretty_console_log(log_entry)
        return data

    except Exception as e:
        log_entry["latency_ms"] = (time.time() - start) * 1000
        log_entry["error"] = str(e)
        write_log(log_entry)
        write_db(log_entry)
        pretty_console_log(log_entry)
        return JSONResponse(status_code=502, content={"error": str(e)})


# ── OpenAI-compatible endpoints ───────────────────────────────────────────────

@app.get("/api/version")
async def api_version():
    """Proxy Ollama's /api/version endpoint."""
    try:
        resp = await app.state.httpx_client.get(f"{get_ollama_base()}/api/version", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {"error": "unable to reach Ollama"}, 502


@app.get("/api/tags")
async def api_tags():
    """Proxy Ollama's /api/tags endpoint."""
    try:
        resp = await app.state.httpx_client.get(f"{get_ollama_base()}/api/tags", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {"error": "unable to reach Ollama"}, 502


@app.get("/v1/models")
async def list_models():
    """Proxy Ollama's model list in OpenAI format."""
    try:
        resp = await app.state.httpx_client.get(f"{get_ollama_base()}/api/tags", timeout=10)
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
        f"[{_iso()}] [DEBUG] /v1/chat/completions stream={stream} model={model} "
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
        "_log_detail": _LOG_DETAIL,
    }

    if _LOG_DETAIL in ("detailed", "full"):
        params = {}
        if "max_tokens" in body:
            params["max_tokens"] = body["max_tokens"]
        if "temperature" in body:
            params["temperature"] = body["temperature"]
        if "top_p" in body:
            params["top_p"] = body["top_p"]
        if "frequency_penalty" in body:
            params["frequency_penalty"] = body["frequency_penalty"]
        if "presence_penalty" in body:
            params["presence_penalty"] = body["presence_penalty"]
        if "stream" in body:
            params["stream"] = body["stream"]
        if "tools" in body:
            params["tools"] = body["tools"]
        if "tool_choice" in body:
            params["tool_choice"] = body["tool_choice"]
        if "logit_bias" in body:
            params["logit_bias"] = body["logit_bias"]
        if "response_format" in body:
            params["response_format"] = body["response_format"]
        if "stream_options" in body:
            params["stream_options"] = body["stream_options"]
        log_entry["request_params"] = params

    ollama_url = f"{get_ollama_base()}/v1/chat/completions"

    if stream:
        async def generate_stream():
            accumulated_content = ""
            accumulated_metadata = {}
            stream_error = None
            try:
                async with app.state.httpx_client.stream(
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
                                if "usage" in chunk:
                                    accumulated_metadata["usage"] = chunk["usage"]
                                if "model" in chunk:
                                    accumulated_metadata["model"] = chunk["model"]
                            except Exception:
                                pass

                log_entry["latency_ms"] = (time.time() - start) * 1000
                log_entry["response"] = accumulated_content
                log_entry["output_tokens"] = estimate_tokens(accumulated_content)

                if _LOG_DETAIL in ("detailed", "full") and accumulated_metadata:
                    entry_metadata = {}
                    if "usage" in accumulated_metadata:
                        entry_metadata["usage"] = accumulated_metadata["usage"]
                    if "model" in accumulated_metadata:
                        entry_metadata["response_model"] = accumulated_metadata["model"]
                    log_entry["response_metadata"] = entry_metadata

                write_log(log_entry)
                write_db(log_entry)
                pretty_console_log(log_entry)

            except Exception as e:
                stream_err = str(e)
                log_entry["latency_ms"] = (time.time() - start) * 1000
                log_entry["error"] = stream_err
                write_log(log_entry)
                write_db(log_entry)
                pretty_console_log(log_entry)
                error_data = json.dumps({"error": {"message": stream_err, "type": "proxy_error"}})
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
            resp = await app.state.httpx_client.post(
                ollama_url,
                json=body,
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()

            log_entry["latency_ms"] = (time.time() - start) * 1000
            content = ""
            finish_reasons = []
            for choice in data.get("choices", []):
                content += choice.get("message", {}).get("content") or ""
                if "finish_reason" in choice:
                    finish_reasons.append({"index": choice.get("index"), "finish_reason": choice["finish_reason"]})
            log_entry["response"] = content
            log_entry["output_tokens"] = estimate_tokens(content)

            if _LOG_DETAIL in ("detailed", "full"):
                metadata = {}
                if "usage" in data:
                    metadata["usage"] = data["usage"]
                if finish_reasons:
                    metadata["finish_reason"] = finish_reasons
                if "model" in data and data["model"] != log_entry["model"]:
                    metadata["response_model"] = data["model"]
                log_entry["response_metadata"] = metadata

            write_log(log_entry)
            write_db(log_entry)
            pretty_console_log(log_entry)
            return data

        except Exception as e:
            log_entry["latency_ms"] = (time.time() - start) * 1000
            log_entry["error"] = str(e)
            write_log(log_entry)
            write_db(log_entry)
            pretty_console_log(log_entry)
            return JSONResponse(
                status_code=502,
                content={"error": {"message": str(e), "type": "proxy_error"}},
            )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)