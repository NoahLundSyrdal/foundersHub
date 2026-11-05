import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from office_procurement.models import Assessment, Interpretation, WorkerInput, Research
from office_procurement.worker import Context, run_worker, search_item
from office_procurement import worker


def request(text="We need coffee", **kwargs):
    return WorkerInput.model_validate({
        "text": text,
        "station": {"id": "kitchen", "name": "Kitchen", "responsible_team": "Facilities",
                    "equipment": ["coffee machine"], "item_ids": ["coffee", "cups", "ventilation-service"]},
        "catalogue": [
            {"id": "coffee", "name": "Coffee 1kg", "station_id": "kitchen", "price_nok": 189,
             "delivery_estimate": "Demo tomorrow", "supplier": "Demo"},
            {"id": "cups", "name": "Cups", "station_id": "kitchen", "price_nok": 79,
             "delivery_estimate": "Demo tomorrow", "supplier": "Demo"},
            {"id": "ventilation-service", "name": "Ventilation inspection", "station_id": "kitchen", "price_nok": 800,
             "delivery_estimate": "Demo tomorrow", "supplier": "Demo", "fulfillment_kind": "service"},
        ],
        "policy": {"max_order_nok": 1000, "require_approval": True, "preferences": []},
        "open_requests": [], "provider": "mock", **kwargs,
    })


class DiscoveryPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        worker._DISCOVERY_CACHE.clear()
        self.environment = patch.dict(os.environ, {"TAVILY_API_KEY": "", "ANTHROPIC_API_KEY": "", "FINN_DISCOVERY_ENABLED": "false"})
        self.environment.start()


    async def asyncTearDown(self):
        worker._DISCOVERY_CACHE.clear()
        self.environment.stop()


    async def test_discovery_sources_overlap_without_timing_assumptions(self):
        entered = set()
        both_entered = asyncio.Event()
        async def handler(req):
            body = json.loads(req.content)
            source = "finn" if body.get("include_domains") else "general"
            entered.add(source)
            if len(entered) == 2:
                both_entered.set()
            # A sequential implementation cannot release this barrier.
            await asyncio.wait_for(both_entered.wait(), 2)
            url = "https://www.finn.no/item/1" if source == "finn" else "https://example.com/item/1"
            return httpx.Response(200, json={"results": [{"title": source, "url": url, "content": "Unverified discovery"}]})
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key", "FINN_DISCOVERY_ENABLED": "true"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await search_item(Context(request()), "coffee", client=client)
        self.assertEqual(entered, {"general", "finn"})
        self.assertEqual([r.title for r in result], ["general", "finn"])


    async def test_discovery_cache_ttl_and_key_do_not_include_transcripts(self):
        calls = []
        def handler(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200, json={"results": [{"title": "Supplier", "url": "https://example.com/item", "content": "Unverified discovery"}]})
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(worker, "monotonic", return_value=100):
                    await search_item(Context(request("Coffee for private-person-one")), "coffee", client=client)
                with patch.object(worker, "monotonic", return_value=110):
                    second = Context(request("Coffee for private-person-two"))
                    result = await search_item(second, "coffee", client=client)
                    self.assertTrue(any(p.stage == "research.cached" for p in second.progress))
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(result[0].executable)
                with patch.object(worker, "monotonic", return_value=100 + worker.DISCOVERY_CACHE_TTL_SECONDS):
                    await search_item(Context(request()), "coffee", client=client)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("private-person", str(worker._DISCOVERY_CACHE))
        self.assertNotIn("test-key", str(worker._DISCOVERY_CACHE))


    async def test_concurrent_tools_share_call_budget_and_two_request_gate(self):
        first_two = asyncio.Event()
        release = asyncio.Event()
        active, peak, calls = 0, 0, 0
        async def handler(req):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            number = calls
            peak = max(peak, active)
            if active == 2:
                first_two.set()
            await release.wait()
            active -= 1
            body = json.loads(req.content)
            host = "finn.no" if body.get("include_domains") else "example.com"
            return httpx.Response(200, json={"results": [{"title": "Supplier", "url": f"https://{host}/item/{number}", "content": "Unverified"}]})
        context = Context(request())
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key", "FINN_DISCOVERY_ENABLED": "true"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tasks = [asyncio.create_task(search_item(context, item, client=client)) for item in ("coffee", "cups", "ventilation-service")]
                try:
                    await asyncio.wait_for(first_two.wait(), 2)
                    self.assertEqual(active, 2)
                finally:
                    release.set()
                    await asyncio.gather(*tasks)
        self.assertEqual(calls, 3)
        self.assertEqual(peak, 2)


    async def test_disabled_or_failed_discovery_is_not_cached(self):
        calls = []
        def handler(req):
            calls.append(1)
            return httpx.Response(500, text="private-error")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await search_item(Context(request()), "coffee", client=client)
            self.assertFalse(worker._DISCOVERY_CACHE)
            self.assertEqual(calls, [])
            with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}):
                await search_item(Context(request()), "coffee", client=client)
                await search_item(Context(request()), "coffee", client=client)
        self.assertEqual(len(calls), 2)
        self.assertFalse(worker._DISCOVERY_CACHE)


    async def test_discovery_cache_separates_finn_mode(self):
        calls = []
        def handler(req):
            body = json.loads(req.content)
            calls.append(body)
            url = "https://finn.no/item/1" if body.get("include_domains") else "https://example.com/item/1"
            return httpx.Response(200, json={"results": [{"title": "Supplier", "url": url, "content": "Unverified"}]})
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await search_item(Context(request()), "coffee", client=client)
                with patch.dict(os.environ, {"FINN_DISCOVERY_ENABLED": "true"}):
                    results = await search_item(Context(request()), "coffee", client=client)
        self.assertEqual(len(calls), 3)
        self.assertTrue(any(r.marketplace == "finn" for r in results))


    async def test_native_search_payload_real_source_parsing_and_finn_filter(self):
        calls = []
        def handler(req):
            body = json.loads(req.content)
            calls.append(body)
            finn = body["tools"][0].get("allowed_domains") == ["finn.no"]
            sources = [{"type": "web_search_result", "title": "Real source title", "url": "https://finn.no/item/1" if finn else "https://example.com/coffee", "encrypted_content": "do-not-emit", "page_age": "2 days ago"}]
            if finn:
                sources.extend([
                    {"type": "web_search_result", "title": "Wrong domain", "url": "https://example.com/finn", "encrypted_content": "hidden"},
                    {"type": "web_search_result", "title": "Private", "url": "https://127.0.0.1/item", "encrypted_content": "hidden"},
                ])
            return httpx.Response(200, json={
                "id": "msg-test", "type": "message", "role": "assistant", "model": body["model"], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
                "content": [{"type": "web_search_tool_result", "tool_use_id": "srvtool-1", "content": sources},
                            {"type": "text", "text": "Untrusted generated prose must not become a quote.", "citations": []}],
            })
        context = Context(request("Coffee for private meeting attendee", provider="anthropic", model="claude-sonnet-5-5"))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-native-key", "FINN_DISCOVERY_ENABLED": "true"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                results = await search_item(context, "coffee", client=client)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["model"] == "claude-sonnet-5-5" for call in calls))
        self.assertTrue(all(call["tools"][0]["type"] == "web_search_20250305" and call["tools"][0]["max_uses"] == 1 for call in calls))
        self.assertNotIn("private meeting", str(calls))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.source == "anthropic_web_search" and not r.executable for r in results))
        self.assertEqual([r.marketplace for r in results], [None, "finn"])
        self.assertNotIn("do-not-emit", str(results))


    async def test_native_provider_error_does_not_retry_or_invent_mock_results(self):
        calls = []
        def handler(req):
            calls.append(1)
            return httpx.Response(503, json={"type": "error", "error": {"type": "overloaded_error", "message": "secret-error-body"}})
        context = Context(request(provider="anthropic"))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-native-key"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(await search_item(context, "coffee", client=client), [])
        self.assertEqual(len(calls), 1)
        self.assertFalse(worker._DISCOVERY_CACHE)
        self.assertNotIn("secret-error-body", str(context.progress))
        self.assertTrue(any(p.stage == "search_unavailable" for p in context.progress))


    async def test_native_key_never_enables_network_for_mock_provider(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-native-key"}):
            with patch.object(worker, "AsyncAnthropic", side_effect=AssertionError("must not instantiate")):
                self.assertEqual(await search_item(Context(request(provider="mock")), "coffee"), [])


    async def test_native_tool_error_or_generated_links_are_not_discovery_sources(self):
        def handler(req):
            return httpx.Response(200, json={
                "id": "msg-test", "type": "message", "role": "assistant", "model": "test", "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [
                    {"type": "web_search_tool_result", "tool_use_id": "srvtool-1", "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}},
                    {"type": "text", "text": "Buy at https://example.com/invented", "citations": []},
                ]})
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-native-key"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(await search_item(Context(request(provider="anthropic")), "coffee", client=client), [])
        self.assertFalse(worker._DISCOVERY_CACHE)
