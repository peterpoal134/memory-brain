"""Owner-authenticated, read-only profile snapshots and important receipts.

Host text is an explicit installation snapshot, not an assertion about the
current model request. This module never discovers files or edits a persona.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from types import SimpleNamespace

from .memory_map import _DOCUMENT_FIELDS, _project


_MANIFEST_BYTES = 512 * 1024
_SOURCE_BYTES = 8 * 1024 * 1024
_SOURCE_COUNT = 32
_IMPORTANT_LIMIT = 50


def _string(value, maximum, *, empty=False):
    if not isinstance(value,str) or len(value)>maximum or '\x00' in value or (not empty and not value.strip()):
        raise ValueError('invalid profile manifest field')
    return value


def _source_hash(path):
    """Hash only one owner-configured local source, with a finite read bound."""
    try:
        target=Path(path)
        if not target.is_file():return None,'source_unavailable'
        digest=hashlib.sha256()
        with target.open('rb') as source:
            remaining=_SOURCE_BYTES
            while remaining:
                chunk=source.read(min(65536,remaining))
                if not chunk:return digest.hexdigest(),None
                digest.update(chunk);remaining-=len(chunk)
            if source.read(1):return None,'source_too_large'
        return digest.hexdigest(),None
    except (OSError,ValueError):return None,'source_unavailable'


def _host_snapshot(config):
    result={'configured':bool(config.profile_sources_file),'status':'not_configured','sources':[],
            'runtime_verified':False,'basis':'owner_configured_disk_snapshot',
            'notice':'宿主人格文件快照；未证明它已进入当前模型请求。'}
    if not config.profile_sources_file:return result
    try:
        with Path(config.profile_sources_file).open('rb') as source:
            raw=source.read(_MANIFEST_BYTES+1)
        if len(raw)>_MANIFEST_BYTES:raise ValueError('manifest_too_large')
        manifest=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(manifest,dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version']!=1:
            raise ValueError('manifest_schema')
        records=manifest.get('sources')
        if not isinstance(records,list) or len(records)>_SOURCE_COUNT:raise ValueError('manifest_sources')
        observed=manifest.get('observed_at')
        if isinstance(observed,bool) or not isinstance(observed,(str,int,float)):
            raise ValueError('manifest_time')
        if isinstance(observed,str):_string(observed,80)
        elif not math.isfinite(observed):raise ValueError('manifest_time')
        sources=[];ids=set()
        # Validate the entire snapshot before returning any of its text.
        for record in records:
            if not isinstance(record,dict) or record.get('kind')!='host_persona':raise ValueError('manifest_kind')
            ident=_string(record.get('id'),128)
            if ident in ids:raise ValueError('manifest_duplicate_id')
            ids.add(ident)
            source_path=_string(record.get('source_path'),4096)
            if not Path(source_path).is_absolute() or '://' in source_path:raise ValueError('manifest_source_path')
            digest=_string(record.get('source_sha256'),64)
            if not re.fullmatch('[0-9a-fA-F]{64}',digest):raise ValueError('manifest_source_hash')
            scope=record.get('role_scope','')
            if isinstance(scope,list):
                if len(scope)>16:raise ValueError('manifest_scope')
                scope=[_string(part,300) for part in scope]
            else:scope=_string(scope,300,empty=True)
            sources.append({'id':ident,'title':_string(record.get('title'),200),
                'text':_string(record.get('text'),32000,empty=True),'source_path':source_path,
                'source_sha256':digest.lower(),'kind':'host_persona',
                'host':_string(record.get('host'),100),'role_scope':scope})
        for record in sources:
            current,error=_source_hash(record['source_path'])
            record['current_sha256']=current
            record['stale']=None if current is None else current!=record['source_sha256']
            record['status']='unavailable' if error else ('stale' if record['stale'] else 'current')
            if error:record['error']=error
        result.update(status='ready',observed_at=observed,sources=sources)
    except (OSError,UnicodeError,json.JSONDecodeError,ValueError,TypeError,OverflowError):
        result.update(status='unavailable',error='profile_manifest_unavailable_or_invalid')
    return result


def _important_rows(app, cutoff):
    explicit="""(CASE WHEN json_valid(d.metadata) THEN
      d.kind='fact' AND json_type(d.metadata,'$.retention.explicit_user_request')='true'
      AND json_type(d.metadata,'$.retention.operation_verified')='true'
      AND json_extract(d.metadata,'$.retention.kind')='explicit_user_request'
      ELSE 0 END)"""
    where="d.owner=? AND d.status='active' AND d.recorded_at<=? AND d.created_at<=? AND (d.pinned=1 OR "+explicit+")"
    args=[app.owner,cutoff,cutoff]
    with app.store.lock:
        total=app.store.conn.execute('SELECT count(*) FROM documents d WHERE '+where,args).fetchone()[0]
        rows=app.store.conn.execute('SELECT '+_DOCUMENT_FIELDS+','+explicit+' AS explicit_user_request '
            'FROM documents d WHERE '+where+' ORDER BY d.recorded_at DESC,d.id DESC LIMIT ?',
            [*args,_IMPORTANT_LIMIT]).fetchall()
        items=_project(app,rows,cutoff)
        for item,row in zip(items,rows):
            item['origin']='explicit_user_request' if row['explicit_user_request'] else 'owner_pinned'
            item['factual_truth_verified']=False
    return {'items':items,'total':total,'limit':_IMPORTANT_LIMIT,'has_more':total>len(items),'status':'ready',
            'notice':'明确记住与置顶的回执；重要性不代表事实已核实。'}


def _important(app, cutoff):
    """Use an independent read-only WAL snapshot, never retrieval's Python lock."""
    connection=None
    try:
        if app.store.path==':memory:':raise ValueError('independent_profile_database_unavailable')
        connection=sqlite3.connect(Path(app.store.path).resolve().as_uri()+'?mode=ro',
                                  uri=True,timeout=.05,isolation_level=None)
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        deadline=time.monotonic()+1.5
        connection.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,1000)
        connection.execute('BEGIN')
        view=SimpleNamespace(owner=app.owner,store=SimpleNamespace(conn=connection,lock=threading.RLock()))
        return _important_rows(view,cutoff)
    except (OSError,ValueError,sqlite3.Error):
        return {'items':[],'total':None,'limit':_IMPORTANT_LIMIT,'has_more':None,
                'status':'unavailable','error':'important_view_unavailable',
                'notice':'重要记忆视图暂时不可用；这不代表没有已保存条目。'}
    finally:
        if connection is not None:connection.close()


def snapshot(app):
    now=time.time();settings=app.extensions.settings()
    return {'schema_version':1,'observed_at':now,'host_persona':_host_snapshot(app.config),
        'self':{**app.extensions.self_notes(),'enabled':settings['self_notes_enabled'],
            'injection':'independent_request_channel','runtime_verified':False,
            'notice':'用户管理的角色与表达笔记；新版适配器通过独立限额通道携带，完整条目超额会明确列为省略，未证明当前宿主已请求。'},
        'standards':app.extensions.standards(),
        'settings':settings,'important':_important(app,now)}
