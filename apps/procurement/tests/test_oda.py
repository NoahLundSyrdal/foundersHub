import asyncio
import base64
import hashlib
import json
import os
import stat
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from office_procurement.oda import (
    OdaClient, OdaError, OdaConnectionRequired, OdaUncertainWrite,
    REDIRECT_URI, MCP_URL, cart_quantities,
)


def cart():
    return {"groups": [{"items": [{"product": {"id": 11, "name": "Milk", "price": "22.90"}, "quantity": 1, "totalGrossAmount": "22.90"}]}],
            "productQuantityCount": 1, "totalGrossAmount": "22.90", "totalBreakdown": {"delivery": "0.00"}, "url": "https://oda.com/no/cart/"}


class OdaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "oda/oauth.json"

    async def asyncTearDown(self):
        self.directory.cleanup()

    def connected_client(self, http):
        client = OdaClient(self.path, http_client=http)
        client._state = {"client": {"client_id": "test-public-client", "redirect_uri": REDIRECT_URI},
                         "tokens": {"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_at": time.time() + 3600}}
        client._save()
        return client

    async def test_oauth_public_registration_pkce_single_use_secure_storage(self):
        calls = []
        def handler(req):
            calls.append(req)
            if req.url.path == "/o/register/":
                body = json.loads(req.content)
                self.assertEqual(body["token_endpoint_auth_method"], "none")
                self.assertEqual(body["redirect_uris"], [REDIRECT_URI])
                return httpx.Response(201, json={"client_id": "test-public-client", "token_endpoint_auth_method": "none"})
            self.assertEqual(req.url.path, "/o/token/")
            fields = parse_qs(req.content.decode())
            self.assertEqual(fields["resource"], [MCP_URL])
            self.assertEqual(fields["grant_type"], ["authorization_code"])
            self.assertNotIn("client_secret", fields)
            self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"][0].encode()).digest()).rstrip(b"=").decode(), challenge)
            return httpx.Response(200, json={"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 3600, "token_type": "Bearer"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = OdaClient(self.path, http_client=http)
            self.assertFalse(client.connected())
            first = await client.authorization_url(REDIRECT_URI)
            latest = await client.authorization_url(REDIRECT_URI)
            query = parse_qs(urlsplit(latest).query)
            challenge = query["code_challenge"][0]
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertNotEqual(first, latest)
            with self.assertRaises(OdaError):
                await client.complete_authorization("code", parse_qs(urlsplit(first).query)["state"][0], REDIRECT_URI)
            await client.complete_authorization("code", query["state"][0], REDIRECT_URI)
            with self.assertRaises(OdaError):
                await client.complete_authorization("code", query["state"][0], REDIRECT_URI)
        self.assertTrue(client.connected())
        self.assertEqual(sum(r.url.path == "/o/register/" for r in calls), 1)
        self.assertEqual(sum(r.url.path == "/o/token/" for r in calls), 1)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertTrue(OdaClient(self.path).connected())
        self.assertNotIn("pending", json.loads(self.path.read_text()))

    async def test_expired_state_is_consumed_and_wrong_callback_rejected(self):
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(201, json={"client_id": "test-public-client"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = OdaClient(self.path, http_client=http)
            with self.assertRaises(OdaError):
                await client.authorization_url("https://attacker.example/callback")
            url = await client.authorization_url(REDIRECT_URI)
            state = parse_qs(urlsplit(url).query)["state"][0]
            client._state["pending"]["expires_at"] = time.time() - 1
            with self.assertRaises(OdaError):
                await client.complete_authorization("code", state, REDIRECT_URI)
            self.assertNotIn("pending", client._state)
        self.assertEqual(len(calls), 1)

    async def test_refresh_is_once_under_concurrency_and_sanitized_on_rejection(self):
        calls = []
        async def handler(req):
            calls.append(req)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token": "new-secret-access", "expires_in": 3600})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = self.connected_client(http)
            client._state["tokens"]["expires_at"] = 0
            values = await asyncio.gather(client._access_token(), client._access_token())
        self.assertEqual(values, ["new-secret-access", "new-secret-access"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(client._state["tokens"]["refresh_token"], "secret-refresh")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(400, text="secret-provider-error"))) as http:
            client = self.connected_client(http)
            client._state["tokens"]["expires_at"] = 0
            with self.assertRaises(OdaConnectionRequired) as raised:
                await client._access_token()
            self.assertNotIn("secret", str(raised.exception))
            self.assertFalse(client.connected())

    async def test_official_mcp_roundtrip_search_cart_and_additive_write(self):
        calls = []
        def handler(req):
            self.assertEqual(req.headers.get("Authorization"), "Bearer secret-access")
            if req.method == "GET":
                return httpx.Response(405)
            message = json.loads(req.content)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "Oda fake", "version": "2.0.0"}}
            elif message["method"] == "notifications/initialized":
                return httpx.Response(202)
            elif message["method"] == "tools/list":
                result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ("product_search", "get_cart", "manipulate_cart")]}
            else:
                self.assertEqual(message["method"], "tools/call")
                name, arguments = message["params"]["name"], message["params"]["arguments"]
                calls.append((name, arguments))
                data = {"result": [{"query": "milk", "hasMore": False, "products": [{"id": 11, "name": "Milk", "price": "22.90", "unitPrice": "22.90", "unitName": "l", "availability": {"isAvailable": True}}]}]} if name == "product_search" else cart()
                result = {"content": [], "structuredContent": data, "isError": False}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = self.connected_client(http)
            results = await client.search_products(["milk"])
            self.assertEqual(results[0]["products"][0]["price"], "22.90")
            self.assertEqual(await client.get_cart(), cart())
            self.assertEqual(await client.add_products([{"product_id": 11, "quantity": 2}]), cart())
            # Session bearer auth must not contaminate OAuth client default headers.
            self.assertNotIn("Authorization", http.headers)
        self.assertEqual(calls[-1], ("manipulate_cart", {"operations": [{"productId": 11, "quantity": 2}]}))
        self.assertEqual(len(calls), 3)

    async def test_write_timeout_is_uncertain_and_never_retried(self):
        writes = []
        def handler(req):
            if req.method == "GET":
                return httpx.Response(405)
            message = json.loads(req.content)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "Oda fake", "version": "2.0.0"}}
            elif message["method"] == "notifications/initialized":
                return httpx.Response(202)
            else:
                writes.append(message)
                raise httpx.ReadTimeout("secret-provider-body", request=req)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = self.connected_client(http)
            with self.assertRaises(OdaUncertainWrite) as raised:
                await client.add_products([{"product_id": 11, "quantity": 2}])
        self.assertEqual(len(writes), 1)
        self.assertNotIn("secret", str(raised.exception))

    async def test_mcp_unauthorized_requires_reconnection(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(401, text="secret-provider-body"))) as http:
            client = self.connected_client(http)
            with self.assertRaises(OdaConnectionRequired) as raised:
                await client.get_cart()
        self.assertNotIn("secret", str(raised.exception))

    async def test_invalid_writes_and_unapproved_tools_never_make_http_calls(self):
        def handler(req):
            self.fail("Invalid operation must not reach provider")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = self.connected_client(http)
            for lines in [[{"product_id": 11, "quantity": -1}], [{"product_id": True, "quantity": 1}], [{"product_id": 11, "quantity": 1, "clear_cart": True}]]:
                with self.assertRaises(OdaError):
                    await client.add_products(lines)
            with self.assertRaises(OdaError):
                await client._call_tool("checkout", {})

    async def test_cart_quantity_helper_preserves_decimal_totals_across_groups(self):
        value = cart()
        value["groups"].append({"id": "recipe-1", "groupType": "recipes", "items": [
            {"product": {"id": 11}, "quantity": 1.5, "totalGrossAmount": "34.35"},
        ]})
        self.assertEqual(cart_quantities(value), {11: Decimal("2.5")})
        with self.assertRaises(OdaError):
            cart_quantities({"groups": [{"products": []}]})


if __name__ == "__main__":
    unittest.main()
