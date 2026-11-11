# Implementation contract

Independent memory framework and request-context integration; existing user configuration and raw data are preserved.
Python package: xiaojiu_brain. Core has no AstrBot/DSH imports. Model providers are injected.

## Store (storage.py)

`Store(path)` uses SQLite WAL and a shared RLock; connections may be used by HTTP/worker threads. Dicts are JSON serializable. `close()` closes cleanly.

- ingest(owner, conversation_id, message_id, role, text, recorded_at=None, metadata=None): dict accepted/duplicate/id. Timestamp accepts ISO string or epoch seconds. Only user/assistant visible finals. Stable key(owner,conversation,message_id,role); same key/different body raises ValueError. Create raw chunks + lexical index and durable work in the same transaction. No punctuation/keyword semantic veto.
- documents(owner, conversation_id=None, as_of=None, include_archived=False): list of document dicts.
- lexical_search(owner, query, conversation_id=None, limit=40, as_of=None): independently ranked document dicts, scope BEFORE top-k.
- get_message(owner, message_id): original message or None.
- list_messages(owner, conversation_id=None, limit=50, offset=0): list.
- list_memories(owner, kind=None, status=None, limit=50): list.
- add_derived(owner, conversation_id, kind, text, source_refs, metadata=None, importance=.5, disclosure='normal'): document dict. Validate source IDs/spans/scope, never trust model-supplied quote text.
- set_vector(owner, document_id, vector, model): persist numeric vector/model version.
- update_memory(owner, document_id, patch): dict; pin/importance/status/disclosure, no authority to mutate original words.
- delete_message(owner, message_id), delete_memory(owner, document_id): explicit deletion also removes related indexes/derived disclosures.
- stats(owner), job_stats(), backup(destination), export_json(owner): JSON dicts.

Document fields: id string, kind(raw/summary/fact), text, owner, conversation_id, role, recorded_at epoch, importance float, status(active/archived/deleted), pinned bool, disclosure(normal/only_if_asked/never_say), source_refs [{message_id:int,start:int,end:int}], metadata dict, vector(list|None), vector_model(str|None).
Message fields: id(int), owner, conversation_id, message_id(external str), role, text, recorded_at(epoch), metadata.
Worker-specific job APIs may be chosen inside storage.py/derivation.py, parent does not rely on them.

## Providers (providers.py, parent owned)

EmbeddingProvider(model, url, key=None, timeout=3).embed_many(texts)->list[list[float]], raises ProviderError. model attribute is a string. Can be absent.
ChatProvider(model,url,key=None,timeout=45).complete_json(system,user,max_tokens=1200)->dict. Can be absent. Strict total deadline transport.

## Derivation (derivation.py)

DerivationWorker(store,embedder=None,llm=None,interval=2,batch_size=12).start()/stop()/run_once(). Index raw immediately, summarize/fact extraction asynchronously. Models propose source spans; code validates. Failure keeps raw searchable and durable retry status visible. No similarity-only fact deletion. Do not merge different subjects/attributes/roles. No model calls in unit tests.

## Retrieval (retrieval.py)

Retriever(store,embedder=None,reranker=None).recall(owner,conversation_id,query,token_budget=1200,as_of=None,include_archived=False,exclude_message_ids=None)->dict.
Return evidence(list), assertions(list), conflicts(list), coverage_gaps(list), degraded(list[str]), trace(dict), block(str), policy_version(str).
Evidence: document_id, kind, text, role, recorded_at, source_refs(validated original spans), score/rank info, metadata. Provide all relevant alternatives; do not choose latest by surface words. Candidate selection uses independent lexical and semantic routes + RRF + diversity/coverage under token budget. Cross-conversation is OFF by default (None only explicit administrative operations). No production round14 import, no question-pattern regex veto, no gold clues.
Optional reranker `.score(query,documents)->list[float]`; model absence/failure is visible lexical degradation. Salience is bounded post-ranking, never a semantic candidate admission gate.

## HTTP (api.py, parent owned)

Bearer token + one configured owner, loopback default 18880. Static panel /panel. JSON replies `{ok:true,data:...}` or `{ok:false,error:...}`. Cookie/CSRF may be added by parent. No CORS wildcard, no body/query/credential logging.
POST /v1/ingest body conversation_id,message_id,role,text,recorded_at,metadata.
POST /v1/recall body conversation_id,query,token_budget. Owner never from request.
GET /v1/status (stats/jobs/config-safe/build_id); GET /v1/messages?conversation_id=&limit=; GET /v1/memories?kind=&status=.
GET /v1/evidence/<message-db-id>; PATCH /v1/memories/<document-id>; DELETE /v1/messages/<id>; DELETE /v1/memories/<document-id>.
POST /v1/remember creates a user manual note; body conversation_id,text,importance(optional),disclosure(optional).
POST /v1/remember-important is an explicit, source-bound operation; body
`{conversation_id,request_message_id,source_refs?,disclosure?}`. IDs are stable
external user-message IDs already acknowledged by `/v1/ingest`; omitted refs use
the current request verbatim. Each ref is `{message_id,start?,end?}` with Unicode
code-point offsets, never model-rewritten text. Only original user sources in the
authenticated owner's same conversation at or before the request are eligible.
The operation atomically reuses or creates literal fact projections, pins them at
fixed importance 0.95, and records validated request provenance. It never creates
replacement original messages. Optional disclosure may only tighten inherited
source/request restrictions; pinning does not bypass privacy. Replaying an old
request is idempotent and does not undo later manual unpinning or archiving.
Receipt data contains `accepted`, `duplicate`, `document_ids`,
`source_message_ids`, and `items:[{id,pinned,importance,disclosure,status,duplicate}]`.
Callers must inspect actual `status` as well as `pinned` before claiming a memory
is currently active. `metadata.retention` authority is assigned by this operation;
model derivation cannot propose its trusted markers.
POST /v1/backup; GET /v1/export; GET /v1/trace/<trace-id>; GET/PATCH /v1/settings (safe mutable settings).
GET /v1/extensions; GET/PATCH /v1/emotion (owner state, nonauthoritative signals); GET/POST /v1/self (owner-authored self notes).
No model key returned by any API. Disallowed model configuration writes rejected.

## DSH plugin and panel (adapter/UI owner)

adapters/dsh/xiaojiu-brain.mjs exports apply(ctx,config), name. Runtime SessionStore module path comes from config.sessionModule or bare import, not hardcoded E path. Capture visible messages via source.kind user/model, no reasoning/tools/injected context. Durable local queue and idempotent replay. Hook pre-step returns requestMessages from /v1/recall only for this request, never into session history. Preserve other plugins' decisions; catch backend failure without blocking conversation. Deduplicate per stable message ID and handle tool steps. Backend URL/token configurable. No automatic mounting/restarts of production.
panel.html and panel assets live inside xiaojiu_brain/static/. User-facing Chinese. Browser login uses HttpOnly local sessions with CSRF protection; external adapters use the management bearer token, never model keys. Render user text through textContent, no unsafe HTML. Status, recall with source drilldown, messages/memories, pin/archive/importance, backups/export, jobs, extension toggles. Responsive desktop/mobile. Clear empty/degraded states and no unsupported performance claims.

## Local life layer (0.2.0a5)

`LifeLayer(store, owner, timezone, settings_callable)` stores owner-scoped life
events, tasks, books and tick cursors in additive SQLite tables. It does not call
models, execute tools, fetch URLs, send messages, or insert synthetic experiences
into factual memory. Simulated routine states, owner-authored notes, actual local
text excerpts and source-linked conversation activity are explicitly distinguished.
All source excerpts retain their original text and offsets; reading progress is
program processing, not evidence of model understanding.

Authenticated `/v1/life` returns state; `/events`, `/tasks`, `/books` provide CRUD;
`/journal` composes a deterministic view, and POST `/tick` checks normal scheduling
without bypassing the interval. Life tables are in the existing online DB backup
and are included in JSON export. A code-only rollback to a4 leaves these additive
tables intact.

The existing DSH adapter receives the optional life block through `/v1/recall`.
This labelled state channel is separate from evidence; it never changes retrieval
candidates, scores or source provenance. The existing evidence budget is retained;
the labelled life state has a separate maximum of 160 estimated input tokens,
including its separator. Trace reports evidence, life and total estimates distinctly.
This is an estimator ceiling, not a provider-tokenizer usage claim. Per-conversation tasks
never cross into another conversation, even if cross-conversation factual retrieval
is enabled. No host plugin change or DSH restart is needed for this integration.

## Historical candidate scope

Candidate package + actual local isolated acceptance + install/import tools. No production route/config/DB change or restart without concrete deployment review. Do not claim benchmark improvement from new synthetic regression tests.

## Archive confirmation and context management (0.2.1)

Authenticated `POST /v1/archive/confirm` takes `{conversation_id,messages:[{message_id,role,body_sha256}]}` (1..256 exact field sets, role user/assistant, SHA-256 hex). The configured owner is authoritative. It returns `{confirmed,items:[{message_id,role,state}]}` where state is stored/missing/deleted/mismatch. Confirmation checks current original body and delivery key; it never writes, queues, restores deleted data or returns source text.

The DSH adapter's turn-based compaction only calls the active agent-scoped native service after selected raw originals have been acknowledged and confirmed. The native service owns balanced tool boundaries, durable replacement and cancellation. Original Session events are retained; compact checkpoints are not original user messages. Existing host pressure/manual compaction remains host-owned.

Verified final-request wrappers perform a conservative text budget check after transient memory injection. Over-budget errors are explicit and must not be swallowed by a fail-open compatibility branch. Unknown model capacity and media pricing are visible partial scopes. Context policy has a separate owner-only settings file and existing setup CSRF protection; it is not an extra Python configuration field.

## Authorized literal session history (0.2.2)

Authenticated `POST /v1/history/search` accepts `conversation_id`, the current
external user `request_message_id` and exact `request_body_sha256`, literal
`keyword` (at most 80 Unicode code points), optional timezone-qualified ISO
`from_time`/`to_time`, booleans `auto_time`/`ascending`, optional host-event epoch
milliseconds `capture_after`, and at most 48 `sources`. Each source contains
`seq`, external `message_id`, `role` (user/assistant), `body_sha256`, host
`event_time` and optional `neighbor_of` sequence. No caller-supplied source body,
owner or arbitrary session selection is accepted by the DSH tool. The server's
configured owner is authoritative. Both capture and recall must be enabled.

The adapter projects genuine visible originals from the current native Session
log, not compact checkpoints, tool output, reasoning, injected messages or
personal aliases. It never archives old host material as a lookup side effect.
The acknowledged current user and every candidate's exact original hash must
still exist in the same owner/conversation, at or before that question. Deleted
originals, inactive raw projections, imported notes and owner notes cannot be
recovered through host history. Existing disclosure rules apply to originals
and every overlapping source span, including restricted derived projections.
`only_if_asked` uses the actual stored current question and owner-approved
topics/questions; a model-selected search keyword does not grant permission.

Replies contain `scope:current_conversation`, `state`, bounded `results` groups
with `match`/`previous`/`next`, returned-match sequence cursors, explicit
`coverage:bounded_host_candidates` and truncation. Quote rows carry external and
internal source IDs, speaker role, UTC timestamp, exact Unicode `start`/`end`
offsets and original `text`. At most four matches, 400 code points per quote and
12KB of backend JSON are returned. The adapter verifies each returned quote
against its host original. Empty output means no authorized match in the bounded
search, never proof that something was never said. `auto_time` only interprets
explicit 今天/昨天 in the real current question using the configured timezone;
上午 narrows that explicit day to midnight–noon. Other times require explicit
ISO bounds. It does not infer a date from a model keyword or a personal name.

Native tool `search_conversation_history` accepts only literal `keyword`, optional
time bounds and exclusive `before_seq`/`after_seq` cursors. Each lookup inspects at
most 4096 host events and 512000 UTF-16 text units, skipping individual originals
over 65536 units. Its `host_scan` states the applied limits and whether coverage
was limited. These limits keep local work bounded; they do not promise an
exhaustive arbitrary-length history search. The tool makes no model calls.
Missing/old backends visibly fail without falling back to unauthorised host text.

Automatic lookup only runs for a clear historical question with an extracted
literal keyword when core factual recall returned no evidence. Profile/life
blocks do not count as factual evidence. It adds complete, explicitly untrusted
historical entries within 2400 UTF-8 bytes to request-only material. One result
is frozen for that same user's tool continuation, without another backend call
or repeated block in a request; the next user turn obtains fresh authorization.
As with core per-turn recall, a deletion or permission change during that already
admitted turn does not rewrite its frozen input. No lookup block is written to
native history. Adapter capture/recall/history switches, cancellation and unload
disable lookup. Status exposes counts and fixed reason codes, never content,
IDs, credentials or local paths.
