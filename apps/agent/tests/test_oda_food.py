"""Oda API integration uses injected read/write fakes, never credentials or network."""
import unittest
import json
from unittest.mock import patch

import httpx

from office import config, db, oda_food
from office.api import create_app
from office_procurement.serialization import proposal_fingerprint
from office_procurement.food import FoodPlanningError


class FakeOda:
    def __init__(self):
        self.is_connected = True
        self.price = "39.90"
        self.available = True
        self.quantities = {42: 1, 99: 7}  # selected product already present; unrelated item preserved
        self.writes = []
        self.reads = 0
        self.fail_write = False
        self.apply_before_timeout = False

    def connected(self):
        return self.is_connected

    async def search_products(self, queries):
        return [{"query": query, "hasMore": False, "products": [{
            "id": 42, "name": "Coffee 500g", "description": "Ground coffee, 500g pack",
            "price": self.price, "unitPrice": "79.80 NOK/kg", "unitName": "pack",
            "availability": {"isAvailable": self.available, "description": "Available" if self.available else "Unavailable", "code": "available" if self.available else "unavailable"},
            "url": "https://oda.com/no/products/42-coffee/"}]} for query in queries]

    async def get_cart(self):
        self.reads += 1
        return {"url": "https://oda.com/no/cart/", "groups": [{"items": [
            {"product": {"id": product_id}, "quantity": quantity} for product_id, quantity in self.quantities.items()
        ]}]}

    async def add_products(self, lines):
        self.writes.append(lines)
        if not self.fail_write or self.apply_before_timeout:
            for line in lines:
                product_id = line["product_id"]
                self.quantities[product_id] = self.quantities.get(product_id, 0) + line["quantity"]
        if self.fail_write:
            raise TimeoutError("uncertain provider result")
        return await self.get_cart()


class OdaFoodAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_provider_failure_is_failed_without_fake_clarification(self):
        with patch("office.oda_food.plan_groceries", side_effect=FoodPlanningError("Live selection failed; no basket prepared.")):
            result = await self.client.post("/api/input", json={"board_id": "kitchen", "text": "Order 2 coffee"})
        self.assertEqual(result.status_code, 200)
        task = result.json()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["grocery"]["state"], "failed")
        self.assertIsNone(task["grocery"]["proposal"])
        self.assertEqual(self.fake.writes, [])

    async def asyncSetUp(self):
        self.environment = patch.dict("os.environ", {"ODA_FOOD_ENABLED": "true", "ANTHROPIC_API_KEY": "", "TAVILY_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.model = patch.object(config, "MODEL", "mock")
        self.model.start()
        self.addCleanup(self.model.stop)
        self.app = create_app(":memory:")
        self.fake = FakeOda()
        self.app.state.oda_food.client = self.fake
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(lambda: oda_food.release(self.app.state.conn))
        self.addCleanup(self.app.state.conn.close)

    async def new_basket(self):
        writes_before = list(self.fake.writes)
        response = await self.client.post("/api/input", json={"board_id": "kitchen", "text": "Order 2 coffee"})
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertEqual(task["grocery"]["state"], "awaiting_approval")
        self.assertEqual(task["grocery"]["proposal"]["subtotal_nok"], 79.8)
        self.assertIsNone(task.get("proposal"))  # no fixture purchase proposal
        self.assertEqual(self.fake.writes, writes_before)
        return task

    async def approve(self, task, fingerprint=None):
        return await self.client.post(f"/api/tasks/{task['id']}/decide", json={
            "approve": True, "proposal_fingerprint": fingerprint if fingerprint is not None else proposal_fingerprint(task["grocery"]["proposal"])})

    async def assert_no_orders(self):
        self.assertEqual((await self.client.get("/api/state")).json()["orders"], [])

    async def test_exact_approval_adds_delta_once_and_leaves_checkout_to_user(self):
        task = await self.new_basket()
        self.assertTrue(task["grocery"]["proposal"]["delivery_fees_pending"])
        result = await self.approve(task)
        self.assertEqual(result.status_code, 200, result.text)
        ready = result.json()
        self.assertEqual(ready["status"], "checkout_ready")
        self.assertEqual(ready["grocery"]["state"], "cart_ready")
        self.assertEqual(ready["grocery"]["cart_url"], "https://oda.com/no/cart/")
        self.assertIn("no order has been placed", ready["reply"].lower())
        self.assertEqual(self.fake.writes, [[{"product_id": 42, "quantity": 2}]])
        self.assertEqual(self.fake.quantities, {42: 3, 99: 7})
        repeated = await self.approve(task)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(len(self.fake.writes), 1)
        await self.assert_no_orders()

    async def test_stale_snapshot_rejected_without_write(self):
        task = await self.new_basket()
        response = await self.approve(task, "stale")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fake.writes, [])
        await self.assert_no_orders()

    async def test_declined_basket_is_resolved_without_cart_write(self):
        task = await self.new_basket()
        result = await self.client.post(f"/api/requests/{task['id']}/reject", json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "resolved")
        self.assertIn("declined", result.json()["summary"])
        self.assertEqual(self.fake.writes, [])
        self.assertEqual((await self.approve(task)).status_code, 409)
        await self.assert_no_orders()

    async def test_started_cart_update_cannot_be_declined_as_unexecuted(self):
        task = await self.new_basket()
        self.app.state.conn.execute("INSERT INTO oda_cart_attempts VALUES(?,?,'started',?,NULL,?)",
                                    (task["id"], proposal_fingerprint(task["grocery"]["proposal"]), '{"42":"1"}', "2026-11-05T12:00:00+00:00"))
        result = await self.client.post(f"/api/requests/{task['id']}/reject", json={})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.fake.writes, [])
        self.assertEqual((await self.client.get("/api/requests")).json()[0]["status"], "awaiting_approval")

    async def test_changed_price_rejected_before_write(self):
        task = await self.new_basket()
        self.fake.price = "40.00"
        response = await self.approve(task)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fake.writes, [])
        await self.assert_no_orders()

    async def test_unavailable_product_rejected_before_write(self):
        task = await self.new_basket()
        self.fake.available = False
        response = await self.approve(task)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fake.writes, [])
        await self.assert_no_orders()

    async def test_uncertain_delta_never_retried_when_no_change_is_visible(self):
        task = await self.new_basket()
        self.fake.fail_write = True
        first = await self.approve(task)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["grocery"]["state"], "uncertain")
        reads = self.fake.reads
        second = await self.approve(task)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["grocery"]["state"], "uncertain")
        self.assertGreater(self.fake.reads, reads)
        self.assertEqual(len(self.fake.writes), 1)
        self.assertEqual(self.fake.quantities, {42: 1, 99: 7})
        await self.assert_no_orders()

    async def test_timed_out_write_reconciles_read_only_when_delta_did_apply(self):
        task = await self.new_basket()
        self.fake.fail_write = self.fake.apply_before_timeout = True
        first = await self.approve(task)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["grocery"]["state"], "uncertain")
        second = await self.approve(task)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["grocery"]["state"], "cart_ready")
        self.assertEqual(len(self.fake.writes), 1)
        self.assertEqual(self.fake.quantities, {42: 3, 99: 7})
        await self.assert_no_orders()

    async def test_disconnected_oda_requires_connection_without_fixture_fallback(self):
        self.fake.is_connected = False
        response = await self.client.post("/api/input", json={"board_id": "kitchen", "text": "Order 2 coffee"})
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual(task["status"], "needs_clarification")
        self.assertEqual(task["grocery"]["state"], "connection_required")
        self.assertIsNone(task["grocery"]["proposal"])
        self.assertIsNone(task.get("proposal"))
        self.assertEqual(self.fake.writes, [])
        await self.assert_no_orders()


    async def test_equivalent_second_basket_blocked_while_first_is_still_in_cart(self):
        first = await self.new_basket()
        approved = await self.approve(first)
        self.assertEqual(approved.status_code, 200, approved.text)
        second = await self.new_basket()
        self.assertNotEqual(first["id"], second["id"])
        response = await self.approve(second)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(self.fake.writes), 1)
        self.assertEqual(self.fake.quantities, {42: 3, 99: 7})
        await self.assert_no_orders()

    async def test_crash_after_started_attempt_blocks_equivalent_task_before_write(self):
        first = await self.new_basket()
        fingerprint = proposal_fingerprint(first["grocery"]["proposal"])
        self.app.state.conn.execute("INSERT INTO oda_cart_attempts VALUES(?,?,'started',?,NULL,?)",
                                    (first["id"], fingerprint, json.dumps({"42": "1"}), db.now()))
        # A crash can leave the durable attempt started while the context still awaits approval.
        second = await self.new_basket()
        response = await self.approve(second)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.fake.quantities, {42: 1, 99: 7})
        await self.assert_no_orders()

    async def test_new_explicit_identical_request_allowed_after_cart_is_emptied(self):
        first = await self.new_basket()
        approved = await self.approve(first)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.fake.quantities.clear()  # User completed checkout or removed the previous basket on Oda.
        second = await self.new_basket()
        response = await self.approve(second)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["grocery"]["state"], "cart_ready")
        self.assertEqual(len(self.fake.writes), 2)
        self.assertEqual(self.fake.quantities, {42: 2})
        await self.assert_no_orders()

    async def test_legacy_approval_marks_live_grocery_checkout_without_an_order(self):
        task = await self.new_basket()
        response = await self.client.post(f"/api/requests/{task['id']}/approve", json={
            "proposal_fingerprint": proposal_fingerprint(task["grocery"]["proposal"])})
        self.assertEqual(response.status_code, 200, response.text)
        current = response.json()
        self.assertIs(current["simulation"], False)
        self.assertEqual(current["intent"], "grocery")
        self.assertEqual(current["status"], "checkout_ready")
        self.assertEqual(current["grocery"]["state"], "cart_ready")
        self.assertEqual(len(self.fake.writes), 1)
        await self.assert_no_orders()
