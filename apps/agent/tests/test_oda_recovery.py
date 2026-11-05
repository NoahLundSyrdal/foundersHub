"""Recovery must distinguish a prepared basket from an already dispatched cart delta."""
import json
from unittest.mock import AsyncMock

import pytest

from office import agent, db, oda_food, procurement_workflow as workflow
from office_procurement.serialization import proposal_fingerprint


class ReadOnlyCart:
    def __init__(self, quantity=3):
        self.quantity = quantity
        self.reads = self.writes = self.searches = 0

    def connected(self):
        return True

    async def get_cart(self):
        self.reads += 1
        return {"groups": [{"items": [{"product": {"id": 11}, "quantity": self.quantity}]}],
                "url": "https://oda.com/no/cart/"}

    async def add_products(self, lines):
        self.writes += 1
        raise AssertionError("An interrupted additive cart mutation must never be replayed")

    async def search_products(self, queries):
        self.searches += 1
        raise AssertionError("Attempted baskets must reconcile without sourcing a replacement")


def seed(conn, attempt_state=None):
    workflow.ensure_schema(conn)
    task_id = agent.start_task(conn, "kitchen", "unknown speaker", "Two milk packs")
    assessment = {"severity": "routine", "confidence": .95, "requires_confirmation": True, "reason": "Review the real cart."}
    proposal = {"kind": "grocery", "lines": [{"product_id": 11, "name": "Milk", "quantity": 2, "unit_price_nok": 22.9}],
                "subtotal_nok": 45.8, "currency": "NOK", "expires_at": "2099-01-01T00:00:00Z"}
    pending = [{"tool": "oda_cart", "details": {"proposal": proposal, "proposal_fingerprint": proposal_fingerprint(proposal)}}]
    value = {"source": "oda-food", "interpretation": {"intent": "grocery", "item_id": None, "quantity": 1,
             "summary": "Two milk packs", "question": None, "assessment": assessment}, "assessment": assessment,
             "proposal": None, "research": [], "approval_required": True,
             "grocery": {"state": "awaiting_approval", "proposal": proposal, "cart_url": None}}
    workflow.store_context(conn, task_id, value)
    conn.execute("UPDATE tasks SET pending=? WHERE id=?", (json.dumps(pending), task_id))
    # Constructing the service only initializes its local table; the fake never contacts Oda.
    client = ReadOnlyCart()
    oda_food._services[id(conn)] = oda_food.OdaFoodService(conn, client=client)
    if attempt_state:
        conn.execute("INSERT INTO oda_cart_attempts VALUES(?,?,?,?,?,?)",
                     (task_id, proposal_fingerprint(proposal), attempt_state, '{"11":"1"}',
                      "https://oda.com/no/cart/" if attempt_state == "confirmed" else None, db.now()))
    return task_id, proposal, pending, client


@pytest.mark.anyio
@pytest.mark.parametrize("attempt_state", ["started", "uncertain"])
async def test_restart_preserves_attempt_and_exact_basket_then_reconciles_read_only(tmp_path, monkeypatch, attempt_state):
    path = str(tmp_path / "office.sqlite")
    conn = db.connect(path)
    task_id, proposal, pending, _ = seed(conn, attempt_state)
    oda_food.release(conn)
    conn.close()
    conn = db.connect(path)  # real durable state across a fresh process connection
    client = ReadOnlyCart()
    oda_food._services[id(conn)] = oda_food.OdaFoodService(conn, client=client)
    worker = AsyncMock(side_effect=AssertionError("Do not reinterpret an attempted basket"))
    monkeypatch.setattr(workflow, "run_worker", worker)
    try:
        agent.recover_interrupted(conn)
        task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
        value = workflow.get_context(conn, task_id)
        assert task["status"] == "failed"
        assert json.loads(task["pending"]) == pending
        assert value["grocery"]["state"] == "uncertain"
        assert value["grocery"]["proposal"] == proposal
        attempt = workflow.oda_attempt(conn, task_id)
        assert attempt["state"] == "uncertain"
        assert attempt["fingerprint"] == proposal_fingerprint(proposal)
        assert json.loads(attempt["before_quantities"]) == {"11": "1"}
        workflow.release_quote(conn, task_id)  # defensive calls cannot erase execution evidence
        assert workflow.get_context(conn, task_id)["grocery"]["proposal"] == proposal
        with pytest.raises(workflow.ProcurementConflict, match="already attempted"):
            await workflow.prepare_need(conn, task_id, "Replace it with something else")
        worker.assert_not_called()
        reconciled = await agent.decide(conn, task_id, True, proposal_fingerprint(proposal))
        assert reconciled["status"] == "checkout_ready"
        assert reconciled["grocery"]["state"] == "cart_ready"
        assert client.reads == 1 and client.writes == client.searches == 0
        repeated = await agent.decide(conn, task_id, True, proposal_fingerprint(proposal))
        assert repeated["status"] == "checkout_ready" and client.reads == 1
        assert db.rows(conn, "SELECT * FROM orders") == []
    finally:
        oda_food.release(conn)
        conn.close()


def test_confirmed_cart_restart_recovers_checkout_ready_without_claiming_an_order(tmp_path):
    conn = db.connect(str(tmp_path / "office.sqlite"))
    try:
        task_id, proposal, _, client = seed(conn, "confirmed")
        agent.recover_interrupted(conn)
        task = workflow.enrich_task(conn, db.task_view(db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)))
        assert task["status"] == "checkout_ready"
        assert task["grocery"]["state"] == "cart_ready"
        assert task["grocery"]["proposal"] == proposal
        assert task["grocery"]["cart_url"] == "https://oda.com/no/cart/"
        assert "no order has been placed" in task["reply"]
        assert client.reads == client.writes == client.searches == 0
        assert db.rows(conn, "SELECT * FROM orders") == []
    finally:
        oda_food.release(conn)
        conn.close()


@pytest.mark.anyio
async def test_failed_run_after_confirmed_cart_keeps_ready_command():
    conn = db.connect(":memory:")
    try:
        task_id, proposal, _, client = seed(conn, "confirmed")
        # A repeat analysis is rejected by the execution guard; closing that run
        # must retain the already confirmed provider outcome and its green feedback.
        task = await agent.run(conn, task_id, "Do this again")
        assert task["status"] == "checkout_ready"
        assert task["grocery"]["proposal"] == proposal
        assert task["command"]["color"] == "green"
        assert client.reads == client.writes == client.searches == 0
        assert db.rows(conn, "SELECT * FROM orders") == []
    finally:
        oda_food.release(conn)
        conn.close()


@pytest.mark.anyio
async def test_unmatched_cart_remains_uncertain_and_rechecks_never_write():
    conn = db.connect(":memory:")
    try:
        task_id, proposal, _, client = seed(conn, "started")
        client.quantity = 1  # baseline only: there is no confirmed evidence the delta landed
        agent.recover_interrupted(conn)
        for _ in range(2):
            task = await agent.decide(conn, task_id, True, proposal_fingerprint(proposal))
            assert task["status"] == "failed"
            assert task["grocery"]["proposal"] == proposal
        assert client.reads == 2 and client.writes == client.searches == 0
        assert workflow.oda_attempt(conn, task_id)["state"] == "uncertain"
    finally:
        oda_food.release(conn)
        conn.close()


@pytest.mark.anyio
async def test_pre_dispatch_failure_releases_grocery_quote_for_safe_new_analysis(monkeypatch):
    conn = db.connect(":memory:")
    try:
        task_id, _, _, _ = seed(conn)
        failed = agent._close_failed_run(conn, task_id, "Read operation failed before dispatch.")
        assert failed["status"] == "failed" and failed["pending"] == []
        assert failed["grocery"]["proposal"] is None
        assert failed["grocery"]["state"] == "failed"
        assert not workflow.execution_attempted(conn, task_id)
        worker = AsyncMock(side_effect=RuntimeError("Read-only analysis was reached"))
        monkeypatch.setattr(workflow, "run_worker", worker)
        with pytest.raises(RuntimeError, match="Read-only analysis"):
            await workflow.prepare_need(conn, task_id, "Two milk packs")
        worker.assert_awaited_once()
    finally:
        oda_food.release(conn)
        conn.close()


def test_startup_before_oda_table_exists_keeps_generic_failure_recovery_working():
    conn = db.connect(":memory:")
    try:
        workflow.ensure_schema(conn)
        task_id = agent.start_task(conn, "kitchen", "unknown speaker", "Coffee")
        agent.recover_interrupted(conn)
        assert db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)["status"] == "failed"
        assert workflow.oda_attempt(conn, task_id) is None
    finally:
        conn.close()
