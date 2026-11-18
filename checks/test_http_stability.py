"""Synthetic real-socket checks. No private configuration or model calls."""
from pathlib import Path
import argparse,importlib,json,socket,sys,tempfile,threading,time,urllib.request,urllib.error
from types import SimpleNamespace

parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);parser.add_argument('--baseline',action='store_true');args=parser.parse_args()
sys.path.insert(0,str(Path(args.source).resolve()));api=importlib.import_module('xiaojiu_brain.api')
TOKEN='synthetic-http-stability-token-00000000'
checks=[]
class ProbeServer(api.BrainServer):
    request_socket_timeout=.25
    request_body_timeout=.35
    max_request_threads=2
    def handle_error(self,request,address):self.errors.append(type(sys.exception()).__name__)
class App:
    def __init__(self,folder):self.data_dir=Path(folder);self.config=SimpleNamespace(api_token=TOKEN);self.owner='synthetic';self.build_id='test'
    def status(self):return {'ready':True}
    def ingest(self,body):return {'accepted':True}
def record(name,result):checks.append({'name':name,'passed':bool(result)})
def connect(server):
    s=socket.create_connection(server.server_address,timeout=1);s.settimeout(1);return s
def health(server):
    with urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port)+'/health',timeout=1) as response:return response.status
def start(folder):
    server=ProbeServer(('127.0.0.1',0),App(folder));server.errors=[]
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True);thread.start()
    return server,thread
def cleanup(server,thread,connections):
    for s in connections:
        try:s.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        s.close()
    server.shutdown();server.server_close();thread.join(1)
def partial(server):
    s=connect(server);s.sendall((f'POST /v1/ingest HTTP/1.0\r\nHost: 127.0.0.1:{server.server_port}\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 100\r\n\r\n'+ '{').encode());return s

with tempfile.TemporaryDirectory(prefix='memory-http-stability-') as folder:
    server,thread=start(folder);connections=[]
    try:
        record('normal health works',health(server)==200)
        first=partial(server);connections.append(first)
        second=partial(server);connections.append(second)
        time.sleep(.08)
        third=connect(server);connections.append(third)
        try:
            third.sendall(b'GET /health HTTP/1.0\r\n\r\n')
            response=third.recv(4096)
        except OSError:
            response=b''
        record('excess concurrent clients receive bounded 503',response.startswith(b'HTTP/1.0 503'))
        time.sleep(.45)
        try:body_reply=first.recv(4096)
        except socket.timeout:body_reply=b''
        record('abandoned body releases handler and returns 408',body_reply.startswith(b'HTTP/1.0 408'))
        record('request capacity recovers after body timeout',health(server)==200)
    finally:cleanup(server,thread,connections)

    server,thread=start(folder);connections=[]
    try:
        # Saturate the slots without depending on request-thread scheduling.
        for _ in range(server.max_request_threads):
            assert server._request_slots.acquire(blocking=False)
        waiting=threading.Event()
        actions=[]
        get_request=server.get_request
        def observed_accept():
            accepted,address=get_request()
            def receive(size):
                actions.append('receive')
                waiting.set()
                return accepted.recv(size)
            def reply(data):
                actions.append('reply')
                return accepted.sendall(data)
            return SimpleNamespace(settimeout=accepted.settimeout,recv=receive,sendall=reply,
                                   shutdown=accepted.shutdown,close=accepted.close),address
        server.get_request=observed_accept
        delayed=connect(server);connections.append(delayed)
        # Send only once the server has started its drain, while the real
        # socket is still open. A send-before-drain baseline fails the order
        # check even on Windows runs where the reset does not happen.
        ready=waiting.wait(1)
        response=b''
        if ready:
            try:
                delayed.sendall(b'GET /health HTTP/1.0\r\n\r\n')
                response=delayed.recv(4096)
            except OSError:
                pass
        record('accepted delayed GET is drained before replying 503',
               ready and actions==['receive','reply'] and response.startswith(b'HTTP/1.0 503'))
        silent=connect(server);connections.append(silent)
        started=time.monotonic()
        try:response=silent.recv(4096)
        except OSError:response=b''
        record('overload drain is bounded when the client sends no data',
               response.startswith(b'HTTP/1.0 503') and time.monotonic()-started<.4)
    finally:cleanup(server,thread,connections)

    server,thread=start(folder);connections=[]
    try:
        s=partial(server);connections.append(s)
        def drip():
            for _ in range(22):
                time.sleep(.04)
                try:s.sendall(b' ')
                except OSError:break
        writer=threading.Thread(target=drip,daemon=True);writer.start();started=time.monotonic()
        try:response=s.recv(4096)
        except socket.timeout:response=b''
        elapsed=time.monotonic()-started
        record('body has total deadline despite continuously arriving bytes',response.startswith(b'HTTP/1.0 408') and elapsed<.9)
        writer.join(1)
    finally:cleanup(server,thread,connections)

    server,thread=start(folder);connections=[]
    try:
        s=connect(server);connections.append(s);s.sendall(b'GET /hea')
        time.sleep(.4)
        try:response=s.recv(4096)
        except socket.timeout:response=None
        record('idle incomplete headers are closed',response==b'')
        record('server remains usable after idle header',health(server)==200)
        s2=partial(server);connections.append(s2);s2.shutdown(socket.SHUT_WR)
        record('truncated body returns explicit 400',s2.recv(4096).startswith(b'HTTP/1.0 400'))
        time.sleep(.08);record('disconnect and read failures do not escape handler',not server.errors)
    finally:cleanup(server,thread,connections)

    for target in ('headers','body'):
        handler=object.__new__(api.Handler);handler.close_connection=False;handler._discard_rejected_body=lambda:None
        def reset(*args,**kwargs):raise ConnectionAbortedError('synthetic client disconnect')
        handler._headers=reset if target=='headers' else lambda *args,**kwargs:None
        handler.wfile=SimpleNamespace(write=reset)
        escaped=False
        try:handler._bytes(200,b'{}')
        except OSError:escaped=True
        record('disconnect during '+target+' write does not trigger a second response',not escaped and handler.close_connection)

result={'checks':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'baseline':args.baseline,'real_model_calls':0,'production_mutations':0}
print(json.dumps(result));sys.exit(0 if args.baseline or not result['failed'] else 1)
