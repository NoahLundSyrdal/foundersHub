"""Local coordination templates. A handoff never asserts provider execution."""
from copy import deepcopy


def _step(id, title, evidence, depends_on=()):
    return {"id": id, "title": title, "owner": "office-coordinator", "depends_on": list(depends_on),
            "required_evidence": [evidence], "action_kind": "human", "action_snapshot": {}, "requires_approval": False}


_TEMPLATES = [
    {"key": "replenishment", "title": "Replenishment", "description": "Track an existing approved order through receipt and inventory update.", "steps": [
        _step("order", "Confirm authorized order or purchasing handoff", "order_confirmation"),
        _step("receipt", "Confirm received stock", "receipt", ["order"]),
        _step("inventory", "Update inventory", "inventory_update", ["receipt"])]},
    {"key": "maintenance", "title": "Maintenance", "description": "Track repair work and independently verify the result.", "steps": [
        _step("repair", "Confirm repair or maintenance performed", "repair"),
        _step("verify", "Verify the repaired condition", "verification", ["repair"])]},
    {"key": "onboarding", "title": "Onboarding", "description": "Collect confirmations from equipment, access and orientation owners.", "steps": [
        _step("equipment", "Confirm equipment ready", "equipment_confirmation"),
        _step("access", "Confirm authorized access owner handoff", "access_confirmation"),
        _step("orientation", "Confirm orientation completed", "orientation_confirmation", ["equipment", "access"])]},
    {"key": "meeting_action", "title": "Meeting action", "description": "Track an owned commitment through accepted delivery.", "steps": [
        _step("deliver", "Deliver and obtain acceptance", "accepted_deliverable")]},
    {"key": "project_milestone", "title": "Project milestone", "description": "Track a company milestone against explicit acceptance evidence.", "steps": [
        _step("deliver", "Deliver milestone and obtain acceptance", "accepted_deliverable")]},
    {"key": "vendor_renewal", "title": "Vendor renewal", "description": "Prepare a decision and track an authorized provider handoff.", "steps": [
        _step("decision", "Record accountable owner's renewal decision", "decision"),
        _step("confirm", "Confirm authorized owner executed the decision", "provider_confirmation", ["decision"])]},
]


def templates():
    return deepcopy(_TEMPLATES)


def template_steps(key, owner):
    template = next((item for item in templates() if item["key"] == key), None)
    if template is None:
        raise ValueError(f"Unknown workflow template: {key}")
    for step in template["steps"]:
        step["owner"] = owner
    return template["steps"]
