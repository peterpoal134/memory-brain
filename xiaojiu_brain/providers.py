from __future__ import annotations
import json, math, re, time, uuid
from .transport import post_json, UpstreamHTTPError, UpstreamTransportError, UpstreamTimeout

class ProviderError(RuntimeError):
    """Provider failure with a safe, stable classification and no model text."""
    category = 'provider_error'

    def __init__(self, message=None, *, finish_reason=None, http_status=None):
        super().__init__(message or self.category)
        self.finish_reason = finish_reason
        self.http_status = http_status


class OutputTruncatedError(ProviderError):category = 'output_truncated'
class InvalidJSONError(ProviderError):category = 'invalid_json'
class EmptyResponseError(ProviderError):category = 'empty'
class ProviderTransportError(ProviderError):category = 'transport'
class ProviderHTTPError(ProviderError):category = 'provider_http'


class UsageRecordingError(ProviderError):
    category = 'usage_recording_error'

    def __init__(self, *, original_outcome, original_error_category=None):
        super().__init__()
        self.original_outcome = original_outcome
        self.original_error_category = original_error_category


def _token_number(value):
    return value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def _reported_usage(reply):
    """Normalize only reported accounting; unknown usage is never zero cost."""
    raw = reply.get('usage') if isinstance(reply, dict) else None
    if not isinstance(raw, dict):return None
    def first(*values):return next((n for v in values if (n := _token_number(v)) is not None), None)
    details = raw.get('prompt_tokens_details') or raw.get('input_tokens_details') or {}
    if not isinstance(details, dict):details = {}
    usage = {
        'input_tokens':first(raw.get('prompt_tokens'), raw.get('input_tokens')),
        'output_tokens':first(raw.get('completion_tokens'), raw.get('output_tokens')),
        'cache_hit_tokens':first(raw.get('prompt_cache_hit_tokens'), details.get('cached_tokens')),
        'cache_miss_tokens':first(raw.get('prompt_cache_miss_tokens')),
        'total_tokens':first(raw.get('total_tokens')),
    }
    if usage['cache_miss_tokens'] is None and usage['input_tokens'] is not None and usage['cache_hit_tokens'] is not None:
        if usage['cache_hit_tokens'] <= usage['input_tokens']:
            usage['cache_miss_tokens'] = usage['input_tokens'] - usage['cache_hit_tokens']
    return usage if any(v is not None for v in usage.values()) else None


def _request_context(context):
    """Only generated correlation IDs/counts may enter an operational record."""
    if not isinstance(context, dict):return {}
    result = {}
    for key in ('run_id', 'batch_id', 'parent_batch_id', 'root_batch_id', 'mode'):
        value = context.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):result[key] = value
    for key in ('message_count', 'job_count', 'chunk_count', 'split_depth'):
        value = context.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:result[key] = value
    ids = context.get('job_ids')
    if isinstance(ids, (list, tuple)) and len(ids) <= 128 and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in ids):
        result['job_ids'] = list(ids)
    return result

class EmbeddingProvider:
    def __init__(self,model,url,key=None,timeout=3):
        self.model=model;self.url=url;self.key=key;self.timeout=timeout;self.last_error=None

    def embed_many(self,texts):
        if not texts:return []
        results=[]
        try:
            for start in range(0,len(texts),16):
                batch=texts[start:start+16]
                headers={'Authorization':'Bearer '+self.key} if self.key else {}
                reply=post_json(self.url,{'model':self.model,'input':batch},headers=headers,total_timeout=self.timeout,read_timeout=self.timeout)
                data=reply.get('data',[])
                if not isinstance(data,list) or len(data)!=len(batch):raise ProviderError('Embedding response count mismatch')
                indices=[r.get('index') for r in data]
                if any(isinstance(i,bool) or not isinstance(i,int) for i in indices) or set(indices)!=set(range(len(batch))):raise ProviderError('Embedding response index mismatch')
                data=sorted(data,key=lambda r:r['index'])
                for row in data:
                    vector=[float(v) for v in row['embedding']]
                    if not vector or not all(math.isfinite(v) for v in vector):raise ProviderError('Invalid embedding vector')
                    if results and len(vector)!=len(results[0]):raise ProviderError('Embedding dimension changed')
                    results.append(vector)
            self.last_error=None;return results
        except Exception as e:
            self.last_error=type(e).__name__;raise ProviderError(self.last_error) from None

class ChatProvider:
    supports_request_context = True

    def __init__(self,model,url,key=None,timeout=45):
        self.model=model;self.url=url.rstrip('/');self.key=key;self.timeout=timeout;self.last_error=None;self.calls=0;self.usage={}
        self.attempts=0
        self.attempt_recorder=None

    def complete_json(self,system,user,max_tokens=1200,*,request_context=None):
        request={'model':self.model,'temperature':0,'max_tokens':max_tokens,'messages':[{'role':'system','content':system},{'role':'user','content':user}]}
        # This provider is used only for deriving data, never as a hidden answerer.
        if 'deepseek' in self.model.lower():request['thinking']={'type':'disabled'}
        started_at=time.time();started=time.monotonic()
        outcome='provider_error';error=None;obj=None;usage=None;finish_reason=None;http_status=None
        self.attempts+=1
        try:
            endpoint=self.url if self.url.endswith('/chat/completions') else self.url+'/chat/completions'
            reply=post_json(endpoint,request,headers={'Authorization':'Bearer '+self.key} if self.key else {},total_timeout=self.timeout,read_timeout=min(20,self.timeout))
            self.calls+=1
            usage=_reported_usage(reply)
            raw_usage=reply.get('usage') if isinstance(reply,dict) else None
            for k,v in (raw_usage.items() if isinstance(raw_usage,dict) else []):
                if _token_number(v) is not None:self.usage[k]=self.usage.get(k,0)+v
            choice=reply['choices'][0]
            raw_finish=choice.get('finish_reason')
            finish_reason=raw_finish if raw_finish in ('stop','length','max_tokens','max_output_tokens','content_filter','tool_calls','function_call') else ('other' if raw_finish is not None else None)
            # A syntactically complete JSON prefix is not a complete model answer.
            if finish_reason in ('length','max_tokens','max_output_tokens'):
                raise OutputTruncatedError(finish_reason=finish_reason)
            content=choice['message'].get('content')
            if content is None or isinstance(content,str) and not content.strip():raise EmptyResponseError()
            if not isinstance(content,str):raise InvalidJSONError()
            if content.strip().startswith('```'):
                lines=content.strip().splitlines();content='\n'.join(lines[1:-1])
            try:obj=json.loads(content)
            except (ValueError,TypeError):raise InvalidJSONError() from None
            if not isinstance(obj,dict):raise InvalidJSONError()
            outcome='success';self.last_error=None
        except ProviderError as exc:error=exc;outcome=exc.category
        except UpstreamHTTPError as exc:
            http_status=exc.status;error=ProviderHTTPError(http_status=http_status);outcome=error.category
        except (UpstreamTimeout,UpstreamTransportError,TimeoutError,OSError):
            error=ProviderTransportError();outcome=error.category
        except Exception:
            error=ProviderError();outcome=error.category
        if error is not None:self.last_error=error.category
        event={'attempt_id':str(uuid.uuid4()),'model':self.model,'started_at':started_at,
               'elapsed_ms':round((time.monotonic()-started)*1000,3),'outcome':outcome,
               'error':error.category if error else None,'finish_reason':finish_reason,
               'http_status':http_status,'usage':usage,'usage_known':usage is not None,
               'request_context':_request_context(request_context)}
        # Exactly one callback per actual HTTP attempt, including failed parsing.
        # A failed ledger must stop paid work rather than silently lose accounting.
        if self.attempt_recorder is not None:
            try:self.attempt_recorder(event)
            except Exception:
                self.last_error='usage_recording_error'
                raise UsageRecordingError(original_outcome=outcome,original_error_category=error.category if error else None) from None
        if error is not None:raise error from None
        return obj
