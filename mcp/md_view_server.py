#!/usr/bin/env python3
"""Minimal MCP server exposing md-view as a tool."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SERVER_INFO = {"name": "md-view-mcp", "version": "0.1.0"}
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {
    LATEST_PROTOCOL_VERSION,
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
}


REPO_ROOT = Path(__file__).resolve().parents[1]


def default_md_view_bin() -> Path:
    return REPO_ROOT / "bin" / "md-view"


def default_log_dir() -> Path:
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    ).expanduser()
    return state_home / "md-view" / "mcp"


MD_VIEW_BIN = Path(
    os.environ.get("MD_VIEW_BIN", str(default_md_view_bin()))
).expanduser()
LOG_DIR = Path(
    os.environ.get("MD_VIEW_MCP_LOG_DIR", str(default_log_dir()))
).expanduser()
SERVER_LOG = LOG_DIR / "server.log"
URL_RE = re.compile(r"md-view:\s+(http://127\.0\.0\.1:\d+/)")
STARTUP_TIMEOUT_SECONDS = float(os.environ.get("MD_VIEW_STARTUP_TIMEOUT_SECONDS", "60"))
RPC_MODE = "content-length"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolInvocationError(Exception):
    pass


def debug_log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with SERVER_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


@dataclass
class LaunchResult:
    url: str
    pid: int
    files: list[str]
    browser: str
    stdout_log: str
    stderr_log: str


MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdx", ".mdown", ".mkd"}


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Markdown file paths to open. Relative paths resolve from the opencode session working directory.",
        },
        "browser": {
            "type": "string",
            "enum": ["firefox", "none"],
            "description": "Whether md-view should open Firefox immediately or only return the local preview URL.",
            "default": "firefox",
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "Optional preferred localhost port. md-view will choose a nearby free port unless an exact port is explicitly requested and unavailable.",
        },
    },
    "required": ["paths"],
    "additionalProperties": False,
}


def send_message(message: dict[str, Any]) -> None:
    debug_log(f"send: {json.dumps(message, ensure_ascii=False)}")
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if RPC_MODE == "ndjson":
        sys.stdout.buffer.write(payload + b"\n")
    else:
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header)
        sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def read_message() -> dict[str, Any] | None:
    global RPC_MODE
    headers: dict[str, str] = {}
    first_line = sys.stdin.buffer.readline()
    if not first_line:
        debug_log("stdin EOF")
        return None

    stripped = first_line.lstrip()
    if stripped.startswith(b"{"):
        RPC_MODE = "ndjson"
        try:
            message = json.loads(first_line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise JsonRpcError(-32700, f"Invalid NDJSON body: {exc}") from exc
        debug_log(f"recv ndjson body={json.dumps(message, ensure_ascii=False)}")
        return message

    while True:
        line = first_line if not headers else sys.stdin.buffer.readline()
        if not line:
            debug_log("stdin EOF")
            return None
        if line in (b"\r\n", b"\n"):
            break
        try:
            key, value = line.decode("ascii").split(":", 1)
        except ValueError as exc:
            raise JsonRpcError(-32700, f"Malformed header line: {line!r}") from exc
        headers[key.strip().lower()] = value.strip()
        first_line = b""

    length_text = headers.get("content-length")
    if length_text is None:
        raise JsonRpcError(-32700, "Missing Content-Length header")
    try:
        length = int(length_text)
    except ValueError as exc:
        raise JsonRpcError(-32700, f"Invalid Content-Length: {length_text}") from exc

    body = sys.stdin.buffer.read(length)
    if len(body) != length:
        raise JsonRpcError(-32700, "Unexpected EOF while reading request body")
    try:
        message = json.loads(body.decode("utf-8"))
        debug_log(
            f"recv headers={headers} body={json.dumps(message, ensure_ascii=False)}"
        )
        return message
    except json.JSONDecodeError as exc:
        raise JsonRpcError(-32700, f"Invalid JSON body: {exc}") from exc


def make_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def drain_stream(stream: Any, line_queue: queue.Queue[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        while True:
            line = stream.readline()
            if line == "":
                break
            clean_line = line.rstrip("\n")
            line_queue.put(clean_line)
            log_file.write(clean_line + "\n")
            log_file.flush()
    stream.close()


def resolve_paths(raw_paths: Any) -> tuple[list[Path], list[str]]:
    """Return (resolved_paths, warnings).  Warns when a file lacks a Markdown extension."""
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ToolInvocationError("'paths' must be a non-empty array of file paths")

    cwd = Path(os.environ.get("PWD", os.getcwd())).expanduser().resolve()
    resolved: list[Path] = []
    warnings: list[str] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str) or raw_path.strip() == "":
            raise ToolInvocationError(
                "Each entry in 'paths' must be a non-empty string"
            )
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (cwd / path).resolve()
        else:
            path = path.resolve()
        if not path.exists():
            raise ToolInvocationError(f"File does not exist: {path}")
        if not path.is_file():
            raise ToolInvocationError(f"Not a regular file: {path}")
        if path.suffix.lower() not in MARKDOWN_EXTENSIONS:
            warnings.append(
                f"{path.name!r} does not have a recognised Markdown extension "
                f"({', '.join(sorted(MARKDOWN_EXTENSIONS))}); proceeding anyway."
            )
        resolved.append(path)
    return resolved, warnings


def collect_available_lines(line_queue: queue.Queue[str], sink: list[str]) -> None:
    while True:
        try:
            sink.append(line_queue.get_nowait())
        except queue.Empty:
            return


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def launch_md_view(paths: list[Path], browser: str, port: Any) -> LaunchResult:
    if browser not in {"firefox", "none"}:
        raise ToolInvocationError("'browser' must be either 'firefox' or 'none'")
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool):
            raise ToolInvocationError("'port' must be an integer between 1 and 65535")
        if port < 1 or port > 65535:
            raise ToolInvocationError("'port' must be an integer between 1 and 65535")

    if not MD_VIEW_BIN.exists():
        raise ToolInvocationError(f"md-view was not found at {MD_VIEW_BIN}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    stdout_log = LOG_DIR / f"launch-{timestamp}-stdout.log"
    stderr_log = LOG_DIR / f"launch-{timestamp}-stderr.log"

    command = [str(MD_VIEW_BIN), *[str(path) for path in paths]]
    if port is not None:
        command.extend(["--port", str(port)])
    command.extend(["--browser", browser])

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_queue: queue.Queue[str] = queue.Queue()
    stderr_queue: queue.Queue[str] = queue.Queue()
    threading.Thread(
        target=drain_stream,
        args=(process.stdout, stdout_queue, stdout_log),
        daemon=True,
    ).start()
    threading.Thread(
        target=drain_stream,
        args=(process.stderr, stderr_queue, stderr_log),
        daemon=True,
    ).start()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    url: str | None = None

    while time.monotonic() < deadline:
        collect_available_lines(stderr_queue, stderr_lines)
        try:
            line = stdout_queue.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None:
                collect_available_lines(stdout_queue, stdout_lines)
                break
            continue

        stdout_lines.append(line)
        match = URL_RE.search(line)
        if match is not None:
            url = match.group(1)
            break

    collect_available_lines(stdout_queue, stdout_lines)
    collect_available_lines(stderr_queue, stderr_lines)

    if url is None:
        terminate_process(process)
        details: list[str] = ["md-view did not report a preview URL before timing out."]
        if stdout_lines:
            details.append("stdout: " + " | ".join(stdout_lines[-5:]))
        if stderr_lines:
            details.append("stderr: " + " | ".join(stderr_lines[-5:]))
        details.append(f"stdout log: {stdout_log}")
        details.append(f"stderr log: {stderr_log}")
        raise ToolInvocationError(" ".join(details))

    return LaunchResult(
        url=url,
        pid=process.pid,
        files=[str(path) for path in paths],
        browser=browser,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )


def relative_to_cwd(abs_path: str) -> str:
    """Return a path relative to CWD when possible, otherwise absolute."""
    cwd = Path(os.environ.get("PWD", os.getcwd())).expanduser().resolve()
    try:
        return str(Path(abs_path).relative_to(cwd))
    except ValueError:
        return abs_path


def handle_open_markdown(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ToolInvocationError("Tool arguments must be a JSON object")
    paths, path_warnings = resolve_paths(arguments.get("paths"))
    browser = arguments.get("browser", "firefox")
    port = arguments.get("port")
    result = launch_md_view(paths, browser, port)
    # Use CWD-relative paths so structuredContent does not leak machine-local
    # absolute paths to the model.
    relative_files = [relative_to_cwd(f) for f in result.files]
    text_parts = [
        f"Started md-view for {len(relative_files)} file(s). URL: {result.url}. "
        f"Browser: {result.browser}. PID: {result.pid}."
    ]
    if path_warnings:
        text_parts.append("Warnings: " + " ".join(path_warnings))
    return {
        "content": [{"type": "text", "text": " ".join(text_parts)}],
        "structuredContent": {
            "url": result.url,
            "pid": result.pid,
            "files": relative_files,
            "browser": result.browser,
        },
    }


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {})

    debug_log(f"handle method={method!r} id={request_id!r}")

    if not isinstance(method, str):
        raise JsonRpcError(-32600, "Request is missing a valid 'method'")

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol_version = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return make_response(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None

    if method == "ping":
        return make_response(request_id, {})

    if method == "tools/list":
        return make_response(
            request_id,
            {
                "tools": [
                    {
                        "name": "open_markdown",
                        "description": "Open one or more Markdown files in md-view and return the local preview URL.",
                        "inputSchema": TOOL_SCHEMA,
                    }
                ]
            },
        )

    if method == "tools/call":
        if not isinstance(params, dict):
            raise JsonRpcError(-32602, "'params' must be an object")
        tool_name = params.get("name")
        if tool_name != "open_markdown":
            raise JsonRpcError(-32602, f"Unknown tool: {tool_name}")
        try:
            result = handle_open_markdown(params.get("arguments", {}))
        except ToolInvocationError as exc:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": str(exc)}],
            }
        return make_response(request_id, result)

    raise JsonRpcError(-32601, f"Method not found: {method}")


def main() -> int:
    debug_log("server starting")
    while True:
        try:
            request = read_message()
            if request is None:
                debug_log("server exiting on EOF")
                return 0
            response = handle_request(request)
            if response is not None and "id" in request:
                send_message(response)
        except JsonRpcError as exc:
            request_id = None
            if "request" in locals() and isinstance(request, dict):
                request_id = request.get("id")
            debug_log(f"jsonrpc error code={exc.code} message={exc.message}")
            send_message(make_error(request_id, exc.code, exc.message))
        except Exception as exc:  # pragma: no cover - safety net for MCP transport.
            request_id = None
            if "request" in locals() and isinstance(request, dict):
                request_id = request.get("id")
            debug_log(f"internal error: {exc!r}")
            send_message(make_error(request_id, -32603, f"Internal error: {exc}"))


if __name__ == "__main__":
    raise SystemExit(main())
