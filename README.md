# md-view

`md-view` is a local, offline Markdown visualizer with live reload, Mermaid
diagram support, syntax highlighting, and server-side LaTeX-to-MathML rendering.

## Requirements

- **Python 3.9 or newer** (the launcher enforces this at startup)
- Firefox (or any browser) for `--browser firefox`; use `--browser none` for URL-only mode

This directory is a standalone repo-style extraction of Julien's local tool. It
does not depend on `~/.local/share/md-view` or the old opencode MCP wrapper path.

## Layout

```text
bin/md-view                  # launcher
src/md_view/server.py        # localhost-only Markdown server
assets/vendor/               # offline Mermaid assets
mcp/md_view_server.py        # MCP wrapper exposing open_markdown
requirements.txt             # Python runtime dependencies
scripts/smoke_mcp.py         # optional MCP smoke test helper
```

## Local usage

From the repo checkout:

```bash
./bin/md-view README.md --browser none
```

The launcher creates a contained Python virtualenv at `.venv/` on first real
run and installs `requirements.txt`. Override paths when needed:

```bash
MD_VIEW_VENV=/tmp/md-view-venv ./bin/md-view README.md --browser none
MD_VIEW_HOME=/path/with/assets ./bin/md-view README.md
```

`MD_VIEW_HOME` should contain an `assets/` directory. By default it is the repo
root, so the vendored assets in `assets/vendor/` are used automatically.

## MCP server

`mcp/md_view_server.py` implements a minimal stdio MCP server with one tool:
`open_markdown`. It resolves relative Markdown paths from the client process'
working directory and launches the repo-local `bin/md-view` by default.

Example opencode configuration snippet for a checkout at this path:

```json
{
  "mcp": {
    "md-view": {
      "type": "local",
      "command": [
        "python3",
        "<checkout>/mcp/md_view_server.py"
      ],
      "enabled": true
    }
  }
}
```

Useful overrides:

- `MD_VIEW_BIN`: launcher path used by the MCP wrapper.
- `MD_VIEW_MCP_LOG_DIR`: directory for MCP wrapper logs.
- `MD_VIEW_STARTUP_TIMEOUT_SECONDS`: launch timeout for first-run venv setup.
- `MD_VIEW_VENV`: venv path used by the launched `bin/md-view`.

After adding or changing an opencode MCP config entry, restart opencode so it
loads the new server command.

## Development checks

```bash
python3 -m py_compile src/md_view/server.py mcp/md_view_server.py scripts/smoke_mcp.py
./bin/md-view --help
python3 scripts/smoke_mcp.py
```
