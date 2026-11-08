"""Explicit, idempotent legacy import; source SQLite is opened read-only.

Original conversations keep their scope. Imported memories are separately
labelled historical notes, never invented first-person user statements.
No model provider is constructed or called by this module.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile

from .storage import DISCLOSURES, _timestamp


def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):digest.update(data)
    return digest.hexdigest()


@contextmanager
def _snapshot(path):
    source=Path(path).expanduser().resolve()
    if not source.is_file():raise ValueError('legacy database does not exist')
    before=_sha(source)
    with tempfile.TemporaryDirectory(prefix='xiaojiu-legacy-snapshot-') as folder:
        destination=Path(folder)/'snapshot.db'
        connection=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=10)
        target=sqlite3.connect(str(destination))
        try:
            connection.backup(target)
        finally:
            target.close();connection.close()
        snapshot=sqlite3.connect(destination.as_uri()+'?mode=ro',uri=True)
        snapshot.row_factory=sqlite3.Row
        try:
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('legacy snapshot integrity check failed')
            yield snapshot,{'path':str(source),'source_main_sha256_before':before,'snapshot_sha256':_sha(destination)}
        finally:snapshot.close()


def _tables(connection):
    result=[]
    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name=row[0];quoted='"'+name.replace('"','""')+'"'
        result.append({'name':name,'rows':connection.execute('SELECT count(*) FROM '+quoted).fetchone()[0],
            'columns':[r['name'] for r in connection.execute('PRAGMA table_info('+quoted+')')]})
    return result


def inspect_legacy(path):
    with _snapshot(path) as (connection,info):
        result={**info,'tables':_tables(connection),'read_only':True,'includes_message_bodies':False}
    result['source_main_sha256_after']=_sha(path)
    result['source_main_hash_stable']=result['source_main_sha256_before']==result['source_main_sha256_after']
    return result


def _meta(value):
    if isinstance(value,dict):return dict(value)
    if not value:return {}
    try:
        parsed=json.loads(value)
        return parsed if isinstance(parsed,dict) else {'legacy_meta_value':parsed}
    except (TypeError,ValueError):return {'legacy_meta_unparsed':str(value)}


def _scope(original,origin):
    text=str(original or '')
    if not text:return 'legacy:'+origin+':unscoped'
    if len(text)>512:return 'legacy:'+origin+':scope:'+hashlib.sha256(text.encode()).hexdigest()[:24]
    return text


def _time(value):
    try:return _timestamp(value) if value not in (None,'') else 0
    except ValueError:return 0


def _policy(metadata,visibility=None):
    disclosure=metadata.get('disclosure','normal')
    if disclosure not in DISCLOSURES:disclosure='only_if_asked'
    if visibility=='private' and disclosure=='normal':disclosure='only_if_asked'
    topics=metadata.get('disclosure_topics',[])
    if not isinstance(topics,list) or any(not isinstance(t,str) for t in topics):topics=[]
    return disclosure,topics


def _note_policy(metadata,visibility,private_notes_policy):
    # Legacy visibility=private selected the owner's private-chat namespace.
    # It was not an explicit instruction to withhold a fact until asked.
    # Compatibility is opt-in and never overrides an explicit disclosure rule.
    if private_notes_policy=='owner' and visibility=='private' and 'disclosure' not in metadata:
        return _policy(metadata)
    return _policy(metadata,visibility)


def _weight(value):
    try:
        score=float(value)
        return max(0.,min(1.,score)) if math.isfinite(score) else .5
    except (TypeError,ValueError):return .5


def _legacy_record(row):
    """Preserve old columns without duplicating the body or guessing semantics."""
    record={}
    for key,value in row.items():
        if key=='text':continue
        if isinstance(value,bytes):value={'encoding':'base64','data':base64.b64encode(value).decode('ascii')}
        elif isinstance(value,float) and not math.isfinite(value):value={'nonfinite_number':str(value)}
        record[key]=value
    return record


def import_legacy(store,path,owner,apply=False,origin_key=None,private_notes_policy='explicit_only'):
    """Preview by default. Apply is administrative and never invokes a model.

    External IDs are namespaced by origin/table/row; original turn IDs stay
    intact in metadata. This preserves legacy duplicate turn IDs without
    overwriting an existing new-project message.
    """
    source=Path(path).expanduser().resolve()
    if not source.is_file():raise ValueError('legacy database does not exist')
    if private_notes_policy not in ('explicit_only','owner'):raise ValueError('invalid private notes policy')
    if store.path!=':memory:' and (source==Path(store.path).resolve() or source.samefile(store.path)):
        raise ValueError('source and destination must be different databases')
    if not isinstance(owner,str) or not owner.strip():raise ValueError('owner is required')
    origin=origin_key or hashlib.sha256(str(source).encode('utf8')).hexdigest()[:20]
    if not isinstance(origin,str) or not origin.strip() or len(origin)>96:raise ValueError('invalid import origin key')
    result={'ok':True,'applied':bool(apply),'dry_run':not apply,'origin_key':origin,'owner':owner,
        'raw_eligible':0,'raw_unsupported_role':0,'raw_empty':0,'raw_imported':0,'raw_duplicates':0,
        'legacy_memories':0,'memory_notes_imported':0,'memory_note_duplicates':0,'linked_memories':0,
        'unlinked_memories':0,'protected_private_notes':0,'suppressed_memories':0,'scopes_renamed':0,'errors':[],
        'private_notes_policy':private_notes_policy,'owner_scoped_private_notes':0,
        'derivation_policy':'skipped_import','embedding_policy':'local_only',
        'profiles_emotions':'not_imported; retain original files and explicit backups'}
    with _snapshot(path) as (connection,info):
        result.update(info)
        names={t['name'] for t in _tables(connection)}
        if not names.intersection(('raw_messages','memories')):
            raise ValueError('unsupported legacy database: expected raw_messages or memories table')
        result['unimported_tables']=sorted(n for n in names if n not in ('raw_messages','memories'))
        raw=[dict(r) for r in connection.execute('SELECT * FROM raw_messages ORDER BY id')] if 'raw_messages' in names else []
        memories=[dict(r) for r in connection.execute('SELECT * FROM memories ORDER BY id')] if 'memories' in names else []
        raw_by_old={};inserted={};deleted_raw=set()
        for row in raw:
            if row.get('role') not in ('user','assistant'):
                result['raw_unsupported_role']+=1;continue
            text=row.get('text')
            if not isinstance(text,str) or not text.strip():
                result['raw_empty']+=1;continue
            oldid=int(row['id']);metadata=_meta(row.get('metadata') or row.get('meta'))
            disclosure,topics=_policy(metadata,row.get('visibility'))
            original_session=str(row.get('session_id') or metadata.get('session') or '')
            conversation=_scope(original_session,origin)
            if original_session and conversation!=original_session:result['scopes_renamed']+=1
            metadata.update(source_kind='imported_raw',original_role=row['role'],original_id=oldid,
                original_turn_id=str(row.get('turn_id') or ''),original_session_id=original_session,
                original_sender=str(row.get('sender') or ''),legacy_origin=origin,
                skip_derivation=True,import_model_policy='local_only',disclosure=disclosure,disclosure_topics=topics,
                legacy_time_unknown=_time(row.get('created_at'))==0)
            metadata['legacy_record']=_legacy_record(row)
            raw_by_old[oldid]={'row':row,'metadata':metadata,'conversation':conversation,'text':text}
            result['raw_eligible']+=1
            if apply:
                try:
                    accepted=store.ingest(owner,conversation,f'legacy:{origin}:raw:{oldid}',row['role'],text,
                        recorded_at=_time(row.get('created_at')),metadata=metadata)
                    result['raw_imported' if accepted['accepted'] else 'raw_duplicates']+=1
                    if not accepted.get('deleted'):inserted[oldid]=accepted['id']
                    else:deleted_raw.add(oldid)
                except Exception as exc:
                    result['errors'].append({'stage':'raw','legacy_id':oldid,'error':type(exc).__name__})
        for row in memories:
            result['legacy_memories']+=1
            oldid=int(row['id']);text=row.get('text')
            if not isinstance(text,str) or not text.strip():
                result['errors'].append({'stage':'memory','legacy_id':oldid,'error':'empty_or_invalid_text'});continue
            original=_meta(row.get('meta') or row.get('metadata'))
            references=original.get('msg_ids')
            if not isinstance(references,list) or not references:
                one=row.get('source_msg_id') or original.get('msg_id')
                # A batch's last ID alone does not prove a summary's evidence.
                # A single-source link is safe only when the remembered text
                # actually occurs in that exact original message.
                try:single=raw_by_old.get(int(one)) if one else None
                except (TypeError,ValueError):single=None
                references=[one] if single and text in single['text'] else []
            try:
                if any(isinstance(i,bool) or not isinstance(i,(int,str)) or int(i)<=0 for i in references):
                    raise ValueError('invalid source IDs')
                refs=sorted({int(i) for i in references})
            except (TypeError,ValueError):refs=[]
            complete=bool(refs) and all(i in raw_by_old for i in refs)
            scopes={raw_by_old[i]['conversation'] for i in refs if i in raw_by_old}
            complete=complete and len(scopes)==1
            conversation=next(iter(scopes)) if complete else _scope(original.get('session'),origin)
            disclosure,topics=_note_policy(original,row.get('visibility'),private_notes_policy)
            if row.get('status')=='deleted':disclosure='never_say'
            if row.get('visibility')=='private':result['protected_private_notes']+=1
            if private_notes_policy=='owner' and row.get('visibility')=='private' and 'disclosure' not in original:
                result['owner_scoped_private_notes']+=1
            meta={'source_kind':'imported_memory','original_role':'unknown','original_id':oldid,
                'original_kind':row.get('kind'),'original_provenance':row.get('provenance') or '',
                'original_source':row.get('source') or '', 'original_source_msg_id':row.get('source_msg_id',0),
                'original_status':row.get('status','active'),'original_importance':row.get('importance'),
                'original_visibility':row.get('visibility'),'legacy_meta':original,'legacy_origin':origin,
                'legacy_private_notes_policy':private_notes_policy,
                'source_uncertain':True,'is_original_conversation_message':False,
                'skip_derivation':True,'import_model_policy':'local_only','disclosure':disclosure,
                'disclosure_topics':topics,'importance':_weight(row.get('importance')),
                'legacy_source_ids':refs,'legacy_time_unknown':_time(row.get('created_at'))==0}
            meta['legacy_record']=_legacy_record(row)
            result['linked_memories' if complete else 'unlinked_memories']+=1
            if not apply:continue
            if complete and any(i in deleted_raw for i in refs):
                result['suppressed_memories']+=1
                continue
            try:
                previous=store.find_message(owner,conversation,f'legacy:{origin}:memory:{oldid}',role='assistant')
                if previous and previous['metadata'].get('legacy_private_notes_policy','explicit_only')!=private_notes_policy:
                    raise ValueError('a previous import used a different private notes policy')
                note=store.ingest(owner,conversation,f'legacy:{origin}:memory:{oldid}','assistant',text,
                    recorded_at=_time(row.get('created_at')),metadata=meta,
                    source_dependencies=[inserted[i] for i in refs] if complete and all(i in inserted for i in refs) else None)
                result['memory_notes_imported' if note['accepted'] else 'memory_note_duplicates']+=1
                if note.get('deleted'):continue
                status='archived' if row.get('status') in ('archived','deleted') else 'active'
                pinned=original.get('pin') is True or original.get('pinned') is True
                for doc in store.raw_documents_for_message(owner,note['id']):
                    store.update_memory(owner,doc['id'],{'status':status,'pinned':pinned})
                if complete and all(i in inserted for i in refs):
                    source_refs=[{'message_id':inserted[i],'start':0,'end':len(raw_by_old[i]['text'])} for i in refs]
                    derived=store.add_derived(owner,conversation,'summary' if row.get('kind')=='episode' else 'fact',
                        text,source_refs,metadata=meta,importance=_weight(row.get('importance')),disclosure=disclosure,
                        dependency_message_ids=[note['id']]+[inserted[i] for i in refs])
                    store.update_memory(owner,derived['id'],{'status':status,'pinned':pinned})
            except Exception as exc:
                result['errors'].append({'stage':'memory','legacy_id':oldid,'error':type(exc).__name__})
    result['source_main_sha256_after']=_sha(path)
    result['source_main_hash_stable']=result['source_main_sha256_before']==result['source_main_sha256_after']
    result['ok']=not result['errors']
    return result
