#!/usr/bin/env bash
# Register Scrutai with Claude Code. Pick one.
set -euo pipefail

# 1. Local: Claude Code launches `scrutai mcp` over stdio for this repository.
claude mcp add scrutai -- scrutai mcp --root "$PWD"

# 2. Remote: a server you started elsewhere with
#      SCRUTAI_MCP_TOKEN=... scrutai mcp --transport http --port 8000 --root ~/src
#    (behind a TLS proxy when it is not on this machine).
# claude mcp add --transport http scrutai https://scrutai.example.com/mcp \
#   --header "Authorization: Bearer $SCRUTAI_MCP_TOKEN"
