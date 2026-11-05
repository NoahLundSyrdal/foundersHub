import copy
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from .serialization import epoch


def minor(value) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def offer_total(offer, quantity):
    return (minor(offer["unit_price_nok"]) * quantity + minor(offer["shipping_nok"])) / 100


def fixture_offers(templates, rules, now: float):
    expiry = datetime.fromtimestamp(now + rules["quote_ttl_seconds"], timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return [{**copy.deepcopy(o), "expires_at": expiry} for o in templates]


def rank_offers(station, item_id, quantity, offers, rules, now):
    if item_id not in station["item_ids"] or type(quantity) is not int or not 1 <= quantity <= 20:
        return None
    rejected, eligible = [], []
    for offer in offers:
        if offer["item_id"] != item_id or offer["station_id"] != station["id"]:
            continue
        total = offer_total(offer, quantity)
        reason = ("Unavailable" if not offer["available"] else "Quote expired" if epoch(offer["expires_at"]) <= now
                  else "Below minimum quality" if offer["quality_score"] < rules["minimum_quality"]
                  else "Delivery exceeds policy" if offer["delivery_hours"] > rules["max_delivery_hours"]
                  else "Total including shipping exceeds spending limit" if minor(total) > minor(rules["max_order_nok"]) else None)
        if reason:
            rejected.append({"offer_id": offer["id"], "reason": reason})
        else:
            eligible.append((offer, total))
    if not eligible:
        return None
    minimum = min(total for _, total in eligible)
    fastest = min(o["delivery_hours"] for o, _ in eligible)
    weight_sum = sum(rules["ranking_weights"].values())
    weights = {key: value / weight_sum for key, value in rules["ranking_weights"].items()}
    alternatives = []
    for offer, total in eligible:
        components = {"price": minimum / total, "quality": offer["quality_score"] / 100,
                      "delivery": (fastest + 1) / (offer["delivery_hours"] + 1)}
        score = float(Decimal(str(100 * sum(weights[k] * components[k] for k in weights))).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
        alternatives.append({"offer": offer, "total_nok": total, "components": components, "score": score})
    alternatives.sort(key=lambda a: (-a["score"], a["total_nok"], a["offer"]["id"]))
    best = alternatives[0]
    o = best["offer"]
    return {"weights": weights, "selected_offer_id": o["id"], "alternatives": alternatives, "rejected": rejected,
            "reason": f"Highest policy score {best['score']:g}/100: NOK {best['total_nok']:g} including shipping; {o['delivery_hours']}h fixture delivery; quality {o['quality_score']:g}/100 ({o['quality_basis']}). All compared offers are simulated."}


def plan_purchase(station, item_id, quantity, requests, products, templates, rules, now):
    product = next((p for p in products if p["id"] == item_id and p["station_id"] == station["id"] and item_id in station["item_ids"]), None)
    if not product:
        return {"kind": "clarification", "question": "Choose an approved item for this station."}
    duplicate = next((r for r in requests if r["station_id"] == station["id"] and r["item_id"] == item_id and
                      (r["status"] in {"processing", "awaiting_approval", "ordered"} or (r["status"] == "failed" and r.get("proposal") is not None))), None)
    if duplicate:
        return {"kind": "duplicate", "request": duplicate}
    offers = [o for o in fixture_offers(templates, rules, now) if o["fulfillment_kind"] == product.get("fulfillment_kind", "goods")]
    sourcing = rank_offers(station, item_id, quantity, offers, rules, now)
    if not sourcing:
        return {"kind": "clarification", "question": f"No eligible offer meets the NOK {rules['max_order_nok']:g} spending limit, quality, availability and delivery rules. Clarify the quantity or ask an administrator to review sourcing."}
    selected = sourcing["alternatives"][0]
    offer = selected["offer"]
    return {"kind": "proposal", "proposal": {
        "kind": "purchase", "product": {**product, "name": offer["name"], "price_nok": offer["unit_price_nok"],
                                         "supplier": offer["supplier"], "delivery_estimate": f"Simulation: {offer['delivery_hours']} hours (not verified)"},
        "quantity": quantity, "total_nok": selected["total_nok"], "currency": "NOK", "simulation": True,
        "fulfillment_kind": offer["fulfillment_kind"], "offer_id": offer["id"], "expires_at": offer["expires_at"], "sourcing": sourcing}}


def missing_assessment():
    return {"severity": "routine", "confidence": 0, "requires_confirmation": True, "reason": "Agent assessment unavailable; human review is required."}


def mock_assessment(text, decision):
    if re.search(r"\b(fire|smoke|gas leak|electric shock|flooding|brann|røyk|gasslekkasje)\b", text, re.I):
        return {"severity": "critical", "confidence": .9, "requires_confirmation": True,
                "reason": "Reported hazard requires immediate human review; purchasing cannot resolve the hazard automatically."}
    if decision["intent"] == "clarification":
        return {"severity": "routine", "confidence": .4, "requires_confirmation": True, "reason": "Need or measurement is ambiguous; clarify before taking action."}
    if decision["intent"] == "maintenance" or (decision.get("item_id") or "").endswith("-service"):
        return {"severity": "urgent", "confidence": .9, "requires_confirmation": True, "reason": "A repair or site visit needs review of scope and access before booking."}
    return {"severity": "routine", "confidence": .95, "requires_confirmation": False,
            "reason": "Recognized routine replenishment; automatic execution is subject to office policy and spending limits."}


def needs_approval(request, rules):
    assessment = request.get("assessment") or missing_assessment()
    proposal = request.get("proposal") or {}
    return (rules["require_approval"] or assessment["severity"] == "critical" or assessment["requires_confirmation"] or
            assessment["confidence"] < rules["auto_approval_min_confidence"] or proposal.get("total_nok", float("inf")) > rules["auto_approval_max_nok"] or
            (proposal.get("fulfillment_kind") == "service" and rules["service_requires_approval"]))
