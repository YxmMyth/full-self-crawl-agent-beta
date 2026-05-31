"""noVNC same-origin reverse proxy.

The display stack (scripts/server_display.sh) runs websockify on
127.0.0.1:6080 serving the noVNC web client + a WebSocket at /websockify that
bridges to x11vnc on :5900. We proxy ALL of it under Helmsman's own origin at
/vnc/* so:

  - the embedded iframe is same-origin (no CORS, no iframe sandbox needed), and
  - the operator forwards only ONE port (8000) over SSH — websockify's 6080
    never needs a separate tunnel and stays bound to 127.0.0.1.

Two paths:
  - HTTP  /vnc/{path}        → httpx stream to http://UPSTREAM/{path}
  - WS    /vnc/websockify    → full-duplex relay to ws://UPSTREAM/websockify

The relay pumps raw binary frames both directions; the VNC framebuffer rides
this WS continuously and is independent of the SSE firehose and any human-assist
channel.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from starlette.responses import Response

from src.utils.logging import get_logger
from web.config import WebConfig

logger = get_logger("helmsman.vnc")

# Hop-by-hop headers we must not forward (RFC 7230 §6.1) + length/encoding which
# httpx recomputes for us.
_DROP_REQ_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "accept-encoding",
}
_DROP_RESP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "content-encoding",
    "content-length",
}


class NoVncProxy:
    """Reverse proxy to the local websockify (noVNC) server."""

    def __init__(self, upstream: Optional[str] = None) -> None:
        self.upstream = upstream or WebConfig.NOVNC_UPSTREAM  # host:port
        self._client: Optional[httpx.AsyncClient] = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── HTTP assets ──────────────────────────────────────

    async def proxy_http(self, request, path: str) -> Response:
        """Proxy an HTTP GET for a noVNC static asset."""
        url = f"http://{self.upstream}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _DROP_REQ_HEADERS
        }
        client = await self._http_client()
        try:
            upstream = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.warning(f"noVNC HTTP proxy error for {path}: {e}")
            return Response(
                content=b"noVNC upstream unreachable. Is the display stack up? "
                b"(scripts/server_display.sh start)",
                status_code=502,
                media_type="text/plain",
            )
        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _DROP_RESP_HEADERS
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )

    # ── WebSocket relay ──────────────────────────────────

    async def relay_ws(self, client_ws: WebSocket) -> None:
        """Relay the noVNC framebuffer WebSocket to upstream websockify."""
        # noVNC negotiates a subprotocol ("binary" on older clients). Echo the
        # first requested one back so the handshake matches.
        requested = client_ws.headers.get("sec-websocket-protocol", "")
        subprotocols = [p.strip() for p in requested.split(",") if p.strip()]
        chosen = subprotocols[0] if subprotocols else None

        await client_ws.accept(subprotocol=chosen)

        upstream_url = f"ws://{self.upstream}/websockify"
        try:
            async with websockets.connect(
                upstream_url,
                subprotocols=subprotocols or None,
                max_size=None,  # framebuffer messages can be large
                ping_interval=None,  # let the VNC protocol manage liveness
                open_timeout=10,
            ) as upstream_ws:
                await self._pump(client_ws, upstream_ws)
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
            logger.warning(f"noVNC WS upstream connect failed: {e}")
            try:
                await client_ws.close(code=1011)
            except Exception:
                pass

    async def _pump(self, client_ws: WebSocket, upstream_ws) -> None:
        """Bidirectional frame pump until either side closes."""

        async def c2u() -> None:
            try:
                while True:
                    msg = await client_ws.receive()
                    t = msg.get("type")
                    if t == "websocket.disconnect":
                        break
                    if "bytes" in msg and msg["bytes"] is not None:
                        await upstream_ws.send(msg["bytes"])
                    elif "text" in msg and msg["text"] is not None:
                        await upstream_ws.send(msg["text"])
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                try:
                    await upstream_ws.close()
                except Exception:
                    pass

        async def u2c() -> None:
            try:
                async for message in upstream_ws:
                    if isinstance(message, (bytes, bytearray)):
                        await client_ws.send_bytes(bytes(message))
                    else:
                        await client_ws.send_text(message)
            except (websockets.WebSocketException, RuntimeError):
                pass
            finally:
                try:
                    await client_ws.close()
                except Exception:
                    pass

        await asyncio.gather(c2u(), u2c(), return_exceptions=True)
