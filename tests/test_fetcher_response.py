"""Regression: a fully consumed HTTP Content-Length response must not trigger Errno 9."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay_web.fetcher import Fetcher


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = b'<rss><channel><item>test</item></channel></rss>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/xml')
        if self.path != '/eof':
            self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class FetchResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path):
        # Private HTTP is permitted only for this isolated unit test.
        return Fetcher(allow_private=True, allow_http=True).fetch(
            f'http://127.0.0.1:{self.server.server_port}{path}',
            max_bytes=1024, accept='text/xml', allowed_types=('text/xml',))

    def test_content_length_read_does_not_touch_closed_socket(self):
        self.assertIn(b'<rss>', self.fetch('/length').body)

    def test_connection_close_read(self):
        self.assertIn(b'<rss>', self.fetch('/eof').body)


if __name__ == '__main__':
    unittest.main()
