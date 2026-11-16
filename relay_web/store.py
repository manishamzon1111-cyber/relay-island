"""Request queue, approval state machine, background worker and persistence.

Life cycle:  pending -> running -> ready | failed
             pending -> rejected | cancelled | expired

Nothing touches the network until a request leaves `pending`. Even DNS lookups
wait for approval, so an unapproved request leaks nothing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from .articles import ArticleError, download_images, fetch_article
from .fetcher import FetchError, Fetcher

LOG = logging.getLogger('relay_island')
AUDIT = logging.getLogger('relay_island.audit')

MAX_QUERY = 120
MAX_PENDING = 20
MAX_KEEP = 150
PENDING_TTL = 30 * 60
TERMINAL = {'ready', 'failed', 'rejected', 'cancelled', 'expired'}
DEFAULT_SETTINGS = {'auto_wiki_articles': False, 'auto_wiki_search': False, 'images': True, 'paused': False}
ID_RE = re.compile(r'[0-9a-f]{16}')


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Bucket:
    """Token bucket: `capacity` burst, one token every `period` seconds."""

    def __init__(self, capacity: int, period: float):
        self.capacity, self.period = capacity, period
        self.tokens, self.stamp = float(capacity), time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) / self.period)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Store:
    def __init__(self, data_dir, providers, fetcher: Fetcher, *, workers: int = 2, persist: bool = True,
                 pending_ttl: float = PENDING_TTL):
        self.data = Path(data_dir)
        self.providers = providers
        self.fetcher = fetcher
        self.persist = persist
        self.pending_ttl = pending_ttl
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.seq = 1
        self.requests: dict[str, dict] = {}
        self.settings = dict(DEFAULT_SETTINGS)
        self.buckets: dict[str, Bucket] = {}
        self.auto_bucket = Bucket(12, 5.0)
        self.clients: dict[str, float] = {}
        self.admin_seen = 0.0
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='relay-job')
        self._save_lock = threading.Lock()
        self._mem_articles: dict = {}
        self._mem_images: dict = {}
        if persist:
            (self.data / 'articles').mkdir(parents=True, exist_ok=True)
            (self.data / 'images').mkdir(parents=True, exist_ok=True)
            self._load()

    # ------------------------------------------------------------------ io
    def _load(self):
        path = self.data / 'state.json'
        try:
            raw = json.loads(path.read_text('utf-8'))
        except (OSError, ValueError):
            return
        self.settings.update({k: v for k, v in raw.get('settings', {}).items() if k in DEFAULT_SETTINGS and isinstance(v, bool)})
        for req in raw.get('requests', [])[-MAX_KEEP:]:
            if not isinstance(req, dict) or not ID_RE.fullmatch(str(req.get('id', ''))):
                continue
            if req.get('status') in ('pending', 'running'):
                req['status'] = 'expired'
                req['error'] = {'code': 'restart', 'message': 'The Mac server restarted before this finished. Please send it again.'}
            self.requests[req['id']] = req

    def _save(self):
        if not self.persist:
            return
        with self.lock:
            snapshot = {'settings': dict(self.settings), 'requests': list(self.requests.values())}
        with self._save_lock:
            tmp = self.data / 'state.json.tmp'
            try:
                tmp.write_text(json.dumps(snapshot), 'utf-8')
                os.replace(tmp, self.data / 'state.json')
            except OSError:
                LOG.exception('Could not save state')

    def _bump(self):
        with self.cond:
            self.seq += 1
            self.cond.notify_all()
        self._save()

    def wait(self, after: int, timeout: float) -> int:
        end = time.monotonic() + timeout
        with self.cond:
            while self.seq <= after:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            return self.seq

    # ------------------------------------------------------------ settings
    def set_setting(self, key: str, value) -> None:
        if key not in DEFAULT_SETTINGS or not isinstance(value, bool):
            raise RequestError(400, 'bad_setting', 'Unknown setting.')
        with self.lock:
            self.settings[key] = value
        AUDIT.info('setting %s=%s', key, value)
        self._bump()

    # ------------------------------------------------------------- clients
    def touch_client(self, ip: str):
        with self.lock:
            self.clients[ip] = time.time()
            if len(self.clients) > 64:
                oldest = min(self.clients, key=self.clients.get)
                self.clients.pop(oldest, None)

    def touch_admin(self):
        self.admin_seen = time.time()

    # -------------------------------------------------------------- submit
    @staticmethod
    def clean_query(q) -> str:
        if not isinstance(q, str):
            raise RequestError(400, 'bad_query', 'Type something to search for.')
        q = re.sub(r'\s+', ' ', q).strip()
        if not q:
            raise RequestError(400, 'bad_query', 'Type something to search for.')
        if len(q) > MAX_QUERY:
            raise RequestError(400, 'bad_query', f'Keep searches under {MAX_QUERY} characters.')
        if any(ord(c) < 32 or ord(c) == 127 for c in q):
            raise RequestError(400, 'bad_query', 'That text contains characters that are not allowed.')
        return q

    def _resolve_article_url(self, params: dict) -> tuple[str, str | None]:
        """Return (url, parent_id). URLs come from server-side tables or the typed field."""
        parent_id = params.get('parent')
        if parent_id is not None:
            if not isinstance(parent_id, str) or not ID_RE.fullmatch(parent_id):
                raise RequestError(400, 'bad_parent', 'Unknown source page.')
            with self.lock:
                parent = self.requests.get(parent_id)
                if not parent or parent['status'] != 'ready':
                    raise RequestError(404, 'bad_parent', 'That page is no longer available. Search again.')
                if parent['kind'] == 'search':
                    idx = params.get('result')
                    results = parent.get('results', [])
                    if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(results):
                        raise RequestError(400, 'bad_link', 'That result does not exist.')
                    return results[idx]['url'], parent_id
            idx = params.get('link')
            article = self.load_article(parent_id)
            links = article.get('links', []) if article else []
            if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(links):
                raise RequestError(400, 'bad_link', 'That link does not exist.')
            return links[idx], parent_id
        # No arbitrary URL submission from the managed tablet.
        # Only a server-selected search-result / article link identifier is accepted.
        raise RequestError(400, 'bad_link', 'Open a link from approved search results instead.')

    def submit(self, kind: str, ip: str, params: dict) -> tuple[dict, bool]:
        if kind not in ('search', 'article'):
            raise RequestError(400, 'bad_kind', 'Unsupported request type.')
        with self.lock:
            if self.settings['paused']:
                raise RequestError(503, 'paused', 'Requests are paused on the Mac.')
        if kind == 'search':
            provider = self.providers.get(params.get('provider'))
            if provider is None:
                raise RequestError(400, 'bad_provider', 'Unknown search source.')
            query = self.clean_query(params.get('query'))
            key = ('search', provider.id, query.casefold())
            fields = {'provider': provider.id, 'query': query, 'host': provider.host, 'url': None, 'parent': None}
        else:
            url, parent = self._resolve_article_url(params)
            host = urlsplit(url).hostname or ''
            key = ('article', url)
            fields = {'provider': None, 'query': None, 'host': host, 'url': url, 'parent': parent}
        with self.lock:
            for existing in self.requests.values():
                if existing['status'] == 'pending' and existing['key'] == list(key):
                    return existing, False
            self._expire_locked()
            bucket = self.buckets.setdefault(ip, Bucket(8, 1.5))
            if not bucket.take():
                raise RequestError(429, 'rate_limited', 'Slow down a little, then try again.')
            if sum(r['status'] == 'pending' for r in self.requests.values()) >= MAX_PENDING:
                raise RequestError(429, 'queue_full', 'Too many requests are waiting on the Mac.')
            now = time.time()
            req = {'id': secrets.token_hex(8), 'kind': kind, 'key': list(key), 'status': 'pending', 'created': now,
                   'decided': None, 'finished': None, 'approval': None, 'error': None, 'results': [],
                   'title': None, 'words': None, 'has_article': False, 'ip': ip, **fields}
            self.requests[req['id']] = req
            self._prune_locked()
            auto = self._auto_ok(req)
            if auto:
                req['approval'], req['status'], req['decided'] = 'auto', 'running', now
        AUDIT.info('submit id=%s kind=%s host=%s auto=%s text=%r', req['id'], kind, req['host'], bool(auto),
                   (req['query'] or req['url'])[:200])
        if auto:
            self.pool.submit(self._run, req['id'])
        self._bump()
        return req, True

    def _auto_ok(self, req) -> bool:
        host = req['host'] or ''
        wiki = host.endswith('.wikipedia.org')
        if req['kind'] == 'article' and req['parent'] and wiki and self.settings['auto_wiki_articles']:
            return self.auto_bucket.take()
        if req['kind'] == 'search' and (req['provider'] or '').startswith('wikipedia') and self.settings['auto_wiki_search']:
            return self.auto_bucket.take()
        return False

    # ---------------------------------------------------------- decisions
    def decide(self, ident: str, approve: bool) -> dict:
        with self.lock:
            req = self.requests.get(ident)
            if not req:
                raise RequestError(404, 'unknown', 'That request no longer exists.')
            if req['status'] != 'pending':
                raise RequestError(409, 'not_pending', f'Already {req["status"]}.')
            req['decided'] = time.time()
            req['approval'] = 'manual'
            req['status'] = 'running' if approve else 'rejected'
            if not approve:
                req['finished'] = req['decided']
        AUDIT.info('%s id=%s kind=%s host=%s', 'approve' if approve else 'reject', ident, req['kind'], req['host'])
        if approve:
            self.pool.submit(self._run, ident)
        self._bump()
        return req

    def cancel(self, ident: str) -> dict:
        with self.lock:
            req = self.requests.get(ident)
            if not req:
                raise RequestError(404, 'unknown', 'That request no longer exists.')
            if req['status'] != 'pending':
                raise RequestError(409, 'not_pending', f'Already {req["status"]}.')
            req['status'], req['finished'] = 'cancelled', time.time()
        AUDIT.info('cancel id=%s', ident)
        self._bump()
        return req

    def _expire_locked(self):
        now = time.time()
        changed = False
        for req in self.requests.values():
            if req['status'] == 'pending' and now - req['created'] > self.pending_ttl:
                req['status'], req['finished'] = 'expired', now
                req['error'] = {'code': 'expired', 'message': 'Nobody approved this on the Mac in time.'}
                changed = True
        return changed

    def expire(self):
        with self.lock:
            changed = self._expire_locked()
        if changed:
            self._bump()

    # ------------------------------------------------------------- worker
    def _run(self, ident: str):
        with self.lock:
            req = self.requests.get(ident)
            if not req:
                return
            kind, query, url, provider_id = req['kind'], req['query'], req['url'], req['provider']
        t0 = time.monotonic()
        try:
            if kind == 'search':
                results = self.providers[provider_id].search(self.fetcher, query)
                with self.lock:
                    req.update(status='ready', results=results, finished=time.time())
            else:
                article = fetch_article(self.fetcher, url)
                if self.settings['images'] and article['images']:
                    for idx, mime, data in download_images(self.fetcher, article['images']):
                        self._save_image(ident, idx, mime, data)
                        article['images'][idx]['ok'] = True
                        article['images'][idx]['mime'] = mime
                self._save_article(ident, article)
                with self.lock:
                    req.update(status='ready', title=article['title'], words=article['words'], has_article=True,
                               finished=time.time())
            AUDIT.info('done id=%s status=ready seconds=%.1f', ident, time.monotonic() - t0)
        except (FetchError, ArticleError) as exc:
            self._fail(ident, exc.code, exc.message)
        except Exception:  # noqa: BLE001 - never let a worker die silently
            LOG.exception('Unexpected error while running %s', ident)
            self._fail(ident, 'internal', 'Something went wrong on the Mac. Details are in its Terminal log.')
        self._bump()

    def _fail(self, ident, code, message):
        AUDIT.warning('failed id=%s code=%s', ident, code)
        with self.lock:
            req = self.requests.get(ident)
            if req:
                req.update(status='failed', error={'code': code, 'message': message}, finished=time.time())

    # ----------------------------------------------------- article storage
    def _save_article(self, ident, article):
        if self.persist:
            (self.data / 'articles' / f'{ident}.json').write_text(json.dumps(article), 'utf-8')
        else:
            self._mem_articles[ident] = article

    def _save_image(self, ident, idx, mime, data):
        if self.persist:
            (self.data / 'images' / f'{ident}-{idx}.bin').write_bytes(data)
        else:
            self._mem_images[(ident, idx)] = data

    def load_article(self, ident):
        if not ID_RE.fullmatch(ident or ''):
            return None
        if not self.persist:
            return self._mem_articles.get(ident)
        try:
            return json.loads((self.data / 'articles' / f'{ident}.json').read_text('utf-8'))
        except (OSError, ValueError):
            return None

    def load_image(self, ident, idx):
        article = self.load_article(ident)
        if not article or not isinstance(idx, int) or not 0 <= idx < len(article['images']):
            return None
        meta = article['images'][idx]
        if not meta.get('ok'):
            return None
        if not self.persist:
            data = self._mem_images.get((ident, idx))
        else:
            try:
                data = (self.data / 'images' / f'{ident}-{idx}.bin').read_bytes()
            except OSError:
                return None
        return (meta['mime'], data) if data else None

    # ------------------------------------------------------------ housekeeping
    def _prune_locked(self):
        done = [r for r in self.requests.values() if r['status'] in TERMINAL]
        excess = len(self.requests) - MAX_KEEP
        for req in sorted(done, key=lambda r: r['created'])[:max(0, excess)]:
            self._delete_locked(req['id'])

    def _delete_locked(self, ident):
        self.requests.pop(ident, None)
        if self.persist:
            (self.data / 'articles' / f'{ident}.json').unlink(missing_ok=True)
            for p in (self.data / 'images').glob(f'{ident}-*.bin'):
                p.unlink(missing_ok=True)
        else:
            self._mem_articles.pop(ident, None)
            for k in [k for k in self._mem_images if k[0] == ident]:
                self._mem_images.pop(k, None)

    def clear_history(self) -> int:
        with self.lock:
            ids = [i for i, r in self.requests.items() if r['status'] in TERMINAL]
            for i in ids:
                self._delete_locked(i)
        AUDIT.info('clear_history removed=%d', len(ids))
        self._bump()
        return len(ids)

    # ---------------------------------------------------------------- views
    def _base_view(self, req) -> dict:
        label = req['query'] or req.get('title') or req['url']
        provider = self.providers.get(req['provider']) if req['provider'] else None
        return {'id': req['id'], 'kind': req['kind'], 'status': req['status'], 'created': req['created'],
                'decided': req['decided'], 'finished': req['finished'], 'host': req['host'], 'label': label,
                'query': req['query'], 'provider': req['provider'], 'provider_label': provider.label if provider else None,
                'parent': req['parent'], 'error': req['error'], 'title': req['title'], 'words': req['words'],
                'has_article': req['has_article'], 'n_results': len(req['results'])}

    def public_list(self) -> list[dict]:
        with self.lock:
            return [self._base_view(r) for r in list(self.requests.values())[-60:]][::-1]

    def public_detail(self, ident) -> dict | None:
        with self.lock:
            req = self.requests.get(ident)
            if not req:
                return None
            view = self._base_view(req)
            view['results'] = [{'title': r['title'], 'snippet': r.get('snippet', ''), 'host': urlsplit(r['url']).hostname}
                               for r in req['results']]
            return view

    def public_article(self, ident) -> dict | None:
        art = self.load_article(ident)
        if not art:
            return None
        return {'title': art['title'], 'site': art['site'], 'byline': art['byline'], 'lang': art['lang'],
                'blocks': art['blocks'], 'truncated': art['truncated'], 'words': art['words'],
                'link_hosts': [urlsplit(u).hostname for u in art['links']],
                'images': [{'ok': bool(i.get('ok'))} for i in art['images']]}

    def admin_list(self) -> list[dict]:
        with self.lock:
            out = []
            for req in list(self.requests.values())[-100:][::-1]:
                v = self._base_view(req)
                v.update(url=req['url'], approval=req['approval'], ip=req['ip'])
                out.append(v)
            return out