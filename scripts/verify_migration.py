"""Read-only migration reconciliation. Reports counts/IDs, never message bodies.

Both inputs are copied with SQLite backup into memory, not opened through Store.
This includes committed WAL data and gives each input a consistent read view.
The report verifies preservation and provenance, not semantic truth or recall scores.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from xiaojiu_brain.migration import _meta, _policy, _note_policy, _scope, _time
from xiaojiu_brain.storage import DISCLOSURES


def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


@contextmanager
def _read_snapshot(path):
    path=Path(path).expanduser().resolve()
    if not path.is_file():raise ValueError('database path does not exist')
    before=_sha(path)
    source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)
    copy=sqlite3.connect(':memory:');copy.row_factory=sqlite3.Row
    try:
        source.backup(copy)
    finally:source.close()
    try:
        if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('database snapshot integrity check failed')
        yield copy,{'path':str(path),'main_sha256_before':before,
                    'read_view_sha256':hashlib.sha256(copy.serialize()).hexdigest()}
    finally:copy.close()


def _names(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(connection,table,names):
    return [dict(row) for row in connection.execute('SELECT * FROM '+table+' ORDER BY id')] if table in names else []


def _label(value):
    return 'empty' if not value else 'sha256:'+hashlib.sha256(str(value).encode()).hexdigest()[:16]


def verify_migration(legacy_path,destination_path,owner,origin_key,expected_sessions=(),private_notes_policy='explicit_only'):
    """No database writes or model calls. Metadata includes only safe labels/counts."""
    if not owner or not origin_key:raise ValueError('explicit owner and stable origin_key are required')
    if private_notes_policy not in ('explicit_only','owner'):raise ValueError('invalid private notes policy')
    problems=[];warnings=[];checked=Counter()
    def issue(code,**fields):problems.append({'code':code,**fields})
    with _read_snapshot(legacy_path) as (old,old_info),_read_snapshot(destination_path) as (new,new_info):
        old_names=_names(old);new_names=_names(new)
        if not old_names.intersection(('raw_messages','memories')):
            raise ValueError('unsupported legacy database: expected raw_messages or memories table')
        required={'messages','documents','document_sources','document_dependencies','jobs','ingest_keys','document_fts'}
        if not required.issubset(new_names):raise ValueError('destination is not a supported new brain database')
        raw=_rows(old,'raw_messages',old_names);memories=_rows(old,'memories',old_names)
        eligible=[r for r in raw if r.get('role') in ('user','assistant') and isinstance(r.get('text'),str) and r['text'].strip()]
        supported=[r for r in memories if isinstance(r.get('text'),str) and r['text'].strip()]
        imported=[]
        for r in new.execute('SELECT * FROM messages WHERE owner=?',(owner,)):
            row=dict(r);row['meta']=_meta(row['metadata'])
            if row['meta'].get('legacy_origin')==origin_key:imported.append(row)
        by_key={(r['conversation_id'],r['message_id'],r['role']):r for r in imported}
        message_by_id={r['id']:r for r in imported};mapped_raw={};mapped_notes={}
        raw_by_old={int(r['id']):r for r in eligible}
        scope_rows=defaultdict(list);source_stable=Counter();same_body=Counter();sender_rows=Counter()
        for row in eligible:
            oid=int(row['id']);meta=_meta(row.get('metadata') or row.get('meta'))
            original_session=str(row.get('session_id') or meta.get('session') or '')
            conversation=_scope(original_session,origin_key);scope_rows[conversation].append(row)
            if row.get('turn_id'):source_stable[(conversation,str(row['turn_id']),row['role'])]+=1
            same_body[(conversation,row['role'],hashlib.sha256(row['text'].encode()).digest())]+=1
            sender_rows[(row['role'],_label(row.get('sender')))]+=1
            key=(conversation,f'legacy:{origin_key}:raw:{oid}',row['role']);target=by_key.get(key)
            if target is None:issue('raw_missing',legacy_id=oid,conversation_id=conversation);continue
            mapped_raw[oid]=target['id'];checked['raw']+=1
            if target['text']!=row['text']:issue('raw_text_mismatch',legacy_id=oid)
            if target['recorded_at']!=_time(row.get('created_at')):issue('raw_time_mismatch',legacy_id=oid)
            m=target['meta'];disclosure,topics=_policy(meta,row.get('visibility'))
            expected={'source_kind':'imported_raw','original_role':row['role'],'original_id':oid,
                'original_turn_id':str(row.get('turn_id') or ''),'original_session_id':original_session,
                'original_sender':str(row.get('sender') or ''),'skip_derivation':True,
                'import_model_policy':'local_only','derivation_status':'skipped_import','disclosure':disclosure}
            for name,value in expected.items():
                if m.get(name)!=value:issue('raw_metadata_mismatch',legacy_id=oid,field=name)
            if sorted(m.get('disclosure_topics',[]))!=sorted(set(t.strip() for t in topics if t.strip())):
                issue('raw_topic_policy_mismatch',legacy_id=oid)
            record=m.get('legacy_record',{})
            for field,value in row.items():
                if field=='text' or isinstance(value,bytes):continue
                if record.get(field)!=value:issue('raw_original_column_mismatch',legacy_id=oid,field=field)
        expected_note_scopes={}
        for row in supported:
            oid=int(row['id']);meta=_meta(row.get('meta') or row.get('metadata'))
            refs=meta.get('msg_ids')
            if not isinstance(refs,list) or not refs:
                one=row.get('source_msg_id') or meta.get('msg_id')
                try:single=raw_by_old.get(int(one)) if one else None
                except (ValueError,TypeError):single=None
                refs=[one] if single and row['text'] in single['text'] else []
            try:
                if any(isinstance(i,bool) or not isinstance(i,(int,str)) or int(i)<=0 for i in refs):raise ValueError()
                refs=sorted({int(i) for i in refs})
            except (ValueError,TypeError):refs=[]
            scopes={_scope(str(raw_by_old[i].get('session_id') or _meta(raw_by_old[i].get('metadata') or raw_by_old[i].get('meta')).get('session') or ''),origin_key) for i in refs if i in raw_by_old}
            complete=bool(refs) and all(i in raw_by_old for i in refs) and len(scopes)==1
            conversation=next(iter(scopes)) if complete else _scope(meta.get('session'),origin_key)
            expected_note_scopes[oid]={'conversation_id':conversation,'refs':refs if complete else []}
            target=by_key.get((conversation,f'legacy:{origin_key}:memory:{oid}','assistant'))
            if target is None:issue('historical_note_missing',legacy_id=oid,conversation_id=conversation);continue
            mapped_notes[oid]=target['id'];checked['historical_notes']+=1
            if target['text']!=row['text']:issue('historical_note_text_mismatch',legacy_id=oid)
            if target['recorded_at']!=_time(row.get('created_at')):issue('historical_note_time_mismatch',legacy_id=oid)
            m=target['meta'];disclosure,topics=_note_policy(meta,row.get('visibility'),private_notes_policy)
            if row.get('status')=='deleted':disclosure='never_say'
            expected={'source_kind':'imported_memory','original_role':'unknown','original_id':oid,
                'source_uncertain':True,'is_original_conversation_message':False,'skip_derivation':True,
                'import_model_policy':'local_only','derivation_status':'skipped_import','disclosure':disclosure,
                'original_kind':row.get('kind'),'original_source':row.get('source') or '',
                'original_provenance':row.get('provenance') or '',
                'original_source_msg_id':row.get('source_msg_id',0),
                'original_visibility':row.get('visibility'),'legacy_private_notes_policy':private_notes_policy}
            for name,value in expected.items():
                if m.get(name)!=value:issue('historical_note_metadata_mismatch',legacy_id=oid,field=name)
            record=m.get('legacy_record',{})
            for field,value in row.items():
                if field=='text' or isinstance(value,bytes):continue
                if record.get(field)!=value:issue('historical_note_column_mismatch',legacy_id=oid,field=field)
            if complete:checked['linked_memories_expected']+=1
            else:checked['unlinked_memories_expected']+=1
        document_counts=Counter();derived_legacy_ids=set();doc_ids=set();indexed=set()
        imported_docs=[]
        for r in new.execute('SELECT * FROM documents WHERE owner=?',(owner,)):
            row=dict(r);row['meta']=_meta(row['metadata'])
            if row['meta'].get('legacy_origin')==origin_key:imported_docs.append(row)
        for doc in imported_docs:
            doc_ids.add(doc['id']);document_counts[doc['kind']]+=1
            if doc['meta'].get('import_model_policy')!='local_only':issue('document_not_local_only',document_id=doc['id'])
            refs=[dict(r) for r in new.execute('SELECT message_id,start,end FROM document_sources WHERE document_id=?',(doc['id'],))]
            if not refs:issue('document_missing_source',document_id=doc['id'])
            source_disclosure=0
            for ref in refs:
                msg=message_by_id.get(ref['message_id'])
                if not msg or msg['owner']!=owner or msg['conversation_id']!=doc['conversation_id']:
                    issue('source_scope_mismatch',document_id=doc['id']);continue
                if not 0<=ref['start']<ref['end']<=len(msg['text']):issue('invalid_source_span',document_id=doc['id'])
                source_disclosure=max(source_disclosure,DISCLOSURES.get(msg['meta'].get('disclosure','normal'),2))
                if doc['kind']=='raw':
                    indexed.add(msg['id'])
                    if doc['text']!=msg['text'][ref['start']:ref['end']]:issue('raw_chunk_text_mismatch',document_id=doc['id'])
            if DISCLOSURES.get(doc['disclosure'],-1)<source_disclosure:issue('source_policy_weakened',document_id=doc['id'])
            if doc['kind']!='raw':
                oid=doc['meta'].get('original_id');derived_legacy_ids.add(oid)
                spec=expected_note_scopes.get(oid,{})
                if not spec.get('refs'):issue('unlinked_memory_promoted',legacy_id=oid)
                expected_ids={mapped_raw[i] for i in spec.get('refs',[]) if i in mapped_raw}
                if {r['message_id'] for r in refs}!=expected_ids:issue('derived_source_set_mismatch',legacy_id=oid)
                if not doc['meta'].get('source_uncertain'):issue('old_summary_authority_upgraded',legacy_id=oid)
            fts=new.execute('SELECT count(*) FROM document_fts WHERE document_id=?',(doc['id'],)).fetchone()[0]
            if fts!=1:issue('fts_entry_count_mismatch',document_id=doc['id'],count=fts)
        for msg in imported:
            if msg['id'] not in indexed:issue('message_not_raw_indexed',message_id=msg['id'])
        for oid,spec in expected_note_scopes.items():
            if spec['refs'] and oid not in derived_legacy_ids:issue('linked_derived_document_missing',legacy_id=oid)
        jobs=Counter()
        for job in new.execute('SELECT * FROM jobs WHERE owner=?',(owner,)):
            if job['message_id'] in message_by_id or job['document_id'] in doc_ids:
                jobs[(job['kind'],job['status'])]+=1
                if job['kind']=='derive':issue('imported_history_queued_for_derivation',job_id=job['id'])
        imported_expected=len(eligible)+len(supported)
        if len(imported)!=imported_expected:issue('imported_message_count_mismatch',expected=imported_expected,actual=len(imported))
        expected_sessions=list(dict.fromkeys(expected_sessions));scope_details=[]
        for conversation,rows in sorted(scope_rows.items()):
            user_senders={str(r.get('sender') or '') for r in rows if r['role']=='user' and r.get('sender')}
            scope_details.append({'conversation_id':conversation,'raw_messages':len(rows),
                'user_sender_distinct':len(user_senders),'user_sender_hashes':sorted(_label(s) for s in user_senders),
                'mixed_user_senders':len(user_senders)>1,'exact_expected_session':conversation in expected_sessions})
        for session in expected_sessions:
            if session not in scope_rows:warnings.append({'code':'expected_dsh_session_has_no_legacy_raw','conversation_id':session})
        mixed=[s for s in scope_details if s['mixed_user_senders']]
        if mixed:warnings.append({'code':'multiple_user_senders_in_scope_review_required','scopes':len(mixed)})
        if not expected_sessions:warnings.append({'code':'no_expected_dsh_session_supplied_scope_binding_not_asserted'})
        if any(not r.get('session_id') for r in eligible):warnings.append({'code':'some_legacy_raw_rows_have_no_session_id'})
        report={'ok':not problems,'read_only':True,'model_calls':0,'owner':owner,'origin_key':origin_key,
            'private_notes_policy':private_notes_policy,
            'legacy':old_info,'destination':new_info,'counts':{'legacy_raw_total':len(raw),
                'legacy_raw_eligible':len(eligible),'legacy_raw_excluded':len(raw)-len(eligible),
                'legacy_memories_total':len(memories),'legacy_memories_nonempty':len(supported),
                'imported_messages':len(imported),'documents':dict(document_counts),'verified':dict(checked),
                'source_duplicate_stable_key_groups':sum(n>1 for n in source_stable.values()),
                'source_repeated_body_groups':sum(n>1 for n in same_body.values()),
                'source_roles':dict(Counter(r.get('role') for r in raw)),
                'legacy_memory_kinds':dict(Counter(str(r.get('kind') or '') for r in memories)),
                'legacy_memory_source_labels':dict(Counter(_label(r.get('source')) for r in memories)),
                'legacy_memory_visibility':dict(Counter(str(r.get('visibility') or '') for r in memories)),
                'source_senders':[{'role':k[0],'sender_hash':k[1],'rows':n} for k,n in sorted(sender_rows.items())],
                'jobs':[{'kind':k[0],'status':k[1],'count':n} for k,n in sorted(jobs.items())]},
            'scope':{'adapter_conversation_rule':'String(session.id), no sender prefix',
                'migration_conversation_rule':'legacy session_id verbatim; metadata.session fallback; unscoped namespace otherwise',
                'expected_dsh_sessions':expected_sessions,'details':scope_details},
            'problems':problems,'warnings':warnings,
            'limits':['Aggregate provenance verification is not a recall/answer evaluation.',
                'Sender is a legacy label, not an authenticated identity; mixed labels require owner review.',
                'Missing expected-session raw can be normal for a new chat; cross-conversation lookup is off.',
                'Unknown-source old notes remain labelled uncertain; no guessed source is created.']}
    for field,path in (('legacy',legacy_path),('destination',destination_path)):
        report[field]['main_sha256_after']=_sha(path)
        report[field]['main_file_stable_during_check']=report[field]['main_sha256_before']==report[field]['main_sha256_after']
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--legacy',required=True);p.add_argument('--destination',required=True)
    p.add_argument('--owner',required=True);p.add_argument('--origin-key',required=True)
    p.add_argument('--expected-session',action='append',default=[])
    p.add_argument('--private-notes',choices=['explicit_only','owner'],default='explicit_only')
    p.add_argument('--output',required=True)
    args=p.parse_args();out=Path(args.output).resolve()
    inputs={Path(args.legacy).resolve(),Path(args.destination).resolve()}
    if out in inputs:raise SystemExit('report path must differ from both database paths')
    result=verify_migration(args.legacy,args.destination,args.owner,args.origin_key,args.expected_session,args.private_notes)
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'ok':result['ok'],'checked':result['counts']['verified'],
        'problems':len(result['problems']),'warnings':len(result['warnings']),'report':str(out)},ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
