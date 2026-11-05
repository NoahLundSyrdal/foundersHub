import copy
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from office_procurement.food import plan_groceries
from office_procurement.models import GroceryNeed
from office_procurement.worker import run_worker, validate_decision
from test_worker import request


def need(query="milk", quantity=2):
    return GroceryNeed(items=[{"query": query, "quantity": quantity}], summary="Food needed")


def product(product_id=42, price="24.90", **kwargs):
    return {"id": product_id, "name": "Milk 1 litre", "description": "Milk in a one litre carton",
            "price": price, "unitPrice": "24.90 NOK/l", "unitName": "carton",
            "url": "https://oda.com/no/products/42-milk/",
            "availability": {"isAvailable": True, "description": "Available", "code": "available"}, **kwargs}


def results(*products, query="milk"):
    return [{"query": query, "hasMore": False, "products": list(products) or [product()]}]


def selection(product_id=42, quantity=2, **kwargs):
    return TestModel(custom_output_args={"lines": [{"query_index": 0, "product_id": product_id, "quantity": quantity}],
                                         "reason": "Matching milk carton from Oda.", "question": None, **kwargs})


class FoodTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "TAVILY_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_valid_structured_selection_uses_actual_snapshot(self):
        result = await plan_groceries(need(), results(), "anthropic:claude-sonnet-5-5", 100, test_model=selection())
        self.assertEqual(result["kind"], "proposal")
        proposal = result["proposal"]
        self.assertEqual(proposal["subtotal_nok"], 49.8)
        self.assertEqual(proposal["lines"][0]["name"], "Milk 1 litre")
        self.assertEqual(proposal["lines"][0]["product_id"], 42)
        self.assertTrue(proposal["delivery_fees_pending"])
        self.assertTrue(proposal["checkout_required"])
        self.assertGreater(datetime.fromisoformat(proposal["expires_at"]), datetime.now(timezone.utc))

    async def test_invented_id_unavailable_and_changed_quantity_fail_closed(self):
        cases = [(results(), selection(product_id=999)), (results(), selection(quantity=3)),
                 (results(product(availability={"isAvailable": False})), selection()),
                 (results(query="coffee"), selection()),
                 (results(), selection(lines=[]))]
        for search, model in cases:
            with self.subTest(search=search):
                self.assertEqual((await plan_groceries(need(), search, "test", 100, test_model=model))["kind"], "clarification")

    async def test_price_and_budget_fail_closed(self):
        for price in ["NaN", "Infinity", "-1", "0", "2.999", "1e10", 24.90, "USD 2"]:
            self.assertEqual((await plan_groceries(need(), results(product(price=price)), "mock", 100))["kind"], "clarification")
        for budget in [49.79, float("nan"), float("inf"), -1]:
            self.assertEqual((await plan_groceries(need(), results(), "mock", budget))["kind"], "clarification")
        exact = await plan_groceries(need(quantity=3), results(product(price="0.10")), "mock", 0.30)
        self.assertEqual(exact["proposal"]["subtotal_nok"], 0.30)

    async def test_unknown_match_asks_but_mixed_units_get_a_default(self):
        # A search that returned something else entirely is genuinely unclear.
        self.assertEqual((await plan_groceries(need(), results(product(name="Soap", description="Hand soap")), "mock", 100))["kind"], "clarification")
        # Different package units are not a reason to stop: propose a sensible default for human review.
        mixed = await plan_groceries(need(), results(product(), product(product_id=43, unitName="bottle")), "mock", 100)
        self.assertEqual(mixed["kind"], "proposal")

    async def test_duplicate_product_aggregates_but_respects_pack_limit(self):
        groceries = GroceryNeed(items=[{"query": "milk", "quantity": 2}, {"query": "milk carton", "quantity": 3}], summary="Milk")
        search = results() + results(query="milk carton")
        model = selection(lines=[{"query_index": 0, "product_id": 42, "quantity": 2}, {"query_index": 1, "product_id": 42, "quantity": 3}])
        result = await plan_groceries(groceries, search, "test", 500, test_model=model)
        self.assertEqual(len(result["proposal"]["lines"]), 1)
        self.assertEqual(result["proposal"]["lines"][0]["quantity"], 5)
        groceries.items[1].quantity = 20
        model = selection(lines=[{"query_index": 0, "product_id": 42, "quantity": 2}, {"query_index": 1, "product_id": 42, "quantity": 20}])
        self.assertEqual((await plan_groceries(groceries, search, "test", 1000, test_model=model))["kind"], "clarification")

    async def test_grocery_schema(self):
        for value in [{"items": [], "summary": "Unknown"}, {"items": [{"query": " ", "quantity": 1}], "summary": "Food"}]:
            with self.assertRaises(ValidationError):
                GroceryNeed.model_validate(value)
        self.assertEqual(GroceryNeed(items=[], summary="Unknown", question="What food?").items, [])

    async def test_enabled_food_classification_and_preserved_legacy(self):
        disabled = await run_worker(request("2 coffee"))
        self.assertEqual(disabled.interpretation.intent, "purchase")
        enabled = await run_worker(request("2 milk and 3 bananas and coffee", grocery_enabled=True))
        decision = enabled.interpretation
        self.assertEqual(decision.intent, "grocery")
        self.assertEqual([(i.query, i.quantity) for i in decision.grocery.items], [("milk", 2), ("bananas", 3), ("coffee", 1)])
        self.assertIsNone(decision.item_id)
        self.assertEqual(validate_decision(request(), decision).intent, "clarification")
        for text in ["Order food", "Pizza for 10 people", "Order vegan pizza"]:
            self.assertEqual((await run_worker(request(text, grocery_enabled=True))).interpretation.intent, "clarification")
        # Sensible defaults instead of questions: a stated weight is a size hint; number words and "a lot" set packs.
        for text, packs in [("Order 3 kg bananas", 1), ("to store pakker med kaffe", 2), ("tung bestilling på kaffe", 3), ("vi er tomme for kaffe", 1)]:
            decision = (await run_worker(request(text, grocery_enabled=True))).interpretation
            self.assertEqual(decision.intent, "grocery", text)
            self.assertEqual(decision.grocery.items[0].quantity, packs, text)
        for text in ["Coffee machine is broken", "There is a fire"]:
            self.assertEqual((await run_worker(request(text, grocery_enabled=True))).interpretation.intent, "maintenance")

    async def test_live_interpretation_accepts_grocery(self):
        model = TestModel(call_tools=[], custom_output_args={"intent": "grocery", "item_id": None,
                         "summary": "Milk", "quantity": 1, "question": None, "grocery": need().model_dump(),
                         "assessment": {"severity": "routine", "confidence": .95, "requires_confirmation": False, "reason": "Food request"}})
        result = await run_worker(request("2 milk", grocery_enabled=True), model=model)
        self.assertEqual(result.interpretation.intent, "grocery")
