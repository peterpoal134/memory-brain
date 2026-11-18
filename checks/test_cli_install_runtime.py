"""Portable CLI installation regressions with ephemeral loopback servers."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiaojiu_brain import __version__, cli
from xiaojiu_brain.config import Config


class StatusProbe(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.observed.append((self.path, self.headers.get('Authorization') is not None))
        payload = b'{"ok":true,"data":{"ready":true}}'
        if self.server.redirect:
            self.send_response(302)
            self.send_header('Location', self.server.redirect)
            payload = b''
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class CliInstallRuntime(unittest.TestCase):
    def start_probe(self, redirect=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), StatusProbe)
        server.observed, server.redirect = [], redirect
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()

        def stop_probe():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop_probe)
        return server

    def test_loopback_cli_request_ignores_proxy_configuration(self):
        actual = self.start_probe()
        proxy = self.start_probe()
        config = Config(port=actual.server_port, api_token='synthetic-cli-proxy-test')
        clean_environment = {'http_proxy': f'http://127.0.0.1:{proxy.server_port}'}
        with patch.dict(os.environ, clean_environment, clear=True), patch.object(request, '_opener', None):
            self.assertEqual(cli.api(config, '/v1/status'), {'ready': True})
        self.assertEqual(actual.observed, [('/v1/status', True)])
        self.assertEqual(proxy.observed, [])

    def test_loopback_cli_does_not_follow_redirects(self):
        sink = self.start_probe()
        actual = self.start_probe(redirect=f'http://127.0.0.1:{sink.server_port}/unrelated')
        config = Config(port=actual.server_port, api_token='synthetic-cli-redirect-test')
        with self.assertRaises(HTTPError) as response:
            cli.api(config, '/v1/status')
        self.assertEqual(response.exception.code, 302)
        response.exception.close()
        self.assertEqual(actual.observed, [('/v1/status', True)])
        self.assertEqual(sink.observed, [])

    def test_start_preserves_isolation_for_installed_callers(self):
        for isolated in (True, False):
            with self.subTest(isolated=isolated), tempfile.TemporaryDirectory(prefix='brain-cli-launch-') as folder:
                config_path = Path(folder) / 'synthetic.json'
                config = Config(data_dir=folder, api_token='synthetic-cli-launch-test')
                with (
                    patch.object(cli.Config, 'load', return_value=config),
                    patch.object(cli, 'sys', SimpleNamespace(executable=sys.executable, stderr=sys.stderr,
                                                              flags=SimpleNamespace(isolated=isolated))),
                    patch.object(cli, 'api', side_effect=[OSError('stopped'), {'version': __version__}]),
                    patch.object(cli.subprocess, 'Popen') as launch,
                    redirect_stdout(StringIO()),
                ):
                    self.assertEqual(cli.main(['--config', str(config_path), 'start']), 0)
                flags = ['-I'] if isolated else []
                self.assertEqual(launch.call_args.args[0], [
                    sys.executable, *flags, '-X', 'utf8', '-m', 'xiaojiu_brain', '--config', str(config_path.resolve()), 'serve',
                ])

    def test_start_reuses_only_matching_version(self):
        config = Config(api_token='synthetic-cli-version-test')
        with patch.object(cli.Config, 'load', return_value=config), patch.object(cli.subprocess, 'Popen') as launch:
            with patch.object(cli, 'api', return_value={'version': __version__}), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(['start']), 0)
            error = StringIO()
            with patch.object(cli, 'api', return_value={'version': '0.0.0'}), patch.object(sys, 'stderr', error):
                self.assertEqual(cli.main(['start']), 1)
            self.assertIn(__version__, error.getvalue())
            launch.assert_not_called()

    def test_failed_browser_dispatch_is_an_error_without_ticket(self):
        config = Config(api_token='synthetic-cli-browser-test')
        ticket = 'synthetic-one-use-ticket'
        browser_failure = RuntimeError('browser returned ' + ticket)
        for outcome in (False, browser_failure):
            with self.subTest(failed_by=type(outcome).__name__), patch.object(cli, 'api', return_value={'ticket': ticket}):
                settings = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with patch.object(cli.webbrowser, 'open', **settings):
                    with self.assertRaisesRegex(RuntimeError, '未能打开默认浏览器') as response:
                        cli.open_panel(config)
                self.assertNotIn(ticket, str(response.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
