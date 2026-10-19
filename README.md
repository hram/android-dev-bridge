# Android Dev Bridge

USB debug proxy for connecting a physical Android device to a local HTTP backend.
The proxy CLI uses only the Python 3 standard library. The optional stdio MCP
server uses the official MCP Python SDK and requires Python 3.11+. Android
Platform Tools (`adb`) are needed to connect a device.

## How it works

```text
Android application → device HTTP proxy → adb reverse
                    → proxy.py on the development machine → local backend
```

HTTP requests for the selected hostname are routed to `127.0.0.1:18080`.
Other HTTP requests are forwarded to their original servers. HTTPS CONNECT is
passed through as an encrypted tunnel: HTTPS requests are not decrypted or
redirected to the local backend.

Request and response bodies are streamed in blocks of up to 64 KiB, without
recording them. Local-request logs contain method, path without query parameters,
status, request Content-Length and whether Session-Id is present, never its value.

## Start

Start your backend on `127.0.0.1:18080`, then run:

```sh
python3 -u proxy.py --host api.example.test
```

Replace `api.example.test` with the HTTP hostname already used by your debug app.
The proxy listens only on `127.0.0.1:18888`. Optional flags:

```text
--proxy-port 18888
--backend-port 18080
--reject-path /api/upload
```

Verify routing before connecting the device, using a path your backend supports:

```sh
curl --noproxy '' -x http://127.0.0.1:18888 http://api.example.test/health
```

## Connect the device

Enable USB debugging, connect the device and approve the computer on its screen.
Find its serial number:

```sh
adb devices
```

Set the serial number for the commands below:

```sh
DEVICE_SERIAL=your-device-serial
```

Inspect and **save the original proxy setting** and existing reverse connections:

```sh
adb -s "$DEVICE_SERIAL" shell settings get global http_proxy
adb -s "$DEVICE_SERIAL" reverse --list
```

Connect using the default proxy port:

```sh
adb -s "$DEVICE_SERIAL" reverse --no-rebind tcp:18888 tcp:18888
adb -s "$DEVICE_SERIAL" shell settings put global http_proxy 127.0.0.1:18888
```

If reverse reports a conflict, inspect the existing mapping or select another
proxy port. Match that port in the proxy command, reverse and device setting.
Restart the application normally if its HTTP client has already been created.
Do not clear application data or authentication. The proxy setting is global:
other applications that honor it are affected too. Some clients bypass the system
proxy; this prototype does not force their traffic through it.

## Simulate a server error

Error injection is disabled by default. To return 503 for a particular path on
the selected hostname:

```sh
python3 -u proxy.py --host api.example.test --reject-path /api/upload
```

The entire request body is consumed before returning an empty 503 response.
Matching is by exact path, for all supported HTTP methods; query parameters do
not affect matching. Other paths are forwarded normally.

To resume forwarding without restarting the proxy, send SIGUSR1 to that specific
proxy process:

```sh
kill -USR1 <proxy-pid>
```

This signal-based control requires a system with SIGUSR1, such as Linux or macOS.
App retries and their backoff remain under application control.

## Disconnect

Restore the original proxy setting **before stopping the proxy**. If the original
value was `null`:

```sh
adb -s "$DEVICE_SERIAL" shell settings delete global http_proxy
```

Otherwise restore the exact value saved earlier:

```sh
adb -s "$DEVICE_SERIAL" shell settings put global http_proxy <previous-value>
```

Remove only the reverse mapping created for this proxy:

```sh
adb -s "$DEVICE_SERIAL" reverse --remove tcp:18888
```

Preserve unrelated reverse mappings, including any used for device internet
access. Stop the Python proxy and, if needed, restart the app so it forgets the
previous proxy setting.

## Scope

The CLI and MCP share `bridge.py`; neither changes Android settings. Device
setup, backend startup and restoration of the global proxy remain manual in this
version. No automatic recovery, HTTPS interception or application-specific
instrumentation helpers are included.

Each MCP process owns its own proxy. Tools do not attach to an existing CLI
process. Stop a CLI proxy before starting an MCP proxy on the same port. On normal
MCP shutdown or stdin closure, its proxy is stopped. Device settings and ADB
reverse mappings remain in place until you restore them manually; abrupt process
termination also requires manual device cleanup.

## MCP setup

Install the SDK in a virtual environment (the CLI still has no dependencies):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Alternatively, with uv:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Configure your MCP client with absolute paths to this checkout:

```json
{
  "mcpServers": {
    "android-dev-bridge": {
      "command": "/absolute/path/android-dev-bridge/.venv/bin/python",
      "args": ["/absolute/path/android-dev-bridge/mcp_server.py"]
    }
  }
}
```

The MCP transport uses stdio. Proxy events are retained in memory, not printed to
stdout. No backend or proxy is started during initialization or tools/list.
For `bridge_devices`, ensure `adb` is on the MCP process's PATH.

| Tool | Purpose |
| --- | --- |
| `bridge_devices` | List ADB device serials and authorization state. |
| `bridge_start` | Start a loopback proxy with `host`, optional `proxy_port`, `backend_port`, `reject_path`. |
| `bridge_status` | Get the current process's proxy state. |
| `bridge_set_fault` | Set `reject_path` to inject 503, or null to disable injection. Requires `session_id`. |
| `bridge_requests` | Read local HTTP metadata with sequence pagination (`after`, `limit`). |
| `bridge_stop` | Stop the proxy identified by `session_id`. Restore device proxy settings first. |

Responses contain JSON facts, without scenario planning or analysis. A typical
workflow is:

1. Call `bridge_start` with the application's HTTP hostname and local backend port.
2. Connect the chosen device manually using the ADB instructions above.
3. Exercise the app. Read `bridge_requests`; use `bridge_set_fault` when needed.
4. Restore the original device proxy and remove the created reverse mapping.
5. Call `bridge_stop` with the `session_id` returned by start.

The journal keeps the last 500 completed local requests in memory. `content_length`
is the declared request Content-Length (0 when absent), not a measured byte count.
`status: null` with `error` indicates a forwarding failure. HTTPS tunnels and
passthrough HTTP requests are not included. A new start clears the journal;
sequence numbers increase for the lifetime of the process. `truncated` marks a
cursor older than retained data. No bodies, query strings or header values are
returned. Faults match all supported HTTP methods at the exact selected path.

## Tests

After installing MCP requirements:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use a local HTTP backend and real stdio MCP transport. They do not change
Android settings or require a connected device. They cover HTTP body streaming,
chunked uploads, fault toggling, session identity, bounded request history,
CLI signals and MCP process-exit cleanup.
