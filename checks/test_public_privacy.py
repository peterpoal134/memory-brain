"""Regression fixtures for the public package's file and content checks."""

from io import BytesIO
from pathlib import Path
import runpy
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
validate_content = runpy.run_path(str(ROOT / 'scripts/build_plugin_release.py'))['validate_content']


class PublicPrivacy(unittest.TestCase):
    def test_private_file_paths_fail(self):
        for name in ('.env.local', '.credentials-client', 'assets/.private/info.json',
                     'memory.sqlite3', 'memory.db-wal', 'auth.dpapi', 'service.log.1',
                     'chat.jsonl', 'backups/file.json'):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Excluded'):
                validate_content(name, b'', ())

    def test_database_disguised_as_asset_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'Embedded database'):
            validate_content('static/asset.bin', b'SQLite format 3' + b'\x00', ())

    def test_unlabelled_provider_signature_and_literal_key_fail(self):
        for value in (b'gh' + b'p_' + b'X'*36,
                      b'{"api_key":"' + b'SampleFixture1_'*3 + b'"}',
                      b'client_secret = "' + b'SampleFixture2_'*3 + b'"'):
            with self.assertRaisesRegex(RuntimeError, 'Credential signature'):
                validate_content('example.json', value, ())

    def test_absolute_profile_paths_fail_even_off_the_build_machine(self):
        for value in (br'C:\Users\Fixture Local\private\config.json',
                      b'/home/fixture-local/private/config.json',
                      '/Users/测试账号/private/config.json'.encode()):
            with self.assertRaisesRegex(RuntimeError, 'user-profile path'):
                validate_content('docs/example.md', value, ())

    def test_public_placeholders_remain_valid(self):
        validate_content('docs/example.md', br'C:\Users\username\public\readme.md', ())
        validate_content('docs/example.md', b'{"api_key":"your-key"}', ())

    def test_shipped_wheel_is_scanned_recursively(self):
        payload = BytesIO()
        with ZipFile(payload, 'w') as wheel:
            wheel.writestr('example/config.json', b'{"api_token":"' + b'SampleFixture3_'*3 + b'"}')
        with self.assertRaisesRegex(RuntimeError, 'Credential signature'):
            validate_content('wheels/example.whl', payload.getvalue(), ())


if __name__ == '__main__':
    unittest.main()
