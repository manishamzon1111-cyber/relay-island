"""Fixed search providers; untrusted client never supplies a destination URL."""
from __future__ import annotations
import json, re, html, os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, parse_qs
from .fetcher import FetchError, decode_body

@dataclass
class Provider:
    id: str
    label: str
    host: str
    experimental: bool = False
    note: str = ''

class Wikipedia(Provider):
    def __init__(self, lang='en', base=None):
        super().__init__('wikipedia' if lang=='en' else 'wikipedia-hi', 'Wikipedia' if lang=='en' else 'विकिपीडिया',f'{lang}.wikipedia.org')
        self.base=(base or f'https://{lang}.wikipedia.org').rstrip('/')
    def search(self, fetcher, query):
        url=self.base+'/w/api.php?action=query&list=search&format=json&formatversion=2&utf8=1&srlimit=10&srprop=snippet&srsearch='+quote(query,safe='')
        got=fetcher.fetch(url,max_bytes=256000,accept='application/json',allowed_types=('application/json',))
        try: hits=json.loads(got.body)['query']['search']
        except (ValueError,TypeError,KeyError) as exc: raise FetchError('parse','Wikipedia sent an unexpected response.') from exc
        out=[]
        for hit in hits[:10]:
            title=hit.get('title','')
            if not isinstance(title,str) or not title or len(title)>250: continue
            snippet=re.sub(r'<[^>]+>','',str(hit.get('snippet','')))
            out.append({'title':title,'snippet':html.unescape(snippet)[:300], 'url':self.base+'/wiki/'+quote(title.replace(' ','_'),safe="_()'!,:;@$*-.")})
        return out

class DuckDuckGo(Provider):
    def __init__(self,base=None):
        super().__init__('web','Web search','html.duckduckgo.com',True,'DuckDuckGo HTML results; this is not Google Search and may be blocked by automation checks.')
        self.base=(base or 'https://html.duckduckgo.com').rstrip('/')
    def search(self,fetcher,query):
        import lxml.html
        url=self.base+'/html/?q='+quote(query,safe='')
        got=fetcher.fetch(url,max_bytes=600000,accept='text/html',allowed_types=('text/html','application/xhtml+xml'))
        doc=lxml.html.fromstring(decode_body(got.body,got.charset))
        out=[]
        for node in doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " result ") and not(contains(@class,"result--ad"))]'):
            links=node.xpath('.//a[contains(@class,"result__a")]')
            if not links: continue
            href=links[0].get('href','')
            if href.startswith('//'): href='https:'+href
            p=urlsplit(href)
            if p.hostname and p.hostname.endswith('duckduckgo.com') and p.path.startswith('/l/'):
                href=parse_qs(p.query).get('uddg',[''])[0]
            target=urlsplit(href)
            if target.scheme!='https' or not target.hostname or target.hostname.endswith('duckduckgo.com'): continue
            sn=node.xpath('.//*[contains(@class,"result__snippet")]')
            out.append({'title':links[0].text_content().strip()[:200], 'snippet':sn[0].text_content().strip()[:300] if sn else '', 'url':href})
            if len(out)>=10: break
        if not out and any(t in got.body.lower() for t in [b'captcha',b'anomaly',b'challenge']):
            raise FetchError('blocked','DuckDuckGo requested human verification. Try Bing RSS or another configured provider.')
        if not out:
            raise FetchError('empty_results', 'DuckDuckGo returned no readable search results. Try Bing RSS or another provider.')
        return out


class BraveSearch(Provider):
    """Brave's documented JSON Search API; requires a Mac-side API token."""
    def __init__(self, token):
        super().__init__('web', 'Web search', 'api.search.brave.com', False, 'Powered by Brave Search')
        self._token = token

    def search(self, fetcher, query):
        url = 'https://api.search.brave.com/res/v1/web/search?q=' + quote(query, safe='') + '&count=10'
        got = fetcher.fetch(url, max_bytes=600000, accept='application/json',
                            allowed_types=('application/json',),
                            extra_headers={'X-Subscription-Token': self._token})
        try:
            items = json.loads(got.body).get('web', {}).get('results', [])
        except (ValueError, AttributeError, TypeError) as exc:
            raise FetchError('parse', 'Search provider sent an invalid response.') from exc
        out = []
        for item in items[:10]:
            address = item.get('url', '')
            parts = urlsplit(address)
            if parts.scheme != 'https' or not parts.hostname:
                continue
            out.append({'title': html.unescape(item.get('title', ''))[:200],
                        'snippet': html.unescape(re.sub(r'<[^>]*>', '', item.get('description', '')))[:300],
                        'url': address})
        return out


class BingRSS(Provider):
    """Unofficial RSS output of Bing web search. No API key, availability not guaranteed."""
    def __init__(self, base=None):
        super().__init__('bing', 'Bing (RSS, beta)', 'www.bing.com', True,
                         'Unofficial Bing RSS endpoint; may stop working or return incomplete results.')
        self.base = (base or 'https://www.bing.com').rstrip('/')

    def search(self, fetcher, query):
        url = self.base + '/search?q=' + quote(query, safe='') + '&format=rss'
        got = fetcher.fetch(url, max_bytes=600_000, accept='application/rss+xml, application/xml, text/xml',
                            allowed_types=('application/rss+xml', 'application/xml', 'text/xml', 'application/octet-stream'))
        if b'<!DOCTYPE' in got.body.upper() or b'<!ENTITY' in got.body.upper():
            raise FetchError('bad_type', 'Unexpected XML declaration in Bing RSS response.')
        try:
            root = ET.fromstring(got.body)
        except ET.ParseError as exc:
            raise FetchError('parse', 'Bing did not return an RSS feed. Try another provider.') from exc
        out = []
        for node in root.findall('./channel/item')[:10]:
            title = html.unescape(node.findtext('title', '')).strip()[:200]
            link = (node.findtext('link', '') or '').strip()
            parsed = urlsplit(link)
            if not title or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                continue
            description = html.unescape(re.sub(r'<[^>]*>', '', node.findtext('description', '') or ''))[:300]
            out.append({'title': title, 'snippet': description, 'url': link})
        if not out:
            raise FetchError('empty_results', 'Bing RSS returned no usable results. Try another provider.')
        return out


class SearXNG(Provider):
    """Optional externally hosted HTTPS SearXNG instance with JSON explicitly enabled."""
    def __init__(self, base):
        p = urlsplit(base)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password or p.port or
                p.path not in ('', '/') or p.query or p.fragment or len(base)>200):
            raise ValueError('RELAY_SEARXNG_URL must be an HTTPS origin (e.g. https://search.example.org)')
        super().__init__('searxng', 'SearXNG', p.hostname, True,
                         'Requires a configured HTTPS instance with JSON search API enabled.')
        self.base = base.rstrip('/')

    def search(self, fetcher, query):
        url = self.base + '/search?q=' + quote(query, safe='') + '&format=json'
        got = fetcher.fetch(url, max_bytes=600_000, accept='application/json',
                            allowed_types=('application/json',))
        try:
            items = json.loads(got.body).get('results', [])
        except (ValueError, TypeError, AttributeError) as exc:
            raise FetchError('parse', 'SearXNG returned an invalid JSON response.') from exc
        out=[]
        for item in items[:20]:
            if not isinstance(item, dict):
                continue
            link=item.get('url','')
            parsed=urlsplit(link)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                continue
            out.append({'title':str(item.get('title',''))[:200], 'snippet':str(item.get('content',''))[:300], 'url':link})
            if len(out)>=10:break
        if not out:
            raise FetchError('empty_results', 'SearXNG returned no usable results; check instance availability.')
        return out

def build_providers(wiki_base=None,ddg_base=None):
    providers=[BraveSearch(os.environ['RELAY_BRAVE_API_KEY']) if os.getenv('RELAY_BRAVE_API_KEY') else DuckDuckGo(ddg_base),
               BingRSS(),Wikipedia('en',wiki_base),Wikipedia('hi',wiki_base)]
    if os.getenv('RELAY_SEARXNG_URL'):
        providers.insert(1, SearXNG(os.environ['RELAY_SEARXNG_URL']))
    return {p.id:p for p in providers}
