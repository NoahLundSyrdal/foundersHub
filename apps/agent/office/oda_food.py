"""Real Oda baskets. The model selects; only this service can change the cart."""
import json
import os
import time
from decimal import Decimal
from urllib.parse import urlsplit

from office import bus, config, db, procurement_workflow as workflow
from office_procurement.food import plan_groceries, _price
from office_procurement.models import GroceryNeed
from office_procurement.oda import OdaClient, cart_quantities
from office_procurement.serialization import proposal_fingerprint, epoch

_services = {}


def enabled():
    return os.getenv("ODA_FOOD_ENABLED", "false").lower() in {"1", "true", "yes"}


def service(conn):
    if id(conn) not in _services:
        _services[id(conn)] = OdaFoodService(conn)
    return _services[id(conn)]


def release(conn):
    _services.pop(id(conn), None)


def cart_url(cart):
    url = cart.get("url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != "https" or parsed.hostname != "oda.com" or parsed.username or parsed.password:
        raise ValueError("Missing verified Oda cart URL")
    return url


class OdaFoodService:
    def __init__(self, conn, client=None):
        self.conn = conn
        self.client = client or OdaClient(storage_path=config.ROOT / ".data" / "oda" / "oauth.json")
        conn.execute("""CREATE TABLE IF NOT EXISTS oda_cart_attempts(
          task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
          before_quantities TEXT NOT NULL, cart_url TEXT, updated_at TEXT NOT NULL)""")

    def finish(self, task_id, value, status, reply):
        workflow.store_context(self.conn, task_id, value)
        self.conn.execute("UPDATE tasks SET status=?,reply=?,pending='[]',updated_at=? WHERE id=?",
                          (status, reply, db.now(), task_id))
        task = workflow.enrich_task(self.conn, db.task_view(db.row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)))
        command = db.set_command(self.conn, task["board_id"], reply, "green" if status == "checkout_ready" else "amber")
        bus.publish("task.updated", task, source=self.conn)
        bus.publish("requests.updated", {"request_id": task_id}, source=self.conn)
        return task | {"command": command}

    async def prepare(self, task_id, value):
        value["source"] = "oda-food"
        value["grocery"] = {"proposal": None, "cart_url": None, "state": "needs_clarification"}
        need = GroceryNeed.model_validate(value["interpretation"]["grocery"])
        grocery = value["grocery"]
        if value["assessment"]["severity"] == "critical":
            grocery["question"] = "Critical food or safety report requires human review. No cart changes were made."
        elif need.question:
            grocery["question"] = need.question
        elif not self.client.connected():
            grocery.update(state="connection_required", question="Connect your Oda account to search live food products and prepare a basket.",
                           connect_url="/api/integrations/oda/connect")
        else:
            try:
                bus.publish("procurement.progress", {"task_id": task_id, "stage": "oda_search", "detail": "Searching live Oda products and prices."})
                results = await self.client.search_products([item.query for item in need.items])
                plan = await plan_groceries(need, results, config.MODEL, config.OFFICE["max_order_nok"])
                if plan["kind"] == "proposal":
                    grocery.update(state="awaiting_approval", proposal=plan["proposal"])
                else:
                    grocery["question"] = plan["question"]
            except Exception:
                grocery.update(state="failed", question="Oda could not be reached. No cart changes were made; try this request again.")
        workflow.store_context(self.conn, task_id, value)
        return value

    def stage(self, task_id, value):
        grocery = value["grocery"]
        if grocery["state"] == "awaiting_approval":
            reply = f"Review the Oda basket: {grocery['proposal']['subtotal_nok']:g} NOK in products; delivery fees pending."
            return self.finish(task_id, value, "awaiting_approval", reply)
        return self.finish(task_id, value, "needs_clarification", grocery.get("question", "Please specify the food and pack quantities."))

    async def decide(self, task_id, approve, fingerprint):
        value = workflow.get_context(self.conn, task_id)
        grocery = value["grocery"]
        proposal = grocery.get("proposal")
        if not proposal or fingerprint != proposal_fingerprint(proposal):
            raise workflow.ProcurementConflict("Basket changed; review the exact products, quantities and prices again.")
        attempt = db.row(self.conn, "SELECT * FROM oda_cart_attempts WHERE task_id=?", task_id)
        if attempt:
            if attempt["fingerprint"] != fingerprint:
                raise workflow.ProcurementConflict("A different basket was already attempted; inspect Oda before continuing.")
            if attempt["state"] == "confirmed":
                return workflow.enrich_task(self.conn, db.task_view(db.row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)))
            # Read-only reconciliation is safe; never repeat an uncertain delta write.
            return await self.reconcile(task_id, value, attempt)
        task = db.row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if task["status"] != "awaiting_approval" or grocery["state"] != "awaiting_approval":
            raise workflow.ProcurementConflict("This basket is not awaiting approval.")
        if not approve:
            grocery.update(state="failed", question="Oda basket declined. No cart changes were made.")
            return self.finish(task_id, value, "done", grocery["question"])
        if epoch(proposal["expires_at"]) <= time.time():
            raise workflow.ProcurementConflict("Basket prices expired; submit the food request again for fresh prices.")
        if not self.client.connected():
            raise workflow.ProcurementConflict("Reconnect Oda before adding products.")
        lines = proposal["lines"]
        subtotal = sum((Decimal(str(line["unit_price_nok"])) * line["quantity"] for line in lines), Decimal(0))
        if (not lines or len(lines) > 10 or any(type(line["quantity"]) is not int or not 1 <= line["quantity"] <= 20 for line in lines)
                or subtotal != Decimal(str(proposal["subtotal_nok"])) or subtotal > Decimal(str(config.OFFICE["max_order_nok"]))):
            raise workflow.ProcurementConflict("Basket no longer satisfies office limits.")
        # Recheck exact provider IDs and prices, never silently substitute another item.
        need = GroceryNeed.model_validate(value["interpretation"]["grocery"])
        try:
            batches = await self.client.search_products([item.query for item in need.items])
            products = {p["id"]: p for batch in batches for p in batch["products"]}
            for line in lines:
                product = products.get(line["product_id"])
                if (not product or product.get("availability", {}).get("isAvailable") is not True
                        or _price(product.get("price")) != Decimal(str(line["unit_price_nok"])) or product.get("name") != line["name"]):
                    raise workflow.ProcurementConflict("Oda price, stock or product changed. Request a fresh basket and review it again.")
            baseline = cart_quantities(await self.client.get_cart())
        except workflow.ProcurementConflict:
            raise
        except Exception as error:
            raise workflow.ProcurementConflict("Could not verify the current Oda basket. No cart write was attempted.") from error
        # Block equivalent reviewed baskets on other active tasks.
        signature = sorted((line["product_id"], line["quantity"]) for line in lines)
        for row in db.rows(self.conn, "SELECT task_id,body FROM procurement_context WHERE task_id<>?", task_id):
            other = json.loads(row["body"]).get("grocery", {})
            prior = db.row(self.conn, "SELECT state FROM oda_cart_attempts WHERE task_id=?", row["task_id"])
            blocked = prior and (prior["state"] in {"started", "uncertain"} or
                                (prior["state"] == "confirmed" and all(baseline.get(l["product_id"], Decimal(0)) >= l["quantity"] for l in lines)))
            if blocked and sorted((l["product_id"], l["quantity"]) for l in (other.get("proposal") or {}).get("lines", [])) == signature:
                raise workflow.ProcurementConflict("An equivalent food basket is already prepared or awaiting reconciliation. Review it in Oda.")
        before = {str(line["product_id"]): str(baseline.get(line["product_id"], Decimal(0))) for line in lines}
        self.conn.execute("INSERT INTO oda_cart_attempts VALUES(?,?,'started',?,NULL,?)", (task_id, fingerprint, json.dumps(before), db.now()))
        try:
            await self.client.add_products([{"product_id": line["product_id"], "quantity": line["quantity"]} for line in lines])
        except Exception:
            grocery["state"] = "uncertain"
            self.conn.execute("UPDATE oda_cart_attempts SET state='uncertain',updated_at=? WHERE task_id=?", (db.now(), task_id))
            return self.finish(task_id, value, "failed", "Oda cart update is uncertain. Inspect Oda or check again; this basket will not be added twice.")
        attempt = db.row(self.conn, "SELECT * FROM oda_cart_attempts WHERE task_id=?", task_id)
        return await self.reconcile(task_id, value, attempt)

    async def reconcile(self, task_id, value, attempt):
        grocery = value["grocery"]
        try:
            cart = await self.client.get_cart()
            quantities = cart_quantities(cart)
            before = json.loads(attempt["before_quantities"])
            if any(quantities.get(line["product_id"], Decimal(0)) != Decimal(before[str(line["product_id"])]) + line["quantity"] for line in grocery["proposal"]["lines"]):
                raise ValueError("Cart quantities differ")
            url = cart_url(cart)
        except Exception:
            self.conn.execute("UPDATE oda_cart_attempts SET state='uncertain',updated_at=? WHERE task_id=?", (db.now(), task_id))
            grocery["state"] = "uncertain"
            return self.finish(task_id, value, "failed", "Oda cart needs review. No retry will add these products again.")
        self.conn.execute("UPDATE oda_cart_attempts SET state='confirmed',cart_url=?,updated_at=? WHERE task_id=?", (url, db.now(), task_id))
        grocery.update(state="cart_ready", cart_url=url)
        return self.finish(task_id, value, "checkout_ready", "Oda cart ready. Review delivery and pay on Oda; no order has been placed yet.")
