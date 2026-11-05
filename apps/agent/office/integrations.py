"""Live connection through pydantic-ai-harness: Notion, the company brain.

Read-only for the agent, optional, and off in mock mode. A connection that is down costs the run its tools,
never the run itself. Connect once with `npm run connect -- notion`; tokens are kept in .data/oauth/
(gitignored). NOTION_ACCESS_TOKEN in the environment takes precedence.
"""

import functools
import logging
import os
from dataclasses import dataclass, replace

from pydantic_ai import RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, WrapperToolset

from office import config

log = logging.getLogger(__name__)

OAUTH_DIR = config.DATA / "oauth"
NOTION_URL = "https://mcp.notion.com/mcp"
NOTION_CALLBACK_PORT = 8765  # fixed so the registered redirect URI stays valid across runs


@dataclass
class Degradable(WrapperToolset):
    """If the service is unreachable or the token is bad, this run has no tools from it (and a warning in the log)."""

    _down: bool = False

    async def for_run(self, ctx: RunContext) -> AbstractToolset:
        try:
            wrapped = await self.wrapped.for_run(ctx)
        except Exception as e:
            log.warning("%s unavailable this run: %s", self.wrapped.label, e)
            return FunctionToolset()
        return self if wrapped is self.wrapped else replace(self, wrapped=wrapped, _down=False)

    async def __aenter__(self):
        try:
            await self.wrapped.__aenter__()
            self._down = False
        except Exception as e:
            log.warning("%s unavailable this run: %s", self.wrapped.label, e)
            self._down = True
        return self

    async def __aexit__(self, *args):
        return None if self._down else await self.wrapped.__aexit__(*args)

    async def get_tools(self, ctx: RunContext) -> dict:
        if self._down:
            return {}
        try:
            return await self.wrapped.get_tools(ctx)
        except Exception as e:
            log.warning("%s unavailable this run: %s", self.wrapped.label, e)
            return {}


# --- Notion ---


NOTION_TOKENS = OAUTH_DIR / "notion"


def _token_store(directory):
    """OAuth tokens on disk. Keys are URLs ('https://mcp.notion.com/mcp/...'): sanitize them into safe file names."""
    from key_value.aio.stores.filetree import (
        FileTreeStore,
        FileTreeV1CollectionSanitizationStrategy,
        FileTreeV1KeySanitizationStrategy,
    )

    directory.mkdir(parents=True, exist_ok=True)
    return FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )


def _notion_oauth(interactive: bool):
    from fastmcp.client.auth import OAuth

    return OAuth(
        client_name="Founders Hub office agent",
        token_storage=_token_store(NOTION_TOKENS),
        callback_port=NOTION_CALLBACK_PORT,
        callback_timeout=300 if interactive else 1,  # a server run must fail fast, never wait for a browser
    )


def notion_connected() -> bool:
    """An env token, or a stored OAuth token (not just a registered client: sign-in may have been abandoned)."""
    if os.getenv("NOTION_ACCESS_TOKEN"):
        return True
    dirs = [d for d in NOTION_TOKENS.iterdir() if d.is_dir() and "token" in d.name] if NOTION_TOKENS.exists() else []
    return any(f for d in dirs for f in d.rglob("*.json"))


def notion_mcp(interactive: bool = False):
    """Raw MCP connection, for application code (brain sync). None if not connected."""
    from pydantic_ai.mcp import MCPToolset

    if token := os.getenv("NOTION_ACCESS_TOKEN"):
        return MCPToolset(NOTION_URL, auth=token)
    if interactive or notion_connected():
        # The browser sign-in happens inside the MCP handshake: give a person time (default is 5 s).
        return MCPToolset(NOTION_URL, auth=_notion_oauth(interactive), init_timeout=300 if interactive else 30)
    return None


@functools.cache
def notion_toolset() -> AbstractToolset | None:
    """The harness Notion capability's tools, read-only, for the model."""
    from pydantic_ai_harness.notion import Notion

    if token := os.getenv("NOTION_ACCESS_TOKEN"):
        capability = Notion(auth=token, read_only=True)
    elif notion_connected():
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        capability = Notion(client=Client(StreamableHttpTransport(NOTION_URL, auth=_notion_oauth(False))), read_only=True)
    else:
        return None
    return Degradable(capability.get_toolset())


def toolsets() -> list[AbstractToolset]:
    """Live integration tools for a run. None in mock mode."""
    if not config.LIVE:
        return []
    return [t for t in (notion_toolset(),) if t is not None]


def status() -> dict:
    return {"notion": config.LIVE and notion_connected()}
