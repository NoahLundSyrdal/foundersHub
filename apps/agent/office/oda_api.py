"""Loopback-only Oda OAuth entry points; credentials never enter API responses."""
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from office import oda_food

CALLBACK = "http://127.0.0.1:3001/api/integrations/oda/callback"


class OAuthAccessFilter(logging.Filter):
    """Uvicorn otherwise includes short-lived authorization codes in access logs."""
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split("?", 1)[0] == "/api/integrations/oda/callback":
                args[2] = "/api/integrations/oda/callback"
                record.args = tuple(args)
        return True


def router(service):
    routes = APIRouter()
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthAccessFilter) for item in logger.filters):
        logger.addFilter(OAuthAccessFilter())

    @routes.get("/api/integrations/oda")
    async def status():
        return {"connected": service.client.connected(), "enabled": oda_food.enabled(), "server": "https://oda.com/mcp",
                "connect_url": "/api/integrations/oda/connect", "checkout_required": True}

    @routes.get("/api/integrations/oda/connect")
    async def connect():
        try:
            url = await service.client.authorization_url(CALLBACK)
        except Exception as error:
            raise HTTPException(503, "Oda authorization could not be started. Please try again.") from error
        return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

    @routes.get("/api/integrations/oda/callback")
    async def callback(code: str = "", state: str = "", error: str = ""):
        if error or not code or not state:
            raise HTTPException(400, "Oda connection was not authorized. You can try connecting again.")
        try:
            await service.client.complete_authorization(code, state, CALLBACK)
        except Exception as failure:
            raise HTTPException(400, "Oda authorization expired or could not be verified. Connect again.") from failure
        return RedirectResponse("http://127.0.0.1:5173/?oda=connected", status_code=303,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    return routes
