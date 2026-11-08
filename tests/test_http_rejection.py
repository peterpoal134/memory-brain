"""Early HTTP rejection must be usable and bounded, without admitting data."""
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config

class RejectedBodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=BrainApp(Config(data_dir=self.tmp.name,api_token='synthetic-rejection-token-0123456789'),start_worker=False)
        self.server=BrainServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.app.close();self.tmp.cleanup()
    def connect(self,headers):
        sock=socket.create_connection(('127.0.0.1',self.server.server_port),timeout=2)
        request=f'POST /v1/ingest HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nContent-Type: application/json\r\nConnection: close\r\n'+headers+'\r\n'
        sock.sendall(request.encode('ascii'));return sock
    def response(self,sock):
        response=http.client.HTTPResponse(sock);response.begin()
        return response.status,json.loads(response.read())
    def test_delayed_small_body_gets_real_401_or_403_and_is_not_ingested(self):
        body=b'{"conversation_id":"fixture","text":"synthetic must not be admitted"}'
        for extra,status in [('',401),('Authorization: Bearer synthetic-rejection-token-0123456789\r\nOrigin: https://untrusted.invalid\r\n',403)]:
            with self.subTest(status=status):
                with self.connect(f'Content-Length: {len(body)}\r\n'+extra) as sock:
                    time.sleep(.02);sock.sendall(body)
                    code,value=self.response(sock);self.assertEqual(code,status);self.assertFalse(value['ok'])
        self.assertEqual(self.app.store.stats(self.app.owner)['messages'],0)
    def test_missing_or_oversized_rejected_body_cannot_extend_the_deadline(self):
        for length in (128,65537,2**31):
            with self.subTest(length=length):
                began=time.monotonic()
                with self.connect(f'Content-Length: {length}\r\n') as sock:
                    code,value=self.response(sock);self.assertEqual(code,401);self.assertFalse(value['ok'])
                self.assertLess(time.monotonic()-began,1)
        self.assertEqual(self.app.store.stats(self.app.owner)['messages'],0)
