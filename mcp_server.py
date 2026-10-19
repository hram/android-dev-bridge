"""Stdio MCP adapter for the shared Android Dev Bridge runtime."""
from contextlib import asynccontextmanager
import subprocess
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from bridge import Bridge

bridge = Bridge()


@asynccontextmanager
async def lifespan(server):
    try:
        yield {}
    finally:
        state = bridge.status()
        if state['running']:
            bridge.stop(state['session_id'])


mcp = FastMCP('android-dev-bridge', lifespan=lifespan)
read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
mutating = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


@mcp.tool(annotations=read_only)
def bridge_devices() -> dict:
    """List ADB devices and authorization state. Does not modify device settings."""
    try:
        result = subprocess.run(['adb', 'devices', '-l'], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise ValueError('adb is not installed or is not on PATH') from None
    except subprocess.TimeoutExpired:
        raise ValueError('adb devices timed out') from None
    if result.returncode:
        raise ValueError('adb devices failed; check ADB on the host')
    devices = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(('List of devices', '*')):
            devices.append({'serial': parts[0], 'state': parts[1]})
    return {'devices': devices}


@mcp.tool(annotations=mutating)
def bridge_start(host: str, proxy_port: int = 18888, backend_port: int = 18080,
                 reject_path: str | None = None) -> dict:
    """Start a loopback HTTP proxy. Route host to local backend; other HTTP/HTTPS tunnels pass through.
    No ADB reverse or device settings are changed: connect the device manually as in README.
    Optional reject_path injects 503 at an exact path on host, ignoring query, for all methods.
    Returns session_id required by fault/stop. One proxy per MCP process; HTTPS is not redirected.
    """
    return bridge.start(host, proxy_port, backend_port, reject_path)


@mcp.tool(annotations=read_only)
def bridge_status() -> dict:
    """Get this MCP process's proxy state. Does not inspect or control other proxy processes."""
    return bridge.status()


@mcp.tool(annotations=mutating)
def bridge_set_fault(session_id: str, reject_path: str | None = None) -> dict:
    """Inject 503 for an exact path on the selected host; null disables errors immediately.
    Does not cancel application background work, change retry schedules or clear data.
    """
    return bridge.set_fault(session_id, reject_path)


@mcp.tool(annotations=read_only)
def bridge_requests(after: int = 0, limit: int = 50) -> dict:
    """Read completed local HTTP request metadata, oldest first, from a 500-event memory buffer.
    No bodies, query strings, header values, or HTTPS contents. after is a sequence cursor.
    truncated indicates events were lost; next_after continues paging. No traffic interpretation.
    """
    return bridge.requests(after, limit)


@mcp.tool(annotations=mutating)
def bridge_stop(session_id: str) -> dict:
    """Stop this proxy and close device proxy connections. Restore device settings manually FIRST.
    Does not remove ADB reverse or restore global http_proxy: device setup is manual in this version.
    """
    return bridge.stop(session_id)


if __name__ == '__main__':
    mcp.run(transport='stdio')
