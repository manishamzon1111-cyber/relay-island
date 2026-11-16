"""macOS-only preflight and launch guard for a physically isolated LAN.

This never alters system routing/firewall settings, and cannot guarantee that an
untrusted tablet has no alternative route. Requires an isolated AP with NO WAN.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import platform
import re
import subprocess
import sys


class IsolationError(RuntimeError):
    pass


def command(*args):
    r = subprocess.run(args, text=True, capture_output=True, timeout=8, check=False)
    if r.returncode:
        raise IsolationError(f'{args[0]} failed: {r.stderr.strip()}')
    return r.stdout.strip()


def parse_forwarding(value):
    match = re.search(r'(?:=|:)\s*([01])\s*$', value)
    if not match:
        raise IsolationError('Cannot read kernel forwarding setting')
    return int(match.group(1))


def validate_host(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise IsolationError('A numerical IPv4 address is required') from exc
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise IsolationError('Host must be a dedicated private LAN IPv4 address')
    return host


def check(host, interface, *, platform_name=None, run=command):
    if (platform_name or platform.system()) != 'Darwin':
        raise IsolationError('Network preflight must run on the actual macOS host')
    validate_host(host)
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9]{0,14}', interface):
        raise IsolationError('Invalid interface name')
    ipv4 = run('ipconfig', 'getifaddr', interface).strip()
    if ipv4 != host:
        raise IsolationError(f'{interface} address {ipv4!r} differs from configured {host!r}')
    ipv4_forwarding = parse_forwarding(run('sysctl', 'net.inet.ip.forwarding'))
    ipv6_forwarding = parse_forwarding(run('sysctl', 'net.inet6.ip6.forwarding'))
    if ipv4_forwarding or ipv6_forwarding:
        raise IsolationError('Packet forwarding enabled: stop Internet Sharing/VPN routing and rerun')
    # An interface with its own default router is not considered isolated.
    routes = run('netstat', '-rn', '-f', 'inet')
    # Interface may have a gateway without being a default route; flag an upstream default only.
    for line in routes.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == 'default' and parts[-1] == interface:
            raise IsolationError(f'{interface} has a default route; verify offline AP configuration')
    return {
        'bind_ip': host,
        'interface': interface,
        'ipv4_forwarding': ipv4_forwarding,
        'ipv6_forwarding': ipv6_forwarding,
        'checks': 'macOS forwarding and interface only',
        'not_verified': [
            'AP/Wi-Fi is physically isolated from the internet',
            'tablet has no alternate Wi-Fi or radio route',
            'other Mac services are inaccessible from the LAN',
            'no unexpected forwarding through third-party software',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='RELAY Island Mac preflight; not an isolation firewall')
    parser.add_argument('action', choices=['check', 'serve'])
    parser.add_argument('--interface', required=True, help='Dedicated LAN interface (e.g. en7), not the internet interface')
    parser.add_argument('--host', required=True, help='IPv4 address already configured on that interface')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--media', default='./media')
    args = parser.parse_args(argv)
    try:
        result = check(args.host, args.interface)
        print(json.dumps(result, indent=2), flush=True)
        print('WARNING: PASSED preflight is NOT proof of isolation. Use an AP with no WAN uplink.', flush=True)
        if args.action == 'serve':
            # Recheck immediately before serving; guard is not an ongoing enforcement mechanism.
            check(args.host, args.interface)
            from relay_web.server import make_server
            import threading
            server, state = make_server(args.host, args.port, args.media)
            try:
                admin, _ = make_server('127.0.0.1', args.port, args.media, state=state)
            except Exception:
                server.server_close()
                raise
            worker = threading.Thread(target=admin.serve_forever, daemon=True)
            worker.start()
            print(f'Tablet: http://{args.host}:{args.port}/', flush=True)
            print(f'Mac approvals: http://127.0.0.1:{args.port}/admin', flush=True)
            stop = __import__('threading').Event()
            def watchdog():
                while not stop.wait(3):
                    try:
                        check(args.host, args.interface)
                    except Exception as exc:
                        print(f'ISOLATION PRECHECK FAILED: {exc}; stopping web service', file=sys.stderr, flush=True)
                        server.shutdown()
                        return
            watcher = threading.Thread(target=watchdog, daemon=True)
            watcher.start()
            try:
                server.serve_forever()
            finally:
                stop.set()

                server.server_close()
                admin.shutdown()
                admin.server_close()
    except (IsolationError, OSError, subprocess.TimeoutExpired) as exc:
        print('REFUSED: '+str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

class LanGuard:
    """Read-only, periodically checked preflight; NOT a firewall."""
    def __init__(self, host=None, interface=None, *, enabled=True, interval=3):
        import threading
        self.host,self.interface,self.enabled,self.interval=host,interface,enabled,interval
        self.ok=not enabled
        self.reason='Not checked' if enabled else ''
        self._stop=threading.Event()
        self._thread=None
    def allowed(self):
        return self.ok
    def status(self):
        return {'enabled':self.enabled,'ok':self.ok,'reason':self.reason,'interface':self.interface}
    def tick(self):
        try:
            check(self.host,self.interface)
            self.ok,self.reason=True,''
        except Exception as exc:
            self.ok,self.reason=False,str(exc)
    def start(self):
        if not self.enabled:return
        self.tick()
        import threading
        self._thread=threading.Thread(target=self._loop,daemon=True)
        self._thread.start()
    def _loop(self):
        while not self._stop.wait(self.interval):self.tick()
    def stop(self):self._stop.set()
