"""HTTP layer. Two listeners share one Store:

* LAN listener (tablet):   public API, media, static files. Never serves /admin.
* Loopback listener (Mac): everything above plus the approval console.

Admin access needs ALL of: the loopback listener, a Host header of 127.0.0.1 or
localhost, the session cookie obtained from the one-time URL printed in the
Terminal, and (for writes) the custom header + exact same-origin check.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .isolation import LanGuard
from .media import Library, parse_range
from .store import ID_RE, RequestError, Store

LOG = logging.getLogger('relay_island')
SECURITY = logging.getLogger('relay_island.security')
WEB = Path(__file__).parent / 'web'
MAX_BODY = 2048
STATIC_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,40}')
STATIC_TYPES = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                '.svg': 'image/svg+xml', '.html': 'text/html; charset=utf-8', '.png': 'image/png'}
ADMIN_ONLY_STATIC = {'admin.js', 'admin.css'}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
DEFAULT_SELFTEST = [
    {'id': 'v4', 'label': 'Internet by IPv4 address', 'url': 'http://1.1.1.1/'},
    {'id': 'v6', 'label': 'Internet by IPv6 address', 'url': 'http://[2606:4700:4700::1111]/'},
    {'id': 'dns', 'label': 'Internet by name (DNS)', 'url': 'http://connectivitycheck.gstatic.com/generate_204'},
]


class App:
    def __init__(self, store: Store, library: Library, *, guard: LanGuard | None = None, lan_host: str | None = None,
                 port: int = 0, selftest=None):
        self.store = store
        self.library = library
        self.guard = guard or LanGuard(enabled=False)
        self.lan_host = lan_host
        self.port = port
        self.admin_key = secrets.token_urlsafe(24)
        self.selftest = selftest if selftest is not None else DEFAULT_SELFTEST
        self.started = time.time()
        self.listeners: list[str] = []

    @staticmethod
    def features() -> dict:
        try:
            import trafilatura  # noqa: F401
            articles_generic = True
        except ImportError:
            articles_generic = False
        try:
            import lxml  # noqa: F401
            articles_wiki = True
        except ImportError:
            articles_wiki = False
        return {'articles_wikipedia': articles_wiki, 'articles_generic': articles_generic}

    def status(self) -> dict:
        s = self.store
        return {'version': __version__, 'now': time.time(),
                'providers': [{'id': p.id, 'label': p.label, 'host': p.host, 'experimental': p.experimental, 'note': p.note}
                              for p in s.providers.values()],
                'features': {**self.features(), 'images': s.settings['images'], 'probe': bool(self.library.ffprobe)},
                'admin_active': time.time() - s.admin_seen < 45, 'paused': s.settings['paused']}

    def admin_state(self) -> dict:
        s = self.store
        now = time.time()
        with s.lock:
            clients = [{'ip': ip, 'seen': now - t} for ip, t in s.clients.items() if ip not in ('127.0.0.1', '::1')]
            settings = dict(s.settings)
        return {'seq': s.seq, 'now': now, 'requests': s.admin_list(), 'settings': settings,
                'status': {**self.status(), 'listeners': self.listeners, 'guard': self.guard.status(),
                           'media_root': str(self.library.root), 'media_count': len(self.library.listing()),
                           'data_dir': str(s.data) if s.persist else None, 'clients': sorted(clients, key=lambda c: c['seen'])}}


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = True

    def __init__(self, addr, handler, app: App, admin: bool, max_connections: int = 64):
        self.app, self.admin = app, admin
        self._sem = threading.BoundedSemaphore(max_connections)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nRetry-After: 2\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        LOG.exception('Unhandled error serving %s', client_address[0])


def make_handler(app: App):
    store, library = app.store, app.library

    class Handler(BaseHTTPRequestHandler):
        server_version = 'RelayIsland'
        sys_version = ''
        protocol_version = 'HTTP/1.0'
        timeout = 15

        # ------------------------------------------------------- plumbing
        def log_message(self, fmt, *args):
            if self.path.startswith(('/static/', '/api/events', '/admin/api/events')):
                return
            LOG.info('%s %s', self.client_address[0], (fmt % args).replace('\r', ' ').replace('\n', ' ')[:200])

        def base_headers(self, csp=CSP):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()')
            self.send_header('Content-Security-Policy', csp)

        def respond(self, status, body=b'', mime='text/html; charset=utf-8', headers=None, cache='no-store', csp=CSP):
            raw = body.encode('utf-8') if isinstance(body, str) else body
            self.send_response(status)
            self.base_headers(csp)
            self.send_header('Cache-Control', cache)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD' and status not in (204, 304):
                self.wfile.write(raw)

        def jsend(self, status, obj, headers=None):
            self.respond(status, json.dumps(obj, separators=(',', ':')), 'application/json; charset=utf-8', headers)

        def jerror(self, status, code, message, headers=None):
            self.jsend(status, {'error': {'code': code, 'message': message}}, headers)

        def send_error(self, code, message=None, explain=None):  # noqa: A003 - override: keep security headers on every error
            self.jerror(code, 'http_%d' % code, message or 'Request failed')

        def redirect(self, location, headers=None):
            h = {'Location': location, **(headers or {})}
            self.respond(303, b'', headers=h)

        def not_found(self):
            if 'text/html' in self.headers.get('Accept', '') and not self.path.startswith(('/api', '/admin/api')):
                self.respond(404, page_message('Page not found', 'That page does not exist.', '/', 'Go home'))
            else:
                self.jerror(404, 'not_found', 'Not found.')

        # -------------------------------------------------------- security
        @property
        def is_admin_listener(self):
            return self.server.admin

        def host_ok(self):
            host = self.headers.get('Host', '')
            port = str(self.server.server_port)
            if self.is_admin_listener:
                return host in (f'127.0.0.1:{port}', f'localhost:{port}')
            allowed = {f'{app.lan_host}:{port}'} if app.lan_host else set()
            if app.lan_host and port == '80':
                allowed.add(app.lan_host)
            return host in allowed or not app.lan_host

        def admin_check(self, write=False):
            """Return None if authorised, else (status, code, message)."""
            ip = self.client_address[0]
            if not self.is_admin_listener:
                return 404, 'not_found', 'Not found.'
            if not self.host_ok():
                SECURITY.warning('admin: bad Host %r from %s', self.headers.get('Host'), ip)
                return 403, 'bad_host', 'Open the console at http://127.0.0.1:%d/admin (the address must be 127.0.0.1).' % self.server.server_port
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get('Cookie', ''))
            except Exception:  # noqa: BLE001 - malformed cookie header
                jar = SimpleCookie()
            morsel = jar.get(f'relay_admin_{self.server.server_port}')
            if not morsel or not secrets.compare_digest(morsel.value, app.admin_key):
                SECURITY.warning('admin: missing/invalid session from %s', ip)
                return 401, 'no_session', ('This page was opened before the server restarted, or without the Terminal link. '
                                           'Open the newest link printed in the Terminal.')
            if write:
                host = self.headers.get('Host', '')
                origin = self.headers.get('Origin')
                if self.headers.get('X-Relay-Admin') != '1':
                    return 403, 'missing_header', 'Request was not sent by the console page.'
                if origin is not None and origin != 'http://' + host:
                    SECURITY.warning('admin: origin mismatch %r from %s', origin, ip)
                    return 403, 'bad_origin', 'Blocked: the request came from a different website.'
                if self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none'):
                    SECURITY.warning('admin: cross-site fetch from %s', ip)
                    return 403, 'bad_origin', 'Blocked: the request came from a different website.'
            return None

        def read_json(self):
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                raise RequestError(415, 'bad_type', 'Expected JSON.')
            length = self.headers.get('Content-Length', '')
            if not length.isdecimal() or int(length) > MAX_BODY:
                raise RequestError(413, 'too_large', 'Request body is missing or too large.')
            try:
                data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as exc:
                raise RequestError(400, 'bad_json', 'Invalid JSON.') from exc
            if not isinstance(data, dict):
                raise RequestError(400, 'bad_json', 'Expected a JSON object.')
            return data

        # --------------------------------------------------------- routing
        def do_HEAD(self):
            self.dispatch('GET')

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')

        def do_PUT(self):
            self.jerror(405, 'method', 'Method not allowed.', {'Allow': 'GET, HEAD, POST'})
        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

        def dispatch(self, method):
            try:
                target = urlsplit(self.path)
                path, query = target.path, parse_qs(target.query, max_num_fields=8)
                if not self.is_admin_listener:
                    if not app.guard.allowed():
                        return self.jerror(503, 'network_guard', 'Paused: the Mac’s network safety check is failing.',
                                           {'Retry-After': '5'})
                    if not self.host_ok():
                        return self.jerror(421, 'bad_host', 'Use the address shown on the Mac.')
                    if path.startswith('/api/'):
                        store.touch_client(self.client_address[0])
                if path.startswith('/admin'):
                    return self.admin_route(method, path, query)
                if method == 'GET':
                    return self.get(path, query)
                return self.post(path)
            except RequestError as exc:
                self.jerror(exc.status, exc.code, exc.message)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:  # noqa: BLE001
                LOG.exception('Handler error on %s', self.path.split('?')[0])
                try:
                    self.jerror(500, 'internal', 'Internal error on the Mac. See its Terminal log.')
                except OSError:
                    pass

        # ------------------------------------------------------------- GET
        def get(self, path, query):
            if path in ('/', '/index.html'):
                return self.serve_file('index.html')
            if path.startswith('/static/'):
                name = path[8:]
                if not STATIC_NAME.fullmatch(name) or (name in ADMIN_ONLY_STATIC and not self.is_admin_listener):
                    return self.not_found()
                return self.serve_file(name)
            if path == '/selftest':
                return self.selftest_page()
            if path == '/api/status':
                return self.jsend(200, app.status())
            if path == '/api/selftest':
                return self.jsend(200, {'targets': app.selftest})
            if path == '/api/events':
                return self.jsend(200, self.events(query, admin=False))
            if path == '/api/library':
                return self.jsend(200, {'files': library.listing()})
            m = re.fullmatch(r'/api/requests/([0-9a-f]{16})', path)
            if m:
                view = store.public_detail(m.group(1))
                return self.jsend(200, view) if view else self.jerror(404, 'unknown', 'That request no longer exists.')
            m = re.fullmatch(r'/api/article/([0-9a-f]{16})', path)
            if m:
                art = store.public_article(m.group(1))
                return self.jsend(200, art) if art else self.jerror(404, 'gone', 'This article is no longer stored on the Mac.')
            m = re.fullmatch(r'/api/img/([0-9a-f]{16})/(\d{1,2})', path)
            if m:
                img = store.load_image(m.group(1), int(m.group(2)))
                if not img:
                    return self.not_found()
                return self.respond(200, img[1], img[0], cache='private, max-age=3600')
            m = re.fullmatch(r'/media/([0-9a-f]{12})', path)
            if m:
                return self.serve_media(m.group(1))
            m = re.fullmatch(r'/media/([0-9a-f]{12})/sub/(\d{1,2})\.vtt', path)
            if m:
                return self.serve_subtitle(m.group(1), int(m.group(2)))
            return self.not_found()

        def events(self, query, admin):
            try:
                after = int(query.get('after', ['0'])[0])
                wait = min(25.0, max(0.0, float(query.get('wait', ['0'])[0])))
            except ValueError as exc:
                raise RequestError(400, 'bad_query', 'Invalid parameters.') from exc
            if admin:
                store.touch_admin()
            store.expire()
            store.wait(after, wait)
            if admin:
                store.touch_admin()
                return app.admin_state()
            data = app.status()
            data.update(seq=store.seq, requests=store.public_list())
            return data

        def serve_file(self, name):
            path = WEB / name
            if not path.is_file():
                return self.not_found()
            data = path.read_bytes()
            etag = '"%s"' % hashlib.sha1(data).hexdigest()[:16]  # noqa: S324 - cache validator only
            if self.headers.get('If-None-Match') == etag:
                return self.respond(304, b'', headers={'ETag': etag}, cache='no-cache')
            self.respond(200, data, STATIC_TYPES.get(path.suffix, 'application/octet-stream'),
                         headers={'ETag': etag}, cache='no-cache')

        def selftest_page(self):
            origins = sorted({'%s://%s' % (urlsplit(t['url']).scheme, urlsplit(t['url']).netloc) for t in app.selftest})
            csp = CSP.replace("connect-src 'self'", "connect-src 'self' " + ' '.join(origins))
            self.respond(200, (WEB / 'selftest.html').read_bytes(), csp=csp)

        # ----------------------------------------------------------- media
        def serve_media(self, ident):
            entry = library.get(ident)
            if not entry:
                return self.not_found()
            try:
                fd, size = library.open(entry)
            except OSError:
                return self.jerror(404, 'gone', 'That file is no longer available.')
            try:
                rng = parse_range(self.headers.get('Range'), size)
                if rng is False:
                    return self.respond(416, b'', 'text/plain', {'Content-Range': f'bytes */{size}'})
                start, end = rng if rng else (0, size - 1)
                partial = rng is not None
                self.send_response(206 if partial else 200)
                self.base_headers()
                self.send_header('Cache-Control', 'private, no-cache')
                self.send_header('ETag', f'W/"{size}-{int(entry.mtime)}"')
                self.send_header('Content-Type', entry.mime)
                self.send_header('Accept-Ranges', 'bytes')
                if partial:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                length = max(0, end - start + 1) if size else 0
                self.send_header('Content-Length', str(length))
                self.end_headers()
                if self.command == 'HEAD' or not length:
                    return
                self.connection.settimeout(300)  # a paused player may idle for minutes
                os.lseek(fd, start, os.SEEK_SET)
                remaining = length
                while remaining > 0:
                    chunk = os.read(fd, min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(fd)

        def serve_subtitle(self, ident, n):
            entry = library.get(ident)
            if not entry:
                return self.not_found()
            try:
                text = library.subtitle_vtt(entry, n)
            except OSError:
                text = None
            if text is None:
                return self.not_found()
            self.respond(200, text, 'text/vtt; charset=utf-8', cache='private, no-cache')

        # ------------------------------------------------------------ POST
        def post(self, path):
            if path == '/api/requests':
                data = self.read_json()
                kind = data.pop('kind', None)
                req, created = store.submit(kind, self.client_address[0], data)
                return self.jsend(201 if created else 200, store.public_detail(req['id']))
            m = re.fullmatch(r'/api/requests/([0-9a-f]{16})/cancel', path)
            if m:
                self.read_json()
                store.cancel(m.group(1))
                return self.jsend(200, store.public_detail(m.group(1)))
            return self.not_found()

        # ----------------------------------------------------------- admin
        def admin_route(self, method, path, query):
            if path == '/admin/enter' and method == 'GET':
                if not self.is_admin_listener or not self.host_ok():
                    return self.not_found()
                key = query.get('key', [''])[0]
                if not secrets.compare_digest(key, app.admin_key):
                    SECURITY.warning('admin: wrong key presented from %s', self.client_address[0])
                    return self.respond(403, page_message('Link out of date', 'This link is from an earlier run. Use the newest link printed in the Terminal.'))
                cookie = f'relay_admin_{self.server.server_port}={app.admin_key}; Path=/admin; HttpOnly; SameSite=Strict'
                return self.redirect('/admin', {'Set-Cookie': cookie})
            if path == '/admin' and method == 'GET':
                if not self.is_admin_listener:
                    return self.not_found()
                bad = self.admin_check()
                if bad and bad[1] == 'no_session':
                    return self.respond(401, page_message('Approval console locked',
                                                          'Open the link printed in the Terminal where RELAY Island is running. '
                                                          'It signs this browser in.'))
                if bad:
                    return self.respond(bad[0], page_message('Not available', bad[2]))
                return self.serve_file('admin.html')
            if not path.startswith('/admin/api/'):
                return self.not_found()
            bad = self.admin_check(write=(method == 'POST'))
            if bad:
                return self.jerror(*bad)
            if method == 'GET' and path == '/admin/api/events':
                return self.jsend(200, self.events(query, admin=True))
            if method == 'GET' and path == '/admin/api/state':
                store.touch_admin()
                return self.jsend(200, app.admin_state())
            if method == 'POST':
                data = self.read_json()
                if path == '/admin/api/decide':
                    ident, decision = data.get('id'), data.get('decision')
                    if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or decision not in ('approve', 'reject'):
                        raise RequestError(400, 'bad_request', 'Invalid decision.')
                    store.decide(ident, decision == 'approve')
                    return self.jsend(200, {'ok': True})
                if path == '/admin/api/settings':
                    for key, value in data.items():
                        store.set_setting(key, value)
                    return self.jsend(200, {'ok': True})
                if path == '/admin/api/clear':
                    return self.jsend(200, {'ok': True, 'removed': store.clear_history()})
            return self.not_found()

    return Handler


def page_message(title: str, message: str, href: str | None = None, label: str = '') -> str:
    from html import escape
    link = f'<p><a class="btn" href="{escape(href)}">{escape(label)}</a></p>' if href else ''
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><link rel="stylesheet" href="/static/ui.css"></head>'
            f'<body class="plain"><main class="message"><h1>{escape(title)}</h1><p>{escape(message)}</p>{link}</main></body></html>')


def make_server(host: str, port: int, app: App, admin: bool) -> LimitedServer:
    return LimitedServer((host, port), make_handler(app), app, admin)