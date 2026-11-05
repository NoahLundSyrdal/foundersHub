"""All routes and agent tools share one SQLite connection, so none may run on a worker thread.

Parallel reads on worker threads returned 500s, 404s and wrong request statuses to the dashboard.
"""

import inspect

from fastapi.routing import APIRoute

from office import agent, api, plugins


def test_routes_stay_on_the_event_loop():
    app = api.create_app(":memory:")
    threaded = [r.path for r in app.routes if isinstance(r, APIRoute) and not inspect.iscoroutinefunction(r.endpoint)]
    assert threaded == []


def test_agent_tools_stay_on_the_event_loop():
    threaded = []
    for board in api.config.BOARDS.values():
        for toolset in plugins.for_board(board):
            threaded += [name for name, tool in toolset.tools.items() if not inspect.iscoroutinefunction(tool.function)]
    assert threaded == []


def test_agent_instructions_stay_on_the_event_loop():
    assert inspect.iscoroutinefunction(agent.context)
