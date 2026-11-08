"""Build a complete, positively selected public DSH release, never zip a worktree.

No private configuration, data, evaluation reports or host runtime
are included. The kernel wheel is built from this same sanitized source stage.
"""
from pathlib import Path
import argparse,ast,datetime,hashlib,io,json,os,platform,re,runpy,shutil,subprocess,sys,tempfile,zipfile

PUBLIC_DOCS=('BACKGROUND-DERIVATION.md','BRANDING.md','CONTRACT.md','DAILY-STATE-RULES.md',
    'LIFE-LAYER.md','MEMORY-MAP.md','OBSERVATORY-API.md','PROFILE-IMPORTANT.md','DSH-INSTALL.md',
    'SEMANTIC-MEMORY.md','TIME-SEMANTICS.md','DSH-DESKTOP-COMPAT.md','DSH-FIRST-RUN.md')
PUBLIC_SCRIPTS=('install.ps1','start-local.ps1','open-panel.ps1','connect-dsh.ps1','register-dsh.mjs',
    'verify-package.ps1','build_brain.mjs','build_fallback.py','build_plugin_release.py','build_release.py','dsh_plan.py',
    'dsh-request-hook.mjs','dsh-compat.ps1','__init__.py','verify_migration.py','build_runtime_wheel.py','dsh-compat-report.mjs')
ROOT_FILES=('README.md','LICENSE','NOTICE.md','pyproject.toml','MANIFEST.in','gitignore.template','package.json','cordis.patch.yml',
    'install.cmd','open-panel.cmd','stop-brain.cmd','connect-dsh.cmd','remove-dsh.cmd','dsh-compat-check.cmd')
FORBIDDEN_NAMES={'private','data','artifacts','dist','node_modules','.venv','__pycache__','.git'}
FORBIDDEN_SUFFIXES={'.db','.sqlite','.sqlite3','.dpapi','.log','.zstd','.pyc','.pyo','.pem','.key','.exe','.dll'}
LOCAL_IDENTIFIERS=()

def public_sources(root):
    files={root/name for name in ROOT_FILES}
    files.update(root/'docs'/name for name in PUBLIC_DOCS)
    files.update(root/'scripts'/name for name in PUBLIC_SCRIPTS)
    files.update(root/'compat'/name for name in ('dsh-request-messages.json','dsh-request-messages.patch'))
    dependency_manifest=root/'compat/runtime-dependencies.json'
    dependencies=json.loads(dependency_manifest.read_text('utf8'))
    if dependencies.get('format')!=1 or len(dependencies.get('files',[]))!=1:raise RuntimeError('Unexpected bundled runtime dependencies')
    files.add(dependency_manifest)
    for entry in dependencies['files']:
        name=entry.get('filename','')
        if name!='tzdata-2026.6-py2.py3-none-any.whl':raise RuntimeError('Unexpected bundled dependency filename')
        wheel=root/'wheels'/name
        if wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest()!=entry.get('sha256'):raise RuntimeError('Bundled dependency checksum mismatch')
        files.add(wheel)
    for directory,extensions in [('xiaojiu_brain',{'.py','.html','.css','.js','.svg','.bin','.json','.md'}),
        ('adapters/dsh',{'.mjs','.md','.json'}),('client',{'.js'}),('tests',{'.py','.mjs'}),('frontend',{'.js','.json','.md'})]:
        for p in (root/directory).rglob('*'):
            if p.is_file() and p.suffix in extensions and not any(part in FORBIDDEN_NAMES or part.startswith('run-') for part in p.relative_to(root).parts):files.add(p)
    missing=[p.relative_to(root).as_posix() for p in files if not p.is_file()]
    if missing:raise RuntimeError('Required public files missing: '+','.join(sorted(missing)))
    # A file existing in the development tree does not prove it is selected.
    # Close local absolute imports over the actual public-source allowlist.
    selected={p.relative_to(root).as_posix() for p in files}
    for source in files:
        if source.suffix!='.py':continue
        for node in ast.walk(ast.parse(source.read_text('utf-8-sig'),filename=source.name)):
            imports=([node.module] if isinstance(node,ast.ImportFrom) and node.level==0 else
                [item.name for item in node.names] if isinstance(node,ast.Import) else [])
            for name in imports:
                if not name or name.split('.')[0] not in ('scripts','xiaojiu_brain'):continue
                candidates=(name.replace('.','/')+'.py',name.replace('.','/')+'/__init__.py')
                if not any(candidate in selected for candidate in candidates):
                    raise RuntimeError('Public local import missing: '+source.relative_to(root).as_posix()+' -> '+name)
    return sorted(files)

def known_secrets(root):
    # Load only configuration-shaped files, not conversations or memory bodies.
    values=[]
    p=root/'private/config.json'
    if p.is_file():
        for key,value in json.loads(p.read_text('utf-8-sig')).items():
            if any(word in key.lower() for word in ('token','password','api_key')) and isinstance(value,str) and len(value)>=20:values.append(value.encode())
    return values

def local_identifiers(root):
    # Compute signatures locally: the scanner must not itself publish the
    # publisher's username, machine name or absolute worktree paths.
    candidates={str(Path.home()),str(root),str(root.parent),platform.node()}
    for key in ('USERPROFILE','COMPUTERNAME'):
        if os.getenv(key):candidates.add(os.environ[key])
    return tuple(value.encode() for item in candidates if len(item)>5
        for value in {item,item.replace('\\','/'),item.replace('/','\\')})

def validate_content(name,content,secrets):
    parts=Path(name).parts
    if any(p in FORBIDDEN_NAMES for p in parts) or Path(name).suffix in FORBIDDEN_SUFFIXES:raise RuntimeError('Excluded private/runtime file: '+name)
    if Path(name).name in ('brain-fallback.png','LIVE-CUTOVER-20261108.md','ACCEPTANCE.md','PANEL-LAUNCHER-FIX-20261108.md','LEGACY-PRIVATE-MAPPING.md'):
        raise RuntimeError('Excluded internal/uncleared asset: '+name)
    normalized=content.replace(b'\\\\',b'\\')
    for marker in LOCAL_IDENTIFIERS:
        if marker.lower() in normalized.lower():raise RuntimeError('Local-machine identifier found in '+name)
    for value in secrets:
        if value in content:raise RuntimeError('Local configuration credential found in '+name)
    if name.endswith('.whl'):
        with zipfile.ZipFile(io.BytesIO(content)) as wheel:
            for entry in wheel.namelist():
                if entry.endswith('/'):continue
                validate_content(entry,wheel.read(entry),secrets)

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def verify_extracted_archive(archive,work,test_python):
    """Run the unchanged Python suite from archive bytes, outside the checkout.

    Dependencies must be installed in test_python. -I ignores inherited Python
    paths; the runner explicitly asserts that the kernel comes from extraction.
    Any error blocks publication. There is no retry/skip/relaxed assertion here.
    """
    destination=Path(tempfile.mkdtemp(prefix='archive-check-',dir=work))
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if not (destination/name).resolve().is_relative_to(destination.resolve()):raise RuntimeError('Unsafe archive path')
        z.extractall(destination)
    roots=list(destination.iterdir())
    if len(roots)!=1 or not roots[0].is_dir():raise RuntimeError('Expected one public package root')
    extracted=roots[0]
    runner='''import pathlib,sys,unittest
root=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
import xiaojiu_brain
assert pathlib.Path(xiaojiu_brain.__file__).resolve().is_relative_to(root), 'Kernel escaped the extracted archive'
suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern='test_*.py')
result=unittest.TextTestRunner(verbosity=2).run(suite)
print('EXTRACTED_RESULT='+str(result.testsRun)+':'+str(len(result.failures))+':'+str(len(result.errors))+':'+str(len(result.skipped)),flush=True)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
'''
    env=os.environ.copy();env.update(PYTHONUTF8='1')
    result=subprocess.run([str(test_python),'-I','-c',runner,str(extracted)],cwd=extracted,env=env,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180)
    log=work/('extracted-python-tests-'+archive.stem+'.log');log.write_bytes(result.stdout)
    (work/'extracted-python-tests.log').write_bytes(result.stdout)
    if result.returncode:raise RuntimeError('Extracted public archive Python tests failed; publication blocked. See extracted-python-tests.log')
    match=re.search(rb'EXTRACTED_RESULT=(\d+):(\d+):(\d+):(\d+)',result.stdout)
    if not match or not int(match[1]):raise RuntimeError('Extracted test results missing; publication blocked')
    return {'tests':int(match[1]),'failures':int(match[2]),'errors':int(match[3]),'skipped':int(match[4]),
        'source':'freshly_extracted_final_archive','kernel_import_verified':True}

def build(root,output,test_python=None):
    global LOCAL_IDENTIFIERS
    root=Path(root).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    LOCAL_IDENTIFIERS=local_identifiers(root)
    kernel_version=runpy.run_path(str(root/'xiaojiu_brain/__init__.py'))['__version__']
    plugin_version=json.loads((root/'package.json').read_text('utf-8-sig'))['version']
    release_match=re.fullmatch(r'(\d+\.\d+\.\d+)-alpha\.(\d+)',plugin_version)
    if not release_match:raise RuntimeError('Expected an explicit alpha plugin release version')
    version=release_match[1]+'a'+release_match[2]
    work=root/'artifacts/public-plugin-package';work.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='stage-',dir=work));secrets=known_secrets(root)
    selected=public_sources(root)
    for source in selected:
        relative=source.relative_to(root);blob=source.read_bytes();validate_content(relative.as_posix(),blob,secrets)
        dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(blob)
    wheels=stage/'wheels';wheels.mkdir(exist_ok=True)
    env=os.environ.copy();env.update(PIP_DISABLE_PIP_VERSION_CHECK='1',PYTHONUTF8='1')
    result=subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--no-index','--no-build-isolation',
        '--no-cache-dir','--wheel-dir',str(wheels),str(stage)],cwd=work,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120)
    (work/'wheel-build.log').write_bytes(result.stdout)
    if result.returncode:raise RuntimeError('Public wheel build failed; see local wheel-build.log')
    artifacts=list(wheels.glob('xiaojiu_brain-'+kernel_version+'-*.whl'))
    if len(artifacts)!=1:raise RuntimeError('Expected exactly one kernel wheel')
    wheel=artifacts[0];validate_content('wheels/'+wheel.name,wheel.read_bytes(),secrets)
    # Check all package/static members against the same selected source stage.
    with zipfile.ZipFile(wheel) as z:
        for source in selected:
            relative=source.relative_to(root).as_posix()
            if relative.startswith('xiaojiu_brain/') and source.suffix in ('.py','.html','.css','.js','.svg','.bin','.json','.md'):
                if z.read(relative)!=(stage/relative).read_bytes():raise RuntimeError('Wheel/source mismatch: '+relative)
    from importlib.util import spec_from_file_location,module_from_spec
    spec=spec_from_file_location('release_config',stage/'xiaojiu_brain/config.py');module=module_from_spec(spec)
    sys.modules[spec.name]=module;spec.loader.exec_module(module)
    manifest={p.relative_to(root).as_posix():sha(stage/p.relative_to(root)) for p in selected}
    manifest['wheels/'+wheel.name]=sha(wheel)
    info={'name':'Memory Brain','display_name':'忆核大脑','version':version,'plugin_version':plugin_version,
        'kernel_version':kernel_version,'build_id':module.build_id(),
        'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'platform':'Windows 10/11',
        'python':'>=3.10','node':'>=22','entry':'adapters/dsh/memory-brain.mjs',
        'dsh_contract':'request-only snapshots via native host contract or verified stock per-agent compatibility bridge',
        'contains':['kernel_source','kernel_wheel','DSH_adapter','DSH_bundle_manifest','3D_panel','life_and_emotion_rules','synthetic_tests','licenses'],
        'excludes':['private_config','API_keys','persona_content','chat_history','memory_databases','queue_logs','private_reports','model_weights','DSH_runtime'],
        'fresh_install':True,'default_owner':'local-owner','default_cross_conversation':False,
        'dependencies_downloaded_by_installer':[],
        'bundled_runtime_dependencies':['tzdata-2026.6-py2.py3-none-any.whl'],
        'first_run_network_required':False,'python_must_already_be_installed':True}
    (stage/'PUBLIC-RELEASE.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf8')
    manifest['PUBLIC-RELEASE.json']=sha(stage/'PUBLIC-RELEASE.json')
    (stage/'SHA256SUMS.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    final_archive=output/('memory-brain-dsh-'+version+'-public.zip')
    archive=work/('candidate-'+version+'-'+stage.name+'.zip');prefix='memory-brain-dsh-'+version
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for name in sorted([*manifest,'SHA256SUMS.json']):z.write(stage/name,prefix+'/'+name)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():validate_content(name,z.read(name),secrets)
        for required in ('package.json','cordis.patch.yml','adapters/dsh/memory-brain.mjs','adapters/dsh/onboarding.mjs','adapters/dsh/runtime-home.mjs','client/client.js','scripts/build_runtime_wheel.py','scripts/register-dsh.mjs','docs/DSH-INSTALL.md','docs/DSH-FIRST-RUN.md','xiaojiu_brain/static/brain-fallback.svg'):
            if prefix+'/'+required not in z.namelist():raise RuntimeError('Release missing '+required)
    test_result=verify_extracted_archive(archive,work,test_python or sys.executable)
    # Publish only after both privacy and archive-sourced tests pass.
    os.replace(archive,final_archive);archive=final_archive
    public_report={**info,'archive_name':archive.name,'bytes':archive.stat().st_size,'sha256':sha(archive),
        'files':len(manifest)+1,'credential_scan':'pass','local_identifier_scan':'pass','private_path_policy':'positive_allowlist',
        'extracted_python_tests':test_result}
    report=output/('memory-brain-dsh-'+version+'-public.json');report.write_text(json.dumps(public_report,ensure_ascii=False,indent=2),encoding='utf8')
    local={'archive':str(archive),'stage':str(stage),'wheel':str(wheel),'report':str(report),'sha256':sha(archive),'bytes':archive.stat().st_size,'files':len(manifest)+1}
    (work/'LATEST.json').write_text(json.dumps(local,ensure_ascii=False,indent=2),encoding='utf8');return local

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out');parser.add_argument('--test-python');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    print(json.dumps(build(root,args.out or root/'dist',test_python=args.test_python),ensure_ascii=False))

if __name__=='__main__':main()
