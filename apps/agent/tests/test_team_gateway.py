import logging
import asyncio
from pathlib import Path
import stat
import tempfile
import unittest

import httpx

from office.team_gateway import BODY_LIMIT, COOKIE, RedactCapabilityPaths, create_gateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        (self.dist / "index.html").write_text("<html>team app</html>")
        (self.dist / "main.js").write_text("console.log('app')")
        (self.dist / ".env").write_text("private")
        self.secret = self.root / "private" / "invite-token"
        self.calls = []
        self.redirect = None
        async def backend(request):
            self.calls.append(request)
            if self.redirect:
                return httpx.Response(302, headers={"Location": self.redirect, "Set-Cookie": "upstream=secret"})
            if request.url.path == "/api/events":
                return httpx.Response(200, content=b"data: update\n\n", headers={"Content-Type": "text/event-stream", "Set-Cookie": "upstream=secret"})
            return httpx.Response(200, json={"path": request.url.path, "method": request.method}, headers={"Set-Cookie": "upstream=secret"})
        self.app = create_gateway(dist_path=self.dist, secret_path=self.secret, transport=httpx.MockTransport(backend))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://team.test", follow_redirects=False)
        self.addAsyncCleanup(self.client.aclose)

    async def join(self):
        return await self.client.get("/join/" + self.secret.read_text())

    async def test_unauthorized_and_bad_invite_disclose_nothing(self):
        for path in ["/", "/main.js", "/api/state", "/docs", "/openapi.json"]:
            self.assertIn((await self.client.get(path)).status_code, {401, 404})
        response = await self.client.get("/join/invalid")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(COOKIE, response.headers.get("set-cookie", ""))
        health = await self.client.get("/healthz")
        self.assertEqual(health.text, "ok")
        self.assertNotIn(self.secret.read_text(), health.text)
        self.assertEqual(self.calls, [])

    async def test_join_cookie_is_secure_and_secret_permissions_are_private(self):
        response = await self.join()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertNotIn(self.secret.read_text(), cookie)
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.secret.parent.stat().st_mode), 0o700)
        original = self.secret.read_text()
        create_gateway(dist_path=self.dist, secret_path=self.secret)
        self.assertEqual(self.secret.read_text(), original)
        self.assertEqual((await self.client.get("/")).status_code, 200)
        self.assertEqual((await self.client.get("/main.js")).status_code, 200)
        self.assertEqual((await self.client.get("/.env")).status_code, 404)

    async def test_only_allowlisted_reads_and_intake_reach_fixed_backend(self):
        await self.join()
        for path in ["/api/state", "/api/requests", "/api/work", "/api/health", "/api/integrations/oda"]:
            response = await self.client.get(path, headers={"Authorization": "must-not-forward", "X-Forwarded-Host": "evil.test"})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("set-cookie", response.headers)
            self.assertEqual(self.calls[-1].url.host, "127.0.0.1")
            self.assertNotIn("authorization", self.calls[-1].headers)
            self.assertNotIn("cookie", self.calls[-1].headers)
            self.assertNotIn("x-forwarded-host", self.calls[-1].headers)
        for path in ["/api/input", "/api/device-events", "/api/requests/task-1/transcript"]:
            response = await self.client.post(path, json={"text": "coffee"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.calls[-1].method, "POST")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    async def test_approval_oauth_bridge_and_other_mutations_are_blocked(self):
        await self.join()
        for method, path in [("POST", "/api/tasks/task-1/decide"), ("POST", "/api/requests/task-1/approve"),
                             ("POST", "/api/requests/task-1/requote"), ("GET", "/api/integrations/oda/connect"),
                             ("GET", "/api/integrations/oda/callback"), ("GET", "/api/whatsapp"),
                             ("POST", "/api/work"), ("PUT", "/api/input"), ("DELETE", "/api/state")]:
            self.assertEqual((await self.client.request(method, path)).status_code, 403, path)
        self.assertEqual(self.calls, [])

    async def test_public_confirmation_preserves_methods_and_reject_is_exact(self):
        token = "opaque_capability_1234567890"
        for method, path in [("GET", f"/confirm/{token}"), ("HEAD", f"/confirm/{token}"),
                             ("POST", f"/confirm/{token}"), ("POST", f"/confirm/{token}/reject")]:
            response = await self.client.request(method, path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.calls[-1].method, method)
            self.assertEqual(self.calls[-1].url.path, path)
        before = len(self.calls)
        for path in [f"/confirm/{token}/approve", f"/confirm/{token}/other/path", "/confirm/x"]:
            self.assertIn((await self.client.get(path)).status_code, {401, 404})
        self.assertEqual(len(self.calls), before)

    async def test_body_limit_is_enforced_before_backend_call(self):
        await self.join()
        response = await self.client.post("/api/input", content=b"x" * (BODY_LIMIT + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.calls, [])

    async def test_confirmation_keeps_origin_for_backend_validation(self):
        response = await self.client.post("/confirm/" + "a" * 43, headers={"Origin": "https://other.test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[-1].headers["origin"], "https://other.test")

    async def test_redirects_cannot_escape_gateway_and_upstream_cookies_are_stripped(self):
        await self.join()
        for location in ["https://evil.test/steal", "//evil.test/steal", "https://127.0.0.1:3011/other", "/\\evil.test"]:
            self.redirect = location
            response = await self.client.get("/api/state")
            self.assertEqual(response.status_code, 502, location)
        self.redirect = "http://127.0.0.1:3011/"
        response = await self.client.get("/api/state")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/")
        self.assertNotIn("set-cookie", response.headers)

    async def test_events_are_forwarded_as_event_stream(self):
        await self.join()
        response = await self.client.get("/api/events", headers={"Last-Event-ID": "event-2"})
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertEqual(response.content, b"data: update\n\n")
        self.assertEqual(self.calls[-1].headers["last-event-id"], "event-2")
        self.assertNotIn("set-cookie", response.headers)

    async def test_logging_redacts_invite_and_capability_paths(self):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, '%s "%s %s HTTP/1.1" %s',
                                   ("client", "GET", "/join/secret-invite?x=1", 303), None)
        RedactCapabilityPaths().filter(record)
        self.assertNotIn("secret-invite", record.getMessage())
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, "POST /confirm/secret-capability/reject", (), None)
        RedactCapabilityPaths().filter(record)
        self.assertNotIn("secret-capability", record.getMessage())

    async def test_nonloopback_upstream_is_rejected(self):
        for url in ["https://evil.test", "http://127.0.0.1:3011/path", "http://user:pass@127.0.0.1", "http://127.0.0.1?url=evil"]:
            with self.assertRaises(ValueError):
                create_gateway(backend_url=url, secret_path=self.secret, dist_path=self.dist)


    async def test_sse_disconnect_closes_upstream_without_waiting_for_entire_stream(self):
        await self.join()
        delivered = asyncio.Event()
        closed = asyncio.Event()
        class EndlessStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: first\n\n"
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        async def backend(request):
            return httpx.Response(200, stream=EndlessStream(), headers={"Content-Type": "text/event-stream"})
        app = create_gateway(dist_path=self.dist, secret_path=self.secret, transport=httpx.MockTransport(backend))
        first_receive = True
        async def receive():
            nonlocal first_receive
            if first_receive:
                first_receive = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await delivered.wait()
            return {"type": "http.disconnect"}
        chunks = []
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                delivered.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "https", "path": "/api/events", "raw_path": b"/api/events",
                 "query_string": b"", "root_path": "", "server": ("team.test", 443), "client": ("127.0.0.1", 1000),
                 "headers": [(b"cookie", (COOKIE + "=" + self.client.cookies.get(COOKIE)).encode())]}
        await asyncio.wait_for(app(scope, receive, send), timeout=2)
        self.assertEqual(chunks, [b"data: first\n\n"])
        self.assertTrue(closed.is_set())
