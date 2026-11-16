"""Turn fetched pages into a small, safe *block model* for the reader.

The tablet never receives HTML from the web. It receives JSON blocks made only
of text, a fixed set of block types, numbered links and numbered images, and
builds the DOM itself with textContent. Nothing from a page can run script,
load a remote resource or embed styles on the tablet.

Block types: h, p, list, quote, pre, img, table.
Inline items: plain strings, or {"k": b|i|code|sup|sub|a|br, "c": [...], "n": link}.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urljoin, urlsplit

from .fetcher import FetchError, Fetcher, decode_body

MAX_HTML = 3_000_000
MAX_CHARS = 300_000
MAX_LINKS = 500
MAX_IMAGES = 12
MAX_IMAGE_BYTES = 2_500_000

SKIP = {'script', 'style', 'noscript', 'template', 'svg', 'iframe', 'form', 'button', 'input', 'select',
        'textarea', 'object', 'embed', 'audio', 'video', 'canvas', 'map', 'head', 'title', 'meta', 'link',
        'nav', 'aside', 'footer', 'dialog'}
HEADINGS = {'h1': 2, 'h2': 2, 'h3': 3, 'h4': 4, 'h5': 4, 'h6': 4}
BLOCKISH = {'p', 'ul', 'ol', 'blockquote', 'pre', 'figure', 'table', 'dl', 'hr'}
CONTAINERS = {'div', 'section', 'article', 'main', 'body', 'html', 'details', 'summary', 'center', 'header', 'figcaption'}
WIKI_DROP_SECTIONS = {'references', 'external links', 'notes', 'further reading', 'citations', 'bibliography',
                      'footnotes', 'sources', 'संदर्भ', 'बाहरी कड़ियाँ', 'बाहरी कड़ियां', 'टिप्पणियाँ', 'सन्दर्भ'}


class ArticleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _cls(name: str) -> str:
    return f"contains(concat(' ',normalize-space(@class),' '),' {name} ')"


def _any_class(*names: str) -> str:
    return '//*[' + ' or '.join(_cls(n) for n in names) + ']'


class Builder:
    def __init__(self, base_url: str, wiki: bool = False):
        self.base = base_url
        self.wiki = wiki
        self.links: list[str] = []
        self._link_ix: dict[str, int] = {}
        self.images: list[dict] = []
        self._image_ix: dict[str, int] = {}
        self.pending: list[dict] = []
        self.chars = 0
        self.truncated = False

    # -- links & images -----------------------------------------------------
    def link_index(self, href: str):
        if not href or href.startswith('#'):
            return None
        url = urljoin(self.base, href.strip())
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return None
        url = url.split('#', 1)[0]
        if self.wiki and (parts.hostname or '').endswith('wikipedia.org'):
            path = unquote(parts.path)
            if not path.startswith('/wiki/') or ':' in path[6:] or 'action=' in parts.query:
                return None
        if url in self._link_ix:
            return self._link_ix[url]
        if len(self.links) >= MAX_LINKS or len(url) > 1500:
            return None
        self.links.append(url)
        self._link_ix[url] = len(self.links) - 1
        return self._link_ix[url]

    def image_block(self, img, caption=None):
        if len(self.images) >= MAX_IMAGES:
            return None
        src = img.get('src') or ''
        if src.startswith('data:') or not src:
            src = img.get('data-src') or ''
        if not src:
            return None
        url = urljoin(self.base, src.strip())
        parts = urlsplit(url)
        low = parts.path.lower()
        if parts.scheme != 'https' or low.endswith('.svg') or '/math/render/' in low or '/static/images/' in low:
            return None
        cls = (img.get('class') or '').lower()
        if 'emoji' in cls or 'mwe-math' in cls:
            return None
        for attr in ('width', 'height'):
            val = img.get(attr)
            if val and val.isdigit() and int(val) < 48:
                return None
        if url not in self._image_ix:
            self.images.append({'src': url})
            self._image_ix[url] = len(self.images) - 1
        block = {'t': 'img', 'i': self._image_ix[url], 'alt': (img.get('alt') or '').strip()[:300]}
        if caption:
            block['cap'] = caption
        return block

    # -- inline -------------------------------------------------------------
    def inline(self, el) -> list:
        out = []
        if el.text:
            out.append(el.text)
        for ch in el:
            if isinstance(ch.tag, str):
                out.extend(self.inline_el(ch))
            if ch.tail:
                out.append(ch.tail)
        return out

    def inline_el(self, ch) -> list:
        tag = ch.tag.lower()
        if tag in SKIP:
            return []
        if tag == 'br':
            return [{'k': 'br'}]
        if tag == 'img':
            block = self.image_block(ch)
            if block:
                self.pending.append(block)
            return []
        if self.wiki and tag == 'span' and 'mwe-math-element' in (ch.get('class') or ''):
            alt = ch.xpath('string(.//img/@alt)') or ch.text_content()
            alt = re.sub(r'^\{\\displaystyle\s*|\}$', '', alt.strip())
            return [{'k': 'code', 'c': [alt[:300]]}] if alt else []
        kids = self.inline(ch)
        kind = {'b': 'b', 'strong': 'b', 'i': 'i', 'em': 'i', 'cite': 'i', 'code': 'code', 'kbd': 'code',
                'samp': 'code', 'tt': 'code', 'sup': 'sup', 'sub': 'sub'}.get(tag)
        if tag == 'a':
            n = self.link_index(ch.get('href', ''))
            if n is None:
                return kids
            return [{'k': 'a', 'n': n, 'c': kids}]
        if kind:
            return [{'k': kind, 'c': kids}]
        return kids

    @staticmethod
    def normalize(items: list) -> list:
        """Collapse whitespace, merge adjacent strings, drop empties."""
        out: list = []

        def add(s):
            s = re.sub(r'[\s\u00a0]+', ' ', s)
            if s:
                if out and isinstance(out[-1], str):
                    out[-1] += s
                else:
                    out.append(s)
        for it in items:
            if isinstance(it, str):
                add(it)
            else:
                kids = Builder.normalize(it.get('c', [])) if it.get('c') is not None else None
                if it['k'] == 'br':
                    out.append(it)
                elif kids:
                    out.append({**it, 'c': kids})
        while out and isinstance(out[0], str) and not out[0].strip():
            out.pop(0)
        if out and isinstance(out[0], str):
            out[0] = out[0].lstrip()
        if out and isinstance(out[-1], str):
            out[-1] = out[-1].rstrip()
        return [o for o in out if o != '']

    @staticmethod
    def plain(items) -> str:
        return ''.join(i if isinstance(i, str) else ('\n' if i['k'] == 'br' else Builder.plain(i.get('c', []))) for i in items)

    # -- blocks -------------------------------------------------------------
    def emit_pending(self, out):
        while self.pending:
            out.append(self.pending.pop(0))

    def add_text_budget(self, n: int) -> bool:
        self.chars += n
        if self.chars > MAX_CHARS:
            self.truncated = True
            return False
        return True

    def walk(self, el, out):
        buf: list = []

        def flush():
            items = self.normalize(buf)
            buf.clear()
            text = self.plain(items).strip()
            if text and self.add_text_budget(len(text)):
                out.append({'t': 'p', 'c': items})
            self.emit_pending(out)
        if el.text:
            buf.append(el.text)
        for ch in el:
            if isinstance(ch.tag, str):
                tag = ch.tag.lower()
                if tag in SKIP:
                    pass
                elif tag in HEADINGS or tag in BLOCKISH or tag in CONTAINERS:
                    flush()
                    self.block(ch, tag, out)
                else:
                    buf.extend(self.inline_el(ch))
            if ch.tail:
                buf.append(ch.tail)
            if self.truncated:
                break
        flush()

    def block(self, el, tag, out):
        if self.truncated:
            return
        if tag in HEADINGS:
            items = self.normalize(self.inline(el))
            text = self.plain(items).strip()
            if text:
                out.append({'t': 'h', 'l': HEADINGS[tag], 'c': [text]})
            self.pending.clear()
        elif tag == 'p':
            items = self.normalize(self.inline(el))
            text = self.plain(items).strip()
            if text and self.add_text_budget(len(text)):
                out.append({'t': 'p', 'c': items})
            self.emit_pending(out)
        elif tag in ('ul', 'ol'):
            items = self.list_items(el, 0)
            if items:
                out.append({'t': 'list', 'o': tag == 'ol', 'items': items})
            self.emit_pending(out)
        elif tag == 'blockquote':
            sub: list = []
            self.walk(el, sub)
            if sub:
                out.append({'t': 'quote', 'b': sub})
        elif tag == 'pre':
            text = el.text_content().strip('\n')[:6000]
            if text.strip() and self.add_text_budget(len(text)):
                out.append({'t': 'pre', 'x': text})
        elif tag == 'figure' or (tag == 'div' and 'thumb' in (el.get('class') or '').split()):
            imgs = el.xpath('.//img')
            cap_nodes = el.xpath('.//figcaption | .//*[contains(@class,"thumbcaption")]')
            cap = None
            if cap_nodes:
                cap = self.normalize(self.inline(cap_nodes[0]))
                cap = cap if self.plain(cap).strip() else None
            made = False
            for img in imgs[:1]:
                block = self.image_block(img, cap)
                if block:
                    out.append(block)
                    made = True
            if not made and cap:
                out.append({'t': 'p', 'c': cap})
        elif tag == 'table':
            tb = self.table(el)
            if tb:
                out.append(tb)
        elif tag == 'dl':
            for node in el:
                if isinstance(node.tag, str) and node.tag in ('dt', 'dd'):
                    items = self.normalize(self.inline(node))
                    if self.plain(items).strip():
                        out.append({'t': 'p', 'c': [{'k': 'b', 'c': items}] if node.tag == 'dt' else items})
            self.pending.clear()
        elif tag == 'hr':
            pass
        else:
            self.walk(el, out)

    def list_items(self, el, depth):
        items = []
        for li in el:
            if not isinstance(li.tag, str) or li.tag.lower() not in ('li', 'dt', 'dd'):
                continue
            subs = [c for c in li if isinstance(c.tag, str) and c.tag.lower() in ('ul', 'ol')]
            clone_text = self.inline_without(li, subs)
            sub_items = []
            if depth < 2:
                for s in subs:
                    sub_items.extend(self.list_items(s, depth + 1))
            text = self.plain(clone_text).strip()
            if (text or sub_items) and self.add_text_budget(len(text)):
                entry = {'c': clone_text}
                if sub_items:
                    entry['sub'] = sub_items
                items.append(entry)
            if len(items) >= 300:
                break
        return items

    def inline_without(self, li, skip_children):
        out = []
        if li.text:
            out.append(li.text)
        for ch in li:
            if isinstance(ch.tag, str) and ch not in skip_children:
                out.extend(self.inline_el(ch))
            if ch.tail:
                out.append(ch.tail)
        return self.normalize(out)

    def table(self, el):
        rows = []
        header = False
        for tr in el.xpath('.//tr')[:60]:
            cells = []
            for cell in tr:
                if not isinstance(cell.tag, str) or cell.tag.lower() not in ('td', 'th'):
                    continue
                if cell.tag.lower() == 'th' and not rows:
                    header = True
                items = self.normalize(self.inline(cell))
                cells.append(items)
                if len(cells) >= 10:
                    break
            if any(self.plain(c).strip() for c in cells):
                rows.append(cells)
        self.pending.clear()
        if len(rows) < 1 or max(len(r) for r in rows) < 1:
            return None
        total = sum(len(self.plain(c)) for r in rows for c in r)
        if not self.add_text_budget(total):
            return None
        return {'t': 'table', 'h': header, 'r': rows}


def _finish(blocks: list, title: str, wiki: bool) -> list:
    out = []
    skipping = False
    for b in blocks:
        if b['t'] == 'h':
            text = b['c'][0].strip()
            if wiki and b['l'] == 2:
                skipping = text.lower() in WIKI_DROP_SECTIONS
            if skipping:
                continue
            if not out and text.lower() == title.strip().lower():
                continue
            if out and out[-1]['t'] == 'h':
                out.pop()  # heading followed by heading: first one has no content
        elif skipping:
            continue
        out.append(b)
    while out and out[-1]['t'] == 'h':
        out.pop()
    return out


def _word_count(blocks) -> int:
    total = 0

    def inline_words(items):
        return len(Builder.plain(items).split())
    for b in blocks:
        t = b['t']
        if t in ('p', 'h'):
            total += inline_words(b['c'])
        elif t == 'list':
            stack = list(b['items'])
            while stack:
                it = stack.pop()
                total += inline_words(it['c'])
                stack.extend(it.get('sub', []))
        elif t == 'quote':
            total += _word_count(b['b'])
        elif t == 'pre':
            total += len(b['x'].split())
    return total


def extract_wikipedia(html: str, url: str) -> dict:
    import lxml.html
    doc = lxml.html.fromstring(html)
    content = doc.xpath('//*[@id="mw-content-text"]')
    if not content:
        raise ArticleError('no_content', 'This Wikipedia page has no readable article text.')
    title_nodes = doc.xpath('//*[@id="firstHeading"]')
    title = (title_nodes[0].text_content().strip() if title_nodes else '') or (doc.findtext('.//title') or '').split(' - ')[0].strip()
    root = content[0]
    lead_img = None
    for img in root.xpath('.//table[' + _cls('infobox') + ']//img')[:3]:
        lead_img = img
        break
    builder = Builder(url, wiki=True)
    lead_block = builder.image_block(lead_img) if lead_img is not None else None
    drop_xpath = ('//*[@id="toc"] | //style | //script | //sup[' + _cls('reference') + '] | ' + _any_class(
        'infobox', 'navbox', 'vertical-navbox', 'sidebar', 'metadata', 'ambox', 'hatnote', 'mw-editsection',
        'reflist', 'refbegin', 'mw-references-wrap', 'noprint', 'toc', 'mw-empty-elt', 'shortdescription',
        'navbar', 'sistersitebox', 'mbox-small', 'noviewer', 'mw-cite-backlink', 'magnify', 'portal',
        'authority-control', 'catlinks', 'printfooter'))
    for node in root.xpath(drop_xpath):
        if node.getparent() is not None:
            node.drop_tree()  # keeps the tail text, unlike remove()
    blocks: list = []
    builder.walk(root, blocks)
    blocks = _finish(blocks, title, wiki=True)
    if lead_block:
        blocks.insert(0, lead_block)
    if not blocks:
        raise ArticleError('no_content', 'This Wikipedia page has no readable article text.')
    lang = (urlsplit(url).hostname or '').split('.')[0]
    return {'title': title or 'Wikipedia', 'site': urlsplit(url).hostname, 'byline': '', 'lang': lang,
            'url': url, 'blocks': blocks, 'links': builder.links, 'images': builder.images,
            'truncated': builder.truncated, 'words': _word_count(blocks)}


def extract_generic(html: str, url: str) -> dict:
    try:
        import trafilatura
    except ImportError as exc:
        raise ArticleError('unavailable', 'Reading general web pages needs the “trafilatura” package. '
                           'Run: pip install -r requirements.txt') from exc
    import lxml.html
    out = None
    for recall in (False, True):
        out = trafilatura.extract(html, url=url, output_format='html', include_images=True, include_links=True,
                                  include_tables=True, include_formatting=True, favor_recall=recall)
        if out and len(out) > 200:
            break
    if not out:
        raise ArticleError('no_content', 'No readable article text was found on that page.')
    meta = trafilatura.extract_metadata(html, default_url=url)
    root = lxml.html.fromstring(out)
    title = (getattr(meta, 'title', None) or '').strip()
    if not title:
        h1 = root.xpath('.//h1')
        title = h1[0].text_content().strip() if h1 else (urlsplit(url).hostname or 'Article')
    builder = Builder(url)
    blocks: list = []
    builder.walk(root, blocks)
    blocks = _finish(blocks, title, wiki=False)
    if not blocks:
        raise ArticleError('no_content', 'No readable article text was found on that page.')
    return {'title': title[:300], 'site': (getattr(meta, 'sitename', None) or urlsplit(url).hostname or '')[:100],
            'byline': (getattr(meta, 'author', None) or '')[:150], 'lang': (getattr(meta, 'language', None) or '')[:10],
            'url': url, 'blocks': blocks, 'links': builder.links, 'images': builder.images,
            'truncated': builder.truncated, 'words': _word_count(blocks)}


def fetch_article(fetcher: Fetcher, url: str) -> dict:
    got = fetcher.fetch(url, max_bytes=MAX_HTML, accept='text/html,application/xhtml+xml',
                        allowed_types=('text/html', 'application/xhtml+xml'))
    html = decode_body(got.body, got.charset)
    host = urlsplit(got.url).hostname or ''
    if host.endswith('.wikipedia.org') and '/wiki/' in got.url:
        return extract_wikipedia(html, got.url)
    return extract_generic(html, got.url)


MAGIC = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


def sniff_image(data: bytes) -> str | None:
    for magic, mime in MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def download_images(fetcher: Fetcher, images: list[dict]) -> list[tuple[int, str, bytes]]:
    """Fetch article images in parallel. Failures are skipped silently."""
    def one(item):
        i, img = item
        try:
            got = fetcher.fetch(img['src'], max_bytes=MAX_IMAGE_BYTES, accept='image/jpeg,image/png,image/webp,image/gif',
                                allowed_types=('image/jpeg', 'image/png', 'image/webp', 'image/gif'), deadline=10)
        except FetchError:
            return None
        mime = sniff_image(got.body)
        return (i, mime, got.body) if mime else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [r for r in pool.map(one, list(enumerate(images))) if r]