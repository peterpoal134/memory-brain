"""Synthetic local-only stability checks; no model keys or private data required.

Run: python -I checks/test_transport_lifecycle.py
Optional BASELINE points to an old source root (or its xiaojiu_brain directory).
Without it, only the actual repository candidate is tested; no old-version
comparison is claimed. This test writes no result files.
"""
from pathlib import Path
import importlib, importlib.util, json, os, tempfile, threading, time, unittest, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
candidate=importlib.import_module('xiaojiu_brain.transport')
baseline=None
if os.environ.get('BASELINE'):
    directory=Path(os.environ['BASELINE']).resolve()
    if not (directory/'__init__.py').is_file():directory=directory/'xiaojiu_brain'
    if not (directory/'__init__.py').is_file():raise RuntimeError('BASELINE must contain the old Python package')
    spec=importlib.util.spec_from_file_location('optional_baseline',directory/'__init__.py',submodule_search_locations=[str(directory)])
    package=importlib.util.module_from_spec(spec);sys.modules[spec.name]=package;spec.loader.exec_module(package)
    baseline=importlib.import_module('optional_baseline.transport')

BODY=json.dumps({'synthetic':'x'*70000}).encode()

class Stub(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        mode=self.path.lstrip('/');self.send_response(200)
        if mode in ('fixed','normal','stream-fixed'):self.send_header('Content-Length',str(len(BODY)))
        elif mode in ('chunked','line','stream-chunked'):self.send_header('Transfer-Encoding','chunked')
        elif mode=='declared':self.send_header('Content-Length',str(1024*1024*1024))
        self.send_header('Connection','close');self.end_headers();self.close_connection=True
        try:
            if mode=='declared':time.sleep(.25)
            elif mode=='line':self.wfile.write(b'0'*9000+b'\r\n\r\n')
            elif mode in ('chunked','stream-chunked'):
                self.wfile.write(hex(len(BODY))[2:].encode()+b'\r\n'+BODY+b'\r\n0\r\n\r\n')
            elif mode=='drip':
                for value in BODY[:20]:self.wfile.write(bytes([value]));self.wfile.flush();time.sleep(.04)
            else:self.wfile.write(BODY)
        except (OSError,ValueError):pass

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Stub);cls.server.daemon_threads=True
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join(1)
    def test_response_allowance_for_all_framings_with_optional_old_version_comparison(self):
        for framing in ('fixed','chunked','eof'):
            if baseline is not None:
                original=baseline.post_json(self.url+'/'+framing,{},total_timeout=2)
                self.assertEqual(len(original['synthetic']),70000)
            with self.assertRaises(candidate.UpstreamResponseTooLarge):
                candidate.post_json(self.url+'/'+framing,{},total_timeout=2,max_response_bytes=8192)
    def test_declared_giant_body_is_rejected_before_waiting_for_or_allocating_the_body(self):
        with self.assertRaises(candidate.UpstreamResponseTooLarge):
            candidate.post_json(self.url+'/declared',{},total_timeout=2)
    def test_normal_reply_and_socket_cleanup_after_success_and_limit_error(self):
        opened=[];original=candidate._open_socket
        def tracked(*args,**kwargs):
            sock=original(*args,**kwargs);opened.append(sock);return sock
        candidate._open_socket=tracked
        try:
            actual=candidate.post_json(self.url+'/normal',{},total_timeout=2)
            self.assertEqual(actual,json.loads(BODY))
            if baseline is not None:self.assertEqual(actual,baseline.post_json(self.url+'/normal',{},total_timeout=2))
            with self.assertRaises(candidate.UpstreamResponseTooLarge):
                candidate.post_json(self.url+'/fixed',{},total_timeout=2,max_response_bytes=8192)
        finally:candidate._open_socket=original
        self.assertTrue(opened);self.assertTrue(all(sock.fileno()==-1 for sock in opened))
    def test_excessive_chunk_header_is_rejected_without_full_body_accumulation(self):
        with self.assertRaisesRegex(candidate.UpstreamTransportError,'upstream_chunk_line_too_large'):
            candidate.post_json(self.url+'/line',{},total_timeout=2)
    def test_nonfinite_or_nonpositive_timeouts_cannot_disable_the_hard_deadline(self):
        for value in (float('inf'),float('nan'),0,-1):
            for field in ('total_timeout','read_timeout'):
                with self.assertRaisesRegex(candidate.UpstreamTransportError,'upstream_invalid_timeout'):
                    candidate.post_json(self.url+'/normal',{},**{field:value})
                with self.assertRaisesRegex(candidate.UpstreamTransportError,'upstream_invalid_timeout'):
                    list(candidate.stream_sse(self.url+'/stream-fixed',{},**{field:value}))
    def test_hard_total_timeout_survives_drip(self):
        started=time.monotonic()
        with self.assertRaises(candidate.UpstreamTimeout):candidate.post_json(self.url+'/drip',{},total_timeout=.15)
        self.assertLess(time.monotonic()-started,.6)
    def test_sse_fixed_chunked_and_eof_remain_streaming_with_bounded_output_chunks(self):
        for framing in ('stream-fixed','stream-chunked','stream-eof'):
            chunks=list(candidate.stream_sse(self.url+'/'+framing,{},total_timeout=2))
            self.assertEqual(b''.join(chunks),BODY);self.assertLessEqual(max(map(len,chunks)),65536)
    def test_maintenance_stop_budget_does_not_wait_on_the_database_lock(self):
        namespaces=['xiaojiu_brain'] if baseline is None else ['optional_baseline','xiaojiu_brain']
        with tempfile.TemporaryDirectory(prefix='memory-brain-stop-')as directory:
            for namespace in namespaces:
                appmod=importlib.import_module(namespace+'.app');storemod=importlib.import_module(namespace+'.storage')
                store=storemod.Store(Path(directory)/(namespace+'.db'))
                held=threading.Event();release=threading.Event()
                def maintenance():
                    with store.lock:held.set();release.wait(10)
                maintenance_thread=threading.Thread(target=maintenance,daemon=True);maintenance_thread.start();self.assertTrue(held.wait(1))
                app=appmod.BrainApp.__new__(appmod.BrainApp);app.store=store;app._stop=threading.Event();app._maintenance_thread=maintenance_thread
                app.life=SimpleNamespace(stop=lambda:True);app.worker=SimpleNamespace(stop=lambda:True)
                outcome=[];finished=threading.Event()
                def close():outcome.append(app.close());finished.set()
                closer=threading.Thread(target=close,daemon=True);closer.start()
                try:
                    did_finish=finished.wait(2.8)
                    if namespace=='optional_baseline':self.assertFalse(did_finish)
                    else:
                        self.assertTrue(did_finish);self.assertEqual(outcome,[False])
                        self.assertEqual(store.conn.execute('SELECT 1').fetchone()[0],1)
                finally:release.set();maintenance_thread.join(1);closer.join(1)
                if namespace=='xiaojiu_brain':self.assertTrue(app.close())
    def test_background_channels_recover_after_one_transient_exception_and_stop(self):
        Worker=importlib.import_module('xiaojiu_brain.derivation').DerivationWorker
        worker=Worker(None,interval=.05,embedding_interval=.02);counts={'derive':0,'embed':0}
        def run(channel):
            counts[channel]+=1
            if counts[channel]==1:raise OSError('synthetic_transient_io')
            return worker._publish(channel,worker._result())
        worker.run_derivation_once=lambda:run('derive');worker.run_embedding_once=lambda:run('embed')
        worker.start();deadline=time.monotonic()+1
        while min(counts.values())<2 and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(worker.stop(timeout=.5));self.assertGreaterEqual(min(counts.values()),2)
        self.assertFalse(worker._thread.is_alive());self.assertFalse(worker._embedding_thread.is_alive())

if __name__=='__main__':unittest.main(verbosity=2)
