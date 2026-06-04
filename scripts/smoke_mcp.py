#!/usr/bin/env python3
"""Smoke-test the repo-local md-view MCP wrapper."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = REPO_ROOT / "mcp" / "md_view_server.py"


class SmokeFailure(Exception):
    pass


def send_message(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
    process.stdin.write(header + payload)
    process.stdin.flush()


def read_message(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    assert process.stdout is not None
    headers: dict[str, str] = {}
    while True:
        line = process.stdout.readline()
        if not line:
            raise SmokeFailure("MCP server closed stdout before responding")
        if line in (b"\r\n", b"\n"):
            break
        try:
            key, value = line.decode("ascii").split(":", 1)
        except ValueError as exc:
            raise SmokeFailure(f"Malformed MCP header line: {line!r}") from exc
        headers[key.strip().lower()] = value.strip()

    length_text = headers.get("content-length")
    if length_text is None:
        raise SmokeFailure("MCP response missing Content-Length")
    body = process.stdout.read(int(length_text))
    return json.loads(body.decode("utf-8"))


def request(
    process: subprocess.Popen[bytes], request_id: int, method: str, params: Any = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    send_message(process, message)
    response = read_message(process)
    if response.get("id") != request_id:
        raise SmokeFailure(f"Unexpected response id: {response!r}")
    if "error" in response:
        raise SmokeFailure(f"MCP error for {method}: {response['error']}")
    return response["result"]


def notify(process: subprocess.Popen[bytes], method: str, params: Any = None) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    send_message(process, message)


def terminate_pid_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="md-view-mcp-smoke-") as temp_dir:
        env = os.environ.copy()
        env.setdefault("MD_VIEW_MCP_LOG_DIR", str(Path(temp_dir) / "logs"))
        env.setdefault("MD_VIEW_VENV", str(Path(temp_dir) / "venv"))
        env.setdefault("PIP_CACHE_DIR", str(Path(temp_dir) / "pip-cache"))
        env["PWD"] = str(REPO_ROOT)

        process = subprocess.Popen(
            [sys.executable, str(MCP_SERVER)],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        launched_pid: int | None = None
        try:
            init = request(
                process,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "md-view-smoke", "version": "0.1.0"},
                },
            )
            notify(process, "notifications/initialized")
            tools = request(process, 2, "tools/list")
            if not any(
                tool.get("name") == "open_markdown" for tool in tools.get("tools", [])
            ):
                raise SmokeFailure(f"open_markdown not listed: {tools!r}")
            call = request(
                process,
                3,
                "tools/call",
                {
                    "name": "open_markdown",
                    "arguments": {"paths": ["README.md"], "browser": "none"},
                },
            )
            if call.get("isError"):
                raise SmokeFailure(f"Tool returned isError: {call!r}")
            structured = call.get("structuredContent", {})
            launched_pid = structured.get("pid")
            if not isinstance(launched_pid, int):
                raise SmokeFailure(f"Missing launched pid: {call!r}")
            print(
                "MCP smoke passed:",
                f"protocol={init.get('protocolVersion')}",
                f"url={structured.get('url')}",
                f"pid={launched_pid}",
            )
            return 0
        finally:
            if launched_pid is not None:
                terminate_pid_group(launched_pid)
            if process.poll() is None:
                try:
                    assert process.stdin is not None
                    process.stdin.close()
                except Exception:
                    pass
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as exc:
        print(f"MCP smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
