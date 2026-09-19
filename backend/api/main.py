"""FastAPI app. Two routes for humans, one websocket, three route groups."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.api import admin, internal, public, ws  # noqa: E402
from backend.voice.adapter import get_voice  # noqa: E402

app = FastAPI(title="Business Saathi", version="1.0")
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
app.include_router(public.router)
app.include_router(internal.router)
app.include_router(admin.router)


def _valid_tunnel_secret(value: str | None) -> bool:
    return bool(value) and secrets.compare_digest(value, config.INTERNAL_SECRET)


def _access_cookie() -> str:
    return hmac.new(config.SITE_PASSWORD.encode(), b"saathi-demo-access",
                    hashlib.sha256).hexdigest()


def _valid_access_cookie(value: str | None) -> bool:
    return bool(value) and secrets.compare_digest(value, _access_cookie())


def _valid_basic_auth(value: str | None) -> bool:
    if not value or not value.startswith("Basic "):
        return False
    try:
        credentials = base64.b64decode(value[6:], validate=True).decode("utf-8")
        _, password = credentials.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return secrets.compare_digest(password, config.SITE_PASSWORD)


@app.middleware("http")
async def protect_tunnel_surface(request: Request, call_next):
    """Expose only secret-bearing n8n callbacks through cloudflared.

    The tunnel process overwrites Host with a private marker, so browser pages,
    merchant reads and admin endpoints are unavailable through its public URL.
    Localhost development remains unchanged.
    """
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    if (host == config.TUNNEL_HOST_HEADER
            and not _valid_tunnel_secret(request.headers.get("x-saathi-secret"))):
        return JSONResponse({"error": "tunnel_auth_required"}, status_code=403)
    if config.SITE_PASSWORD and host != config.TUNNEL_HOST_HEADER:
        path = request.url.path
        callback = (path.startswith("/api/internal/")
                    and _valid_tunnel_secret(request.headers.get("x-saathi-secret")))
        if path != "/api/health" and not callback:
            authenticated = _valid_access_cookie(request.cookies.get("saathi_access"))
            basic = _valid_basic_auth(request.headers.get("authorization"))
            if not authenticated and not basic:
                return JSONResponse(
                    {"error": "demo_auth_required"}, status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Business Saathi Demo"'})
            response = await call_next(request)
            if basic and not authenticated:
                response.set_cookie("saathi_access", _access_cookie(), httponly=True,
                                    secure=request.url.scheme == "https"
                                    or request.headers.get("x-forwarded-proto") == "https",
                                    samesite="lax")
            return response
    return await call_next(request)


@app.on_event("startup")
async def _startup():
    ws.set_loop(asyncio.get_running_loop())
    if not config.DB_PATH.exists():
        print(f"\n  !! No database at {config.DB_PATH}")
        print("  !! Run: python scripts/generate.py\n")
    else:
        for warning in config.config_warnings():
            print(f"\n  !! {warning}")
        print(f"\n  Business Saathi ready.  adapters: {config.adapters()}")
        print(f"  merchant  ->  http://127.0.0.1:{config.PORT}/")
        print(f"  ops       ->  http://127.0.0.1:{config.PORT}/ops\n")
        _warm_graph()


def _warm_graph() -> None:
    """Warm Cognee retrieval for the demo merchants, in the background.

    Retrieval never blocks an answer, so a cold cache costs the FIRST question
    its provenance note rather than its numbers. Warming here means even the
    first question of a demo shows real retrieval. Failure is silent by
    design: this is decoration for the evidence panel, not a dependency.
    """
    from backend.graph.store import get_store
    store = get_store()
    if getattr(store, "name", "") != "cognee":
        return
    import threading
    threading.Thread(
        target=lambda: store.warm([config.DEMO_MERCHANT, config.PEER_MERCHANT,
                                   config.COLDSTART_MERCHANT]),
        name="cognee-warm", daemon=True).start()


@app.websocket("/ws")
async def ws_endpoint(socket: WebSocket):
    host = socket.headers.get("host", "").split(":", 1)[0].lower()
    if host == config.TUNNEL_HOST_HEADER:
        await socket.close(code=1008, reason="WebSocket unavailable through callback tunnel")
        return
    if config.SITE_PASSWORD and not _valid_access_cookie(socket.cookies.get("saathi_access")):
        await socket.close(code=1008, reason="Demo access required")
        return
    await socket.accept()
    ws.register(socket)
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws.unregister(socket)


@app.get("/")
def merchant_page():
    return FileResponse(config.WEB_DIR / "merchant.html")


@app.get("/ops")
def ops_page():
    return FileResponse(config.WEB_DIR / "ops.html")


@app.post("/api/tts")
async def tts(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty_text"}, status_code=400)
    return await asyncio.to_thread(get_voice().speak, text)
