"""Bind a hosted 0.2.10 rebuild to a locally reviewed distribution."""

from argparse import ArgumentParser
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import ZipFile
import json
import re


ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.2.10'
WHEEL = f'wheels/xiaojiu_brain-{VERSION}-py3-none-any.whl'
PREFIX = f'memory-brain-dsh-{VERSION}/'
ARCHIVE = ROOT / 'dist' / f'memory-brain-dsh-{VERSION}-public.zip'
REVIEW = ROOT / 'checks' / f'release-inputs-{VERSION}.json'
GENERATED = {'PUBLIC-RELEASE.json', 'SHA256SUMS.json', WHEEL}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return sha256(data).hexdigest()


def members(archive):
    require(archive.testzip() is None, 'Archive member checksum mismatch')
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    require(len(names) == len(set(names)), 'Archive has duplicate members')
    return {name: archive.read(name) for name in names}


def allowed_build_ids(files):
    kernel = {
        name.removeprefix('xiaojiu_brain/'): data for name, data in files.items()
        if name.startswith('xiaojiu_brain/') and PurePosixPath(name).suffix in {'.py', '.html', '.css', '.js'}
    }
    results = []
    # The kernel hashes native relative path spellings, so Windows and Linux can differ.
    for path_class in (PurePosixPath, PureWindowsPath):
        signature = sha256()
        for name in sorted(kernel, key=path_class):
            signature.update(str(path_class(name)).encode('utf8'))
            signature.update(kernel[name])
        results.append(signature.hexdigest()[:16])
    return sorted(set(results))


def inspect():
    with ZipFile(ARCHIVE) as archive:
        packed = members(archive)
    require(all(name.startswith(PREFIX) for name in packed), 'Unexpected archive root')
    files = {name.removeprefix(PREFIX): data for name, data in packed.items()}
    require(all(name and PurePosixPath(name).as_posix() == name and '\\' not in name
                and '..' not in PurePosixPath(name).parts and not PurePosixPath(name).is_absolute()
                for name in files), 'Unexpected release member path')
    require(GENERATED <= files.keys(), 'Generated release files are missing')
    release = json.loads(files['PUBLIC-RELEASE.json'])
    require(all(release[key] == VERSION for key in ('version', 'plugin_version', 'kernel_version')),
            'Distribution versions differ')
    require(release['release_notes'] == f'docs/RELEASE-{VERSION}.md' and release['release_notes'] in files,
            'Wrong release notes')
    require(release['build_id'] in allowed_build_ids(files), 'Kernel build ID does not match archive source')
    context = release['context_management']
    require(context['default_enabled'] is True and context['trigger_turns'] == 12 and context['keep_complete_turns'] == 4,
            'Release no longer declares the original context defaults')
    setting = files['adapters/dsh/context-settings.mjs'].decode('utf8')
    require(re.search(r'\bcontextEnabled:\s*true,\s*contextTriggerTurns:\s*12,\s*contextKeepTurns:\s*4,', setting),
            'Runtime context defaults changed')
    expected_manifest = {name: digest(data) for name, data in files.items() if name != 'SHA256SUMS.json'}
    require(json.loads(files['SHA256SUMS.json']) == expected_manifest, 'Distribution manifest differs from archive')
    with ZipFile(BytesIO(files[WHEEL])) as wheel:
        kernel = members(wheel)
    source_hashes = {}
    for name, data in files.items():
        if name in GENERATED:
            continue
        require((ROOT / name).read_bytes() == data, 'Archive source differs from checkout: ' + name)
        source_hashes[name] = digest(data)
        if name.startswith('xiaojiu_brain/'):
            require(kernel.get(name) == data, 'Wheel source differs from archive: ' + name)
    expected = {
        'archive_files': sorted(files),
        'source_sha256': source_hashes,
        'version': VERSION,
        'wheel_members': {name: digest(data) for name, data in kernel.items()},
    }
    return files, expected


def main():
    parser = ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--freeze', action='store_true', help='record reviewed local archive contents before the hosted build')
    mode.add_argument('--prepare-promotion', action='store_true', help='attach only verified generated metadata and wheel')
    options = parser.parse_args()
    files, expected = inspect()
    if options.freeze:
        with REVIEW.open('x', encoding='utf8', newline='\n') as target:
            target.write(json.dumps(expected, ensure_ascii=True, indent=2, sort_keys=True) + '\n')
    else:
        require(json.loads(REVIEW.read_text(encoding='utf8')) == expected,
                'Hosted archive differs from the reviewed public distribution')
    archive_hash = digest(ARCHIVE.read_bytes())
    if options.prepare_promotion:
        for name in sorted(GENERATED):
            (ROOT / name).write_bytes(files[name])
        ARCHIVE.with_name(ARCHIVE.name + '.sha256').write_text(
            f'{archive_hash}  {ARCHIVE.name}\n', encoding='ascii', newline='\n')
    print(json.dumps({'version': VERSION, 'archive_sha256': archive_hash, 'members': len(files),
                      'reviewed_sources_match': True, 'wheel_content_matches': True,
                      'default_trigger_turns': 12, 'keep_complete_turns': 4}))


if __name__ == '__main__':
    main()
