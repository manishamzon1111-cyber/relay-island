"""Run with: python3 -m relay_web [--lan-host ... --interface ...]"""
from __future__ import annotations
import argparse, logging, sys, threading, urllib.parse
from pathlib import Path
from .fetcher import Fetcher
from .providers import build_providers
from .media import Library
from .store import Store
from .server import App, make_server
from .isolation import LanGuard, check, IsolationError

def main():
    p=argparse.ArgumentParser(description='RELAY Island local server. Not an internet firewall.')
    p.add_argument('--port',type=int,default=8080)
    p.add_argument('--media',default='./media')
    p.add_argument('--data',default='./data')
    p.add_argument('--lan-host',help='Dedicated isolated LAN IPv4; do not use ordinary home Wi-Fi')
    p.add_argument('--interface',help='macOS LAN network interface, e.g. en7')
    p.add_argument('--demo',action='store_true',help='Use a fake search provider for local UI testing only')
    args=p.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if bool(args.lan_host)!=bool(args.interface):p.error('--lan-host and --interface must be provided together')
    guard=LanGuard(enabled=bool(args.lan_host),host=args.lan_host,interface=args.interface)
    if args.lan_host:
        try:check(args.lan_host,args.interface)
        except IsolationError as e:p.error(f'Network preflight failed: {e}')
    providers=build_providers()
    if args.demo:
        class Demo:
            id='wikipedia';label='Demo Wikipedia';host='example.org';experimental=False;note='Local fake results'
            def search(self,fetcher,query):return [{'title':f'Demo result for {query}','snippet':'Synthetic offline response for testing the workflow.','url':'https://en.wikipedia.org/wiki/Python_(programming_language)'}]
        providers={'wikipedia':Demo()}
    store=Store(Path(args.data),providers,Fetcher(),workers=2)
    lib=Library(Path(args.media))
    app=App(store,lib,guard=guard,lan_host=args.lan_host,port=args.port)
    admin=make_server('127.0.0.1',args.port,app,admin=True)
    app.listeners=['127.0.0.1:'+str(args.port)]
    lan=None
    if args.lan_host:
        lan=make_server(args.lan_host,args.port,app,admin=False)
        app.listeners.append(args.lan_host+':'+str(args.port))
        guard.start()
        threading.Thread(target=lan.serve_forever,daemon=True,name='LAN-server').start()
    threading.Thread(target=admin.serve_forever,daemon=True,name='admin-server').start()
    print('\nRELAY Island 0.5.2 — RUNNING',flush=True)
    print(f'Local interface: http://127.0.0.1:{args.port}/',flush=True)
    print(f'Unlock Mac approvals: http://127.0.0.1:{args.port}/admin/enter?key={urllib.parse.quote(app.admin_key)}',flush=True)
    if lan:print(f'Tablet interface: http://{args.lan_host}:{args.port}/',flush=True)
    print('WARNING: App preflight is NOT a firewall. Never use an internet-connected tablet Wi-Fi for isolation.\n',flush=True)
    try:
        while True:threading.Event().wait(3600)
    except KeyboardInterrupt:pass
    finally:
        guard.stop()
        admin.shutdown()
        if lan:lan.shutdown()
        store.pool.shutdown(wait=False,cancel_futures=True)
    return 0
if __name__=='__main__':sys.exit(main())
