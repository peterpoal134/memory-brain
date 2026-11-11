"""Optional navigation between admitted claims with the same literal key.

The result is only a source-backed candidate, never a verified update, identity,
or truth. Admission, time bounds, deletion and privacy are enforced by caller.
"""
from __future__ import annotations
import re
import unicodedata

_QUESTION=re.compile(r'[?？]|(?:怎么|如何|哪[个些]|还记得|有没|是否|到底|之前|以前|先前)')
_VETO=re.compile(r'别提|不要提|不想提|别说|不要说|不想聊|保密|隐私')
_GENERIC={'user','assistant','unknown','unspecified','用户','助手','自己','我','你','他','她'}


def _normalize(value):
    if not isinstance(value,str) or not 3<=len(value.strip())<=80:return ''
    result=' '.join(unicodedata.normalize('NFKC',value).casefold().split())
    return '' if result in _GENERIC else result


def update_candidates(documents,claims,references,messages,routes,similarities,query,*,limit=3):
    trace={'enabled':True,'candidate_count':0,'meaning':'matching_unverified_claim_key_not_verified_update'}
    if _VETO.search(query) or not _QUESTION.search(query):
        trace['suppressed']='question_or_boundary';return [],trace
    def qualifying(key):
        doc=documents.get(key,{});claim=claims.get(key)
        if doc.get('kind') not in ('fact','summary') or doc.get('disclosure','normal')!='normal' or not isinstance(claim,dict):return None
        subject=_normalize(claim.get('subject'));predicate=_normalize(claim.get('predicate'))
        if not subject or not predicate:return None
        userrefs=[]
        for ref in references.get(key,[]):
            src=messages.get(ref['message_id'],{})
            if ref.get('role')!='user' or not ref.get('is_original_utterance'):continue
            if (src.get('metadata') or {}).get('disclosure','normal')!='normal':continue
            if subject in unicodedata.normalize('NFKC',ref.get('text','')).casefold():userrefs.append(ref)
        if not userrefs:return None
        return (subject,predicate),max(float(ref['recorded_at']) for ref in userrefs)
    anchors=[]
    for route in ('derived_lexical','derived_semantic'):
        for key in routes.get(route,[])[:5]:
            if route.endswith('semantic') and similarities.get(key,0)<.78:continue
            info=qualifying(key)
            if info:anchors.append((key,*info))
    if not anchors:return [],trace
    selected=[]
    for key in claims:
        info=qualifying(key)
        if not info:continue
        claim_key,stamp=info
        if any(key!=anchor and claim_key==anchor_key and stamp>anchor_stamp
               for anchor,anchor_key,anchor_stamp in anchors):
            selected.append((stamp,key))
    selected.sort(key=lambda x:(-x[0],x[1]))
    result=[key for _,key in selected[:min(3,max(0,int(limit)))]]
    trace['candidate_count']=len(result)
    return result,trace
