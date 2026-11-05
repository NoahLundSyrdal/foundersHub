"""Company brain: procedures and approval routing. Written in Notion, read from a local snapshot.

Application code reads the approval routing table (who approves which approval type); the model reads the
procedures. `sync()` pulls the Notion pages into .data/brain/. Until a sync has succeeded (and always in mock
mode) the checked-in copy in config/brain/ is used, so the office works offline.
"""

import json
import os
import re
import shutil
from pathlib import Path

from office import config

SNAPSHOT = config.DATA / "brain"
FIXTURE = config.ROOT / "config" / "brain"
REQUIRED_TYPES = {"purchase", "purchase_over_cap"}  # sync refuses a brain without these: routing must never vanish


class BrainError(Exception):
    pass


def source() -> Path:
    return SNAPSHOT if config.LIVE and any(SNAPSHOT.glob("*.md")) else FIXTURE


def docs() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(source().glob("*.md"))}


def _table_rows(text: str) -> list[list[str]]:
    """Rows of Markdown pipe tables and of Notion-flavored <table><tr><td> tables."""
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.strip().startswith("|") and not re.fullmatch(r"\|[\s:|-]+\|?", line.strip())
    ]
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        rows.append([re.sub(r"<[^>]+>", "", td).strip() for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)])
    return rows


def routing(text: str | None = None) -> dict[str, str]:
    """Approval type → approver. Any table row whose first cell is a snake_case type and second a name."""
    text = "\n".join(docs().values()) if text is None else text
    table = {}
    for cells in _table_rows(text):
        kind = cells[0].strip("`* ") if cells else ""
        if len(cells) >= 2 and re.fullmatch(r"[a-z][a-z_]*", kind) and cells[1].strip("* "):
            table.setdefault(kind, cells[1].strip("* "))
    return table


def approver(approval_type: str) -> str:
    table = routing()
    return table.get(approval_type) or table.get("default") or "an office manager"


def search(query: str, limit: int = 3) -> list[dict]:
    """Best-matching sections (split on Markdown headings) for a free-text question."""
    words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
    scored = []
    for name, text in docs().items():
        for section in re.split(r"\n(?=#{1,3} )", text):
            score = sum(w in section.lower() for w in words)
            if score:
                scored.append((score, {"doc": name, "text": section.strip()[:1500]}))
    return [s for _, s in sorted(scored, key=lambda x: -x[0])[:limit]]


def headings() -> list[str]:
    return [h for text in docs().values() for h in re.findall(r"^#{1,3} (.+)$", text, re.M)]


def _text(result) -> str:
    """notion-fetch returns Notion-flavored Markdown, possibly wrapped in a JSON object."""
    if isinstance(result, dict):
        return str(result.get("text") or json.dumps(result))
    if isinstance(result, list):
        return "\n".join(_text(r) for r in result)
    return str(result)


async def sync() -> dict:
    """Pull the brain from Notion: the page NOTION_BRAIN_PAGE_ID and its direct child pages.

    The new snapshot replaces the old one only if it still contains an approval routing table with the
    required types, so a half-edited Notion page cannot silently remove approvers.
    """
    from office import integrations

    page = os.getenv("NOTION_BRAIN_PAGE_ID")
    notion = integrations.notion_mcp()
    if not page or notion is None:
        raise BrainError("Notion is not connected: set NOTION_BRAIN_PAGE_ID and run `npm run connect -- notion`.")
    async with notion:
        root = _text(await notion.direct_call_tool("notion-fetch", {"id": page}))
        pages = {"index": root}
        for url, title in re.findall(r'<page url="([^"]+)"[^>]*>(.*?)</page>', root):
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "page"
            pages[slug] = _text(await notion.direct_call_tool("notion-fetch", {"id": url}))

    table = routing("\n".join(pages.values()))
    if missing := REQUIRED_TYPES - table.keys():
        raise BrainError(f"Notion brain has no approver for {', '.join(sorted(missing))}; keeping the previous snapshot.")
    tmp = SNAPSHOT.with_name("brain.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for slug, text in pages.items():
        (tmp / f"{slug}.md").write_text(text, encoding="utf-8")
    shutil.rmtree(SNAPSHOT, ignore_errors=True)
    tmp.rename(SNAPSHOT)
    return {"pages": sorted(pages), "routing": table}
