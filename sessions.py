"""
Session management for llm-tracer — capture, export, render, and audit conversations from raw request logs.

Usage:
    python sessions.py list [--log-path logs.jsonl]
    python sessions.py create --range "2026-04-30T10:00 2026-04-30T10:30"
    python sessions.py create --ids <uuid1> <uuid2> ...
    python sessions.py render <session_file>
    python sessions.py audit <session_file>
    python sessions.py export <session_file> --format jsonl|openai
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def discover_log_path(log_path_arg=None):
    """Reuse the same fallback chain as replay.py."""
    for candidate in [log_path_arg, "logs.jsonl", "log.jsonl"]:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def load_all_entries(log_path):
    """Load all JSONL entries from a log file."""
    entries = []
    with open(log_path, "r") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            entry["_index"] = idx
            entries.append(entry)
    return entries


def sort_entries_by_timestamp(entries):
    """Sort entries by timestamp ascending."""
    return sorted(entries, key=lambda e: e.get("timestamp", 0))


def format_timestamp(epoch_ts):
    """Format an epoch timestamp as YYYY-MM-DD HH:MM:SS."""
    dt = datetime.fromtimestamp(epoch_ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_time_only(epoch_ts):
    """Format an epoch timestamp as HH:MM:SS."""
    dt = datetime.fromtimestamp(epoch_ts, tz=timezone.utc)
    return dt.strftime("%H:%M:%S")


def count_message_roles(messages):
    """Count message roles in a messages array."""
    if not isinstance(messages, list):
        return {}
    counts = {}
    for msg in messages:
        role = msg.get("role", "unknown")
        counts[role] = counts.get(role, 0) + 1
    return counts


def role_breakdown_string(counts):
    """Create a string like 'sys:1, usr:2, asst:0' from role counts."""
    if not counts:
        return "0 msgs"
    parts = [f"{r}:{c}" for r, c in counts.items()]
    return ", ".join(parts)


def extract_text_content(content):
    """Extract text content from a message, handling both string and array block formats."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                texts.append(text)
            else:
                texts.append(str(block))
        return "\n".join(texts)
    return str(content)


def get_latest_user_message(messages):
    """Extract the latest user message from a messages array."""
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return extract_text_content(msg.get("content", ""))
    return None


def filter_conversation_messages(messages):
    """Filter messages to only user/assistant/system turns, extracting text content."""
    if not isinstance(messages, list):
        return []
    result = []
    for msg in messages:
        role = msg.get("role", "unknown")
        if role in ("user", "assistant", "system"):
            result.append({
                "role": role,
                "content": extract_text_content(msg.get("content", ""))
            })
        elif role == "tool":
            result.append({
                "role": "tool",
                "content": extract_text_content(msg.get("content", ""))
            })
    return result


def msg_list_preview(messages):
    """Create a preview of messages as '[role] content_preview' strings."""
    if not isinstance(messages, list):
        return []
    previews = []
    for msg in messages:
        role = msg.get("role", "?")
        content = extract_text_content(msg.get("content", ""))
        preview = content[:60] + "..." if len(content) > 60 else content
        previews.append(f"[{role}] {preview}")
    return previews


def get_response_text(entry):
    """Extract response text from a log entry, checking multiple fields."""
    if entry.get("response"):
        return entry["response"]
    metadata = entry.get("response_metadata", {})
    if metadata:
        pass  # response_metadata doesn't contain the raw response text
    return ""


def cmd_list(args):
    """List all log entries and allow user to select entries to create a session."""
    log_path = discover_log_path(args.log_path)
    if not log_path:
        print("Error: no log file found. Try --log-path /path/to/logs.jsonl", file=sys.stderr)
        sys.exit(1)

    entries = sort_entries_by_timestamp(load_all_entries(log_path))
    if not entries:
        print("No entries found in log file.", file=sys.stderr)
        sys.exit(1)

    # Print entry table
    print(f"\nFound {len(entries)} entries in {log_path}\n")
    for i, entry in enumerate(entries):
        idx = entry["_index"]
        ts = format_timestamp(entry["timestamp"])
        model = entry.get("model", "unknown")
        messages = entry.get("messages", [])
        msg_count = len(messages) if isinstance(messages, list) else 0
        role_counts = count_message_roles(messages)
        breakdown = role_breakdown_string(role_counts)
        tokens = entry.get("output_tokens", 0)
        endpoint = entry.get("endpoint", "?")

        print(f"[{i:>3}] {ts} · {model} · {msg_count} msgs ({breakdown}) · {tokens} out tokens · {endpoint}")

    # Interactive selection
    print("\nSelect entries to create a session:")
    print("  Enter indices/range (e.g., '0', '0-5', '0,1,3') or press Enter to skip list mode:")
    selection = input("> ").strip()

    if not selection:
        print("No selection made. Exiting list mode.")
        return

    selected_indices = set()

    if "," in selection:
        for part in selection.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                for idx in range(int(start), int(end) + 1):
                    selected_indices.add(idx)
            else:
                selected_indices.add(int(part))
    elif "-" in selection:
        start, end = selection.split("-", 1)
        for idx in range(int(start), int(end) + 1):
            selected_indices.add(idx)
    else:
        selected_indices.add(int(selection))

    # Filter entries to selected
    selected_entries = [e for i, e in enumerate(entries) if i in selected_indices]
    if not selected_entries:
        print("No valid entries selected.", file=sys.stderr)
        sys.exit(1)

    # Create session automatically (pass to create logic)
    _create_session(selected_entries, log_path)


def _create_session(selected_entries, source_log):
    """Internal: create a session file from selected entries."""
    sessions_dir = Path("sessions")
    sessions_dir.mkdir(exist_ok=True)

    # Build metadata
    models = set(e.get("model", "unknown") for e in selected_entries)
    timestamp_range = [selected_entries[0]["timestamp"], selected_entries[-1]["timestamp"]]
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    session_name = f"session_{now}"
    session_file = sessions_dir / f"{session_name}.jsonl"

    # Write session file
    with open(session_file, "w") as f:
        # Metadata header
        header = {
            "session": {
                "name": session_name,
                "created": int(datetime.now(timezone.utc).timestamp()),
                "source_log": str(source_log),
                "request_count": len(selected_entries),
                "model": ", ".join(sorted(models)) if len(models) > 1 else list(models)[0],
                "timestamp_range": timestamp_range,
                "timestamp_range_formatted": [
                    format_timestamp(timestamp_range[0]),
                    format_timestamp(timestamp_range[1])
                ]
            }
        }
        f.write(json.dumps(header) + "\n")

        # Entry lines
        for idx, entry in enumerate(selected_entries):
            session_entry = {
                "type": "entry",
                "index": idx,
                "request_id": entry.get("id", ""),
                "endpoint": entry.get("endpoint", ""),
                "timestamp": entry.get("timestamp", 0),
                "model": entry.get("model", ""),
                "sent_messages": entry.get("messages", []),
                "response": entry.get("response", ""),
                "latency_ms": entry.get("latency_ms", 0),
                "input_tokens": entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
                "error": entry.get("error", None),
                "request_params": entry.get("request_params", None),
                "response_metadata": entry.get("response_metadata", None),
            }
            f.write(json.dumps(session_entry) + "\n")

    print(f"\nSession created: {session_file}")
    print(f"  Entries: {len(selected_entries)}")
    print(f"  Model(s): {header['session']['model']}")
    print(f"  Time range: {header['session']['timestamp_range_formatted'][0]} — {header['session']['timestamp_range_formatted'][1]}")
    print(f"\nUse 'render', 'audit', or 'export' to view the session.")

    return session_file


def cmd_create(args):
    """Create a session from log entries by range or IDs."""
    log_path = discover_log_path(args.log_path)
    if not log_path:
        print("Error: no log file found. Try --log-path /path/to/logs.jsonl", file=sys.stderr)
        sys.exit(1)

    entries = sort_entries_by_timestamp(load_all_entries(log_path))

    if args.ids:
        # Create a lookup by ID
        entry_by_id = {e.get("id"): e for e in entries}
        selected = []
        for req_id in args.ids:
            if req_id in entry_by_id:
                selected.append(entry_by_id[req_id])
            else:
                print(f"Warning: request ID {req_id} not found in log.", file=sys.stderr)
        if not selected:
            print("No valid entries found for the given IDs.", file=sys.stderr)
            sys.exit(1)
    elif args.range:
        # Parse timestamp range
        ts_parts = args.range.split()
        if len(ts_parts) != 2:
            print("Error: --range requires two ISO timestamps: 'start end'", file=sys.stderr)
            print("Example: --range '2026-04-30T10:00:00 2026-04-30T10:30:00'", file=sys.stderr)
            sys.exit(1)
        try:
            start_dt = datetime.fromisoformat(ts_parts[0])
            end_dt = datetime.fromisoformat(ts_parts[1])
        except ValueError:
            print("Error: invalid timestamp format. Use ISO format: 2026-04-30T10:00:00", file=sys.stderr)
            sys.exit(1)

        start_epoch = start_dt.timestamp()
        end_epoch = end_dt.timestamp()
        selected = [e for e in entries if start_epoch <= e["timestamp"] <= end_epoch]
        if not selected:
            print(f"No entries found in range {ts_parts[0]} to {ts_parts[1]}.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: must specify --range or --ids", file=sys.stderr)
        sys.exit(1)

    _create_session(selected, log_path)


def cmd_render(args):
    """Render a session as clean markdown."""
    session_file, entries = _load_session(args.session_file)

    print(f"## Session: {session_file}")
    session_meta = session_file.get("session", {})
    model = session_meta.get("model", "unknown")
    request_count = session_meta.get("request_count", 0)
    ts_range = session_meta.get("timestamp_range_formatted", ["N/A", "N/A"])
    print(f"{model} · {request_count} requests · {ts_range[0]} – {ts_range[1]}\n")

    # Show conversation turns
    for entry in entries:
        messages = entry.get("sent_messages", [])
        response = entry.get("response", "")
        timestamp = entry.get("timestamp", 0)
        latency = entry.get("latency_ms", 0)
        input_tokens = entry.get("input_tokens", 0)
        output_tokens = entry.get("output_tokens", 0)

        # Find latest user message
        user_msg = get_latest_user_message(messages)
        msg_count = len(messages) if isinstance(messages, list) else 0

        if user_msg:
            time_str = format_time_only(timestamp)
            print("---")
            print()
            print(f"**User** ({time_str}, {msg_count} msgs, {input_tokens} tokens)")
            print(f"> {user_msg[:500]}\n")

        if response:
            print(f"**Assistant** ({latency:.0f}ms, {output_tokens} tokens)")
            print(f"> {response[:500]}")
            if len(response) > 500:
                print("> ...")
            print()
        elif entry.get("error"):
            print(f"**Assistant** ({latency:.0f}ms) — (error: {entry['error']})\n")
        else:
            print(f"**Assistant** ({latency:.0f}ms, {output_tokens} tokens) — (error/no response)\n")


def cmd_audit(args):
    """Audit a session — chronological request → response chain."""
    session_file, entries = _load_session(args.session_file)

    session_meta = session_file.get("session", {})
    source_log = session_meta.get("source_log", "unknown")
    model = session_meta.get("model", "unknown")
    ts_range = session_meta.get("timestamp_range", [0, 0])
    duration_secs = ts_range[1] - ts_range[0] if ts_range else 0

    print("## Audit")
    print(f"\nSource: {source_log} · Model: {model} · Duration: {int(duration_secs // 60)}m {int(duration_secs % 60)}s\n")

    total_latency = 0
    total_input_tokens = 0
    total_output_tokens = 0
    models_seen = set()

    for entry in entries:
        req_id = entry.get("request_id", "unknown")[:8]
        timestamp = entry.get("timestamp", 0)
        latency = entry.get("latency_ms", 0)
        model = entry.get("model", "unknown")
        models_seen.add(model)
        input_tokens = entry.get("input_tokens", 0)
        output_tokens = entry.get("output_tokens", 0)
        endpoint = entry.get("endpoint", "unknown")
        messages = entry.get("sent_messages", [])
        response = entry.get("response", "")
        error = entry.get("error")
        time_str = format_time_only(timestamp)

        total_latency += latency
        total_input_tokens += input_tokens
        total_output_tokens += output_tokens

        model_flag = ""
        if len(models_seen) > 1:
            model_flag = f" ← NEW"

        print(f"### Entry {entry.get('index', '?')} · {req_id} · {time_str} · {latency:.0f}ms · in:{input_tokens} out:{output_tokens}{model_flag}")
        print(f"  Endpoint: {endpoint}")

        if messages:
            preview = msg_list_preview(messages)
            for p in preview:
                print(f"  Sent: {p}")

        if response and len(response) < 300:
            print(f"  Response: \"{response}\"")
        elif response:
            print(f"  Response: \"{response[:200]}...\"")
        elif error:
            print(f"  Error: {error}")
        else:
            print(f"  Response: (none)")

        print()

    # Summary stats
    avg_latency = total_latency / len(entries) if entries else 0
    print("---")
    print(f"\nSession stats: {len(entries)} requests, avg latency {avg_latency:.0f}ms, total tokens {total_input_tokens + total_output_tokens}")
    if len(models_seen) > 1:
        print(f"Models used: {', '.join(sorted(models_seen))}")


def cmd_export(args):
    """Export a session to portable format."""
    session_file, entries = _load_session(args.session_file)
    session_name = session_file.get("session", {}).get("name", "session")

    if args.format == "jsonl":
        # Portable conversation JSONL: one line per message
        for entry in entries:
            messages = entry.get("sent_messages", [])
            timestamp = entry.get("timestamp", 0)
            latency = entry.get("latency_ms", 0)
            input_tokens = entry.get("input_tokens", 0)
            output_tokens = entry.get("output_tokens", 0)
            idx = entry.get("index", 0)

            for msg in messages:
                role = msg.get("role", "unknown")
                content = extract_text_content(msg.get("content", ""))
                exported = {
                    "session": session_name,
                    "timestamp": timestamp,
                    "role": role,
                    "content": content,
                    "source_entry": idx
                }
                print(json.dumps(exported))

            # Add summary line for assistant response
            if entry.get("response"):
                summary = {
                    "session": session_name,
                    "timestamp": timestamp,
                    "role": "assistant",
                    "content": entry["response"],
                    "source_entry": idx,
                    "latency_ms": latency,
                    "tokens_in": input_tokens,
                    "tokens_out": output_tokens
                }
                print(json.dumps(summary))

    elif args.format == "openai":
        # OpenAI assistant import format
        export_dir = Path(f"exports/{session_name}")
        export_dir.mkdir(parents=True, exist_ok=True)

        for entry in entries:
            messages = entry.get("sent_messages", [])
            idx = entry.get("index", 0)
            filename = f"{session_name}__{idx:02d}.jsonl"
            filepath = export_dir / filename

            with open(filepath, "w") as f:
                # Filter to only user/assistant/system messages
                conv_msgs = filter_conversation_messages(messages)
                for msg in conv_msgs:
                    line = json.dumps({"role": msg["role"], "content": msg["content"]})
                    f.write(line + "\n")

            print(f"  Created: {filepath}")
    else:
        print(f"Error: unknown format '{args.format}'. Use 'jsonl' or 'openai'.", file=sys.stderr)
        sys.exit(1)


def _load_session(session_path_str):
    """Load a session file and return (metadata, entries)."""
    session_path = Path(session_path_str)
    if not session_path.exists():
        # Try in sessions/ directory
        alt = Path("sessions") / session_path_str
        if alt.exists():
            session_path = alt
        else:
            print(f"Error: session file '{session_path_str}' not found.", file=sys.stderr)
            sys.exit(1)

    metadata = None
    entries = []
    with open(session_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "session" in obj:
                metadata = obj
            elif obj.get("type") == "entry":
                entries.append(obj)
            else:
                print(f"Warning: unrecognized line in session file: {list(obj.keys())[:3]}", file=sys.stderr)

    if not metadata:
        print("Error: no session metadata found.", file=sys.stderr)
        sys.exit(1)
    if not entries:
        print("Error: no session entries found.", file=sys.stderr)
        sys.exit(1)

    return metadata, entries


def main():
    parser = argparse.ArgumentParser(
        description="Session management for llm-tracer — capture, export, render, and audit conversations.",
        prog="sessions"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List all log entries and optionally create a session")
    list_parser.add_argument("--log-path", default=None, help="Path to the JSONL log file")

    # create subcommand
    create_parser = subparsers.add_parser("create", help="Create a new session by selecting entries")
    create_parser.add_argument("--range", dest="range", default=None,
                               help='Two ISO timestamps: "start end"')
    create_parser.add_argument("--ids", nargs="+", default=None,
                               help="Exact request IDs")
    create_parser.add_argument("--log-path", default=None, help="Path to the JSONL log file")

    # render subcommand
    render_parser = subparsers.add_parser("render", help="Render a session as markdown")
    render_parser.add_argument("session_file", help="Session file path (e.g., sessions/session_20260430_103000.jsonl)")

    # audit subcommand
    audit_parser = subparsers.add_parser("audit", help="Audit session — request/response chain")
    audit_parser.add_argument("session_file", help="Session file path")

    # export subcommand
    export_parser = subparsers.add_parser("export", help="Export session to portable format")
    export_parser.add_argument("session_file", help="Session file path")
    export_parser.add_argument("--format", dest="format", required=True, choices=["jsonl", "openai"],
                               help="Export format: jsonl or openai")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "list": cmd_list,
        "create": cmd_create,
        "render": cmd_render,
        "audit": cmd_audit,
        "export": cmd_export,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
