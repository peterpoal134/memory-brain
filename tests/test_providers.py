"""Provider accounting/classification over a local HTTP server; no paid models."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from xiaojiu_brain.providers import (
    ChatProvider, ProviderError, OutputTruncatedError, InvalidJSONError,
    EmptyResponseError, ProviderTransportError, ProviderHTTPError,
    UsageRecordingError,
)


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.requests.append(request)
                status, payload, delay = self.server.replies.pop(0)
                if delay:time.sleep(delay)
                body = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.server.replies = []
        self.server.requests = []
        self.events = []
        self.provider = ChatProvider('deepseek-isolated-stub',
            f'http://127.0.0.1:{self.server.server_port}/v1', key='synthetic-private-key', timeout=1)
        self.provider.attempt_recorder = self.events.append

    def reply(self, content='{"documents":[]}', finish='stop', usage=None, status=200, delay=0):
        payload = {'choices':[{'finish_reason':finish, 'message':{'content':content,
                   'reasoning_content':'SYNTHETIC_REASONING_NOT_TO_LOG'}}]}
        if usage is not None:payload['usage'] = usage
        self.server.replies.append((status, payload, delay))

    def call(self, **kwargs):
        return self.provider.complete_json('SYNTHETIC_SYSTEM_PRIVATE', 'SYNTHETIC_USER_PRIVATE', **kwargs)

    def test_real_http_usage_and_generated_context_without_payload(self):
        self.reply(usage={'prompt_tokens':100, 'completion_tokens':8,
                         'prompt_cache_hit_tokens':64, 'prompt_cache_miss_tokens':36, 'total_tokens':108})
        self.assertEqual(self.call(max_tokens=2400, request_context={
            'run_id':'run-1', 'batch_id':'batch:2', 'job_ids':[3,4], 'mode':'derive',
            'message_count':2, 'system':'SYNTHETIC_SYSTEM_PRIVATE', 'token':'synthetic-private-key',
            'parent_batch_id':'unsafe private words'}), {'documents':[]})
        event = self.events[0]
        self.assertTrue(event['usage_known'])
        self.assertEqual(event['usage'], {'input_tokens':100,'output_tokens':8,
                         'cache_hit_tokens':64,'cache_miss_tokens':36,'total_tokens':108})
        self.assertEqual(event['request_context'], {'run_id':'run-1','batch_id':'batch:2',
                         'job_ids':[3,4],'mode':'derive','message_count':2})
        self.assertEqual(event['outcome'], 'success')
        self.assertIsNone(event['error'])
        self.assertGreaterEqual(event['elapsed_ms'], 0)
        self.assertGreater(event['started_at'], 0)
        self.assertTrue(event['attempt_id'])
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.provider.attempts, 1)
        self.assertTrue(self.provider.supports_request_context)
        self.assertEqual(self.server.requests[0]['thinking'], {'type':'disabled'})
        self.assertEqual(self.server.requests[0]['max_tokens'], 2400)
        serialized = json.dumps(event)
        for secret in ('SYNTHETIC_SYSTEM_PRIVATE','SYNTHETIC_USER_PRIVATE',
                       'synthetic-private-key','SYNTHETIC_REASONING_NOT_TO_LOG','http://'):
            self.assertNotIn(secret, serialized)

    def test_length_is_rejected_even_with_valid_json_and_never_retried(self):
        for reason in ('length', 'max_tokens', 'max_output_tokens'):
            with self.subTest(reason=reason):
                self.reply(finish=reason, usage={'prompt_tokens':10,'completion_tokens':2400})
                before = len(self.server.requests)
                with self.assertRaises(OutputTruncatedError) as caught:self.call()
                self.assertIsInstance(caught.exception, ProviderError)
                self.assertEqual(caught.exception.category, 'output_truncated')
                self.assertEqual(caught.exception.finish_reason, reason)
                self.assertEqual(len(self.server.requests), before+1)
                self.assertEqual(self.events[-1]['outcome'], 'output_truncated')
                self.assertEqual(self.events[-1]['usage']['output_tokens'], 2400)
        self.assertEqual(len(self.events), 3)

    def test_truncation_precedes_empty_or_invalid_json(self):
        for content in (None, '', '{'):
            self.reply(content=content, finish='length')
            with self.assertRaises(OutputTruncatedError):self.call()
        self.assertEqual(len(self.events), 3)

    def test_empty_visible_content_cannot_be_replaced_by_reasoning(self):
        for content in (None, '', '   '):
            self.reply(content=content)
            with self.assertRaises(EmptyResponseError):self.call()
            self.assertEqual(self.events[-1]['outcome'], 'empty')
            self.assertIsNone(self.events[-1]['usage'])
            self.assertFalse(self.events[-1]['usage_known'])

    def test_malformed_or_non_object_json_records_usage_before_rejection(self):
        for content in ('{"documents":', '[]', 'null', 42):
            self.reply(content=content, usage={'prompt_tokens':40,'completion_tokens':5})
            with self.assertRaises(InvalidJSONError):self.call()
            self.assertEqual(self.events[-1]['outcome'], 'invalid_json')
            self.assertEqual(self.events[-1]['usage']['input_tokens'], 40)
        self.assertEqual(len(self.events), 4)
        self.assertEqual(self.provider.usage['prompt_tokens'], 160)

    def test_openai_usage_aliases_and_unknown_values(self):
        self.reply(content='```json\n{"documents":[]}\n```',
                   usage={'input_tokens':30,'output_tokens':4,'input_tokens_details':{'cached_tokens':20}})
        self.assertEqual(self.call(), {'documents':[]})
        self.assertEqual(self.events[-1]['usage'], {'input_tokens':30,'output_tokens':4,
                         'cache_hit_tokens':20,'cache_miss_tokens':10,'total_tokens':None})
        self.reply(usage={'prompt_tokens':True,'completion_tokens':'12','arbitrary_private':'SECRET'})
        self.call()
        self.assertIsNone(self.events[-1]['usage'])
        self.assertNotIn('SECRET', json.dumps(self.events[-1]))

    def test_http_failure_records_unknown_cost_and_safe_status(self):
        self.server.replies.append((429, {'error':{'message':'PRIVATE_PROVIDER_ERROR'}}, 0))
        with self.assertRaises(ProviderHTTPError) as caught:self.call()
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.category, 'provider_http')
        self.assertEqual(self.events[0]['http_status'], 429)
        self.assertEqual(self.events[0]['outcome'], 'provider_http')
        self.assertIsNone(self.events[0]['usage'])
        self.assertNotIn('PRIVATE_PROVIDER_ERROR', str(caught.exception)+json.dumps(self.events))
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.provider.attempts, 1)

    def test_timeout_has_one_attempt_and_unknown_usage(self):
        self.provider.timeout = .08
        self.reply(delay=.4)
        started = time.monotonic()
        with self.assertRaises(ProviderTransportError):self.call()
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'transport')
        self.assertIsNone(self.events[0]['usage'])
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.provider.attempts, 1)

    def test_invalid_provider_envelope_is_safe_provider_error(self):
        self.server.replies.append((200, {'private':'PRIVATE_RESPONSE'}, 0))
        with self.assertRaises(ProviderError) as caught:self.call()
        self.assertEqual(caught.exception.category, 'provider_error')
        self.assertEqual(self.events[0]['outcome'], 'provider_error')
        self.assertNotIn('PRIVATE_RESPONSE', str(caught.exception)+json.dumps(self.events))

    def test_failed_ledger_is_visible_and_called_once(self):
        attempts = []
        def broken(event):
            attempts.append(event)
            raise OSError('PRIVATE_FILESYSTEM_LOCATION')
        self.provider.attempt_recorder = broken
        self.reply()
        with self.assertRaises(UsageRecordingError) as caught:self.call()
        self.assertEqual(caught.exception.original_outcome, 'success')
        self.assertIsNone(caught.exception.original_error_category)
        self.assertEqual(len(attempts), 1)
        self.assertNotIn('PRIVATE_FILESYSTEM_LOCATION', str(caught.exception))
        self.reply(content='{')
        with self.assertRaises(UsageRecordingError) as caught:self.call()
        self.assertEqual(caught.exception.original_outcome, 'invalid_json')
        self.assertEqual(caught.exception.original_error_category, 'invalid_json')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.server.requests), 2)

    def test_old_call_signature_without_recorder_remains_usable(self):
        self.provider.attempt_recorder = None
        self.reply()
        self.assertEqual(self.provider.complete_json('system','user',2400), {'documents':[]})
        self.assertEqual(self.provider.calls, 1)
        self.assertIsNone(self.provider.last_error)


if __name__ == '__main__':unittest.main()
