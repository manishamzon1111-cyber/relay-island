import json, tempfile, threading, unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor

from relay_web.server import App, make_server
from relay_web.store import Store, RequestError
from relay_web.fetcher import Fetcher
from relay_web.media import Library, parse_range

class DemoProvider:
    id='wikipedia';label='Test provider';host='en.wikipedia.org';experimental=False;note=''
    def search(self, fetcher, query): return [{'title':f'Example {query}','snippet':'Description','url':'https://en.wikipedia.org/wiki/Example'}]

def fetch(url,method='GET',body=None,headers=None,opener=None):
    if body is not None:body=json.dumps(body).encode()
    h={'Content-Type':'application/json',**(headers or {})}
    try:
        with (opener or urlopen)(Request(url,method=method,data=body,headers=h),timeout=4) as r:
            v=r.read();return r.status,json.loads(v) if v and r.headers.get_content_type()=='application/json' else v
    except HTTPError as e:
        v=e.read();return e.code,json.loads(v) if v and e.headers.get_content_type()=='application/json' else v

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        (root/'movie.mp4').write_bytes(b'1234567890')
        self.store=Store(root/'data',{'wikipedia':DemoProvider()},Fetcher(),persist=False)
        self.app=App(self.store,Library(root),port=0)
        self.server=make_server('127.0.0.1',0,self.app,True)
        self.port=self.server.server_port
        self.base=f'http://127.0.0.1:{self.port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.admin=build_opener(HTTPCookieProcessor(CookieJar()))
        fetch(self.base+'/admin/enter?key='+self.app.admin_key,opener=self.admin.open)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.store.pool.shutdown(wait=True);self.tmp.cleanup()
    def test_admin_access_requires_session(self):
        self.assertEqual(fetch(self.base+'/admin/api/state')[0],401)
        self.assertEqual(fetch(self.base+'/admin/api/state',opener=self.admin.open)[0],200)
        self.assertEqual(fetch(self.base+'/admin/api/decide','POST',{'id':'0'*16,'decision':'approve'},opener=self.admin.open)[0],403)
    def test_complete_approval_rejection(self):
        code,req=fetch(self.base+'/api/requests','POST',{'kind':'search','provider':'wikipedia','query':'foo'})
        self.assertEqual(code,201);id=req['id']
        self.assertEqual(fetch(self.base+'/api/requests/'+id)[1]['status'],'pending')
        code,_=fetch(self.base+'/admin/api/decide','POST',{'id':id,'decision':'approve'},
            {'X-Relay-Admin':'1','Origin':self.base},self.admin.open)
        self.assertEqual(code,200)
        self.store.pool.shutdown(wait=True)
        detail=fetch(self.base+'/api/requests/'+id)[1]
        self.assertEqual(detail['status'],'ready');self.assertEqual(detail['results'][0]['title'],'Example foo')
    def test_reject_and_cannot_submit_url(self):
        code,req=fetch(self.base+'/api/requests','POST',{'kind':'search','provider':'wikipedia','query':'bar'})
        self.assertEqual(code,201)
        self.assertEqual(fetch(self.base+'/admin/api/decide','POST',{'id':req['id'],'decision':'reject'},
            {'X-Relay-Admin':'1','Origin':self.base},self.admin.open)[0],200)
        self.assertEqual(fetch(self.base+'/api/requests/'+req['id'])[1]['status'],'rejected')
        self.assertEqual(fetch(self.base+'/api/requests','POST',{'kind':'article','url':'https://example.com/upload?id=secret'})[0],400)
    def test_lan_listener_never_serves_admin(self):
        try:
            lan=make_server('127.0.0.2',0,self.app,False)
        except OSError:
            self.skipTest('Second loopback alias unavailable on this machine')
        th=threading.Thread(target=lan.serve_forever,daemon=True);th.start()
        try:
            import http.client
            # Bypass environment proxies; connect directly to the test listener.
            conn=http.client.HTTPConnection('127.0.0.2',lan.server_port,timeout=3)
            conn.request('GET','/admin',headers={'Host':f'127.0.0.2:{lan.server_port}'})
            resp=conn.getresponse()
            self.assertEqual(resp.status,404)
            conn.close()
        finally:
            lan.shutdown();lan.server_close()

    def test_static_media_and_range(self):
        self.assertEqual(fetch(self.base+'/')[0],200)
        self.assertEqual(fetch(self.base+'/static/app.js')[0],200)
        self.assertEqual(fetch(self.base+'/api/library')[1]['files'][0]['size'],10)
        item=fetch(self.base+'/api/library')[1]['files'][0]['id']
        status,data=fetch(self.base+'/media/'+item,headers={'Range':'bytes=2-5'})
        self.assertEqual(status,206);self.assertEqual(data,b'3456')
        self.assertEqual(fetch(self.base+'/static/../server.py')[0],404)

if __name__=='__main__':unittest.main()
