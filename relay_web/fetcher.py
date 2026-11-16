"""SSRF-resistant HTTP(S) fetcher used by the Mac to retrieve approved content.

Design rules (all enforced here, never delegated to the caller):
* https only, no credentials in URLs, standard port only.
* The hostname is resolved once; every address must be globally routable.
  The TCP connection is then pinned to the validated address, so a DNS answer
  that changes between check and connect (DNS rebinding) cannot redirect us.
* Redirects are followed manually (max 4) and each hop is revalidated.
* Hard caps on response size and total wall-clock time; no cookies, no Referer,
  no proxy environment variables, identity encoding only (no decompression bombs).
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

USER_AGENT = 'RelayIsland/0.5 (personal self-hosted reader)'
REDIRECTS = {301, 302, 303, 307, 308}


class FetchError(Exception):
    """Failure with a stable machine code and a message safe to show the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Fetched:
    url: str
    status: int
    content_type: str
    body: bytes
    charset: str | None


def is_global(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        ip = info[4][0].split('%', 1)[0]
        if ip not in seen:
            seen.append(ip)
    return seen


def decode_body(body: bytes, charset: str | None) -> str:
    candidates = []
    if charset:
        candidates.append(charset)
    else:
        m = re.search(rb'<meta[^>]+charset=["\']?\s*([-\w]+)', body[:4096], re.I)
        if m:
            candidates.append(m.group(1).decode('ascii', 'ignore'))
    candidates.append('utf-8')
    for enc in candidates:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode('utf-8', 'replace')


class Fetcher:
    def __init__(self, *, allow_private: bool = False, allow_http: bool = False,
                 resolver=default_resolver, connector=None, timeout: float = 8.0,
                 deadline: float = 20.0, max_redirects: int = 4):
        # allow_private / allow_http exist for the automated tests ONLY. The CLI
        # never exposes them.
        self.allow_private = allow_private
        self.allow_http = allow_http
        self.resolver = resolver
        self.connector = connector
        self.timeout = timeout
        self.deadline = deadline
        self.max_redirects = max_redirects

    # -- validation ---------------------------------------------------------
    def validate(self, url: str):
        """Return (scheme, host, port, path) or raise FetchError."""
        if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 for c in url):
            raise FetchError('bad_url', 'That address is not valid.')
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme != 'https' and not (scheme == 'http' and self.allow_http):
            raise FetchError('bad_url', 'Only secure https:// addresses are supported.')
        if parts.username or parts.password:
            raise FetchError('bad_url', 'Addresses with embedded credentials are not allowed.')
        host = parts.hostname
        if not host:
            raise FetchError('bad_url', 'That address has no website name.')
        try:
            port = parts.port
        except ValueError as exc:
            raise FetchError('bad_url', 'That address has an invalid port.') from exc
        default = 443 if scheme == 'https' else 80
        if port is None:
            port = default
        elif port != default and not self.allow_private:
            raise FetchError('bad_url', 'Only the standard web port is allowed.')
        if not self.allow_private and (host == 'localhost' or host.endswith(('.local', '.internal', '.localhost', '.lan', '.home.arpa'))):
            raise FetchError('blocked_address', 'Local network addresses are blocked.')
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        return scheme, host, port, path

    def resolve_safe(self, host: str, port: int) -> str:
        try:
            ips = self.resolver(host, port)
        except OSError as exc:
            raise FetchError('dns', f'Could not find “{host}”. Check the address and the Mac’s internet.') from exc
        if not ips:
            raise FetchError('dns', f'Could not find “{host}”.')
        if not self.allow_private:
            if any(not is_global(ip) for ip in ips):
                # One non-global answer poisons the whole name (rebinding defence).
                raise FetchError('blocked_address', 'That address points to a private or local network and was blocked.')
        return ips[0]

    # -- connection ---------------------------------------------------------
    def _connect(self, scheme, host, ip, port, remaining):
        timeout = max(0.5, min(self.timeout, remaining))
        try:
            if self.connector:
                sock = self.connector(ip, port, timeout)
            else:
                sock = socket.create_connection((ip, port), timeout=timeout)
        except OSError as exc:
            raise FetchError('network', 'Could not connect to the website.') from exc
        if scheme == 'https':
            ctx = ssl.create_default_context()
            try:
                sock = ctx.wrap_socket(sock, server_hostname=host)
            except ssl.SSLError as exc:
                sock.close()
                raise FetchError('tls', 'The website’s security certificate could not be verified.') from exc
            except OSError as exc:
                sock.close()
                raise FetchError('network', 'Secure connection failed.') from exc
        return sock

    def fetch(self, url: str, *, max_bytes: int, accept: str, allowed_types: tuple[str, ...],
              deadline: float | None = None, extra_headers: dict[str, str] | None = None) -> Fetched:
        start = time.monotonic()
        limit = deadline or self.deadline
        current = url
        original_host = urlsplit(url).hostname
        for _ in range(self.max_redirects + 1):
            scheme, host, port, path = self.validate(current)
            if extra_headers and host != original_host:
                raise FetchError('redirect_blocked', 'Search provider redirected away from its API host.')
            ip = self.resolve_safe(host, port)
            remaining = limit - (time.monotonic() - start)
            if remaining <= 0:
                raise FetchError('timeout', 'The website took too long to respond.')
            sock = self._connect(scheme, host, ip, port, remaining)
            try:
                conn = http.client.HTTPConnection(host, port)  # never auto-connects: we hand it our pinned socket
                conn.sock = sock
                host_header = host if port in (80, 443) else f'{host}:{port}'
                conn.putrequest('GET', path, skip_host=True, skip_accept_encoding=True)
                for k, v in (('Host', host_header), ('User-Agent', USER_AGENT), ('Accept', accept),
                             ('Accept-Language', 'en'), ('Accept-Encoding', 'identity'), ('Connection', 'close')):
                    conn.putheader(k, v)
                if extra_headers:
                    for k, v in extra_headers.items():
                        if k not in ('X-Subscription-Token',) or not isinstance(v, str) or not v or any(c in v for c in '\r\n'):
                            raise FetchError('bad_header', 'Invalid provider authentication.')
                        conn.putheader(k, v)
                conn.endheaders()
                sock.settimeout(max(0.5, min(self.timeout, limit - (time.monotonic() - start))))
                resp = conn.getresponse()
                if resp.status in REDIRECTS:
                    loc = resp.getheader('Location')
                    resp.close()
                    if not loc:
                        raise FetchError('http_error', 'The website sent an invalid redirect.')
                    current = urljoin(current, loc)
                    continue
                if not 200 <= resp.status < 300:
                    raise FetchError('http_error', f'The website answered with HTTP {resp.status}.')
                ctype = resp.getheader('Content-Type', '')
                main = ctype.split(';', 1)[0].strip().lower()
                if allowed_types and main not in allowed_types:
                    raise FetchError('bad_type', f'Unsupported content type “{main or "unknown"}”.')
                length = resp.getheader('Content-Length')
                if length and length.isdecimal() and int(length) > max_bytes:
                    raise FetchError('too_large', 'The page is too large.')
                chunks, total = [], 0
                while True:
                    left = limit - (time.monotonic() - start)
                    if left <= 0:
                        raise FetchError('timeout', 'The website took too long to respond.')
                    sock.settimeout(max(0.5, min(self.timeout, left)))
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError('too_large', 'The page is too large.')
                    chunks.append(chunk)
                m = re.search(r'charset=["\']?([-\w]+)', ctype, re.I)
                return Fetched(current, resp.status, main, b''.join(chunks), m.group(1) if m else None)
            except FetchError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                raise FetchError('timeout', 'The website took too long to respond.') from exc
            except (http.client.HTTPException, OSError) as exc:
                raise FetchError('network', 'The connection to the website failed.') from exc
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
        raise FetchError('redirects', 'The website redirected too many times.')