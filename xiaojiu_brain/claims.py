"""Conservative, read-only normalization of proposed claims and checked sources.

A structured claim is an index proposal, never semantic proof. Subject and
speaker are independent; only the caller's re-read original spans supply roles.
"""
from __future__ import annotations

import copy
import json

from .temporal import time_metadata

_FIELDS = ('subject', 'predicate', 'value', 'assertion_type')
_USER_AUTHORITY_TYPES = frozenset(('user_assertion', 'user_statement', 'user_fact',
                                 'user_confirmed', 'owner_assertion', 'owner_statement'))


def _metadata(value):
    if isinstance(value, str):
        try:value = json.loads(value)
        except (ValueError, TypeError):return None
    return value if isinstance(value, dict) else None


def _json_key(value):
    # Type-aware equality: 0, False and "0" are not interchangeable claims.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':'))


def _sources(checked_sources):
    refs, roles, checked, seen, messages = [], set(), [], set(), {}
    owners, conversations = set(), set()
    for item in checked_sources or []:
        if not isinstance(item, (tuple, list)) or len(item) != 3:return None
        message, start, end = item
        if not isinstance(message, dict):return None
        mid, text, role = message.get('id'), message.get('text'), message.get('role')
        if isinstance(mid, bool) or not isinstance(mid, int) or mid <= 0:return None
        if not isinstance(text, str) or role not in ('user', 'assistant'):return None
        # Imported notes use an assistant storage row only for compatibility;
        # it is not evidence that the historical speaker was an assistant.
        source_metadata = _metadata(message.get('metadata')) or {}
        if source_metadata.get('source_kind') == 'imported_memory' and source_metadata.get('original_role') == 'unknown':
            role = 'unknown'
        if any(isinstance(offset, bool) or not isinstance(offset, int) for offset in (start, end)):return None
        if not 0 <= start < end <= len(text):return None
        if mid in messages and messages[mid] != (role, text):return None
        messages[mid] = role, text
        for key, values in (('owner', owners), ('conversation_id', conversations)):
            if message.get(key) is not None:
                if not isinstance(message[key], str):return None
                values.add(message[key])
        if len(owners) > 1 or len(conversations) > 1:return None
        identity = mid, start, end
        if identity in seen:continue
        seen.add(identity); roles.add(role); checked.append((message, start, end))
        refs.append({'message_id': mid, 'start': start, 'end': end})
    return refs, sorted(roles), checked


def _assertion_type(value, roles):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:return None
    value = value.strip().casefold()
    if value in _USER_AUTHORITY_TYPES and roles != ['user']:
        return 'assistant_statement' if roles == ['assistant'] else 'unattributed_statement'
    if value == 'statement' and roles == ['assistant']:return 'assistant_statement'
    if value == 'statement' and roles == ['unknown']:return 'unattributed_statement'
    return value


def canonical_claim(metadata, checked_sources=None, timezone='UTC'):
    """Return a canonical proposed claim, or None for missing/conflicting fields.

    Accept legacy flat subject/attribute/value and nested claim objects without
    modifying them. Sources, speaker roles and verification flags from metadata
    are never accepted as proof. checked_sources contains re-read message dicts
    and already-validated Python/Unicode offsets, using internal DB message IDs.
    Event-time proposals are independently checked by the temporal module.
    """
    metadata = _metadata(metadata)
    if metadata is None:return None
    if metadata.get('claim_normalization_status') == 'ambiguous_or_incomplete':return None
    if checked_sources is not None and not isinstance(checked_sources, (list, tuple)):return None
    sources = _sources(checked_sources)
    if sources is None:return None
    refs, roles, checked = sources
    nested = metadata.get('claim')
    if nested is not None and not isinstance(nested, dict):return None
    proposals = [metadata] + ([nested] if isinstance(nested, dict) else [])
    merged = {}
    try:
        for field in _FIELDS:
            candidates = []
            for proposal in proposals:
                keys = ('predicate', 'attribute') if field == 'predicate' else (field,)
                for key in keys:
                    if key not in proposal:continue
                    value = proposal[key]
                    if field in ('subject', 'predicate'):
                        if not isinstance(value, str) or not value.strip() or len(value) > 500:return None
                        value = value.strip()
                    elif field == 'assertion_type':
                        value = _assertion_type(value, roles)
                        if value is None:return None
                    _json_key(value)  # Reject non-JSON/non-finite proposed values.
                    candidates.append(value)
            if candidates:
                if len({_json_key(value) for value in candidates}) != 1:return None
                merged[field] = copy.deepcopy(candidates[0])
    except (TypeError, ValueError, OverflowError):return None
    if not all(field in merged for field in ('subject', 'predicate', 'value')):return None
    merged.setdefault('assertion_type', _assertion_type('statement', roles))
    result = {'schema_version': 1, **merged, 'claims_verified': False,
              'source_refs': refs, 'source_roles': roles}
    time_input = metadata
    original_nested = metadata.get('claim_proposal')
    if metadata.get('claim_normalization_status') == 'canonical' and isinstance(original_nested, dict) and 'event_time' in original_nested:
        # Re-reading a normalized row must not erase an original flat/nested
        # disagreement by comparing the flat proposal with its own cached result.
        time_input = dict(metadata, claim={**(nested or {}), 'event_time': original_nested['event_time']})
    event_time = time_metadata(time_input, checked, timezone=timezone)
    if event_time is not None:result['event_time'] = event_time
    return result
