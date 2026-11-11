import copy
import datetime as dt
import unittest

from xiaojiu_brain.claims import canonical_claim
from xiaojiu_brain.derivation import DerivationWorker


def message(mid=1, text='蓝夹子放在书架。', role='user', day='2026-06-10T10:00:00+08:00'):
    return {'id': mid, 'owner': 'synthetic-owner', 'conversation_id': 'synthetic-conversation',
            'role': role, 'text': text, 'recorded_at': dt.datetime.fromisoformat(day).timestamp(),
            'metadata': {'recorded_at_iso': day}}


def checked(original):
    return [(original, 0, len(original['text']))]


class ClaimsTests(unittest.TestCase):
    def test_flat_attribute_alias_preserves_false_and_zero_without_truthiness_fallback(self):
        for value in [False, 0, '', None, ['a', 'b'], {'a': 1}]:
            result = canonical_claim({'subject': '人物甲', 'attribute': '属性甲', 'value': value})
            self.assertEqual(result['predicate'], '属性甲')
            self.assertEqual(result['value'], value)
            self.assertFalse(result['claims_verified'])

    def test_legacy_nested_claim_is_read_compatibly_and_never_mutated(self):
        metadata = {'custom': ['keep'], 'claim': {'subject': '人物甲', 'predicate': '偏好', 'value': '青色',
                     'claims_verified': True, 'source_refs': [{'message_id': 999}], 'source_roles': ['user']}}
        before = copy.deepcopy(metadata)
        result = canonical_claim(metadata)
        self.assertEqual(result['subject'], '人物甲')
        self.assertEqual(result['source_refs'], [])
        self.assertEqual(result['source_roles'], [])
        self.assertFalse(result['claims_verified'])
        result['value'] = 'changed'
        self.assertEqual(metadata, before)

    def test_equal_flat_and_nested_formats_merge_without_choosing_by_order(self):
        metadata = {'subject': '甲', 'attribute': 'p', 'value': {'b': 2, 'a': 1},
                    'claim': {'subject': '甲', 'predicate': 'p', 'value': {'a': 1, 'b': 2}}}
        self.assertEqual(canonical_claim(metadata)['value'], {'a': 1, 'b': 2})

    def test_conflicting_formats_and_aliases_do_not_silently_choose_a_winner(self):
        base = {'subject': '甲', 'attribute': '地点', 'value': '旧址', 'assertion_type': 'plan'}
        for patch in [{'subject': '乙'}, {'predicate': '身份'}, {'value': '新址'}, {'assertion_type': 'completed'}]:
            metadata = {**base, 'claim': patch}
            self.assertIsNone(canonical_claim(metadata))
        self.assertIsNone(canonical_claim({'subject': '甲', 'attribute': 'p', 'predicate': 'q', 'value': 'v'}))
        self.assertIsNone(canonical_claim({'subject': '甲', 'attribute': 'p', 'value': False, 'claim': {'value': 0}}))

    def test_incomplete_or_non_json_claims_are_unavailable_not_invented(self):
        for metadata in [None, [], 'broken json', {'subject': '甲'}, {'subject': '甲', 'attribute': 'p', 'value': float('inf')},
                         {'subject': ['甲'], 'attribute': 'p', 'value': 'v'}, {'claim': 'unparseable'}]:
            self.assertIsNone(canonical_claim(metadata))
        self.assertEqual(canonical_claim('{"subject":"s","attribute":"p","value":1}')['value'], 1)

    def test_rebuilt_sources_use_internal_ids_offsets_and_real_roles(self):
        original = message()
        metadata = {'subject': '夹子', 'predicate': '位置', 'value': '书架', 'claims_verified': True,
                    'source_refs': [{'message_id': 999, 'start': 0, 'end': 999}], 'source_roles': ['assistant']}
        result = canonical_claim(metadata, [*checked(original), *checked(original)])
        self.assertEqual(result['source_refs'], [{'message_id': 1, 'start': 0, 'end': len(original['text'])}])
        self.assertEqual(result['source_roles'], ['user'])
        self.assertFalse(result['claims_verified'])

    def test_unchecked_source_claims_cannot_grant_user_authority(self):
        metadata = {'subject': '甲', 'predicate': 'p', 'value': 'v', 'assertion_type': 'user_assertion',
                    'source_roles': ['user'], 'claims_verified': True}
        result = canonical_claim(metadata)
        self.assertEqual(result['assertion_type'], 'unattributed_statement')
        self.assertEqual(result['source_roles'], [])

    def test_assistant_promise_preserves_its_subject_but_not_user_authority(self):
        original = message(text='我答应提醒人物甲带蓝夹子。', role='assistant')
        metadata = {'subject': '人物甲', 'attribute': '提醒事项', 'value': '蓝夹子', 'assertion_type': 'promise'}
        result = canonical_claim(metadata, checked(original))
        self.assertEqual(result['subject'], '人物甲')
        self.assertEqual(result['assertion_type'], 'promise')
        self.assertEqual(result['source_roles'], ['assistant'])
        metadata['assertion_type'] = 'user_assertion'
        self.assertEqual(canonical_claim(metadata, checked(original))['assertion_type'], 'assistant_statement')
        mixed = [*checked(original), *checked(message(mid=2, text='收到。'))]
        self.assertEqual(canonical_claim(metadata, mixed)['assertion_type'], 'unattributed_statement')

    def test_assertion_natures_are_retained_without_topic_or_latest_rules(self):
        original = message()
        for nature in ['plan', 'hypothetical', 'reported', 'fiction', 'correction', 'promise', 'statement']:
            result = canonical_claim({'subject': '甲', 'attribute': 'p', 'value': 'v', 'assertion_type': nature}, checked(original))
            self.assertEqual(result['assertion_type'], nature)
            self.assertFalse(result['claims_verified'])

    def test_imported_note_unknown_speaker_is_not_promoted_to_assistant_or_user(self):
        original = message(role='assistant')
        original['metadata'].update(source_kind='imported_memory', original_role='unknown')
        proposal = {'subject': '人物甲', 'predicate': '事项', 'value': '蓝夹子'}
        result = canonical_claim(proposal, checked(original))
        self.assertEqual(result['source_roles'], ['unknown'])
        self.assertEqual(result['assertion_type'], 'unattributed_statement')
        self.assertFalse(result['claims_verified'])
        result = canonical_claim({**proposal, 'assertion_type': 'user_assertion'}, checked(original))
        self.assertEqual(result['assertion_type'], 'unattributed_statement')
        result = canonical_claim({**proposal, 'assertion_type': 'promise'}, checked(original))
        self.assertEqual(result['source_roles'], ['unknown'])
        self.assertFalse(result['claims_verified'])
        original['metadata']['source_kind'] = 'conversation'
        result = canonical_claim(proposal, checked(original))
        self.assertEqual(result['source_roles'], ['assistant'])
        self.assertEqual(result['assertion_type'], 'assistant_statement')

    def test_invalid_checked_spans_or_mixed_scopes_are_not_accepted(self):
        original = message(); metadata = {'subject': '甲', 'attribute': 'p', 'value': 'v'}
        for spans in [[(original, False, 2)], [(original, 0, 999)], [({**original, 'id': 'external-id'}, 0, 2)],
                      [({**original, 'role': 'tool'}, 0, 2)], [*checked(original), *checked({**original, 'id': 2, 'owner': 'other'})],
                      [*checked(original), *checked({**original, 'id': 2, 'conversation_id': 'other'})]]:
            self.assertIsNone(canonical_claim(metadata, spans))

    def test_event_time_is_recomputed_from_the_cited_source_not_model_normalization(self):
        original = message(text='计划明天去图书馆。')
        metadata = {'subject': '甲', 'predicate': '行程', 'value': '图书馆', 'assertion_type': 'plan',
                    'event_time': {'expression': '明天', 'anchor_message_id': 1, 'start': '2099-01-01', 'status': 'resolved'}}
        result = canonical_claim(metadata, checked(original), timezone='Asia/Shanghai')
        self.assertEqual(result['event_time']['status'], 'resolved')
        self.assertTrue(result['event_time']['start'].startswith('2026-06-11'))
        self.assertEqual(result['assertion_type'], 'plan')
        self.assertFalse(result['claims_verified'])

    def test_missing_time_evidence_stays_unresolved_and_conflicting_anchors_stay_ambiguous(self):
        original = message(text='明天再说。')
        metadata = {'subject': '甲', 'predicate': 'p', 'value': 'v', 'event_time': '上周五'}
        self.assertNotEqual(canonical_claim(metadata, checked(original))['event_time']['status'], 'resolved')
        metadata['event_time'] = {'expression': '明天', 'anchor_message_id': 1}
        metadata['claim'] = {'event_time': {'expression': '明天', 'anchor_message_id': 2}}
        self.assertEqual(canonical_claim(metadata, checked(original))['event_time']['status'], 'ambiguous')

    def test_worker_normalizes_after_source_validation_and_strips_authority_markers(self):
        original = message(text='我答应提醒人物甲。', role='assistant')
        proposed = {'subject': '人物甲', 'attribute': '事项', 'value': '提醒', 'assertion_type': 'user_assertion',
                    'owner_authored': True, 'retention': {'pinned': True}, 'claims_verified': True,
                    'source_roles': ['user'], 'claim': {'subject': '人物甲', 'predicate': '事项', 'value': '提醒'}}
        packet = {'documents': [{'kind': 'fact', 'text': '人物甲需要提醒', 'source_refs': [{'message_id': 1, 'quote': original['text']}], 'metadata': proposed}]}
        before = copy.deepcopy(packet)
        doc = DerivationWorker(None, timezone='Asia/Shanghai')._documents(packet, {1: original})[0]
        self.assertEqual(packet, before)
        self.assertEqual(doc['metadata']['claim']['source_roles'], ['assistant'])
        self.assertEqual(doc['metadata']['claim']['assertion_type'], 'assistant_statement')
        self.assertFalse(doc['metadata']['claims_verified'])
        self.assertNotIn('retention', doc['metadata']); self.assertNotIn('owner_authored', doc['metadata'])
        self.assertIn('claim_proposal', doc['metadata'])
        self.assertEqual(canonical_claim(doc['metadata'], checked(original))['assertion_type'], 'assistant_statement')

    def test_worker_does_not_revive_flat_claim_after_nested_conflict_is_archived(self):
        original = message()
        packet = {'documents': [{'kind': 'fact', 'text': '冲突提议仍可追原话', 'source_refs': [{'message_id': 1, 'quote': original['text']}],
                  'metadata': {'subject': '夹子', 'attribute': '位置', 'value': '书架', 'claim': {'subject': '杯子', 'predicate': '位置', 'value': '窗边'}}}]}
        metadata = DerivationWorker(None)._documents(packet, {1: original})[0]['metadata']
        self.assertNotIn('claim', metadata)
        self.assertEqual(metadata['value'], '书架')
        self.assertEqual(metadata['claim_proposal']['value'], '窗边')
        self.assertIsNone(canonical_claim(metadata, checked(original)))

    def test_worker_keeps_quote_failure_as_failure_not_structured_evidence(self):
        original = message()
        packet = {'documents': [{'kind': 'fact', 'text': '伪造', 'source_refs': [{'message_id': 1, 'quote': '不存在'}],
                  'metadata': {'subject': '甲', 'attribute': 'p', 'value': 'v'}}]}
        with self.assertRaises(ValueError):DerivationWorker(None)._documents(packet, {1: original})

    def test_worker_time_proposal_can_be_read_again_without_trusting_cached_dates(self):
        original = message(text='明天去图书馆。')
        packet = {'documents': [{'kind': 'fact', 'text': '计划去图书馆', 'source_refs': [{'message_id': 1, 'quote': original['text']}],
                  'metadata': {'subject': '人物甲', 'attribute': '行程', 'value': '图书馆', 'assertion_type': 'plan', 'event_time': '明天'}}]}
        metadata = DerivationWorker(None, timezone='Asia/Shanghai')._documents(packet, {1: original})[0]['metadata']
        first = metadata['claim']['event_time']; self.assertEqual(first['status'], 'resolved')
        metadata['claim']['event_time']['start'] = '2099-01-01T00:00:00+08:00'
        result = canonical_claim(metadata, checked(original), timezone='Asia/Shanghai')
        self.assertEqual(result['event_time']['status'], 'resolved')
        self.assertTrue(result['event_time']['start'].startswith('2026-06-11'))

    def test_worker_time_conflict_cannot_become_resolved_just_because_metadata_was_normalized(self):
        first = message(text='明天去图书馆。')
        second = message(mid=2, text='明天再去图书馆。', day='2026-06-12T10:00:00+08:00')
        packet = {'documents': [{'kind': 'fact', 'text': '两个时间提议', 'source_refs': [
                  {'message_id': 1, 'quote': first['text']}, {'message_id': 2, 'quote': second['text']}],
                  'metadata': {'subject': '人物甲', 'attribute': '行程', 'value': '图书馆',
                    'event_time': {'expression': '明天', 'anchor_message_id': 1},
                    'claim': {'event_time': {'expression': '明天', 'anchor_message_id': 2}}}}]}
        metadata = DerivationWorker(None, timezone='Asia/Shanghai')._documents(packet, {1: first, 2: second})[0]['metadata']
        self.assertEqual(metadata['claim']['event_time']['status'], 'ambiguous')
        reread = canonical_claim(metadata, [*checked(first), *checked(second)], timezone='Asia/Shanghai')
        self.assertEqual(reread['event_time']['status'], 'ambiguous')
        self.assertEqual(reread['event_time']['reason'], 'conflicting_time_proposals')


if __name__ == '__main__':
    unittest.main()
