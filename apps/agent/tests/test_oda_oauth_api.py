"""OAuth redirects expose neither saved credentials nor callback codes in logs."""
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from office import oda_food
from office.api import create_app
from office.oda_api import CALLBACK, OAuthAccessFilter


@pytest.mark.anyio
async def test_oauth_routes_redirect_and_sanitize_failed_authorization():
    app = create_app(":memory:")
    provider = app.state.oda_food.client
    provider.authorization_url = AsyncMock(return_value="https://oda.com/o/authorize/?state=test-state")
    provider.complete_authorization = AsyncMock()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            connected = await client.get("/api/integrations/oda")
            assert "tokens" not in connected.json()
            start = await client.get("/api/integrations/oda/connect")
            assert start.status_code == 303
            assert start.headers["location"].startswith("https://oda.com/o/authorize/")
            provider.authorization_url.assert_awaited_once_with(CALLBACK)
            finish = await client.get("/api/integrations/oda/callback", params={"code": "test-code", "state": "test-state"})
            assert finish.status_code == 303
            assert finish.headers["location"] == "http://127.0.0.1:5173/?oda=connected"
            assert finish.headers["cache-control"] == "no-store"
            assert finish.headers["referrer-policy"] == "no-referrer"
            provider.complete_authorization.assert_awaited_once_with("test-code", "test-state", CALLBACK)
            provider.complete_authorization.side_effect = ValueError("private-provider-detail")
            failed = await client.get("/api/integrations/oda/callback", params={"code": "bad-code", "state": "bad-state"})
            assert failed.status_code == 400
            assert "private-provider-detail" not in failed.text
    finally:
        oda_food.release(app.state.conn)
        app.state.conn.close()


def test_authorization_codes_are_removed_from_uvicorn_access_records():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/api/integrations/oda/callback?code=private-code&state=private-state", "1.1", 303), None)
    OAuthAccessFilter().filter(record)
    assert "private-code" not in record.getMessage()
    assert "private-state" not in record.getMessage()
    assert "/api/integrations/oda/callback" in record.getMessage()
