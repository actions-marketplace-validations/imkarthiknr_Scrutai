# MCP client configs

Ready-to-paste configurations for connecting MCP clients to `scrutai mcp`. The full reference is
[docs/MCP.md](../../docs/MCP.md).

| File | Client |
|---|---|
| [`claude-code.sh`](claude-code.sh) | Claude Code: local (stdio) or remote (HTTP + bearer token) |
| [`claude_desktop_config.json`](claude_desktop_config.json) | Claude Desktop |
| [`cursor-mcp.json`](cursor-mcp.json) | Cursor (`.cursor/mcp.json`): local, read-only (`--no-post`), and remote |

Desktop clients start the server without your shell's `PATH` or working directory. Use absolute
paths for the `scrutai` executable, `--root` and `--config`.

Starting a remote server:

```bash
export SCRUTAI_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
scrutai mcp --transport http --host 0.0.0.0 --port 8000 \
  --root /srv/repos --allowed-host scrutai.example.com
```

Off your machine, put a TLS-terminating reverse proxy in front: the server speaks plain HTTP.
