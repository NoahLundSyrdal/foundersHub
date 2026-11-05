"""The company brain: approval routing from Notion (or the local copy), procedures, and the Notion connection."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from office import brain, config, integrations
from office.api import create_app

pytestmark = pytest.mark.skipif(config.MODEL != "mock", reason="tests use the offline mock model")


@pytest.fixture
def api():
    return TestClient(create_app(":memory:"))


def say(api, text, board):
    r = api.post("/api/input", json={"board_id": board, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_over_cap_order_is_routed_not_refused_and_runs_only_after_approval(api):
    task = say(api, "we need 3 toner", "printer")
    assert task["status"] == "awaiting_approval" and task["proposal"]["total_nok"] > config.OFFICE["max_order_nok"]
    assert task["approval"] == {"type": "purchase_over_cap", "approver": "Noah", "reason": "Above the office cap of 1000 NOK per order"}
    assert api.get("/api/state").json()["orders"] == []
    assert api.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).status_code == 200
    assert len(api.get("/api/state").json()["orders"]) == 1
    assert api.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).status_code == 409


def test_service_visit_routes_to_contractor_approver(api):
    task = say(api, "we need a plumber, plumbing service", "bathroom")
    assert task["pending"][0]["details"]["approval_type"] == "contractor"
    assert task["approval"]["approver"] == "Noah"


def test_dashboard_request_shows_who_approves(api):
    task = say(api, "we need 3 toner", "printer")
    [request] = [r for r in api.get("/api/requests").json() if r["id"] == task["id"]]
    assert request["approval"]["approver"] == "Noah" and request["approval"]["type"] == "purchase_over_cap"


def test_approver_comes_from_the_brain(api, monkeypatch):
    table = "| Approval type | Approver |\n|---|---|\n| purchase | Ana |\n| purchase_over_cap | Bo |"
    monkeypatch.setattr(brain, "docs", lambda: {"approval-routing": table})
    assert say(api, "we need 3 toner", "printer")["approval"]["approver"] == "Bo"
    assert brain.approver("contractor") == "an office manager"  # not in the table, no default row


def test_routing_reads_markdown_and_notion_tables():
    md = "| Approval type | Approver |\n|---|---|\n| purchase | Ana |\n| purchase_over_cap | **Bo** |"
    notion = '<table header-row="true"><tr><td>Approval type</td><td>Approver</td></tr><tr><td>purchase</td><td>Ana</td></tr></table>'
    assert brain.routing(md) == {"purchase": "Ana", "purchase_over_cap": "Bo"}
    assert brain.routing(notion) == {"purchase": "Ana"}
    assert brain.routing()["purchase_over_cap"] == "Noah"  # the checked-in copy (config/brain/)


def test_procedures_are_searchable():
    [best, *_] = brain.search("broken equipment repair")
    assert "Facilities answers within 2 hours" in best["text"]


def test_brain_api_and_sync_without_notion(api):
    body = api.get("/api/brain").json()
    assert body["source"] == "config/brain" and body["routing"]["purchase"] == "Noah"
    assert api.post("/api/brain/sync").status_code == 409  # mock mode: no Notion


def test_mock_mode_is_offline():
    assert integrations.toolsets() == [] and integrations.status() == {"notion": False}


def test_unreachable_notion_gives_no_tools_instead_of_failing_the_run():
    from pydantic_ai.toolsets import FunctionToolset

    class Down(FunctionToolset):
        async def __aenter__(self):
            raise ConnectionError("Notion is down")

    toolset = integrations.Degradable(Down())

    async def check():
        async with toolset:
            return await toolset.get_tools(None)

    assert asyncio.run(check()) == {}
