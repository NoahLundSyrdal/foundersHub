"""The local UI can authorize one exact cloud request, never a local coding process."""
import asyncio
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from office.development import Development, RemoteError
from office.development_api import router

BASE = "a" * 40


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.dispatch_error = None
        self.runs = []
        self.pulls = []

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            if self.dispatch_error:
                raise self.dispatch_error
            return {}
        if "/commits/" in path:
            return {"sha": BASE}
        if "/pulls?" in path:
            assert "NLS%2Fclaude-123-1" in path
            return self.pulls
        if "/actions/runs/" in path:
            return self.runs[0]
        return {"workflow_runs": self.runs}


@pytest.fixture
def demo():
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    github = FakeGitHub()
    service = Development(conn, github=github, enabled=True)
    app = FastAPI()
    app.include_router(router(service))
    with TestClient(app) as client:
        yield service, github, client
    conn.close()


def save(demo, request_id="request-one", text="Rename History to Activity"):
    response = demo[2].post("/api/development", json={"id": request_id, "text": text})
    assert response.status_code == 200
    return response.json()


def send(demo, item):
    return demo[2].post(f"/api/development/{item['id']}/send", json={"fingerprint": item["fingerprint"]})


def test_draft_does_not_send_and_same_id_cannot_change_text(demo):
    item = save(demo)
    assert demo[1].calls == []
    assert save(demo) == item
    assert demo[2].post("/api/development", json={"id": item["id"], "text": "Different feedback"}).status_code == 409
    assert demo[2].post(f"/api/development/{item['id']}/send", json={"fingerprint": "b" * 64}).status_code == 409
    assert demo[1].calls == []


def test_repeated_and_concurrent_sends_dispatch_once_with_exact_text(demo):
    item = save(demo)
    async def run():
        return await asyncio.gather(*(demo[0].send(item["id"], item["fingerprint"]) for _ in range(5)))
    results = asyncio.run(run())
    assert all(r["state"] == "submitted" for r in results)
    assert send(demo, item).json()["state"] == "submitted"
    posts = [call for call in demo[1].calls if call[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] == {"ref": "main", "inputs": {"request_id": item["id"], "request": item["text"], "expected_base": BASE, "auto_merge": False}}
    other = save(demo, "request-two")
    assert send(demo, other).status_code == 409


def test_uncertain_dispatch_never_retries_and_refresh_finds_existing_run(demo):
    item = save(demo)
    demo[1].dispatch_error = RemoteError(uncertain=True)
    assert send(demo, item).json()["state"] == "uncertain"
    assert send(demo, item).json()["state"] == "uncertain"
    assert len([c for c in demo[1].calls if c[0] == "POST"]) == 1
    demo[1].runs = [{"id": 123, "display_title": "Development request-one", "event": "workflow_dispatch", "status": "in_progress"}]
    updated = demo[2].post("/api/development/request-one/refresh").json()
    assert updated["state"] == "in_progress"
    assert updated["run_url"].endswith("/123")


def test_restart_marks_incomplete_dispatch_uncertain(demo):
    item = save(demo)
    demo[0]._update(item["id"], state="dispatching")
    restarted = Development(demo[0].conn, github=demo[1], enabled=True)
    assert restarted.get(item["id"])["state"] == "uncertain"
    assert asyncio.run(restarted.send(item["id"], item["fingerprint"]))["state"] == "uncertain"
    assert demo[1].calls == []


def test_refresh_reports_actual_pr_and_merge_without_installing_code(demo):
    item = save(demo)
    send(demo, item)
    demo[1].runs = [{"id": 123, "display_title": "Development request-one", "event": "workflow_dispatch", "status": "completed", "conclusion": "success"}]
    demo[1].pulls = [{"number": 9, "state": "open", "merged_at": None}]
    assert demo[2].post("/api/development/request-one/refresh").json()["state"] == "pr_ready"
    demo[1].pulls = [{"number": 9, "state": "closed", "merged_at": "2026-11-05", "merge_commit_sha": "c" * 40}]
    updated = demo[2].post("/api/development/request-one/refresh").json()
    assert updated["state"] == "merged" and updated["merge_sha"] == "c" * 40
    assert len([c for c in demo[1].calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Host": "evil.example"},
    {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://localhost:9999"},
    {"Host": "["}, {"X-Forwarded-For": "192.0.2.1"}, {"Forwarded": "for=192.0.2.1"},
])
def test_browser_origin_cannot_create_or_dispatch_jobs(demo, headers):
    assert demo[2].post("/api/development", headers=headers, json={"id": "request-one", "text": "Fix UI"}).status_code == 403
    assert demo[1].calls == []


def test_local_ui_can_save_and_secrets_are_rejected(demo):
    assert demo[2].post("/api/development", headers={"Origin": "http://localhost:5173"}, json={"id": "request-one", "text": "Fix heading"}).status_code == 200
    for text in ["Use sk-ant-sensitive-test-key", "token=private-value", "password: private", "Nul\0text"]:
        assert demo[2].post("/api/development", json={"id": "request-two", "text": text}).status_code == 400
    assert "private-value" not in json.dumps(demo[0].snapshot())


def test_disabled_feature_does_not_create_or_send(demo):
    demo[0].enabled = False
    assert demo[2].get("/api/development").json()["enabled"] is False
    assert demo[2].post("/api/development", json={"id": "request-one", "text": "Fix heading"}).status_code == 503
    assert demo[1].calls == []


def test_auto_merge_requires_the_merge_fingerprint_and_cannot_change_after_dispatch(demo):
    item = save(demo)
    path = f"/api/development/{item['id']}/send"
    assert demo[2].post(path, json={"fingerprint": item["fingerprint"], "auto_merge": True}).status_code == 409
    assert demo[1].calls == []
    response = demo[2].post(path, json={"fingerprint": item["merge_fingerprint"], "auto_merge": True})
    assert response.status_code == 200 and response.json()["auto_merge"] is True
    assert demo[1].calls[-1][2]["inputs"]["auto_merge"] is True
    assert send(demo, item).status_code == 409
    assert demo[2].post(path, json={"fingerprint": item["merge_fingerprint"], "auto_merge": True}).json()["state"] == "submitted"
    assert len([call for call in demo[1].calls if call[0] == "POST"]) == 1


def test_merge_mode_is_not_coerced_from_text_or_numbers(demo):
    item = save(demo)
    for mode in ["true", "false", 1, 0]:
        assert demo[2].post(f"/api/development/{item['id']}/send", json={"fingerprint": item["merge_fingerprint"], "auto_merge": mode}).status_code == 422
    assert demo[1].calls == []
