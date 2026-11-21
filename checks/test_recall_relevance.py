"""Closed recall regressions against a selected public source tree or wheel."""

from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import math
import sys
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest


OWNER = 'synthetic-owner'
CONVERSATION = 'synthetic-conversation'
NOISE = ('嗯嗯', '对的对的', '好的。', '好滴。', '嗯，嗯！', '可以。', 'ＯＫ。',
         'Yeah, okay, I see.', '嗯OK谢谢！')
TOPIC = '语义索引建好后，先用嵌入模型检查向量维数。'
SYNONYM = '银色跑车的转向需要校准。'


def restrict_effects(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event in {'os.system', 'os.startfile'}:
        raise AssertionError(f'Closed test prohibited external I/O: {event}')
    if event == 'sqlite3.connect' and args != (':memory:',):
        raise AssertionError('Closed test requires an in-memory database')


def body_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class FixedEmbedder:
    model = 'closed-fixture-vectors'

    def __init__(self, vector=(1.0, 0.0), *, model=None):
        self.vector = tuple(vector)
        self.model = model or self.model
        self.calls = []

    def embed_many(self, texts):
        self.calls.extend(texts)
        return [list(self.vector) for _ in texts]


class RecallPrecision(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.counter = 0

    def ingest(self, text, *, vector=None, role='user', owner=OWNER, conversation=CONVERSATION,
               metadata=None, recorded_at=None, model=FixedEmbedder.model):
        self.counter += 1
        external_id = f'fixture-{self.counter}'
        result = self.store.ingest(
            owner, conversation, external_id, role, text,
            recorded_at=100 + self.counter if recorded_at is None else recorded_at,
            metadata=metadata,
        )
        if vector is not None:
            for document_id in result['document_ids']:
                self.store.set_vector(owner, document_id, vector, model)
        return {**result, 'external_id': external_id}

    def recall(self, query, embedder=None, *, automatic=True, as_of=500, **kwargs):
        return Retriever(self.store, embedder=embedder).recall(
            OWNER, CONVERSATION, query, as_of=as_of, automatic=automatic, **kwargs,
        )

    def assert_empty(self, result):
        self.assertEqual(result['block'], '')
        self.assertEqual(result['evidence'], [])
        self.assertEqual(result['assertions'], [])

    def test_lexical_noise_query_leaves_prompt_empty(self):
        for text in NOISE * 8:
            self.ingest(text)
        self.ingest(TOPIC)
        for query in ('嗯嗯，对的对的', '对的！', '嗯，嗯！', '好的。', '好滴。', 'ＯＫ。'):
            with self.subTest(query=query):
                self.assert_empty(self.recall(query))

    def test_lexical_mixed_query_keeps_topic_without_backchannels(self):
        for text in NOISE * 8:
            self.ingest(text, role='assistant')
        self.ingest('书架的第二层有一本旅行杂志。')
        saved = self.ingest(TOPIC)
        result = self.recall('对的，嵌入模型的向量维数怎么检查？')
        self.assertEqual([row['text'] for row in result['evidence']], [TOPIC])
        self.assertEqual(result['trace']['selected_sources'][0]['message_id'], saved['id'])
        self.assertNotIn('书架', result['block'])

    def test_lexical_common_single_character_match_is_insufficient(self):
        self.ingest('自行车的链条昨天重新上油了。')
        self.ingest('书架的第二层有一本旅行杂志。')
        self.assert_empty(self.recall('量子隧穿的实验数据在哪里？'))

    def test_lexical_one_chinese_character_is_not_a_topic(self):
        self.ingest('磁盘阵列要定时检查校验状态。')
        self.assert_empty(self.recall('盘？'))

    def test_lexical_unrelated_english_function_words_are_insufficient(self):
        self.ingest('The warm garden is right beside a lake.')
        self.assert_empty(self.recall('Where is the encrypted recovery capsule?'))

    def test_lexical_common_technical_term_does_not_substitute_for_the_topic(self):
        self.ingest('Python 最新版支持新的静态类型检查。')
        query = '怎么配置 Python 的并发下载限速？'
        self.assertTrue(self.recall(query, automatic=False)['evidence'])
        self.assert_empty(self.recall(query))

    def test_mixed_query_requires_both_named_topic_and_qualifier(self):
        self.ingest('Nginx 配置文件在测试目录。')
        self.ingest('DeepSeek 写作示例在教程目录。')
        self.assert_empty(self.recall('DeepSeek 配置'))
        note = 'DeepSeek 配置文件在开发目录。'
        self.ingest(note)
        self.assertEqual([item['text'] for item in self.recall('DeepSeek 配置')['evidence']], [note])

    def test_lexical_only_matching_cjk_subject_is_insufficient(self):
        self.ingest('苹果手机换屏多少钱？')
        self.ingest('苹果手机电子设备使用手册。')
        self.assert_empty(self.recall('苹果手机电池寿命'))
        self.assert_empty(self.recall('苹果手机电池寿命怎么样？'))
        note = '苹果手机的电池寿命受充电循环影响。'
        self.ingest(note)
        self.assertEqual([item['text'] for item in self.recall('苹果手机电池寿命')['evidence']], [note])

    def test_lexical_a_short_specific_term_still_matches(self):
        self.ingest('宁夏的旅行图册在蓝色书架。')
        self.ingest('宁曦负责交接周报。')
        result = self.recall('宁曦？')
        self.assertEqual([row['text'] for row in result['evidence']], ['宁曦负责交接周报。'])

    def test_lexical_preserves_particles_inside_topic_names(self):
        note = '贴吧的登录测试已完成。'
        self.ingest(note)
        self.ingest('网吧的登录测试已完成。')
        for query in ('贴吧？', '贴吧是什么？'):
            with self.subTest(query=query):
                self.assertEqual([item['text'] for item in self.recall(query)['evidence']], [note])

    def test_lexical_prefix_does_not_suppress_substantive_original(self):
        text = '好滴，离线备份已经存到加密盘。'
        self.ingest(text)
        self.ingest('好的。')
        result = self.recall('离线备份存到哪里了？')
        self.assertEqual([row['text'] for row in result['evidence']], [text])

    def test_long_repeated_backchannel_prefix_still_keeps_substantive_message(self):
        text = '好啊' * 36 + '离线备份已经存到加密盘。'
        self.ingest(text)
        result = self.recall('离线备份存到哪里了？')
        self.assertEqual([row['text'] for row in result['evidence']], [text])

    def test_derived_matches_cannot_reintroduce_low_information_originals(self):
        text = '对的对的。'
        source = self.ingest(text)
        self.store.add_derived(OWNER, CONVERSATION, 'fact', '离线备份位于加密盘。', [
            {'message_id': source['id'], 'start': 0, 'end': len(text)},
        ])
        self.assert_empty(self.recall('离线备份存到哪里了？'))

    def test_derived_assertion_with_a_filtered_source_stays_incomplete(self):
        filler = '对的对的。'
        note = '离线备份已经存到加密盘。'
        sources = [(self.ingest(text), text) for text in (filler, note)]
        derived = self.store.add_derived(OWNER, CONVERSATION, 'fact', '离线备份位于加密盘。', [
            {'message_id': source['id'], 'start': 0, 'end': len(text)} for source, text in sources
        ])
        result = self.recall('离线备份存到哪里了？')
        self.assertEqual([row['text'] for row in result['evidence']], [note])
        version = next(row for row in result['assertions'] if row['document_id'] == derived['id'])
        self.assertFalse(version['source_complete'])

    def test_lexical_evidence_deduplicates_across_source_ids(self):
        text = '离线备份存在加密盘。'
        first = self.ingest(text)
        second = self.ingest(text)
        result = self.recall('离线备份，加密盘')
        self.assertEqual([row['text'] for row in result['evidence']], [text])
        self.assertEqual(self.store.get_message(OWNER, first['id'])['text'], text)
        self.assertEqual(self.store.get_message(OWNER, second['id'])['text'], text)

    def test_dedupe_preserves_speaker_role_and_source_kind(self):
        text = '离线备份存在加密盘。'
        self.ingest(text)
        self.ingest(text, role='assistant')
        self.ingest(text, metadata={'source_kind': 'owner_note'})
        result = self.recall('离线备份，加密盘')
        self.assertCountEqual(
            [(row['role'], row['source_kind']) for row in result['evidence']],
            [('user', 'conversation'), ('assistant', 'conversation'), ('user', 'owner_note')],
        )

    def test_dedupe_preserves_repeated_relative_date_with_different_timestamps(self):
        text = '明天执行离线备份。'
        moments = (100, 86500)
        for moment in moments:
            self.ingest(text, recorded_at=moment)
        result = self.recall('离线备份', as_of=172800)
        self.assertCountEqual([row['recorded_at'] for row in result['evidence']], moments)
        days = {annotation['start'].split('T')[0] for row in result['evidence']
                for annotation in row['time_annotations'] if annotation.get('status') == 'resolved'}
        self.assertEqual(days, {'1970-01-02', '1970-01-03'})

    def test_dedupe_uses_the_same_relative_date_grammar_as_annotations(self):
        moments = (100, 604900)
        for expression, topic, expected in (
            ('明日', '离线备份', {'1970-01-02', '1970-01-09'}),
            ('二天后', '恢复归档', {'1970-01-03', '1970-01-10'}),
            ('下礼拜一', '更新证书', {'1970-01-05', '1970-01-12'}),
        ):
            with self.subTest(expression=expression):
                for moment in moments:
                    self.ingest(f'{expression}执行{topic}。', recorded_at=moment)
                evidence = self.recall(topic, as_of=700000)['evidence']
                self.assertCountEqual([row['recorded_at'] for row in evidence], moments)
                days = {annotation['start'].split('T')[0] for row in evidence
                        for annotation in row['time_annotations'] if annotation.get('status') == 'resolved'}
                self.assertEqual(days, expected)

    def test_dedupe_still_removes_identical_fixed_date_records(self):
        text = '2026-11-21 执行离线备份。'
        for moment in (100, 604900):
            self.ingest(text, recorded_at=moment)
        evidence = self.recall('离线备份', as_of=700000)['evidence']
        self.assertEqual([row['text'] for row in evidence], [text])

    def test_lexical_retains_substantive_short_payload(self):
        self.ingest('C++')
        self.ingest('嗯嗯')
        result = self.recall('C++')
        self.assertEqual([row['text'] for row in result['evidence']], ['C++'])

    def test_semantic_only_synonym_matches_without_lexical_overlap(self):
        saved = self.ingest(SYNONYM, vector=(.98, .2))
        self.ingest('秋天的池塘旁边有芦苇。', vector=(.1, .995))
        result = self.recall('如何维修汽车方向盘？', FixedEmbedder())
        self.assertEqual([row['text'] for row in result['evidence']], [SYNONYM])
        self.assertEqual(result['trace']['selected_sources'][0]['message_id'], saved['id'])
        self.assertEqual(result['trace']['routes']['raw_lexical'], 0)
        self.assertEqual(result['trace']['routes']['raw_semantic'], 1)

    def test_semantic_rank_does_not_force_a_low_similarity_match(self):
        for vector in ((-1.0, 0.0), (0.0, 1.0), (.1, .995)):
            self.ingest('苹果树下的小路铺满碎石。', vector=vector)
        self.assert_empty(self.recall('校验归档索引', FixedEmbedder()))

    def test_semantic_floor_never_fills_budget_with_off_topic_sources(self):
        for model, below, above in [('bge-m3', .61, .63), (FixedEmbedder.model, .64, .66)]:
            with self.subTest(model=model):
                self.ingest('麦田里的灌溉设备需要维护。', vector=(below, math.sqrt(1 - below**2)), model=model)
                self.ingest(SYNONYM, vector=(above, math.sqrt(1 - above**2)), model=model)
                result = self.recall('如何维修汽车方向盘？', FixedEmbedder(model=model))
                self.assertEqual([row['text'] for row in result['evidence']], [SYNONYM])

    def test_semantic_noise_hits_are_filtered_even_at_high_similarity(self):
        for text in NOISE * 5:
            self.ingest(text, vector=(1.0, 0.0), role='assistant')
        self.ingest(TOPIC, vector=(.98, .2))
        result = self.recall('检查语义索引', FixedEmbedder())
        self.assertEqual([row['text'] for row in result['evidence']], [TOPIC])

    def test_semantic_does_not_embed_or_inject_for_bare_backchannels(self):
        self.ingest(TOPIC, vector=(1.0, 0.0))
        embedder = FixedEmbedder()
        self.assert_empty(self.recall('嗯嗯。对的对的。', embedder))
        self.assertEqual(embedder.calls, [])

    def test_semantic_duplicate_is_delivered_once(self):
        self.ingest(SYNONYM, vector=(1.0, 0.0))
        self.ingest(SYNONYM, vector=(1.0, 0.0))
        result = self.recall('汽车方向盘保养', FixedEmbedder())
        self.assertEqual([row['text'] for row in result['evidence']], [SYNONYM])

    def test_lexical_retains_permissions_and_request_cutoff(self):
        self._assert_permissions_and_cutoff()

    def test_semantic_retains_permissions_and_request_cutoff(self):
        self._assert_permissions_and_cutoff(FixedEmbedder())

    def _assert_permissions_and_cutoff(self, embedder=None):
        vector = (1.0, 0.0) if embedder is not None else None
        known = '离线备份用加密盘，校验通过。'
        self.ingest(known, vector=vector)
        self.ingest('离线备份在限制读取的蓝柜。', vector=vector, metadata={'disclosure': 'never_say'})
        self.ingest('离线备份在另一个会话的红柜。', vector=vector, conversation='other-conversation')
        self.ingest('离线备份在另一账户的白柜。', vector=vector, owner='other-owner')
        deleted = self.ingest('离线备份在已删除的灰柜。', vector=vector)
        self.store.delete_message(OWNER, deleted['id'])
        cutoff = self.ingest('哪里有离线备份？', vector=vector)
        self.ingest('离线备份在当前问题之后写入的黑柜。', vector=vector)
        result = self.recall('哪里有离线备份？', embedder, cutoff_message_id=cutoff['id'],
                             exclude_message_ids=[cutoff['id']])
        self.assertEqual([row['text'] for row in result['evidence']], [known])

    def test_raw_archive_and_explicit_history_keep_exact_backchannels(self):
        raw = '嗯嗯，对的对的。'
        saved = self.ingest(raw, role='assistant', recorded_at=101)
        question = '请问你什么时候说过「对的对的」？'
        current = self.ingest(question, recorded_at=200)
        self.assert_empty(self.recall('对的对的', exclude_message_ids=[current['id']]))
        confirmed = self.store.confirm_archive(OWNER, CONVERSATION, [{
            'message_id': saved['external_id'], 'role': 'assistant', 'body_sha256': body_hash(raw),
        }])
        self.assertTrue(confirmed['confirmed'])
        app = SimpleNamespace(
            owner=OWNER, store=self.store,
            extensions=SimpleNamespace(settings=lambda: {'recall_enabled': True, 'capture_enabled': True}),
        )
        request = {
            'conversation_id': CONVERSATION, 'request_message_id': current['external_id'],
            'request_body_sha256': body_hash(question), 'keyword': '对的对的',
            'sources': [{'seq': 1, 'message_id': saved['external_id'], 'role': 'assistant',
                         'body_sha256': body_hash(raw), 'event_time': 101}],
        }
        result = history.search(app, request)
        self.assertEqual(result['state'], 'matched')
        self.assertEqual(result['results'][0]['match']['text'], raw)
        self.assertEqual(history.search(app, {**request, 'automatic': True})['results'], [])

    def test_manual_recall_can_query_literal_response(self):
        text = '对的对的。'
        self.ingest(text)
        result = Retriever(self.store).recall(OWNER, CONVERSATION, '对的对的', as_of=500)
        self.assertEqual([row['text'] for row in result['evidence']], [text])

    def test_automatic_literal_history_keeps_verified_match_without_history_neighbors(self):
        first = self.ingest('嗯嗯，对的。', recorded_at=101)
        second = self.ingest('加密盘里放了离线备份。', recorded_at=102)
        third = self.ingest('我们换个话题，天气转冷了。', recorded_at=103)
        question = '我们以前说过「加密盘」的哪句原话？'
        current = self.ingest(question, recorded_at=104)
        app = SimpleNamespace(
            owner=OWNER, store=self.store,
            extensions=SimpleNamespace(settings=lambda: {'recall_enabled': True, 'capture_enabled': True}),
        )
        sources = [{
            'seq': seq, 'message_id': row['external_id'], 'role': 'user', 'event_time': seq + 100,
            'body_sha256': body_hash(self.store.get_message(OWNER, row['id'])['text']),
            **({'neighbor_of': 2} if seq != 2 else {}),
        } for seq, row in enumerate((first, second, third), 1)]
        result = history.search(app, {
            'conversation_id': CONVERSATION, 'request_message_id': current['external_id'],
            'request_body_sha256': body_hash(question), 'keyword': '加密盘', 'automatic': True,
            'sources': sources,
        })
        self.assertEqual(result['state'], 'matched')
        self.assertEqual(result['results'][0]['match']['text'], '加密盘里放了离线备份。')
        self.assertIsNone(result['results'][0]['previous'])
        self.assertIsNone(result['results'][0]['next'])

    def test_no_matching_records_do_not_create_evidence_header(self):
        self.ingest('烹饪课用新鲜番茄。')
        self.assert_empty(self.recall('量子隧穿'))

    def test_kernel_recall_endpoint_flags_reach_precision_gate(self):
        raw = '嗯嗯，对的对的。'
        self.ingest(raw, role='assistant', recorded_at=101)
        current = self.ingest('嗯嗯，对的对的。', recorded_at=104)
        app = SimpleNamespace(
            owner=OWNER, store=self.store, retriever=Retriever(self.store), build_id='closed-test',
            _apply_settings=lambda: None, _trace_lock=RLock(), _traces=OrderedDict(),
            extensions=SimpleNamespace(settings=lambda: {
                'recall_enabled': True, 'token_budget': 1200, 'cross_conversation': False,
            }),
        )
        body = {
            'conversation_id': CONVERSATION, 'current_message_id': current['external_id'],
            'query': raw, 'include_life_context': False, 'include_profile_context': False,
        }
        before = self.store.conn.total_changes
        self.assert_empty(BrainApp.recall(app, {**body, 'automatic': True}))
        manual = BrainApp.recall(app, body)
        self.assertEqual([item['text'] for item in manual['evidence']], [raw])
        self.assertEqual(self.store.conn.total_changes, before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Public kernel source root or wheel')
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    if source.is_dir() and not (source / 'xiaojiu_brain/__init__.py').is_file():
        parser.error('--source must be the directory containing xiaojiu_brain')
    if source.is_file() and source.suffix != '.whl':
        parser.error('--source must be a .whl or a source root')
    sys.path.insert(0, str(source))
    sys.addaudithook(restrict_effects)
    import xiaojiu_brain
    global BrainApp, Retriever, Store, history
    from xiaojiu_brain import history
    from xiaojiu_brain.app import BrainApp
    from xiaojiu_brain.retrieval import Retriever
    from xiaojiu_brain.storage import Store
    module_file = Path(xiaojiu_brain.__file__)
    if source not in module_file.parents:
        raise AssertionError('Closed tests did not load the selected source')
    print(f'Public kernel {xiaojiu_brain.__version__}: {module_file}', flush=True)
    unittest.main(argv=[sys.argv[0]], verbosity=2)


if __name__ == '__main__':
    main()
