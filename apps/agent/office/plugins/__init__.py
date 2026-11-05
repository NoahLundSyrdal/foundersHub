"""Plugins = capabilities you add as you go.

A plugin is a module with:
  toolset: FunctionToolset      the tools it gives the agent
  requires: set[str]            hardware modules the *current* board must have (empty = every board)

Add a file, append it to PLUGINS, done. Board modules live in config/boards.json.
"""

from office.config import Board
from office.plugins import facilities, hardware, procurement, sensors

PLUGINS = [procurement, facilities, hardware, sensors]


def for_board(board: Board) -> list:
    return [p.toolset for p in PLUGINS if p.requires <= board.modules]
