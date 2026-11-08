"""Independent raw/derived retrieval with scoped, source-faithful evidence.

There are no topic classifiers or question-pattern vetoes. Hard exclusions are
identity/time/status, invalid source spans, and explicit disclosure policies.
"""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import json
import math
import time
import uuid
from zoneinfo import ZoneInfo

from .policy import POLICY_VERSION, READING_POLICY, bounded_salience, disclosure_decision, estimate_tokens
from .claims import canonical_claim
from .relations import relation_candidates
from .temporal import source_times


def _epoch(value):
    if value is None:
        return time.time()
    if isinstance(value, bool):
        raise ValueError("as_of must be a timestamp")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        number = parsed.timestamp()
    if not math.isfinite(number):
        raise ValueError("as_of must be a finite timestamp")
    return number


def _cosine(a, b):
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)) or len(a) != len(b) or not a:
        return None
    try:
        aa = [float(v) for v in a]
        bb = [float(v) for v in b]
        if not all(math.isfinite(x) for x in aa + bb):
            return None
        scale_a, scale_b = max(map(abs, aa)), max(map(abs, bb))
        if not scale_a or not scale_b:
            return None
        aa = [x/scale_a for x in aa]
        bb = [x/scale_b for x in bb]
        norm = math.sqrt(sum(x*x for x in aa) * sum(x*x for x in bb))
        return sum(x*y for x, y in zip(aa, bb)) / norm if norm else None
    except (ValueError, TypeError):
        return None


def _overlap(a, b):
    return a[0] == b[0] and max(a[1], b[1]) < min(a[2], b[2])


def _covered(ref, selected):
    return any(old[0] == ref[0] and old[1] <= ref[1] and old[2] >= ref[2] for old in selected)


class Retriever:
    def __init__(self, store, embedder=None, reranker=None, timezone="UTC", allow_cross_conversation=False):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.timezone = ZoneInfo(timezone)
        self.allow_cross_conversation = bool(allow_cross_conversation)

    def _iso(self, message):
        stored = (message.get("metadata") or {}).get("recorded_at_iso")
        if isinstance(stored, str):
            try:
                parsed = dt.datetime.fromisoformat(stored.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and abs(parsed.timestamp() - float(message["recorded_at"])) < .001:
                    return parsed.isoformat()
            except (ValueError, TypeError):
                pass
        return dt.datetime.fromtimestamp(float(message["recorded_at"]), self.timezone).isoformat()

    def recall(self, owner, conversation_id, query, token_budget=1200, as_of=None,
               include_archived=False, exclude_message_ids=None, requested_topics=None, cutoff_message_id=None):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner is required")
        if conversation_id is None:
            if not self.allow_cross_conversation:
                raise ValueError("conversation_id is required; cross-conversation recall is disabled")
        elif not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be nonempty")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be nonempty")
        if requested_topics is not None and not isinstance(requested_topics, (list, tuple)):
            raise ValueError("requested_topics must be a list")
        if exclude_message_ids is not None and not isinstance(exclude_message_ids, (list, tuple, set, frozenset)):
            raise ValueError("exclude_message_ids must be a collection")
        if isinstance(token_budget, bool) or not isinstance(token_budget, (int, float)) or not math.isfinite(token_budget) or token_budget < 0:
            raise ValueError("token_budget must be nonnegative and finite")
        token_budget = int(token_budget)
        if cutoff_message_id is not None and (type(cutoff_message_id) is not int or cutoff_message_id<=0):raise ValueError('invalid source cutoff')
        cutoff = _epoch(as_of)
        started = time.monotonic()
        trace = {"id": uuid.uuid4().hex, "scope": {"owner": owner, "conversation_id": conversation_id, "as_of": cutoff},
                 "routes": {}, "withheld_documents": 0, "invalid_source_documents": 0,
                 "token_budget": token_budget, "token_estimator": "ceil(utf8_bytes/3)",
                 "coverage_certified": False}
        degraded, gaps = [], []
        excluded_internal = {x for x in (exclude_message_ids or []) if isinstance(x, int) and not isinstance(x, bool)}
        excluded_external = {x for x in (exclude_message_ids or []) if isinstance(x, str)}
        # Read only this owner's scope before any top-k ranking. Archived
        # restricted projections still protect the same source spans.
        scoped = self.store.documents(owner, conversation_id=conversation_id, as_of=cutoff, include_archived=True)
        scoped = [d for d in scoped if d.get("owner") == owner
                  and (conversation_id is None or d.get("conversation_id") == conversation_id)
                  and d.get("status", "active") != "deleted" and float(d.get("recorded_at", 0)) <= cutoff]
        trace["scoped_documents"] = len(scoped)
        messages, protected = {}, []
        for d in scoped:
            permitted, _ = disclosure_decision(d.get("disclosure"), d.get("metadata"), query, requested_topics)
            if not permitted:
                for r in d.get("source_refs") or []:
                    try:
                        protected.append((int(r["message_id"]), int(r["start"]), int(r["end"])))
                    except (KeyError, TypeError, ValueError):
                        pass

        def read_refs(document):
            quotes = []
            if cutoff_message_id is not None and (document.get('max_dependency_id') or 0)>cutoff_message_id:
                return None,'after_question_boundary'
            refs = document.get("source_refs") or []
            if not refs:
                return None, "invalid_source"
            for ref in refs:
                try:
                    mid, start, end = int(ref["message_id"]), int(ref["start"]), int(ref["end"])
                except (KeyError, ValueError, TypeError):
                    return None, "invalid_source"
                if mid in excluded_internal:
                    return None, "excluded_message"
                if cutoff_message_id is not None and mid>cutoff_message_id:return None,'after_question_boundary'
                if mid not in messages:
                    messages[mid] = self.store.get_message(owner, mid)
                source = messages[mid]
                if not source or source.get("owner") != owner or source.get("conversation_id") != document.get("conversation_id"):
                    return None, "invalid_source"
                if conversation_id is not None and source.get("conversation_id") != conversation_id:
                    return None, "invalid_source"
                if str(source.get("message_id")) in excluded_external:
                    return None, "excluded_message"
                text = source.get("text")
                if not isinstance(text, str) or not (0 <= start < end <= len(text)):
                    return None, "invalid_source"
                if source.get("role") not in {"user", "assistant"} or float(source["recorded_at"]) > cutoff:
                    return None, "invalid_source"
                meta = source.get("metadata") or {}
                permission, _ = disclosure_decision(meta.get("disclosure"), meta, query, requested_topics)
                if not permission or any(_overlap((mid, start, end), span) for span in protected):
                    return None, "withheld"
                source_kind = str(meta.get("source_kind") or "conversation")
                imported = source_kind == "imported_memory"
                quotes.append({"message_id": mid, "start": start, "end": end, "text": text[start:end], "quote": text[start:end],
                               "role": "unknown" if imported else source["role"],
                               "source_kind": source_kind, "is_original_utterance": not imported and source_kind != "owner_note",
                               "source_time_semantics": "legacy_record_timestamp_not_verified_utterance_time" if imported else "recorded_at",
                               "recorded_at": float(source["recorded_at"]), "recorded_at_iso": self._iso(source)})
            return quotes, None

        eligible, references = {}, {}
        for doc in scoped:
            if doc.get("status", "active") != "active" and not include_archived:
                continue
            permitted, _ = disclosure_decision(doc.get("disclosure"), doc.get("metadata"), query, requested_topics)
            if not permitted:
                trace["withheld_documents"] += 1
                continue
            quotes, error = read_refs(doc)
            if error:
                counter = "withheld_documents" if error == "withheld" else "invalid_source_documents"
                trace[counter] += 1
                continue
            key = str(doc["id"])
            eligible[key] = doc
            references[key] = quotes
        trace["eligible_documents"] = len(eligible)
        if trace["withheld_documents"]:
            gaps.append({"code": "disclosure_withheld", "count": trace["withheld_documents"],
                         "message": "部分资料因披露限制未进入请求；这不表示记录不存在。"})
        if trace["invalid_source_documents"]:
            gaps.append({"code": "source_unavailable_or_excluded", "count": trace["invalid_source_documents"]})

        normalized_claims={}
        for key,doc in eligible.items():
            if doc.get('kind') not in ('fact','summary'):continue
            checked=[(messages[ref['message_id']],ref['start'],ref['end']) for ref in references[key]]
            claim=canonical_claim(doc.get('metadata') or {},checked_sources=checked,timezone=str(self.timezone))
            if claim is not None:normalized_claims[key]=claim
        routes = {name: [] for name in ["raw_lexical", "derived_lexical", "raw_semantic", "derived_semantic"]}
        try:
            for kind, route in [("raw", "raw_lexical"), ("derived", "derived_lexical")]:
                lexical = self.store.lexical_search(owner, query, conversation_id=conversation_id,
                    limit=40, as_of=cutoff, kind=kind, include_archived=include_archived,cutoff_message_id=cutoff_message_id)
                for d in lexical:
                    key = str(d["id"])
                    if key in eligible and key not in routes[route]:
                        routes[route].append(key)
        except Exception as exc:
            degraded.append("lexical_provider_error:" + type(exc).__name__)
        similarities = {}
        if self.embedder is None:
            degraded.append("embedding_not_configured")
        else:
            try:
                vectors = self.embedder.embed_many([query])
                if len(vectors) != 1 or _cosine(vectors[0], vectors[0]) is None:
                    raise ValueError("invalid query embedding")
                model = str(self.embedder.model)
                for key, d in eligible.items():
                    if d.get("vector_model") != model:
                        continue
                    score = _cosine(vectors[0], d.get("vector"))
                    if score is not None:
                        similarities[key] = score
                for kind, route in [(True, "raw_semantic"), (False, "derived_semantic")]:
                    keys = [key for key in similarities if (eligible[key].get("kind") == "raw") == kind]
                    routes[route] = sorted(keys, key=lambda key: (-similarities[key], key))[:40]
                trace["embedding_model"] = model
                trace["semantic_indexed_documents"] = len(similarities)
                if len(similarities) < len(eligible):
                    degraded.append("semantic_index_incomplete")
            except Exception as exc:
                degraded.append("embedding_provider_error:" + type(exc).__name__)

        route,relation_trace=relation_candidates(eligible,normalized_claims,query,owner,conversation_id)
        routes['entity_relation']=route;trace['relations']=relation_trace
        fused, rank_info = defaultdict(float), defaultdict(dict)
        for route, ids in routes.items():
            trace["routes"][route] = len(ids)
            for rank, key in enumerate(ids, 1):
                fused[key] += 1. / (60 + rank)
                rank_info[key][route] = rank
        if self.reranker is None:
            trace["reranker"] = "optional_not_configured"
        elif fused:
            try:
                keys = sorted(fused, key=lambda key: (-fused[key], key))
                # Rerank only validated originals, never a model proposal that
                # could distort the source or smuggle unrelated private words.
                originals = ["\n".join(f"{r['role']} [{r['recorded_at_iso']}]: {r['text']}"
                                        for r in references[k]) for k in keys]
                values = self.reranker.score(query, originals)
                if len(values) != len(keys) or not all(math.isfinite(float(v)) for v in values):
                    raise ValueError("invalid reranker output")
                ranking = sorted(zip(keys, map(float, values)), key=lambda row: (-row[1], row[0]))
                for rank, (key, value) in enumerate(ranking, 1):
                    fused[key] += 1. / (60 + rank)
                    rank_info[key]["rerank"] = rank
                    rank_info[key]["rerank_score"] = value
            except Exception as exc:
                degraded.append("reranker_provider_error:" + type(exc).__name__)

        units = {}
        for key, score in fused.items():
            doc = eligible[key]
            multiplier = bounded_salience(doc.get("importance", .5), doc.get("pinned", False))
            weighted = score * multiplier
            for ref in references[key]:
                span = (ref["message_id"], ref["start"], ref["end"])
                if span not in units:
                    units[span] = {"ref": ref, "document_id": key, "kind": doc.get("kind", "raw"),
                                   "score": weighted, "supporting_document_ids": [], "rank_routes": {},
                                   "metadata": doc.get("metadata") or {}, "salience_multiplier": multiplier}
                u = units[span]
                u["supporting_document_ids"].append(key)
                u["rank_routes"][key] = rank_info[key]
                if weighted > u["score"]:
                    u.update(document_id=key, kind=doc.get("kind", "raw"), score=weighted,
                             metadata=doc.get("metadata") or {}, salience_multiplier=multiplier)

        header = "### 可追溯记忆材料\n" + READING_POLICY + "\n"
        used, selected, selected_spans = estimate_tokens(header), [], []
        remaining = dict(units)
        omitted = 0
        while remaining:
            source_ids = {r[0] for r in selected_spans}
            def priority(key):
                return units[key]["score"] * (1.08 if key[0] not in source_ids else 1.)
            span = max(remaining, key=lambda key: (priority(key), -key[0], -key[1]))
            u = remaining.pop(span)
            if _covered(span, selected_spans):
                continue
            ref = u["ref"]
            if ref["source_kind"] == "imported_memory":
                label, citation = "旧库导入笔记（非对话原话，原说话者未核实；时间为旧记录时间）", "导入记录"
            elif ref["source_kind"] == "owner_note":
                label, citation = "用户手写笔记", "手写记录"
            else:
                label, citation = ("用户" if ref["role"] == "user" else "助手"), "原话"
            line = f"- [{ref['recorded_at_iso']}] {label}〔{citation}#{span[0]}:{span[1]}-{span[2]}〕：{ref['text']}\n"
            cost = estimate_tokens(line)
            if used + cost > token_budget:
                omitted += 1
                continue
            used += cost
            selected_spans.append(span)
            selected.append({"document_id": u["document_id"], "kind": u["kind"],
                    "text": ref["text"], "role": ref["role"], "recorded_at": ref["recorded_at"],
                    "source_kind": ref["source_kind"], "is_original_utterance": ref["is_original_utterance"],
                    "recorded_at_iso": ref["recorded_at_iso"],
                    "source_refs": [{k: v for k, v in ref.items() if k != "text"}],
                    "score": round(u["score"], 8), "rank_routes": u["rank_routes"],
                    "supporting_document_ids": sorted(set(u["supporting_document_ids"])),
                    "salience_multiplier": u["salience_multiplier"], "metadata": u["metadata"], "_line": line})
        if omitted:
            gaps.append({"code": "token_budget_omitted_evidence", "count": omitted})
        if not fused:
            gaps.append({"code": "no_ranked_candidates", "message": "此次未检索到候选，不能据此断言历史中从未出现。"})
        elif not selected:
            gaps.append({"code": "no_evidence_fits_budget"})
        block = header + "".join(item.pop("_line") for item in selected) if selected else ""
        temporal_included=0;temporal_omitted=0
        for evidence in selected:
            ref=evidence['source_refs'][0]
            source=messages.get(ref['message_id'])
            # Old generated notes have a storage timestamp, not an attested
            # utterance clock. Never use them to resolve relative event dates.
            annotations=source_times(source,ref['start'],ref['end'],timezone=str(self.timezone),limit=3) if source and ref['source_kind']!='imported_memory' else []
            evidence['time_annotations']=annotations
            for annotation in annotations:
                expression=annotation.get('expression','')
                # Explicit full calendar dates need no extra prose in the model
                # request, but remain available as structured source-bound data.
                if annotation.get('status')=='resolved' and any(char.isdigit() for char in expression) and len(expression)>=10 and expression[:4].isdigit():continue
                if annotation.get('status')=='resolved':
                    text=f"{annotation['start']} 至 {annotation['end']}（结束不含，{annotation['timezone']}）"
                else:text='无法唯一确定日期；不要自行补年份或锚点'
                source_label='手写记录' if ref['source_kind']=='owner_note' else '原话'
                line=f"\n时间说明〔{source_label}#{ref['message_id']}〕：{expression} → {text}。这只是日期指代换算，不代表事件已发生。\n"
                if estimate_tokens(block+line)<=token_budget:
                    block+=line;temporal_included+=1
                else:temporal_omitted+=1
        if temporal_omitted:gaps.append({'code':'time_annotation_omitted_budget','count':temporal_omitted})
        assert estimate_tokens(block) <= token_budget
        for rank, evidence in enumerate(selected, 1):
            evidence["rank"] = rank

        assertions = []
        for key in sorted(fused, key=lambda key: (-fused[key], key)):
            doc = eligible[key]
            if doc.get("kind") == "raw":
                continue
            refs = references[key]
            if any(_overlap((r["message_id"], r["start"], r["end"]), span) for r in refs for span in selected_spans):
                metadata=dict(doc.get('metadata') or {})
                metadata.pop('claim',None)
                if key in normalized_claims:metadata['claim']=normalized_claims[key]
                assertions.append({"document_id": key, "kind": doc.get("kind"), "text": doc["text"],
                    "metadata": metadata, "source_refs": [{k:v for k,v in r.items() if k!='text'} for r in refs],
                    "source_complete": all(_covered((r["message_id"], r["start"], r["end"]), selected_spans) for r in refs),
                    "claims_verified": False})
        conflicts = self._alternatives(assertions)
        trace.update(selected_evidence=len(selected), budget_omitted_evidence=omitted,
                     time_annotations_included=temporal_included,time_annotations_omitted=temporal_omitted,
                     estimated_tokens=estimate_tokens(block), ranked_documents=len(fused),
                     selected_document_ids=[r["document_id"] for r in selected],
                     selected_sources=[{"message_id": r["message_id"], "start": r["start"], "end": r["end"]}
                                       for e in selected for r in e["source_refs"]],
                     ranked_candidates=[{"document_id": key, "kind": eligible[key].get("kind"),
                         "rrf_score": round(fused[key], 8), "rank_routes": rank_info[key],
                         "salience_multiplier": bounded_salience(eligible[key].get("importance", .5), eligible[key].get("pinned", False))}
                        for key in sorted(fused, key=lambda key: (-fused[key], key))],
                     degraded=list(dict.fromkeys(degraded)), coverage_gaps=gaps,
                     milliseconds=round((time.monotonic()-started)*1000, 3))
        return {"evidence": selected, "assertions": assertions, "conflicts": conflicts,
                "coverage_gaps": gaps, "degraded": list(dict.fromkeys(degraded)), "trace": trace,
                "block": block, "policy_version": POLICY_VERSION}

    @staticmethod
    def _alternatives(assertions):
        groups = defaultdict(list)
        for assertion in assertions:
            claim = assertion["metadata"].get("claim")
            if not isinstance(claim, dict):
                continue
            if all(k in claim for k in ["subject", "predicate", "value"]):
                groups[(str(claim["subject"]), str(claim["predicate"]))].append((assertion, claim))
        result = []
        for (subject, predicate), rows in groups.items():
            values={json.dumps(claim['value'],ensure_ascii=False,sort_keys=True,allow_nan=False):claim['value'] for _,claim in rows}
            if len(values) > 1:
                result.append({"subject": subject, "predicate": predicate,
                    "status": "unresolved_alternatives", "claims_verified": False,
                    "assertion_ids": [a["document_id"] for a, _ in rows],
                    "values": [values[key] for key in sorted(values)],
                    "message": "存在不同的结构化主张；未根据问法或记录新旧自动覆盖，需结合原话与适用时间核实。"})
        return result
