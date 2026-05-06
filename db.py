#!/usr/bin/env python3
"""DuckDB CLI: init, import, stats, query, analyze."""

import json
import os
import sys
import time
from pathlib import Path

import duckdb

# ── helpers ──────────────────────────────────────────────────────────────

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id              TEXT PRIMARY KEY,
    endpoint        TEXT,
    model           TEXT,
    timestamp       TIMESTAMP,
    latency_ms      DOUBLE,
    input_tokens    BIGINT,
    output_tokens   BIGINT,
    messages        JSON,
    prompt          JSON,
    response        TEXT,
    error           TEXT,
    request_params  JSON,
    response_metadata JSON,
    raw_body        JSON,
    loaded_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_traces_endpoint ON traces(endpoint);
CREATE INDEX IF NOT EXISTS idx_traces_model    ON traces(model);
CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON traces(timestamp);
CREATE INDEX IF NOT EXISTS idx_traces_error     ON traces(error);
"""

CONFIG_PATH = Path("config.json")
DEFAULT_DB = "db/traces.duckdb"


def get_db_path() -> str:
    """Read db_path from config.json, falling back to default."""
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        db = cfg.get("db_path", "")
        if db and db.endswith(".duckdb"):
            return db
    return DEFAULT_DB


def ensure_db_dir(db_path: str) -> str:
    """Create db/ directory if needed, return absolute-ish path."""
    p = Path(db_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def connect(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open and return a DB connection, auto-creating the DB if needed."""
    db_path = ensure_db_dir(db_path)
    if not os.path.exists(db_path):
        conn = duckdb.connect(db_path)
        conn.sql(DB_SCHEMA)
        conn.close()
        print(f"Initialized new DB: {db_path}")
        conn = duckdb.connect(db_path)
        return conn
    conn = duckdb.connect(db_path)
    return conn


def table_formatter(rows, columns, width=16):
    """Column-aligned table output."""
    if not rows:
        return "No rows.\n"
    # Compute column widths
    widths = {col: len(col) for col in columns}
    rows = [list(r) for r in rows]
    for r in rows:
        for i, val in enumerate(r):
            widths[columns[i]] = max(widths[columns[i]], len(str(val)))
    col_w = {k: max(v, width) for k, v in widths.items()}
    lines = []
    header = " | ".join(col.ljust(col_w[col]) for col in columns)
    lines.append(header)
    sep = "-+-".join("-" * col_w[col] for col in columns)
    lines.append(sep)
    for r in rows:
        row_str = " | ".join(
            (str(val) if val is not None else "NULL").ljust(col_w[col])
            for col, val in zip(columns, r)
        )
        lines.append(row_str)
    return "\n".join(lines) + "\n"


# ── subcommands ──────────────────────────────────────────────────────────


def cmd_init():
    db_path = get_db_path()
    conn = connect(db_path)
    conn.sql(DB_SCHEMA)
    conn.close()
    print(f"DB initialized: {ensure_db_dir(db_path)}")


def cmd_import():
    db_path = get_db_path()
    log_path = "logs.jsonl"
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        log_path = cfg.get("log_path", log_path)

    if not os.path.exists(log_path):
        print(f"Log file not found: {log_path}")
        return

    conn = connect(db_path)
    total = 0
    success = 0
    skipped = 0
    PROGRESS_INTERVAL = 1000
    rows_to_insert = []

    INSERT_SQL = (
        "INSERT OR REPLACE INTO traces "
        "(id,endpoint,model,timestamp,latency_ms,input_tokens, "
        "output_tokens,messages,prompt,response,error, "
        "request_params,response_metadata,raw_body) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    )

    with open(log_path, "r") as f:
        for line_num, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                entry = json.loads(line)
                if not entry.get("id"):
                    skipped += 1
                    continue
                ts = entry.get("timestamp")
                if ts and isinstance(ts, (int, float)):
                    import datetime
                    ts = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

                row = (
                    entry.get("id"),
                    entry.get("endpoint"),
                    entry.get("model"),
                    ts,
                    entry.get("latency_ms"),
                    entry.get("input_tokens"),
                    entry.get("output_tokens"),
                    json.dumps(entry.get("messages")) if entry.get("messages") else None,
                    json.dumps(entry.get("prompt")) if entry.get("prompt") else None,
                    json.dumps(entry.get("response")) if entry.get("response") and isinstance(entry["response"], dict) else entry.get("response"),
                    entry.get("error"),
                    json.dumps(entry.get("request_params")) if entry.get("request_params") else None,
                    json.dumps(entry.get("response_metadata")) if entry.get("response_metadata") else None,
                    json.dumps(entry.get("raw_body")) if entry.get("raw_body") else None,
                )
                rows_to_insert.append(row)
                if len(rows_to_insert) >= 500:
                    conn.executemany(INSERT_SQL, rows_to_insert)
                    success += len(rows_to_insert)
                    rows_to_insert = []
                    if line_num % PROGRESS_INTERVAL < 1000:
                        print(f"\r  imported {total} lines...", end="", flush=True)
            except (json.JSONDecodeError, KeyError) as exc:
                skipped += 1
                print(f"  warning: skipping malformed line {total}: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"\n  FATAL at line {total}: {exc}", file=sys.stderr)
                conn.close()
                raise

    if rows_to_insert:
        conn.executemany(INSERT_SQL, rows_to_insert)
        success += len(rows_to_insert)
        rows_to_insert = []

    print(f"\r  imported {total} lines... done.  ", end="", flush=True)

    conn.close()
    print(f"\nImport complete: {success}/{total} rows inserted, {skipped} skipped")
    print(f"DB: {ensure_db_dir(db_path)}")


def cmd_stats():
    db_path = get_db_path()
    conn = connect(db_path)
    row_count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    if row_count == 0:
        print("DB is empty. Run 'python db.py import' first.")
        conn.close()
        return
    date_range = conn.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM traces"
    ).fetchone()
    models = conn.execute("SELECT DISTINCT model FROM traces ORDER BY model").fetchall()
    total_input = conn.execute("SELECT SUM(input_tokens) FROM traces").fetchone()[0] or 0
    total_output = conn.execute("SELECT SUM(output_tokens) FROM traces").fetchone()[0] or 0
    errors = conn.execute("SELECT COUNT(*) FROM traces WHERE error IS NOT NULL").fetchone()[0]

    print(f"  rows: {row_count}")
    print(f"  date range: {date_range[0]} to {date_range[1]}")
    print(f"  models: {', '.join(m[0] for m in models)}")
    print(f"  total input tokens: {total_input:,}")
    print(f"  total output tokens: {total_output:,}")
    print(f"  errors: {errors}")
    conn.close()


def cmd_query(sql=None):
    db_path = get_db_path()
    conn = connect(db_path)

    # If no SQL provided, show top 20 rows
    if sql is None:
        sql = "SELECT * FROM traces ORDER BY timestamp DESC LIMIT 20"

    # Support \p to pipe to pager
    if sql.strip().endswith("\\p"):
        sql = sql.rstrip("\\p").rstrip()
        import subprocess
        result = conn.execute(sql).fetchall()
        columns = [description[0] for description in conn.execute(sql).description]
        output = table_formatter(result, columns)
        subprocess.run(["less", "-R"], input=output, text=True)
        conn.close()
        return

    result = conn.execute(sql)
    rows = result.fetchall()
    columns = [description[0] for description in result.description] if result.description else []

    if not sql.strip().upper().startswith("SELECT"):
        print(f"{len(rows)} row(s) affected.")
    else:
        print(table_formatter(rows, columns))

    conn.close()


def cmd_analyze(what=None):
    db_path = get_db_path()
    conn = connect(db_path)

    if what in (None, "token"):
        # Token usage by model over time
        rows = conn.execute("""
            SELECT model,
                   strftime(timestamp, '%Y-%m-%d') AS day,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens
            FROM traces
            GROUP BY model, day
            ORDER BY model, day
        """).fetchall()
        print("\n── Token usage by model over time ──")
        print(table_formatter(rows, ["model", "day", "input_tokens", "output_tokens"], width=14))

    if what in (None, "latency"):
        # Latency percentiles
        rows = conn.execute("""
            SELECT
                model,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99,
                AVG(latency_ms) AS avg_ms,
                COUNT(*) AS cnt
            FROM traces
            WHERE latency_ms > 0
            GROUP BY model
            ORDER BY avg_ms DESC
        """).fetchall()
        print("\n── Latency distribution by model ──")
        print(table_formatter(rows, ["model", "p50", "p95", "p99", "avg_ms", "count"], width=12))

    if what in (None, "errors"):
        # Error rate by endpoint
        rows = conn.execute("""
            SELECT endpoint,
                   COUNT(*) AS total,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                   ROUND(100.0 * SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS error_rate_pct
            FROM traces
            GROUP BY endpoint
            ORDER BY error_rate_pct DESC
        """).fetchall()
        print("\n── Error rate by endpoint ──")
        print(table_formatter(rows, ["endpoint", "total", "errors", "error_rate_pct"], width=14))

    if what in (None, "model"):
        # Model comparison
        rows = conn.execute("""
            SELECT
                model,
                COUNT(*) AS reqs,
                ROUND(AVG(latency_ms), 1) AS avg_latency,
                SUM(input_tokens) AS total_input,
                SUM(output_tokens) AS total_output
            FROM traces
            GROUP BY model
            ORDER BY reqs DESC
        """).fetchall()
        print("\n── Model comparison ──")
        print(table_formatter(rows, ["model", "reqs", "avg_latency", "total_input", "total_output"], width=14))

    if what in (None, "endpoint"):
        # Endpoint distribution
        rows = conn.execute("""
            SELECT endpoint, COUNT(*) AS cnt,
                   ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM traces), 1) AS pct
            FROM traces
            GROUP BY endpoint
            ORDER BY cnt DESC
        """).fetchall()
        print("\n── Endpoint distribution ──")
        print(table_formatter(rows, ["endpoint", "count", "pct%"], width=14))

    conn.close()


# ── entry point ──────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python db.py <subcommand> [args]")
        print("  init             Initialize DB with schema")
        print("  import           Import JSONL logs into DB")
        print("  query \"SQL...\"  Run SQL query")
        print("  analyze [token|latency|errors|model|endpoint]")
        print("  stats            Quick summary")
        return

    cmd = args[0]

    if cmd == "init":
        cmd_init()
    elif cmd == "import":
        cmd_import()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "query":
        sql = " ".join(args[1:]) if len(args) > 1 else None
        cmd_query(sql)
    elif cmd == "analyze":
        what = args[1] if len(args) > 1 else None
        cmd_analyze(what)
    else:
        print(f"Unknown subcommand: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
