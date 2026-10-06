"""Authenticated Streamable HTTP transport for the existing MCP tools."""

import hmac
import os
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class SharedTokenMiddleware:
    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode('utf-8')

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            values = Headers(scope=scope).getlist('authorization')
            parts = values[0].split(' ', 1) if len(values) == 1 else []
            valid = (len(parts) == 2 and parts[0].lower() == 'bearer'
                     and hmac.compare_digest(parts[1].encode('utf-8'), self.token))
            if not valid:
                response = JSONResponse(
                    {'error': 'Unauthorized'}, status_code=401,
                    headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(mcp):
    """Fail closed without a token; retain explicit Host and Origin validation."""
    token = os.environ.get('MCP_AUTH_TOKEN', '')
    if len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError('Set MCP_AUTH_TOKEN to a token of at least 32 characters without whitespace.')
    public_url = os.environ.get('MCP_PUBLIC_URL', 'https://mcp.leadprogressai.app')
    parsed = urlparse(public_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('MCP_PUBLIC_URL must be an HTTPS origin, without a path.')
    # HTTP is local to the machine; the existing web server terminates HTTPS.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.streamable_http_path = '/mcp'
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[parsed.netloc, '127.0.0.1:*', 'localhost:*', '[::1]:*'],
        allowed_origins=[f'https://{parsed.netloc}', 'http://127.0.0.1:*',
                         'http://localhost:*', 'http://[::1]:*'],
    )
    app = mcp.streamable_http_app()
    app.add_middleware(SharedTokenMiddleware, token=token)
    return app


def run_http(mcp):
    import uvicorn

    app = create_http_app(mcp)
    port = int(os.environ.get('MCP_PORT', '8001'))
    if not 1 <= port <= 65535:
        raise ValueError('MCP_PORT must be between 1 and 65535.')
    uvicorn.run(app, host='127.0.0.1', port=port, workers=1,
                proxy_headers=False, access_log=False)
