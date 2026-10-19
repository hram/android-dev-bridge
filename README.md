# Android Dev Bridge

USB debug proxy for connecting a physical Android device to a local HTTP backend.
An initial prototype using Python 3 standard library and Android Platform Tools
(`adb`). No Python packages need to be installed.

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

This is a manual debugging prototype. Device setup, backend startup and cleanup
are manual. It does not provide MCP, automatic recovery, HTTPS interception or
application-specific instrumentation helpers.
