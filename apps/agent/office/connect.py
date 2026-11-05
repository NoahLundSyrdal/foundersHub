"""Connect the office agent to Notion once, on this machine. Tokens go to .data/oauth/ (gitignored).

    npm run connect -- notion   # browser sign-in to Notion's hosted MCP server, then syncs the brain
    npm run connect -- sync     # re-sync the brain from Notion (same as POST /api/brain/sync)
"""

import asyncio
import json
import os
import sys

from office import brain, integrations


async def notion() -> None:
    mcp = integrations.notion_mcp(interactive=True)
    async with mcp:
        tools = await mcp.list_tools()
    print(f"Notion connected: {len(tools)} tools on the hosted MCP server.")
    if os.getenv("NOTION_BRAIN_PAGE_ID"):
        await sync()
    else:
        print("Set NOTION_BRAIN_PAGE_ID in .env to the brain's root page, then run: npm run connect -- sync")


async def sync() -> None:
    result = await brain.sync()
    print(f"Brain synced to {brain.SNAPSHOT}: pages {', '.join(result['pages'])}")
    print("Approval routing:", json.dumps(result["routing"], indent=2))


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["notion"]:
            asyncio.run(notion())
        case ["sync"]:
            asyncio.run(sync())
        case _:
            sys.exit(__doc__)
