"""Recall resource regressions using only a synthetic SQLite database."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiaojiu_brain.retrieval import Retriever, _cosine, _cosine_for_query, _prepare_cosine_query
from xiaojiu_brain.storage import Store


class RecallResources(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.source = self.store.ingest(
            'owner', 'conversation', 'first', 'user', 'The spare key is in the copper cabinet.',
            recorded_at=100,
        )
        self.document_id = self.source['document_ids'][0]
        self.store.set_vector('owner', self.document_id, [0.1, 0.2, -0.4], 'stub')

    def test_lexical_recall_reads_no_vectors_and_returns_original(self):
        statements = []
        self.store.conn.set_trace_callback(statements.append)
        result = Retriever(self.store).recall('owner', 'conversation', 'spare key', as_of=200)
        self.assertEqual(result['evidence'][0]['text'], 'The spare key is in the copper cabinet.')
        self.assertEqual(result['trace']['selected_sources'][0]['message_id'], self.source['id'])
        self.assertFalse(any('from vectors' in statement.lower() for statement in statements))
        self.assertEqual(len(self.store._vector_cache), 0)

    def test_lexical_projection_returns_same_ids_ranks_and_scores(self):
        args = ('owner', 'spare key')
        options = {'conversation_id': 'conversation', 'as_of': 200}
        full = self.store.lexical_search(*args, **options)
        self.assertEqual(full[0]['vector'], [0.1, 0.2, -0.4])
        statements = []
        self.store.conn.set_trace_callback(statements.append)
        light = self.store.lexical_search(*args, **options, include_details=False)
        self.assertEqual(light, [{key: row[key] for key in ('id', 'lexical_score')} for row in full])
        self.assertFalse(any('from vectors' in statement.lower() or 'from document_sources' in statement.lower()
                             for statement in statements))

    def test_compact_view_is_readonly_and_does_not_follow_storage_updates(self):
        baseline = [0.1, 0.2, -0.4]
        view = self.store.documents('owner', vector_format='compact')[0]['vector']
        self.assertTrue(view.readonly)
        with self.assertRaises(TypeError):
            view[0] = 0
        for query in ([0.3, 0.6, 1.0], [-5e-300, 1e-300, 2e-300], [2, 1, 3], [3, 3]):
            self.assertEqual(_cosine_for_query(_prepare_cosine_query(query), view), _cosine(query, baseline))
        ordinary = self.store.get_document('owner', self.document_id)['vector']
        self.assertEqual(ordinary, baseline)
        ordinary[0] = 99
        self.assertEqual(list(view), baseline)
        self.store.set_vector('owner', self.document_id, [-0.2, 0.9, 0.7], 'stub')
        self.assertEqual(list(view), baseline)
        self.assertEqual(self.store.documents('owner')[0]['vector'], [-0.2, 0.9, 0.7])

    def test_legacy_vector_types_remain_serializable(self):
        with self.store._transaction():
            self.store.conn.execute('UPDATE vectors SET vector=? WHERE document_id=?',
                                    (json.dumps([1, 2, -4]), self.document_id))
        for mode in ('list', 'compact'):
            legacy = self.store.documents('owner', vector_format=mode)[0]['vector']
            self.assertEqual(legacy, [1, 2, -4])
            self.assertTrue(all(type(value) is int for value in legacy))

    def test_recall_selects_a_consistent_embedder_during_settings_changes(self):
        class Stub:
            model = 'stub'

            def embed_many(self, texts):
                return [[0.1, 0.2, -0.4] for _ in texts]

        retriever = Retriever(self.store, embedder=Stub())
        original_documents = self.store.documents

        def disable_semantic(*args, **kwargs):
            retriever.embedder = None
            return original_documents(*args, **kwargs)

        with patch.object(self.store, 'documents', side_effect=disable_semantic):
            result = retriever.recall('owner', 'conversation', 'spare key', as_of=200)
        self.assertEqual(result['trace']['semantic_indexed_documents'], 1)
        following = retriever.recall('owner', 'conversation', 'spare key', as_of=200)
        self.assertEqual(following['degraded'], ['embedding_not_configured'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
