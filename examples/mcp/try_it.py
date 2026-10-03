"""Try the Scrutai MCP server without an AI client: a tiny scripted MCP client.

It starts `scrutai mcp` over stdio (exactly as Claude Code or Cursor would),
lists the tools, reviews the bundled demo patch, and explains the most severe
finding. Runs offline in mock mode.

    pip install "scrutai[mcp]"
    python examples/mcp/try_it.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PATCH = """\
diff --git a/app/runner.py b/app/runner.py
new file mode 100644
--- /dev/null
+++ b/app/runner.py
@@ -0,0 +1,10 @@
+import os
+
+
+def run(cmd, env={}):
+    # never pass user input to os.system(...) unescaped
+    try:
+        return os.system(cmd)
+    except Exception:
+        print("failed", cmd)
+        return -1
"""


def structured(result: object) -> dict:  # works on MCP SDK 1.x and 2.x
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if not isinstance(data, dict):
        raise SystemExit(f"tool failed: {result}")
    return data


async def main() -> None:
    scrutai = shutil.which("scrutai") or sys.exit(
        "scrutai is not on PATH: pip install 'scrutai[mcp]'"
    )
    with tempfile.TemporaryDirectory() as root, open(os.devnull, "w") as quiet:
        server = StdioServerParameters(command=scrutai, args=["mcp", "--root", root], cwd=root)
        async with (
            stdio_client(server, errlog=quiet) as (read, write),  # hide the server's logs
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            tools = (await session.list_tools()).tools
            print(f"Connected to scrutai mcp: {len(tools)} tools")
            print("  " + ", ".join(t.name for t in tools))

            print("\n> call review_patch")
            review = structured(await session.call_tool("review_patch", {"patch": PATCH}))
            print(f"verdict: {review['verdict']}   {review['summary']}")
            for f in review["findings"]:
                print(f"  {f['id']}  {f['severity']:<7} {f['file']}:{f['line']:<3} {f['title']}")
            print(f"  (+{review['dropped']} finding(s) killed by the critic)")

            # Explain a finding the critic challenged and the specialist defended.
            first = next((f for f in review["findings"] if f["defended"]), review["findings"][0])
            print(f"\n> call explain_finding {first['id']}")
            why = structured(
                await session.call_tool(
                    "explain_finding",
                    {"review_id": review["review_id"], "finding_id": first["id"]},
                )
            )
            print(f"challenge: {why['challenge']}")
            print(f"defense:   {why['defense']}")
            print("rulings:   " + json.dumps(why["history"]))
            print("code:")
            print("\n".join("  " + line for line in why["code"].splitlines()))


if __name__ == "__main__":
    asyncio.run(main())
