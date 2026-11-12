"""Build a complete, positively selected public DSH release, never zip a worktree.

No private configuration, data, benchmarks, operational reports or host runtime
are included. The kernel wheel is built from this same sanitized source stage.
"""
from pathlib import Path,PurePosixPath
import argparse,ast,base64,csv,datetime,hashlib,io,json,os,platform,re,runpy,shutil,subprocess,sys,tempfile,zipfile
from email.parser import Parser

PUBLIC_DOCS=('BACKGROUND-DERIVATION.md','CHAT-USAGE.md','BRANDING.md','CONTRACT.md','DAILY-STATE-RULES.md',
    'LIFE-LAYER.md','MEMORY-MAP.md','OBSERVATORY-API.md','PROFILE-IMPORTANT.md','DSH-INSTALL.md',
    'SEMANTIC-MEMORY.md','TIME-SEMANTICS.md','DSH-DESKTOP-COMPAT.md','DSH-FIRST-RUN.md',
    'RELEASE-0.2.3.md','RELEASE-0.2.4.md','RELEASE-0.2.5.md','RESOURCE-MANAGEMENT.md','CONTEXT-MANAGEMENT.md','MAINTENANCE.md')
PUBLIC_SCRIPTS=('install.ps1','start-local.ps1','open-panel.ps1','connect-dsh.ps1','register-dsh.mjs',
    'verify-package.ps1','build_brain.mjs','build_fallback.py','build_plugin_release.py','build_release.py','dsh_plan.py',
    'dsh-request-hook.mjs','dsh-compat.ps1','build_runtime_wheel.py','dsh-compat-report.mjs')
ROOT_FILES=('README.md','LICENSE','NOTICE.md','pyproject.toml','MANIFEST.in','gitignore.template','package.json','cordis.patch.yml',
    '安装.cmd','打开忆核大脑.cmd','停止忆核大脑.cmd','接入DSH.cmd','移除DSH接入.cmd','DSH兼容性检查.cmd')
FORBIDDEN_NAMES={'private','data','artifacts','dist','node_modules','.venv','__pycache__','.git','tests'}
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
        ('adapters/dsh',{'.mjs','.md','.json'}),('client',{'.js'}),('frontend',{'.js','.json','.md'})]:
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

def release_version(plugin_version,kernel_version):
    """Map the public channel to PEP 440; rc/stable cannot hide version skew."""
    match=re.fullmatch(r'(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?',plugin_version)
    if not match:raise RuntimeError('Expected an explicit alpha, beta, rc or stable release version')
    suffix={'alpha':'a','beta':'b','rc':'rc'}
    version=match[1]+(suffix[match[2]]+match[3] if match[2] else '')
    # Historical alpha adapter releases intentionally shared a prior kernel.
    # Release candidates and stable releases must have one coherent identity.
    if match[2] not in ('alpha','beta') and kernel_version!=version:
        raise RuntimeError('Release-candidate/stable kernel and plugin versions differ')
    return version,match[2] or 'stable'

def checked_members(archive):
    """Verify member structure and CRC before consuming an archive."""
    names=archive.namelist()
    if len(names)!=len(set(name.casefold() for name in names)):
        raise RuntimeError('Duplicate or case-colliding archive members')
    for item in archive.infolist():
        name=item.filename;parts=PurePosixPath(name).parts
        if not parts or name.startswith(('/','\\')) or '\\' in name or '..' in parts or ':' in parts[0]:
            raise RuntimeError('Unsafe archive path')
        if (item.external_attr>>16)&0o170000==0o120000:
            raise RuntimeError('Archive symbolic link is not permitted')
    if archive.testzip() is not None:raise RuntimeError('Archive CRC mismatch')
    return {name for name in names if not name.endswith('/')}

def verify_wheel(content):
    """Check a wheel's inventory, RECORD hashes and distribution identity."""
    with zipfile.ZipFile(io.BytesIO(content)) as wheel:
        names=checked_members(wheel)
        records=[name for name in names if name.endswith('.dist-info/RECORD')]
        if len(records)!=1:raise RuntimeError('Expected one wheel RECORD')
        record=records[0];rows=list(csv.reader(io.StringIO(wheel.read(record).decode('utf8'))))
        if any(len(row)!=3 for row in rows):raise RuntimeError('Invalid wheel RECORD row')
        if len(rows)!=len({row[0] for row in rows}) or {row[0] for row in rows}!=names:
            raise RuntimeError('Wheel RECORD inventory mismatch')
        for name,digest,size in rows:
            blob=wheel.read(name)
            if name==record:
                if digest or size:raise RuntimeError('Invalid wheel RECORD self entry')
                continue
            if not size or int(size)!=len(blob):raise RuntimeError('Wheel RECORD size mismatch: '+name)
            if not digest.startswith('sha256='):raise RuntimeError('Wheel member lacks SHA-256: '+name)
            actual=base64.urlsafe_b64encode(hashlib.sha256(blob).digest()).rstrip(b'=').decode('ascii')
            if actual!=digest.split('=',1)[1]:raise RuntimeError('Wheel RECORD hash mismatch: '+name)
        metadata=record.rsplit('/',1)[0]+'/METADATA'
        if metadata not in names:raise RuntimeError('Wheel distribution metadata missing')
        fields=Parser().parsestr(wheel.read(metadata).decode('utf8'))
        if not fields.get('Name') or not fields.get('Version'):raise RuntimeError('Wheel identity missing')
        return {'name':fields['Name'],'version':fields['Version'],'members':len(names)},names

def verify_extracted_archive(archive,work,verify_python):
    """Check the exact distribution's integrity, identity and inert imports.

    This creates only an extracted copy of the distribution. It does not start
    the service, create a database, generate sample conversations or call models.
    """
    destination=Path(tempfile.mkdtemp(prefix='archive-check-',dir=work))
    with zipfile.ZipFile(archive) as z:
        names=checked_members(z)
        roots={PurePosixPath(name).parts[0] for name in names}
        if len(roots)!=1:raise RuntimeError('Expected one public package root')
        prefix=roots.pop()+'/'
        manifest=json.loads(z.read(prefix+'SHA256SUMS.json'))
        if {prefix+name for name in manifest}|{prefix+'SHA256SUMS.json'}!=names:
            raise RuntimeError('Public archive manifest inventory mismatch')
        for name,digest in manifest.items():
            if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or hashlib.sha256(z.read(prefix+name)).hexdigest()!=digest:
                raise RuntimeError('Public archive manifest hash mismatch: '+name)
        info=json.loads(z.read(prefix+'PUBLIC-RELEASE.json'))
        plugin=json.loads(z.read(prefix+'package.json'))
        adapter=json.loads(z.read(prefix+'adapters/dsh/package.json'))
        project=re.search(r'^version\s*=\s*"([^"]+)"\s*$',z.read(prefix+'pyproject.toml').decode('utf8'),re.M)
        if not project:raise RuntimeError('Public kernel version missing')
        kernel_version=project[1];version,channel=release_version(plugin['version'],kernel_version)
        if adapter['version']!=plugin['version'] or any(info.get(key)!=value for key,value in
                {'version':version,'plugin_version':plugin['version'],'kernel_version':kernel_version,'release_channel':channel}.items()):
            raise RuntimeError('Public archive release identity mismatch')
        dependencies=json.loads(z.read(prefix+'compat/runtime-dependencies.json'))
        wheel_names={name for name in names if name.endswith('.whl')}
        dependency_names={prefix+'wheels/'+entry['filename'] for entry in dependencies['files']}
        for entry in dependencies['files']:
            if hashlib.sha256(z.read(prefix+'wheels/'+entry['filename'])).hexdigest()!=entry['sha256']:
                raise RuntimeError('Bundled dependency checksum mismatch')
        kernel_wheels=wheel_names-dependency_names
        if len(kernel_wheels)!=1:raise RuntimeError('Expected one matching kernel wheel')
        kernel_wheel=kernel_wheels.pop();wheel_results=[]
        for name in sorted(wheel_names):
            result,members=verify_wheel(z.read(name));result['file']=name[len(prefix):]
            wheel_results.append(result)
            if name!=kernel_wheel:continue
            if result['name']!='xiaojiu-brain' or result['version']!=kernel_version:
                raise RuntimeError('Bundled kernel wheel identity mismatch')
            source_names={name[len(prefix):] for name in names if name.startswith(prefix+'xiaojiu_brain/')}
            if {name for name in members if name.startswith('xiaojiu_brain/')}!=source_names:
                raise RuntimeError('Kernel wheel/source inventory mismatch')
            with zipfile.ZipFile(io.BytesIO(z.read(name))) as wheel:
                for relative in source_names:
                    if wheel.read(relative)!=z.read(prefix+relative):raise RuntimeError('Kernel wheel/source mismatch: '+relative)
        z.extractall(destination)
    extracted=destination/prefix.rstrip('/')
    runner='''import json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve()
wheel=pathlib.Path(sys.argv[2]).resolve()
expected=sys.argv[3]
sys.path.insert(0,str(root))
import xiaojiu_brain
assert pathlib.Path(xiaojiu_brain.__file__).resolve().is_relative_to(root), 'Kernel escaped the extracted archive'
assert xiaojiu_brain.__version__==expected, 'Extracted kernel version mismatch'
assert set(name for name in sys.modules if name.startswith('xiaojiu_brain'))=={'xiaojiu_brain'}, 'Package import started runtime modules'
del sys.modules['xiaojiu_brain']
sys.path[0]=str(wheel)
import xiaojiu_brain
assert pathlib.Path(xiaojiu_brain.__file__).resolve().is_relative_to(wheel), 'Kernel escaped the bundled wheel'
assert xiaojiu_brain.__version__==expected, 'Wheel kernel version mismatch'
assert set(name for name in sys.modules if name.startswith('xiaojiu_brain'))=={'xiaojiu_brain'}, 'Wheel import started runtime modules'
print('ARTIFACT_IMPORT='+json.dumps({'source_import':True,'wheel_import':True,'version':expected}),flush=True)
'''
    env=os.environ.copy();env.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([str(verify_python),'-I','-B','-c',runner,str(extracted),str(destination/kernel_wheel),kernel_version],cwd=extracted,env=env,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180)
    log=work/('artifact-verification-'+archive.stem+'.log');log.write_bytes(result.stdout)
    (work/'artifact-verification.log').write_bytes(result.stdout)
    if result.returncode:raise RuntimeError('Extracted public artifact import failed; see artifact-verification.log')
    match=re.search(rb'ARTIFACT_IMPORT=(\{[^\r\n]+\})',result.stdout)
    if not match:raise RuntimeError('Artifact import verification result missing')
    imports=json.loads(match[1])
    if imports!={'source_import':True,'wheel_import':True,'version':kernel_version}:
        raise RuntimeError('Artifact import verification identity mismatch')
    return {'scope':'distribution_integrity_versions_and_package_imports','source':'freshly_extracted_final_archive',
        'archive_crc_verified':True,'manifest_files':len(manifest),'manifest_hashes_verified':True,
        'package_versions_verified':True,'kernel_source_import_verified':True,'kernel_wheel_import_verified':True,
        'wheel_source_match_verified':True,'wheel_records_verified':True,'wheels':wheel_results,
        'service_started':False,'sample_data_generated':False,'model_calls':0}

def build(root,output,verify_python=None,test_python=None):
    global LOCAL_IDENTIFIERS
    root=Path(root).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    LOCAL_IDENTIFIERS=local_identifiers(root)
    kernel_version=runpy.run_path(str(root/'xiaojiu_brain/__init__.py'))['__version__']
    plugin_version=json.loads((root/'package.json').read_text('utf-8-sig'))['version']
    version,channel=release_version(plugin_version,kernel_version)
    work=root/'artifacts/public-plugin-package';work.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='stage-',dir=work));secrets=known_secrets(root)
    selected=public_sources(root)
    for source in selected:
        relative=source.relative_to(root);blob=source.read_bytes();validate_content(relative.as_posix(),blob,secrets)
        dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(blob)
    wheels=stage/'wheels';wheels.mkdir(exist_ok=True)
    build_wheels=Path(tempfile.mkdtemp(prefix='kernel-wheel-',dir=work))
    env=os.environ.copy();env.update(PYTHONUTF8='1')
    # The already shipped stdlib builder verifies literal metadata and public
    # paths. Release assembly must not depend on an unshipped wheel/setuptools
    # toolchain or download anything into the user's installed environment.
    result=subprocess.run([sys.executable,'-I',str(stage/'scripts/build_runtime_wheel.py'),
        '--source',str(stage),'--out',str(build_wheels)],cwd=work,env=env,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120)
    (work/'wheel-build.log').write_bytes(result.stdout)
    if result.returncode:raise RuntimeError('Public wheel build failed; see local wheel-build.log')
    artifacts=list(build_wheels.glob('xiaojiu_brain-'+kernel_version+'-*.whl'))
    if len(artifacts)!=1:raise RuntimeError('Expected exactly one kernel wheel')
    wheel=wheels/artifacts[0].name;shutil.copy2(artifacts[0],wheel)
    validate_content('wheels/'+wheel.name,wheel.read_bytes(),secrets)
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
        'kernel_version':kernel_version,'build_id':module.build_id(),'release_channel':channel,
        'production_ready_claimed':False,
        'release_kind':'framework_and_reference_implementation',
        'release_notes':'docs/RELEASE-0.2.5.md',
        'long_term_stability_validated':False,'cross_hardware_validated':False,
        'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'platform':'Windows 10/11',
        'python':'>=3.10','node':'>=22','entry':'adapters/dsh/memory-brain.mjs',
        'dsh_contract':'request-only snapshots via native host contract or verified stock per-agent compatibility bridge',
        'contains':['kernel_source','kernel_wheel','DSH_adapter','DSH_bundle_manifest','context_management',
            'short_summary_policy','pressure_cooldown','native_tool_result_pruning','permission_checked_session_history',
            'desktop_asar_runtime_discovery','existing_live_session_adoption','bounded_legacy_reconnect','cross_conversation_source_diversity',
            '3D_panel','life_and_emotion_rules','licenses'],
        'context_management':{'default_enabled':True,'trigger_turns':12,'keep_complete_turns':4,
            'requires_host_compaction':True,'provides_native_service_when_missing':True,
            'request_budget':'advisory_final_request_text_estimate','exact_tokenizer_claimed':False},
        'compaction_defaults':{'summary_target_chars':2000,'summary_max_tokens':4096,'pressure_cooldown_ms':60000,
            'pressure_threshold_ratio':0.8,'pressure_retain_ratio':0.16,'headroom_tokens':4096,
            'tool_prune_threshold_chars':4096,'tool_prune_head_chars':2048,'tool_prune_tail_chars':512,
            'preserves_existing_custom_host_strategy':True},
        'session_history':{'default_enabled':True,'current_session_only':True,'permission_checked':True,
            'automatic_literal_lookup_when_recall_empty':True},
        'excludes':['private_config','API_keys','persona_content','chat_history','memory_databases','queue_logs','private_reports','benchmark_answers','model_weights','DSH_runtime'],
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
    verification=verify_extracted_archive(archive,work,verify_python or test_python or sys.executable)
    # Publish only after privacy and exact-distribution integrity/import checks.
    os.replace(archive,final_archive);archive=final_archive
    public_report={**info,'archive_name':archive.name,'bytes':archive.stat().st_size,'sha256':sha(archive),
        'files':len(manifest)+1,'credential_scan':'pass','local_identifier_scan':'pass','private_path_policy':'positive_allowlist',
        'artifact_verification':verification}
    report=output/('memory-brain-dsh-'+version+'-public.json');report.write_text(json.dumps(public_report,ensure_ascii=False,indent=2),encoding='utf8')
    local={'archive':str(archive),'stage':str(stage),'wheel':str(wheel),'report':str(report),'sha256':sha(archive),'bytes':archive.stat().st_size,'files':len(manifest)+1}
    (work/'LATEST.json').write_text(json.dumps(local,ensure_ascii=False,indent=2),encoding='utf8');return local

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out');parser.add_argument('--verify-python','--test-python',dest='verify_python');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    print(json.dumps(build(root,args.out or root/'dist',verify_python=args.verify_python),ensure_ascii=False))

if __name__=='__main__':main()
