import json
import os
import unittest
from unittest.mock import patch
from relay_web.providers import BingRSS, SearXNG, build_providers
from relay_web.fetcher import Fetched, FetchError

class StubFetcher:
    def __init__(self, body, mime):
        self.body, self.mime, self.urls = body, mime, []
    def fetch(self, url, **kw):
        self.urls.append(url)
        return Fetched(url, 200, self.mime, self.body, 'utf-8')

class ProviderTests(unittest.TestCase):
    def test_bing_rss_parse_and_query_encoding(self):
        feed = b'<rss><channel><item><title>Renault &amp; cars</title><link>https://example.org/car</link><description>A car</description></item></channel></rss>'
        f=StubFetcher(feed,'application/rss+xml')
        out=BingRSS().search(f,'cars & cars')
        self.assertEqual(out[0]['title'],'Renault & cars')
        self.assertIn('cars%20%26%20cars', f.urls[0])
    def test_bing_html_challenge_rejected(self):
        f=StubFetcher(b'<html>captcha</html>','text/html')
        with self.assertRaises(FetchError): BingRSS().search(f,'cars')
    def test_bing_entity_declaration_rejected(self):
        f=StubFetcher(b'<!DOCTYPE rss [ <!ENTITY x "hi">]><rss/>','application/rss+xml')
        with self.assertRaises(FetchError): BingRSS().search(f,'cars')
    def test_searxng_json(self):
        f=StubFetcher(json.dumps({'results':[{'title':'Example','content':'Summary','url':'https://example.org/read'}, {'title':'Local','url':'http://localhost/'}]}).encode(),'application/json')
        out=SearXNG('https://search.example.org').search(f,'my request')
        self.assertEqual(len(out),1)
        self.assertIn('format=json',f.urls[0])
    def test_searxng_reject_bad_origin(self):
        for bad in ['http://example.org','https://localhost:8080','https://a.test/x','https://a.test?x=1','https://u:p@a.test']:
            with self.subTest(bad=bad),self.assertRaises(ValueError): SearXNG(bad)
    def test_provider_default_and_optional(self):
        with patch.dict(os.environ, {'RELAY_BRAVE_API_KEY':'','RELAY_SEARXNG_URL':''}):
            p=build_providers(); self.assertEqual(p['web'].host,'html.duckduckgo.com');self.assertIn('bing',p)
        with patch.dict(os.environ, {'RELAY_BRAVE_API_KEY':'testkey', 'RELAY_SEARXNG_URL':'https://search.example.org'}):
            p=build_providers();self.assertEqual(p['web'].host,'api.search.brave.com');self.assertIn('searxng',p)

if __name__=='__main__':unittest.main()
