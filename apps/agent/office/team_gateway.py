"""Invite-protected demo gateway. The loopback backend retains purchase authority."""
import hashlib
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parents[3]
COOKIE = "office_team"
BODY_LIMIT = 64 * 1024
CAPABILITY = re.compile(r"/confirm/[A-Za-z0-9_-]{16,256}(?:/reject)?\Z")
SAFE_READ = re.compile(r"/api/(?:health|state|stations|alerts|integrations/oda|requests(?:/[A-Za-z0-9_-]+)?|tasks(?:/[A-Za-z0-9_-]+)?|work(?:/[A-Za-z0-9_-]+)?|events|air-quality|facility|feedback)\Z")
SECURITY_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class RedactCapabilityPaths(logging.Filter):
    """Uvicorn logs request targets through args; strip capabilities before formatting."""
    def filter(self, record):
        def redact(value):
            if isinstance(value, str):
                return re.sub(r"/(join|confirm)/[^\s?\"']+", r"/\1/[redacted]", value)
            return value
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


def _secret(path: Path) -> str:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("Secret directory must not be a symlink")
    path.parent.chmod(0o700)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        value = secrets.token_urlsafe(48)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            return _secret(path)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        return value
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Secret must be a regular file")
        os.fchmod(stream.fileno(), 0o600)
        value = stream.read(256).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", value):
        raise ValueError("Invalid invite secret")
    return value


def _backend(value: str) -> str:
    parsed = urlsplit(value)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        parsed.port
    except ValueError:
        loopback = False
    if (parsed.scheme not in {"http", "https"} or not loopback or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Gateway backend must be a fixed loopback origin")
    return value.rstrip("/")


def _location(value: str, backend: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or value.startswith("//") or "\\" in value:
        return None
    if parsed.scheme or parsed.netloc:
        upstream = urlsplit(backend)
        if (parsed.scheme, parsed.netloc) != (upstream.scheme, upstream.netloc):
            return None
        value = parsed.path or "/"
        if parsed.query:
            value += "?" + parsed.query
    if not value.startswith("/"):
        return None
    return value


def create_gateway(backend_url: str = "http://127.0.0.1:3011", dist_path: str | Path = ROOT / "apps/web/dist",
                   secret_path: str | Path = ROOT / ".data/team-gateway/invite-token", *,
                   transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    backend = _backend(backend_url)
    invite = _secret(Path(secret_path))
    cookie = hmac.new(invite.encode(), b"office-team-cookie-v1", hashlib.sha256).hexdigest()
    root = Path(dist_path).resolve()
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RedactCapabilityPaths) for item in logger.filters):
        logger.addFilter(RedactCapabilityPaths())
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def protect_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def gateway(request: Request, path: str):
        path = "/" + path
        if path == "/api/events" and os.getenv("OFFICE_GATEWAY_DISABLE_SSE", "") == "true":
            return Response(status_code=503)
        if path == "/healthz" and request.method in {"GET", "HEAD"}:
            return Response("ok", media_type="text/plain")
        if path.startswith("/join/"):
            candidate = path.removeprefix("/join/")
            if request.method != "GET" or not hmac.compare_digest(candidate.encode(), invite.encode()):
                return Response(status_code=404)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, cookie, secure=True, httponly=True, samesite="lax", path="/", max_age=12 * 3600)
            return response
        public = bool(CAPABILITY.fullmatch(path)) and request.method in {"GET", "HEAD", "POST"}
        if not public:
            if not hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), cookie.encode()):
                return Response(status_code=401)
            if path.startswith("/api/"):
                allowed = (request.method in {"GET", "HEAD"} and SAFE_READ.fullmatch(path)) or (
                    request.method == "POST" and (path in {"/api/input", "/api/device-events"}
                    or re.fullmatch(r"/api/requests/[A-Za-z0-9_-]+/transcript", path)))
                if not allowed:
                    return Response(status_code=403)
            else:
                if request.method not in {"GET", "HEAD"}:
                    return Response(status_code=403)
                if any(part.startswith(".") for part in Path(path).parts):
                    return Response(status_code=404)
                resolved = (root / path.lstrip("/")).resolve()
                if not resolved.is_relative_to(root):
                    return Response(status_code=404)
                if resolved.is_dir() or not resolved.is_file():
                    # SPA paths may use index, but never disclose hidden or arbitrary files.
                    if "." in Path(path).name or any(part.startswith(".") for part in Path(path).parts):
                        return Response(status_code=404)
                    resolved = root / "index.html"
                if not resolved.is_file():
                    return Response(status_code=404)
                return FileResponse(resolved)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > BODY_LIMIT:
                return Response(status_code=413)
        target = backend + path
        if request.url.query:
            target += "?" + request.url.query
        headers = {key: value for key, value in request.headers.items() if key.lower() in {"accept", "content-type", "last-event-id", "origin"}}
        client = httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=httpx.Timeout(20, read=None if path == "/api/events" else 75))
        try:
            upstream = await client.send(client.build_request(request.method, target, content=bytes(body), headers=headers), stream=True)
            response_headers = {key: value for key, value in upstream.headers.items() if key.lower() in {"content-type", "content-disposition", "retry-after"}}
            if "location" in upstream.headers:
                location = _location(upstream.headers["location"], backend)
                if location is None:
                    await upstream.aclose()
                    await client.aclose()
                    return Response(status_code=502)
                response_headers["location"] = location
            async def content():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()
            return StreamingResponse(content(), status_code=upstream.status_code, headers=response_headers)
        except httpx.HTTPError:
            await client.aclose()
            return Response(status_code=502)

    return app
