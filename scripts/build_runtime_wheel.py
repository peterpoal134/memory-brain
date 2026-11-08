"""Build this project's pure-Python runtime wheel using only the stdlib.

This intentionally supports the project's fixed metadata and package layout,
not arbitrary build backends. It never imports the staged package, executes its
setup code, invokes pip, downloads tools, or changes staged source bytes.
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile


PACKAGE = 'xiaojiu_brain'
STATIC_SUFFIXES = {'.html', '.css', '.js', '.svg', '.bin', '.json', '.md'}
STATIC_DIRS = {'icons', 'cortex', 'vendor'}
FORBIDDEN_PARTS = {'private', 'data', 'artifacts', 'dist', 'node_modules', '.venv', '__pycache__', '.git'}
VERSION_PATTERN = re.compile(r'[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?\Z')


class WheelBuildError(ValueError):
    """A safe fixed-category error, never containing source text or secrets."""


def fail(code):
    raise WheelBuildError(code)


def reject_symlink_components(path):
    current = Path(os.path.abspath(path))
    for candidate in (current, *current.parents):
        if candidate.is_symlink():
            fail('symlink_refused')


def source_file(source, relative):
    parts = PurePosixPath(relative)
    if parts.is_absolute() or '..' in parts.parts or '\\' in relative:
        fail('source_path_escape')
    result = source.joinpath(*parts.parts)
    reject_symlink_components(result)
    if not result.is_file() or not result.resolve().is_relative_to(source):
        fail('required_public_source_missing')
    return result


def _section(text, name):
    match = re.search(r'(?m)^\s*\[' + re.escape(name) + r'\]\s*(?:#.*)?$', text)
    if not match:
        return ''
    tail = text[match.end():]
    end = re.search(r'(?m)^\s*\[', tail)
    return tail[:end.start()] if end else tail


def _literal_fields(section, requested):
    """Python 3.10 fallback for fixed TOML string/list fields, with no eval.

    Unknown TOML tables/inline dictionaries are not interpreted. Unsupported
    literal syntax fails closed instead of executing a backend or config code.
    """
    starts = list(re.finditer(r'(?m)^\s*([A-Za-z0-9_-]+)\s*=\s*', section))
    result = {}
    for index, match in enumerate(starts):
        name = match.group(1)
        if name not in requested:
            continue
        if name in result:
            fail('duplicate_project_metadata')
        end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
        try:
            result[name] = ast.literal_eval(section[match.end():end].strip())
        except (SyntaxError, ValueError):
            fail('unsupported_project_metadata_literal')
    return result


def read_project(source, *, use_tomllib=True):
    text = source_file(source, 'pyproject.toml').read_text(encoding='utf-8-sig')
    fields = {'name', 'version', 'description', 'requires-python', 'dependencies', 'readme'}
    parser = None
    if use_tomllib:
        try:
            import tomllib
            parser = tomllib
        except ImportError:
            pass
    if parser is not None:
        try:
            project = parser.loads(text)['project']
        except (KeyError, ValueError, TypeError):
            fail('invalid_project_metadata')
        if not isinstance(project, dict):
            fail('invalid_project_metadata')
        result = {key: project[key] for key in fields if key in project}
        result['scripts'] = project.get('scripts', {})
    else:
        result = _literal_fields(_section(text, 'project'), fields)
        result['scripts'] = _literal_fields(_section(text, 'project.scripts'), {'xiaojiu-brain'})
    if result.get('name') != 'xiaojiu-brain':
        fail('unexpected_project_name')
    version = result.get('version')
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        fail('unsupported_project_version')
    for key in ('requires-python', 'description'):
        value = result.get(key, '' if key == 'description' else None)
        if not isinstance(value, str) or (key == 'requires-python' and not value) or re.search(r'[\x00-\x1f\x7f]', value):
            fail('invalid_project_metadata_header')
        result[key] = value
    dependencies = result.get('dependencies')
    if not isinstance(dependencies, list) or any(not isinstance(value, str) or not value or re.search(r'[\x00-\x1f\x7f]', value) for value in dependencies):
        fail('invalid_project_dependencies')
    # These are the declared runtime dependencies, never build-system tools.
    for value in dependencies:
        match = re.match(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(?=\s|[<>=!~;\[]|$)', value)
        if not match or match[1].lower() not in {'numpy', 'tzdata'} or '@' in value:
            fail('unsupported_runtime_dependency')
    if result.get('scripts') != {'xiaojiu-brain': 'xiaojiu_brain.cli:main'}:
        fail('unsupported_project_entry_points')
    if result.get('readme', 'README.md') != 'README.md':
        fail('unsupported_project_readme')
    try:
        tree = ast.parse(source_file(source, PACKAGE + '/__init__.py').read_text(encoding='utf-8-sig'))
        versions = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets):
                versions.append(ast.literal_eval(node.value))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == '__version__':
                versions.append(ast.literal_eval(node.value))
    except (SyntaxError, ValueError):
        fail('version_must_be_a_literal')
    if versions != [version]:
        fail('package_version_mismatch')
    return result


def public_package_files(source):
    package = source / PACKAGE
    if not package.is_dir():
        fail('package_directory_missing')
    reject_symlink_components(package)
    result = {}
    for directory, dirs, names in os.walk(package, followlinks=False):
        base = Path(directory)
        for name in [*dirs, *names]:
            if (base / name).is_symlink():
                fail('symlink_refused')
        relative = base.relative_to(package)
        if relative == Path('.'):
            dirs[:] = [name for name in dirs if name == 'static']
            selected = [name for name in names if name.endswith('.py') and not name.startswith('.')]
        elif relative == Path('static'):
            dirs[:] = [name for name in dirs if name in STATIC_DIRS]
            selected = [name for name in names if Path(name).suffix in STATIC_SUFFIXES and not name.startswith('.')]
        elif relative.parts[0] == 'static' and len(relative.parts) == 2 and relative.parts[1] in STATIC_DIRS:
            dirs[:] = []
            selected = [name for name in names if Path(name).suffix in STATIC_SUFFIXES and not name.startswith('.')]
        else:
            dirs[:] = []
            selected = []
        for name in selected:
            filename = base / name
            arcname = filename.relative_to(source).as_posix()
            if any(part in FORBIDDEN_PARTS for part in PurePosixPath(arcname).parts) or name == 'brain-fallback.png':
                fail('private_or_unsupported_package_member')
            result[arcname] = source_file(source, arcname).read_bytes()
    if PACKAGE + '/__init__.py' not in result:
        fail('package_init_missing')
    return result


def _metadata(project, readme):
    fields = [('Metadata-Version', '2.4'), ('Name', project['name']), ('Version', project['version'])]
    if project['description']:
        fields.append(('Summary', project['description']))
    fields.extend([('Requires-Python', project['requires-python']), ('Description-Content-Type', 'text/markdown')])
    fields.extend(('Requires-Dist', value) for value in project['dependencies'])
    fields.extend([('License-File', 'LICENSE'), ('License-File', 'NOTICE.md')])
    return ('\n'.join(key + ': ' + value for key, value in fields) + '\n\n').encode('utf8') + readme


def build(source, out):
    reject_symlink_components(source)
    reject_symlink_components(out)
    source = Path(source).resolve()
    out = Path(out).resolve()
    if not source.is_dir():
        fail('source_directory_missing')
    if out.is_relative_to(source):
        fail('output_must_be_outside_source')
    project = read_project(source)
    members = public_package_files(source)
    dist_info = 'xiaojiu_brain-' + project['version'] + '.dist-info'
    members[dist_info + '/METADATA'] = _metadata(project, source_file(source, 'README.md').read_bytes())
    members[dist_info + '/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: memory-brain-runtime-wheel (stdlib)\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    members[dist_info + '/entry_points.txt'] = b'[console_scripts]\nxiaojiu-brain = xiaojiu_brain.cli:main\n'
    members[dist_info + '/top_level.txt'] = b'xiaojiu_brain\n'
    for name in ('LICENSE', 'NOTICE.md'):
        members[dist_info + '/licenses/' + name] = source_file(source, name).read_bytes()
    record_name = dist_info + '/RECORD'
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, content in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode('ascii')
        writer.writerow((name, 'sha256=' + digest, len(content)))
    writer.writerow((record_name, '', ''))
    members[record_name] = record.getvalue().encode('utf8')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(members.items()):
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name:
                fail('archive_path_escape')
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    blob = buffer.getvalue()
    filename = 'xiaojiu_brain-' + project['version'] + '-py3-none-any.whl'
    out.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(out)
    destination = out / filename
    if destination.is_symlink():
        fail('symlink_refused')
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != blob:
            fail('existing_wheel_content_differs')
    else:
        descriptor, temporary = tempfile.mkstemp(prefix='.runtime-wheel-', dir=out)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.is_symlink() or destination.read_bytes() != blob:
                    fail('existing_wheel_content_differs')
        finally:
            temporary.unlink(missing_ok=True)
    return {'ok': True, 'wheel': str(destination), 'filename': filename,
            'sha256': hashlib.sha256(blob).hexdigest(), 'files': len(members)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build the staged Memory Brain wheel without build tools or network access.')
    parser.add_argument('--source', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args.source, args.out)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (WheelBuildError, OSError, UnicodeError, zipfile.BadZipFile):
        print(json.dumps({'ok': False, 'error': 'runtime_wheel_build_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
