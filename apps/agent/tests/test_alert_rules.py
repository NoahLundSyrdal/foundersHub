import asyncio

import pytest

from office import alert_rules, db, whatsapp_delivery


def ticket(conn, ticket_id, summary, prompt):
    conn.execute("INSERT INTO tasks(id,board_id,person,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                 (f"task-{ticket_id}", "kitchen", "test", prompt, "done", "2026-11-05T17:00:00Z", "2026-11-05T17:00:00Z"))
    conn.execute("INSERT INTO tickets VALUES(?,?,?,?,?,?,?)",
                 (ticket_id, f"task-{ticket_id}", "kitchen", summary, "Facilities", "open", "2026-11-05T17:00:00Z"))


@pytest.fixture
def setup(tmp_path):
    conn = db.connect(str(tmp_path / "a.sqlite"))
    lock = asyncio.Lock()
    delivery = whatsapp_delivery.WhatsAppDelivery(conn, lock, token_path=tmp_path / "token")
    ticket(conn, "old", "Toilet is broken", "doen er tett")  # existed before the feature: never alerted
    service = alert_rules.AlertRules(conn, lock, delivery)
    delivery.validate_job = lambda job: service._validate(job)
    return conn, service, delivery


def test_phone_normalisation_and_masking():
    rule = alert_rules.RuleIn(label="Toalett", keywords=["Toalett", " WC "], name="Noah", phone="123 45 678")
    assert rule.phone == "+4712345678" and rule.keywords == ["toalett", "wc"]
    with pytest.raises(ValueError):
        alert_rules.RuleIn(label="x", keywords=["x"], name="n", phone="abc")


def test_matching_ticket_queues_one_whatsapp_and_ignores_others(setup):
    conn, service, delivery = setup
    service.save(None, alert_rules.RuleIn(label="Toalett", keywords=["toalett", "toilet", "wc"], name="Noah", phone="+4712345678"))
    service.save(None, alert_rules.RuleIn(label="Oppvaskmaskin", keywords=["oppvaskmaskin", "dishwasher"], name="Noah", phone="+4712345678"))
    assert service.rules()[0]["phone"] == "+47 ••• 78"  # never exposed to the dashboard in full
    ticket(conn, "t1", "Toilet out of order; plumber needed", "Toalettet på kjøkkenet er tett")
    ticket(conn, "t2", "Printer jam", "skriveren har papirstopp")
    asyncio.run(service.check())
    asyncio.run(service.check())  # idempotent
    sent = service.sent()
    assert [(s["ticket_id"], s["label"], s["state"]) for s in sent] == [("t1", "Toalett", "queued")]
    assert "Toalett" in sent[0]["body"] and "tett" in sent[0]["body"]
    jobs = [tuple(r) for r in conn.execute("SELECT execution_id,phone FROM whatsapp_delivery_jobs").fetchall()]
    assert jobs == [(sent[0]["execution_id"], "+4712345678")]
    job = dict(zip(("execution_id", "phone", "body"), conn.execute("SELECT execution_id,phone,body FROM whatsapp_delivery_jobs").fetchone()))
    assert service._validate(job)
    conn.execute("UPDATE alert_rules SET enabled=0")
    assert not service._validate(job)  # disabled before the bridge claims it: fail closed


def test_without_bridge_records_but_does_not_send(tmp_path):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    service = alert_rules.AlertRules(conn, asyncio.Lock(), None)
    service.save(None, alert_rules.RuleIn(label="Oppvaskmaskin", keywords=["oppvaskmaskin"], name="Noah", phone="+4712345678"))
    ticket(conn, "t3", "Dishwasher leaking", "Oppvaskmaskinen lekker")
    asyncio.run(service.check())
    assert service.sent()[0]["state"] == "no_bridge"
