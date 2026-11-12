"""Verify a hosted rebuild against the locally reviewed public distribution.

Only public source digests are present. No credentials, private corpus or machine
configuration are needed; archive compression and the build timestamp may vary.
"""
from pathlib import Path
import hashlib, io, json, shutil, zipfile

root=Path(__file__).resolve().parents[1]
expected=json.loads((root/'checks/release-inputs-0.2.4.json').read_text('utf8'))
latest=json.loads((root/'artifacts/public-plugin-package/LATEST.json').read_text('utf8'))
archive=Path(latest['archive']).resolve(); stage=Path(latest['stage']).resolve()
assert archive.is_relative_to(root) and stage.is_relative_to(root)
digest=lambda data:hashlib.sha256(data).hexdigest()
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    prefix='memory-brain-dsh-0.2.4/'
    files={name[len(prefix):]:z.read(name) for name in z.namelist() if name.startswith(prefix) and not name.endswith('/')}
    assert set(files)==set(expected['archive_files']), 'Archive allowlist changed'
    for name,sha in expected['source_sha256'].items():
        assert digest(files[name])==sha, 'Source differs from reviewed input: '+name
    info=json.loads(files['PUBLIC-RELEASE.json'])
    assert info['version']==info['plugin_version']==info['kernel_version']=='0.2.4'
    assert info['build_id']==expected['kernel_build_id']
    manifest=json.loads(files['SHA256SUMS.json'])
    assert set(manifest)==set(files)-{'SHA256SUMS.json'}
    assert all(digest(files[name])==sha for name,sha in manifest.items())
    wheel=expected['kernel_wheel']
    with zipfile.ZipFile(io.BytesIO(files[wheel])) as w:
        assert w.testzip() is None
        wheel_files={name:digest(w.read(name)) for name in w.namelist() if not name.endswith('/')}
        assert wheel_files==expected['wheel_members'], 'Kernel wheel content changed'
for name in ('PUBLIC-RELEASE.json','SHA256SUMS.json',expected['kernel_wheel']):
    destination=root/name; destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(stage/name,destination)
checksum=archive.with_name(archive.name+'.sha256')
checksum.write_text(digest(archive.read_bytes())+'  '+archive.name+'\n',encoding='utf8')
print(json.dumps({'version':'0.2.4','reviewed_sources_match':True,'wheel_content_matches':True,
    'archive_sha256':digest(archive.read_bytes()),'files':len(files)}))
