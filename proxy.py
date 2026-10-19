"""Streaming USB debug proxy. No bodies or credentials are logged/saved."""
import argparse
import json
import signal
import sys
import threading
from bridge import Bridge, BridgeError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='HTTP hostname to route to the local backend')
    parser.add_argument('--proxy-port', type=int, default=18888)
    parser.add_argument('--backend-port', type=int, default=18080)
    parser.add_argument('--reject-path', help='Return 503 for this exact path until SIGUSR1')
    args = parser.parse_args()
    bridge = Bridge(on_event=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
    try:
        state = bridge.start(args.host, args.proxy_port, args.backend_port, args.reject_path)
    except BridgeError as error:
        parser.error(str(error))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if hasattr(signal, 'SIGUSR1'):
        signal.signal(signal.SIGUSR1, lambda *_: bridge.set_fault(state['session_id']))
    print(json.dumps(state), file=sys.stderr, flush=True)
    try:
        stop.wait()
    finally:
        bridge.stop(state['session_id'])


if __name__ == '__main__':
    main()
