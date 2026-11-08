"""Bounded relation navigation over already admitted, source-backed claims.

This is a candidate index, not an entity identity resolver or truth graph.
Only exact normalized names join; two people with one name remain alternatives.
No raw records, aliases or relationship claims are invented or overwritten.
"""
from __future__ import annotations
import re
import unicodedata

def _name(value):
    if not isinstance(value,str) or not value.strip() or len(value)>256:return ''
    return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())

def _mentioned(label,query):
    if len(label)<2 or label in {'user','assistant','用户','助手','unknown','unspecified'}:return False
    if re.search('[a-z0-9]',label):return re.search(r'(?<![a-z0-9_])'+re.escape(label)+r'(?![a-z0-9_])',query) is not None
    return label in query

def relation_candidates(documents,claims,query,owner,conversation_id,limit=40):
    """Return document IDs and content-free trace, after the caller's policies.

    Scope is checked again here. The caller remains responsible for source,
    cutoff, deletion and disclosure admission before supplying these mappings.
    A matched subject may navigate to one *existing* named subject via a value.
    Candidates are always rendered from their original evidence by Retriever.
    """
    limit=max(0,min(40,int(limit)))
    admitted={key:doc for key,doc in documents.items() if doc.get('owner')==owner and
        (conversation_id is None or doc.get('conversation_id')==conversation_id) and
        doc.get('kind') in ('fact','summary') and doc.get('status','active')=='active'}
    subjects={}
    for key in sorted(admitted):
        claim=claims.get(key)
        if not isinstance(claim,dict):continue
        label=_name(claim.get('subject'))
        if label:subjects.setdefault(label,[]).append(key)
    normalized=_name(query)
    seeds=[label for label in sorted(subjects,key=lambda s:(-len(s),s)) if _mentioned(label,normalized)][:8]
    chosen=[]
    def add(key):
        if key not in chosen and len(chosen)<limit:chosen.append(key)
    for label in seeds:
        for key in subjects[label]:add(key)
    neighbors=[]
    for key in list(chosen):
        value=_name(claims[key].get('value'))
        if value in subjects and value not in seeds and value not in neighbors:neighbors.append(value)
        if len(neighbors)>=8:break
    for label in neighbors:
        for key in subjects[label]:add(key)
    return chosen,{'kind':'source_backed_candidate_navigation','truth_certified':False,
        'seed_count':len(seeds),'neighbor_count':len(neighbors),'hop_limit':1,
        'candidate_count':len(chosen),'alias_resolution':'exact_normalized_names_only'}
