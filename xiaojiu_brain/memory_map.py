"""Read-only cortical placement data, with conservative source disclosure.

The positions drawn by a client are illustrative. Records and previews are
real owner-scoped stored documents; this module calls no model or provider.
"""
from __future__ import annotations

import math
import time


RESTRICTED_PREVIEW = '受限记忆，请在控制面板明确查看'
_KINDS = {'memories':('fact','summary'),'fact':('fact',),'summary':('summary',),
          'raw':('raw',),'all':('raw','fact','summary')}
_DAYS = {'7':7,'30':30,'90':90,'all':None}


def _finite(value, name):
    if isinstance(value,bool):raise ValueError('invalid memory map '+name)
    try:number = float(value)
    except (TypeError,ValueError,OverflowError):raise ValueError('invalid memory map '+name) from None
    if not math.isfinite(number):raise ValueError('invalid memory map '+name)
    return number


def _cutoff(until, now):
    value = now if until is None else _finite(until,'until')
    if value > now+1:raise ValueError('memory map until is in the future')
    return min(value,now)


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError('invalid memory map '+name)
    return value


def _base_where(owner, cutoff, kinds, days=None):
    where = ['d.owner=?',"d.status='active'",'d.recorded_at<=?','d.created_at<=?',
             'd.kind IN ('+','.join('?' for _ in kinds)+')']
    args = [owner,cutoff,cutoff,*kinds]
    if days is not None:where.append('d.recorded_at>=?');args.append(cutoff-days*86400)
    return ' AND '.join(where),args


_DOCUMENT_FIELDS = """d.id,d.owner,d.conversation_id,d.kind,d.role,d.text,d.recorded_at,
    d.importance,d.pinned,d.disclosure,
    CASE WHEN json_valid(d.metadata) THEN json_extract(d.metadata,'$.source_kind') ELSE NULL END AS source_kind,
    json_valid(d.metadata) AS metadata_valid"""


def _source_fields(alias='m'):
    # SQL projections avoid loading private notes, topics or legacy metadata text.
    return f"""{alias}.owner AS source_owner,{alias}.conversation_id AS source_conversation,
        {alias}.recorded_at AS source_at,{alias}.role AS source_role,
        length({alias}.text) AS source_length,
        CASE WHEN json_valid({alias}.metadata) THEN
          CASE WHEN json_type({alias}.metadata,'$.disclosure') IS NULL THEN 'normal'
               WHEN json_extract({alias}.metadata,'$.disclosure') IS NULL THEN 'invalid'
               ELSE json_extract({alias}.metadata,'$.disclosure') END
          ELSE 'invalid' END AS source_disclosure,
        CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata,'$.source_kind')
             ELSE NULL END AS source_kind"""


def _source_valid(row, document, cutoff):
    return (row['source_owner']==document['owner'] and
            row['source_conversation']==document['conversation_id'] and
            row['source_role'] in ('user','assistant') and
            isinstance(row['source_at'],(int,float)) and not isinstance(row['source_at'],bool) and
            math.isfinite(row['source_at']) and row['source_at']<=cutoff)


def _project(app, rows, cutoff, *, full=False):
    if not rows:return []
    conn, owner = app.store.conn, app.owner
    documents = {row['id']:row for row in rows}
    ids = list(documents)
    placeholders = ','.join('?' for _ in ids)
    refs = {ident:[] for ident in ids}
    for row in conn.execute(f"""SELECT s.document_id,s.message_id,s.start,s.end,{_source_fields()}
         FROM document_sources s LEFT JOIN messages m ON m.id=s.message_id
         WHERE s.document_id IN ({placeholders}) ORDER BY s.document_id,s.message_id,s.start,s.end""",ids):
        refs[row['document_id']].append(row)
    restricted = {ident:(doc['disclosure']!='normal' or not doc['metadata_valid']) for ident,doc in documents.items()}
    # Generation can have read an unquoted private message. Include its entire
    # dependency context, and original ancestors of imported notes, as policy.
    ancestors = conn.execute(f"""WITH RECURSIVE context(document_id,message_id) AS (
        SELECT document_id,message_id FROM document_dependencies WHERE document_id IN ({placeholders})
        UNION SELECT document_id,message_id FROM document_sources WHERE document_id IN ({placeholders})
        UNION SELECT c.document_id,p.source_id FROM context c
              JOIN message_dependencies p ON p.dependent_id=c.message_id
        ) SELECT c.document_id,c.message_id,{_source_fields()}
          FROM context c LEFT JOIN messages m ON m.id=c.message_id""",[*ids,*ids]).fetchall()
    for row in ancestors:
        doc = documents[row['document_id']]
        if not _source_valid(row,doc,cutoff) or row['source_disclosure']!='normal':restricted[doc['id']] = True
    # Archived private projections continue to protect overlapping original
    # spans. Do not turn a normal raw chunk into a privacy bypass.
    for row in conn.execute(f"""SELECT DISTINCT candidate.document_id
        FROM document_sources candidate JOIN document_sources protected
          ON protected.message_id=candidate.message_id
         AND candidate.start<protected.end AND protected.start<candidate.end
        JOIN documents p ON p.id=protected.document_id
        JOIN messages m ON m.id=protected.message_id
        WHERE candidate.document_id IN ({placeholders}) AND p.owner=?
          AND m.owner=? AND p.conversation_id=m.conversation_id
          AND p.status!='deleted' AND p.disclosure!='normal'""",[*ids,owner,owner]):
        restricted[row['document_id']] = True
    result = []
    for doc in rows:
        direct, valid_refs = refs[doc['id']], []
        if not direct:restricted[doc['id']] = True
        for ref in direct:
            valid = _source_valid(ref,doc,cutoff)
            valid = valid and type(ref['start']) is int and type(ref['end']) is int and \
                ref['source_length'] is not None and 0<=ref['start']<ref['end']<=ref['source_length']
            if not valid:restricted[doc['id']] = True;continue
            valid_refs.append(ref)
            if ref['source_disclosure']!='normal':restricted[doc['id']] = True
        source_ids = sorted({ref['message_id'] for ref in valid_refs})
        kinds = {ref['source_kind'] for ref in valid_refs}
        if doc['source_kind']=='imported_memory' or 'imported_memory' in kinds:source_kind='imported_note'
        elif doc['source_kind']=='owner_note' or kinds=={'owner_note'}:source_kind='owner_note'
        else:source_kind='original' if doc['kind']=='raw' else 'derived'
        role = doc['role'] if doc['role'] in ('user','assistant') and source_kind!='imported_note' else None
        is_restricted = bool(restricted[doc['id']])
        text = doc['text'] if isinstance(doc['text'],str) else ''
        preview = RESTRICTED_PREVIEW if is_restricted else (text if len(text)<=220 else text[:219]+'…')
        importance = doc['importance']
        if isinstance(importance,bool) or not isinstance(importance,(int,float)) or not math.isfinite(importance):importance=0.
        item = {'id':doc['id'],'kind':doc['kind'],'recorded_at':doc['recorded_at'],
                'preview':preview,'restricted':is_restricted,'pinned':bool(doc['pinned']),
                'importance':max(0.,min(1.,float(importance))),
                'source_ids':source_ids[:8],'source_count':len(source_ids),
                'role':role,'source_kind':source_kind}
        if full:item['full_text'] = None if is_restricted else text
        result.append(item)
    return result


def snapshot(app, *, days='30', kind='memories', limit=200, offset=0, until=None):
    """Page active real documents in a fixed recorded-time window for app.owner."""
    if not isinstance(days,(str,int)) or isinstance(days,bool) or str(days) not in _DAYS:
        raise ValueError('invalid memory map days')
    days = str(days)
    if not isinstance(kind,str) or kind not in _KINDS:raise ValueError('invalid memory map kind')
    limit = _integer(limit,1,300,'limit')
    offset = _integer(offset,0,100000,'offset')
    observed_at = time.time()
    cutoff = _cutoff(until,observed_at)
    where, args = _base_where(app.owner,cutoff,_KINDS[kind],_DAYS[days])
    with app.store.lock:
        total = app.store.conn.execute('SELECT count(*) FROM documents d WHERE '+where,args).fetchone()[0]
        rows = app.store.conn.execute('SELECT '+_DOCUMENT_FIELDS+' FROM documents d WHERE '+where+
            ' ORDER BY d.recorded_at DESC,d.id DESC LIMIT ? OFFSET ?',[*args,limit,offset]).fetchall()
        items = _project(app,rows,cutoff)
    following = offset+len(items)
    remaining = following<total
    truncated = remaining and following>100000
    has_more = remaining and not truncated
    return {'schema_version':1,'observed_at':observed_at,'until':cutoff,'days':days,'kind':kind,
            'total':total,'items':items,'offset':offset,'next_offset':following if has_more else None,
            'has_more':has_more,'layout':'illustrative_cortical_placement',
            'timestamp_basis':'recorded_at','truncated':bool(truncated)}


def detail(app, document_id):
    """Explicit detail read; restricted content still requires the management UI."""
    if not isinstance(document_id,str) or not document_id or len(document_id)>512 or '\x00' in document_id:
        raise KeyError('memory map record not found')
    cutoff = time.time()
    where,args = _base_where(app.owner,cutoff,_KINDS['all'])
    with app.store.lock:
        row = app.store.conn.execute('SELECT '+_DOCUMENT_FIELDS+' FROM documents d WHERE '+where+' AND d.id=?',
                                     [*args,document_id]).fetchone()
        if row is None:raise KeyError('memory map record not found')
        item = _project(app,[row],cutoff,full=True)[0]
        item['sources'] = []
        if not item['restricted'] and item['source_ids']:
            placeholders = ','.join('?' for _ in item['source_ids'])
            refs = app.store.conn.execute(f"""SELECT s.message_id,s.start,s.end,m.role,m.recorded_at,
                substr(m.text,s.start+1,s.end-s.start) AS cited_text,
                CASE WHEN json_valid(m.metadata) THEN json_extract(m.metadata,'$.source_kind')
                     ELSE NULL END AS source_kind
                FROM document_sources s JOIN messages m ON m.id=s.message_id
                WHERE s.document_id=? AND m.owner=? AND m.conversation_id=? AND m.recorded_at<=?
                  AND s.message_id IN ({placeholders}) AND s.start>=0 AND s.end>s.start
                  AND s.end<=length(m.text)
                ORDER BY s.message_id,s.start,s.end""",
                [document_id,app.owner,row['conversation_id'],cutoff,*item['source_ids']]).fetchall()
            for ref in refs:
                source_kind = ('imported_note' if ref['source_kind']=='imported_memory' else
                               'owner_note' if ref['source_kind']=='owner_note' else 'original')
                role = ref['role'] if source_kind!='imported_note' and ref['role'] in ('user','assistant') else None
                item['sources'].append({'message_id':ref['message_id'],'start':ref['start'],'end':ref['end'],
                    'role':role,'recorded_at':ref['recorded_at'],'source_kind':source_kind,
                    'text':ref['cited_text']})
        return item
