"""Offline packaging checks; synthetic staged code, never the live application."""
from __future__ import annotations

import base64
import csv
from email.parser import BytesParser
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'build_runtime_wheel.py'
spec = importlib.util.spec_from_file_location('runtime_wheel_builder_for_tests', SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
VERSION = '0.2.0a17'
PROJECT = '''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "xiaojiu-brain"
version = "0.2.0a17"
description = "Synthetic runtime package"
requires-python = ">=3.10"
dependencies = [
  "numpy>=1.24", # Runtime dependencies, not build dependencies.
  "tzdata>=2024.1; platform_system == 'Windows'",
]
readme = "README.md"

[project.scripts]
xiaojiu-brain = "xiaojiu_brain.cli:main"
'''


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file() and not path.is_symlink()}


class RuntimeWheelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='memory-brain-stdlib-wheel-')
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source with spaces'
        self.source.mkdir()
        files = {
            'pyproject.toml': PROJECT,
            'README.md': '# Synthetic public runtime\nNo existing memories.\n',
            'LICENSE': 'Synthetic license notice.\n',
            'NOTICE.md': 'Synthetic attribution.\n',
            'xiaojiu_brain/__init__.py': "__version__ = '" + VERSION + "'\n",
            'xiaojiu_brain/cli.py': 'def main():\n    return 0\n',
            'xiaojiu_brain/app.py': "raise RuntimeError('source must not execute while building or importing init')\n",
            'xiaojiu_brain/static/panel.html': '<p>Synthetic panel</p>\n',
            'xiaojiu_brain/static/icons/brain.svg': '<svg/>\n',
            'xiaojiu_brain/static/cortex/mesh.bin': b'\x00\x01synthetic mesh\xff',
            'xiaojiu_brain/static/vendor/LICENSE.md': 'Synthetic vendor license\n',
        }
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode('utf8'))

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, out=None):
        return builder.build(self.source, out or self.root / 'wheels')

    def test_wheel_records_match_every_byte_and_private_files_are_not_packaged(self):
        excluded = ['private/config.json', 'data/brain.db', 'xiaojiu_brain/private/config.json',
                    'xiaojiu_brain/data/brain.db', 'xiaojiu_brain/__pycache__/cached.pyc',
                    'xiaojiu_brain/static/brain-fallback.png', 'xiaojiu_brain/static/unselected.sql',
                    'xiaojiu_brain/static/nested/private.json']
        for name in excluded:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'SYNTHETIC_EXCLUDED_PRIVATE_SENTINEL')
        # AST inspection must not execute even the package's initializer.
        init = self.source / 'xiaojiu_brain/__init__.py'
        init.write_text(init.read_text('utf8') + "raise RuntimeError('do not execute')\n", encoding='utf8')
        before = tree_bytes(self.source)
        result = self.build()
        self.assertTrue(result['ok'])
        self.assertEqual(tree_bytes(self.source), before)
        self.assertEqual(result['filename'], 'xiaojiu_brain-' + VERSION + '-py3-none-any.whl')
        with zipfile.ZipFile(result['wheel']) as archive:
            self.assertEqual(archive.testzip(), None)
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(result['files'], len(names))
            self.assertFalse(any(name in names for name in excluded))
            self.assertFalse(any(b'SYNTHETIC_EXCLUDED_PRIVATE_SENTINEL' in archive.read(name) for name in names))
            for name in names:
                self.assertFalse(name.startswith('/') or '\\' in name or '..' in Path(name).parts)
                if name.startswith('xiaojiu_brain/'):
                    self.assertEqual(archive.read(name), (self.source / name).read_bytes())
            record_name = next(name for name in names if name.endswith('.dist-info/RECORD'))
            records = list(csv.reader(io.StringIO(archive.read(record_name).decode('utf8'))))
            self.assertEqual({row[0] for row in records}, set(names))
            self.assertEqual(len(records), len(names))
            for name, digest, size in records:
                if name == record_name:
                    self.assertEqual((digest, size), ('', ''))
                else:
                    content = archive.read(name)
                    expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode('ascii')
                    self.assertEqual(digest, 'sha256=' + expected)
                    self.assertEqual(int(size), len(content))
            info = 'xiaojiu_brain-' + VERSION + '.dist-info/'
            metadata = BytesParser().parsebytes(archive.read(info + 'METADATA'))
            self.assertEqual(metadata['Name'], 'xiaojiu-brain')
            self.assertEqual(metadata['Version'], VERSION)
            self.assertEqual(metadata['Requires-Python'], '>=3.10')
            self.assertEqual(metadata.get_all('Requires-Dist'), ['numpy>=1.24', "tzdata>=2024.1; platform_system == 'Windows'"])
            self.assertNotIn('setuptools', '\n'.join(metadata.get_all('Requires-Dist')))
            self.assertEqual(metadata.get_all('License-File'), ['LICENSE', 'NOTICE.md'])
            self.assertEqual(archive.read(info + 'licenses/LICENSE'), (self.source / 'LICENSE').read_bytes())
            self.assertEqual(archive.read(info + 'licenses/NOTICE.md'), (self.source / 'NOTICE.md').read_bytes())
            self.assertIn(b'Root-Is-Purelib: true', archive.read(info + 'WHEEL'))
            self.assertIn(b'Tag: py3-none-any', archive.read(info + 'WHEEL'))
            self.assertIn(b'xiaojiu-brain = xiaojiu_brain.cli:main', archive.read(info + 'entry_points.txt'))

    def test_python_310_literal_parser_preserves_dependencies_and_markers(self):
        legacy = builder.read_project(self.source, use_tomllib=False)
        self.assertEqual(legacy, builder.read_project(self.source))
        self.assertEqual(legacy['dependencies'], ['numpy>=1.24', "tzdata>=2024.1; platform_system == 'Windows'"])

    def test_nonliteral_or_mismatched_version_never_executes_source(self):
        marker = self.root / 'must-not-be-created'
        init = self.source / 'xiaojiu_brain/__init__.py'
        init.write_text("__version__ = __import__('pathlib').Path(" + repr(str(marker)) + ").write_text('bad')\n", encoding='utf8')
        with self.assertRaisesRegex(builder.WheelBuildError, 'version_must_be_a_literal'):
            self.build()
        self.assertFalse(marker.exists())
        init.write_text("__version__ = '9.9.9'\n", encoding='utf8')
        with self.assertRaisesRegex(builder.WheelBuildError, 'package_version_mismatch'):
            self.build()
        self.assertFalse((self.root / 'wheels').exists())

    def test_symlink_source_and_destination_are_rejected_before_target_reads(self):
        linked = self.source / 'xiaojiu_brain/static/leak.json'
        outside = self.root / 'outside-private.txt'
        outside.write_text('SYNTHETIC_PRIVATE_OUTSIDE_STAGE', encoding='utf8')
        try:
            linked.symlink_to(outside)
        except OSError:
            # Windows may prohibit creating links; exercise the identical
            # filesystem predicate without skipping the rejection invariant.
            linked.write_bytes(b'unread')
            original = Path.is_symlink
            with mock.patch.object(Path, 'is_symlink', lambda path: path == linked or original(path)):
                with self.assertRaisesRegex(builder.WheelBuildError, 'symlink_refused'):
                    self.build()
        else:
            with self.assertRaisesRegex(builder.WheelBuildError, 'symlink_refused'):
                self.build()
        linked.unlink()
        out = self.root / 'linked-output'
        original = Path.is_symlink
        with mock.patch.object(Path, 'is_symlink', lambda path: path == out or original(path)):
            with self.assertRaisesRegex(builder.WheelBuildError, 'symlink_refused'):
                self.build(out)
        self.assertEqual(outside.read_text('utf8'), 'SYNTHETIC_PRIVATE_OUTSIDE_STAGE')

    def test_output_cannot_overwrite_source_and_rebuild_is_deterministic(self):
        before = tree_bytes(self.source)
        with self.assertRaisesRegex(builder.WheelBuildError, 'output_must_be_outside_source'):
            self.build(self.source / 'wheels')
        first = self.build()
        again = self.build()
        other = self.build(self.root / 'other-wheels')
        self.assertEqual(first, again)
        self.assertEqual(Path(first['wheel']).read_bytes(), Path(other['wheel']).read_bytes())
        self.assertEqual(tree_bytes(self.source), before)
        original_wheel = Path(first['wheel']).read_bytes()
        (self.source / 'xiaojiu_brain/cli.py').write_text('def main():\n    return 7\n', encoding='utf8')
        with self.assertRaisesRegex(builder.WheelBuildError, 'existing_wheel_content_differs'):
            self.build()
        self.assertEqual(Path(first['wheel']).read_bytes(), original_wheel)

    def test_metadata_cannot_inject_headers_or_redirect_readme_or_dependencies(self):
        project = self.source / 'pyproject.toml'
        cases = [
            (PROJECT.replace('requires-python = ">=3.10"', 'requires-python = ">=3.10\\nRequires-Dist: injected"'), 'invalid_project_metadata_header'),
            (PROJECT.replace('readme = "README.md"', 'readme = "../../private.txt"'), 'unsupported_project_readme'),
            (PROJECT.replace('numpy>=1.24', 'arbitrary-package @ https://untrusted.invalid/a.whl'), 'unsupported_runtime_dependency'),
            (PROJECT.replace('version = "0.2.0a17"', 'version = "../../escape"'), 'unsupported_project_version'),
        ]
        for content, code in cases:
            with self.subTest(code=code):
                project.write_text(content, encoding='utf8')
                with self.assertRaisesRegex(builder.WheelBuildError, code):
                    self.build()
        self.assertFalse((self.root / 'wheels').exists())

    def test_cli_builds_without_importing_the_staged_package(self):
        before = tree_bytes(self.source)
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--source', str(self.source), '--out', str(self.root / 'cli-wheels')],
                                capture_output=True, text=True, encoding='utf8', timeout=30, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['ok'])
        self.assertTrue(Path(data['wheel']).is_file())
        self.assertEqual(tree_bytes(self.source), before)

    def test_real_pip_installs_offline_into_temporary_target_and_init_imports_only_target(self):
        result = self.build()
        target = self.root / 'installed-target'
        env = os.environ.copy()
        for key in list(env):
            if key.upper().startswith('PIP_'):
                del env[key]
        env.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONUTF8='1')
        install = subprocess.run([sys.executable, '-I', '-m', 'pip', 'install', '--no-deps', '--no-index', '--no-compile',
                                  '--no-cache-dir', '--target', str(target), result['wheel']],
                                 capture_output=True, text=True, encoding='utf8', env=env, cwd=self.root, timeout=60)
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        code = '''import pathlib,sys
target=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(target))
import xiaojiu_brain
assert pathlib.Path(xiaojiu_brain.__file__).resolve().is_relative_to(target)
assert xiaojiu_brain.__version__ == '0.2.0a17'
assert 'xiaojiu_brain.app' not in sys.modules
assert (target/'xiaojiu_brain/static/cortex/mesh.bin').read_bytes() == b'\\x00\\x01synthetic mesh\\xff'
print('OFFLINE_TARGET_IMPORT_OK')
'''
        imported = subprocess.run([sys.executable, '-I', '-c', code, str(target)], capture_output=True, text=True,
                                  encoding='utf8', env=env, cwd=self.root, timeout=30)
        self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
        self.assertIn('OFFLINE_TARGET_IMPORT_OK', imported.stdout)
        self.assertFalse((self.source / 'xiaojiu_brain/__pycache__').exists())


if __name__ == '__main__':
    unittest.main()
