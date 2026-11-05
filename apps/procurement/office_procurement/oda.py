"""Oda's official OAuth + MCP adapter. No checkout, payment or model tool exposure.

Product/cart tools follow https://oda.com/mcp/manifest/ (version 2.0.0).
Positive manipulate_cart quantities are additive deltas, never target quantities.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import tempfile
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "https://oda.com/mcp"
REGISTER_URL = "https://oda.com/o/register/"
AUTHORIZE_URL = "https://oda.com/o/authorize/"
TOKEN_URL = "https://oda.com/o/token/"
REDIRECT_URI = "http://127.0.0.1:3001/api/integrations/oda/callback"
_ALLOWED_TOOLS = frozenset({"product_search", "get_cart", "manipulate_cart"})
_MAX_BYTES = 1_000_000


class OdaError(Exception):
    """Sanitized provider/configuration failure; never contains provider response bodies."""


class OdaConnectionRequired(OdaError):
    pass


class OdaUncertainWrite(OdaError):
    """Do not retry: compare persisted intended deltas against Oda's current cart first."""


def cart_quantities(cart: dict) -> dict[int, Decimal]:
    """Sum actual manifest group items, including products repeated in recipe/list groups."""
    totals: dict[int, Decimal] = {}
    try:
        groups = cart["groups"]
        if not isinstance(groups, list):
            raise ValueError
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("items"), list):
                raise ValueError
            for item in group["items"]:
                product_id = item["product"]["id"]
                quantity = Decimal(str(item["quantity"]))
                if type(product_id) is not int or product_id <= 0 or not quantity.is_finite() or quantity < 0:
                    raise ValueError
                totals[product_id] = totals.get(product_id, Decimal(0)) + quantity
        return totals
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise OdaError("Oda returned an invalid cart quantity structure") from None


def _auth_failure(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403}:
        return True
    return any(_auth_failure(item) for item in getattr(error, "exceptions", ()))


class _BearerClient:
    """Add bearer auth to this MCP session without mutating shared OAuth client headers."""
    max_redirects = 3

    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client, self.token = client, token

    def build_request(self, method, url, **kwargs):
        request = self.client.build_request(method, url, **kwargs)
        self._check_url(request)
        request.headers["Authorization"] = f"Bearer {self.token}"
        return request

    @staticmethod
    def _check_url(request):
        if request.url.scheme != "https" or request.url.host != "oda.com" or request.url.port not in {None, 443} or request.url.userinfo:
            raise OdaError("Oda transport refused an unexpected server origin")

    async def send(self, request, **kwargs):
        self._check_url(request)
        return await self.client.send(request, **kwargs)


class OdaClient:
    def __init__(self, storage_path: Path | None = None, http_client: httpx.AsyncClient | None = None):
        self.storage_path = Path(storage_path) if storage_path is not None else Path(__file__).resolve().parents[3] / ".data/oda/oauth.json"
        self._http = http_client
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._state = self._load()

    def _load(self):
        try:
            if self.storage_path.is_symlink() or self.storage_path.parent.is_symlink():
                raise OdaError("Oda credential storage must not be a symbolic link")
            if not self.storage_path.exists():
                return {}
            if self.storage_path.stat().st_uid != os.getuid() or self.storage_path.stat().st_size > 128_000:
                raise OdaError("Oda credential storage is not accessible safely")
            os.chmod(self.storage_path, 0o600)
            data = json.loads(self.storage_path.read_text())
            if not isinstance(data, dict):
                raise ValueError
            return data
        except OdaError:
            raise
        except (OSError, ValueError):
            raise OdaError("Oda credential storage could not be read") from None

    def _save(self):
        temporary = None
        try:
            if self.storage_path.is_symlink() or self.storage_path.parent.is_symlink():
                raise OdaError("Oda credential storage must not be a symbolic link")
            self.storage_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.storage_path.parent, 0o700)
            descriptor, temporary = tempfile.mkstemp(prefix=".oauth-", dir=self.storage_path.parent)
            with os.fdopen(descriptor, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(self._state, handle, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.storage_path)
            os.chmod(self.storage_path, 0o600)
        except (OSError, ValueError):
            raise OdaError("Oda credential storage could not be saved") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def connected(self) -> bool:
        tokens = self._state.get("tokens", {})
        return bool(self._state.get("client", {}).get("client_id") and
                    (tokens.get("refresh_token") or (tokens.get("access_token") and tokens.get("expires_at", 0) > time.time())))

    @staticmethod
    def _redirect(redirect_uri):
        if redirect_uri != REDIRECT_URI:
            raise OdaError("Oda authorization requires the configured local callback URI")

    async def _oauth_request(self, url, *, data=None, payload=None):
        own = self._http is None
        client = self._http or httpx.AsyncClient(timeout=15, follow_redirects=False)
        try:
            response = await client.post(url, data=data, json=payload, timeout=15, follow_redirects=False)
            if response.status_code in {400, 401, 403} and url == TOKEN_URL:
                raise OdaConnectionRequired("Oda authorization expired or was rejected; reconnect your account")
            if not response.is_success or len(response.content) > 128_000:
                raise OdaError("Oda authorization service is unavailable")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except OdaError:
            raise
        except (httpx.HTTPError, ValueError):
            raise OdaError("Oda authorization service is unavailable") from None
        finally:
            if own:
                await client.aclose()

    async def authorization_url(self, redirect_uri: str) -> str:
        self._redirect(redirect_uri)
        async with self._lock:
            client = self._state.get("client", {})
            if not client.get("client_id"):
                registered = await self._oauth_request(REGISTER_URL, payload={
                    "client_name": "Founders Hub Office", "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                    "token_endpoint_auth_method": "none", "scope": "mcp",
                })
                if not isinstance(registered.get("client_id"), str) or not registered["client_id"] or registered.get("token_endpoint_auth_method", "none") != "none":
                    raise OdaError("Oda did not register a supported public client")
                client = {"client_id": registered["client_id"], "redirect_uri": redirect_uri}
                self._state["client"] = client
            if client.get("redirect_uri") != redirect_uri:
                raise OdaError("Oda client callback does not match the configured URI")
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            self._state["pending"] = {"state_hash": hashlib.sha256(state.encode()).hexdigest(), "verifier": verifier,
                                      "redirect_uri": redirect_uri, "expires_at": time.time() + 600}
            self._save()
            return AUTHORIZE_URL + "?" + urlencode({"response_type": "code", "client_id": client["client_id"],
                "redirect_uri": redirect_uri, "scope": "mcp", "resource": MCP_URL, "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256"})

    def _accept_tokens(self, value, previous=None):
        access = value.get("access_token")
        refresh = value.get("refresh_token") or (previous or {}).get("refresh_token")
        try:
            lifetime = float(value.get("expires_in", 3600))
        except (TypeError, ValueError):
            raise OdaError("Oda returned an invalid token response") from None
        if not isinstance(access, str) or not access or len(access) > 16_384 or not math.isfinite(lifetime) or lifetime <= 0 or str(value.get("token_type", "Bearer")).lower() != "bearer":
            raise OdaError("Oda returned an invalid token response")
        if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 16_384):
            raise OdaError("Oda returned an invalid token response")
        self._state["tokens"] = {"access_token": access, "refresh_token": refresh, "expires_at": time.time() + lifetime}
        self._save()

    async def complete_authorization(self, code: str, state: str, redirect_uri: str) -> None:
        self._redirect(redirect_uri)
        if not isinstance(code, str) or not 1 <= len(code) <= 4096 or not isinstance(state, str) or not 1 <= len(state) <= 512:
            raise OdaError("Oda authorization response is invalid")
        async with self._lock:
            pending = self._state.get("pending", {})
            digest = hashlib.sha256(state.encode()).hexdigest()
            valid = hmac.compare_digest(digest, pending.get("state_hash", "")) and pending.get("redirect_uri") == redirect_uri
            if not valid:
                raise OdaError("Oda authorization state is invalid or expired")
            # Consume before token exchange: a failed or interrupted callback cannot replay a code.
            self._state.pop("pending", None)
            self._save()
            if pending.get("expires_at", 0) <= time.time():
                raise OdaError("Oda authorization state is invalid or expired")
            value = await self._oauth_request(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                "client_id": self._state["client"]["client_id"], "redirect_uri": redirect_uri,
                "code_verifier": pending["verifier"], "resource": MCP_URL})
            self._accept_tokens(value)

    async def _access_token(self):
        async with self._lock:
            tokens = self._state.get("tokens", {})
            if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 15:
                return tokens["access_token"]
            refresh, client_id = tokens.get("refresh_token"), self._state.get("client", {}).get("client_id")
            if not refresh or not client_id:
                raise OdaConnectionRequired("Connect your Oda account before using live products or cart")
            try:
                value = await self._oauth_request(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh,
                                                                 "client_id": client_id, "resource": MCP_URL})
            except OdaConnectionRequired:
                self._state.pop("tokens", None)
                self._save()
                raise
            self._accept_tokens(value, tokens)
            return self._state["tokens"]["access_token"]

    @staticmethod
    def _decode_result(result):
        if result.isError:
            raise OdaError("Oda could not complete this operation")
        data = result.structuredContent
        if data is None:
            texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
            if not texts or sum(len(text) for text in texts) > _MAX_BYTES:
                raise OdaError("Oda returned an invalid tool response")
            try:
                data = json.loads(texts[0])
            except ValueError:
                raise OdaError("Oda returned an invalid tool response") from None
        try:
            if len(json.dumps(data, allow_nan=False)) > _MAX_BYTES:
                raise ValueError
        except (TypeError, ValueError):
            raise OdaError("Oda returned an invalid tool response") from None
        if not isinstance(data, dict):
            raise OdaError("Oda returned an invalid tool response")
        return data

    async def _call_tool(self, name: str, arguments: dict):
        if name not in _ALLOWED_TOOLS:
            raise OdaError("Oda tool is outside the application allowlist")
        token = await self._access_token()
        own = self._http is None
        client = self._http or httpx.AsyncClient(timeout=20, follow_redirects=False)
        dispatched = False
        try:
            async with asyncio.timeout(25):
                async with streamable_http_client(MCP_URL, http_client=_BearerClient(client, token), terminate_on_close=False) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                        await session.initialize()
                        dispatched = True
                        result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=20))
                        return self._decode_result(result)
        except asyncio.CancelledError:
            # The caller's persisted attempt remains authoritative; never replay a cancelled write.
            if name == "manipulate_cart" and dispatched:
                raise OdaUncertainWrite("Oda cart outcome is uncertain; reconcile the cart before retrying") from None
            raise
        except Exception as error:
            if _auth_failure(error):
                raise OdaConnectionRequired("Oda authorization was rejected; reconnect your account") from None
            if name == "manipulate_cart" and dispatched:
                raise OdaUncertainWrite("Oda cart outcome is uncertain; reconcile the cart before retrying") from None
            if isinstance(error, OdaError):
                raise
            raise OdaError("Oda is unavailable; try the read operation later") from None
        finally:
            if own:
                await client.aclose()

    async def search_products(self, queries: list[str], size=10) -> list[dict]:
        if not isinstance(queries, list) or not 1 <= len(queries) <= 10 or any(not isinstance(q, str) or not q.strip() or len(q) > 200 for q in queries) or type(size) is not int or not 1 <= size <= 20:
            raise OdaError("Oda search requires 1–10 short queries and a size from 1–20")
        data = await self._call_tool("product_search", {"queries": [q.strip() for q in queries], "page": 1, "size": size})
        results = data.get("result")
        if not isinstance(results, list) or len(results) > 10 or any(not isinstance(batch, dict) or not isinstance(batch.get("products"), list) or len(batch["products"]) > 20 for batch in results):
            raise OdaError("Oda returned an invalid product search structure")
        return results

    async def get_cart(self) -> dict:
        value = await self._call_tool("get_cart", {})
        cart = value.get("result", value)
        if not isinstance(cart, dict) or not isinstance(cart.get("groups"), list):
            raise OdaError("Oda returned an invalid cart structure")
        return cart

    async def add_products(self, lines: list[dict]) -> dict:
        """Add positive quantity deltas; does not clear, replace, pay for or submit the cart."""
        if not isinstance(lines, list) or not 1 <= len(lines) <= 20:
            raise OdaError("Oda cart addition requires 1–20 product lines")
        operations = []
        for line in lines:
            if not isinstance(line, dict) or set(line) != {"product_id", "quantity"} or type(line["product_id"]) is not int or line["product_id"] <= 0 or type(line["quantity"]) is not int or not 1 <= line["quantity"] <= 20:
                raise OdaError("Oda cart additions require a product ID and a positive quantity from 1–20")
            operations.append({"productId": line["product_id"], "quantity": line["quantity"]})
        async with self._write_lock:
            value = await self._call_tool("manipulate_cart", {"operations": operations})
        cart = value.get("result", value)
        if not isinstance(cart, dict) or not isinstance(cart.get("groups"), list):
            raise OdaUncertainWrite("Oda cart outcome is uncertain; reconcile the cart before retrying")
        return cart
