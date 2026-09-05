#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from garmin_mcp.server import mcp

DUCKDNS_HOST = "aroncsikos.duckdns.org"
HOST = "127.0.0.1"
PORT = 8765
ALLOWED_HOSTS = [DUCKDNS_HOST, f"{DUCKDNS_HOST}:443", "127.0.0.1", f"127.0.0.1:{PORT}", "localhost", f"localhost:{PORT}"]
ALLOWED_ORIGINS = [f"https://{DUCKDNS_HOST}"]

if __name__ == "__main__":
    print(f"Starting Garmin MCP HTTP server on {HOST}:{PORT}, public via https://{DUCKDNS_HOST}/mcp")
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.run(
            transport="streamable-http",
            host=HOST,
            port=PORT,
            transport_security=TransportSecuritySettings(
                allowed_hosts=ALLOWED_HOSTS,
                allowed_origins=ALLOWED_ORIGINS,
            ),
        )
    except TypeError as e:
        print(f"v2-style run() failed ({e}), falling back to v1 .settings API")
        mcp.settings.host = HOST
        mcp.settings.port = PORT
        try:
            mcp.settings.transport_security.allowed_hosts = ALLOWED_HOSTS
            mcp.settings.transport_security.allowed_origins = ALLOWED_ORIGINS
        except AttributeError:
            pass
        mcp.run(transport="streamable-http")
