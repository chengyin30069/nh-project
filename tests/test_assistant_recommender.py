import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from server.library_db import LibraryDatabase
from server.assistant.service import AssistantService
from server.assistant.provider import FakeModelProvider, ProviderError
from server.assistant.documents import document

class RecommenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = LibraryDatabase(Path(self.tmp.name))
        for gid, artist, pages in [(1, 'alice', 20), (2, 'bob', 80), (3, 'alice', 100)]:
            archive = Path(self.tmp.name)/f'{gid}.cbz'
            with zipfile.ZipFile(archive, 'w') as cbz: cbz.writestr('1.jpg', b'image')
            self.library.upsert_gallery(archive, {'id': gid, 'title': {'english': f'Calm book {gid}'}, 'num_pages': pages,
                'tags': [{'id': 1 if artist == 'alice' else 2, 'type': 'artist', 'name': artist}]}, complete=True, source='test')
        self.provider = FakeModelProvider()
        self.service = AssistantService(self.library, {'enabled': True, 'background_enabled': False, 'rerank_enabled': True}, provider=self.provider)
        self.addCleanup(self.service.close)

    def test_hard_filters_unknown_ids_and_zero_matches(self):
        plan = {'required': [{'kind': 'artist', 'value': 'alice'}], 'page_range': {'max': 40, 'hard': True}, 'semantic_query': 'Calm'}
        self.provider.replies = [plan, {'results': [{'id': '2'}, {'id': '999'}, {'id': '1'}]}]
        result = self.service.recommend({'message': 'alice short'})
        self.assertEqual([r['id'] for r in result['results']], ['1'])
        self.assertEqual(result['results'][0]['detail_url'], '/g/1/')
        self.provider.replies = [plan | {'excluded': [{'kind': 'artist', 'value': 'alice'}]}]
        before = len(self.provider.calls)
        self.assertEqual(self.service.recommend({'message': 'none'})['results'], [])
        self.assertEqual(len(self.provider.calls)-before, 1)

    def test_offline_followup_and_exclusions(self):
        self.provider.configured = False
        result = self.service.recommend({'message': 'Calm', 'previous_plan': {'excluded_gallery_ids': ['1'], 'required': [{'kind': 'artist', 'value': 'alice'}]}})
        self.assertEqual([r['id'] for r in result['results']], ['3'])
        self.assertTrue(result['warnings'])

    def test_repair_and_unresolved(self):
        self.provider.replies = ['oops', {'required': [{'kind': 'artist', 'value': 'unknown'}], 'semantic_query': 'Calm'}, {'results': [{'id': '999'}]}, 'oops']
        result = self.service.recommend({'message': 'Calm'})
        self.assertEqual(len(result['unresolved_terms']), 1)
        self.assertEqual(len(result['results']), 3)
        self.assertTrue(any('invalid model' in w for w in result['warnings']))

    def test_changes_delete_and_index_resume(self):
        for gid in ('1', '2', '3'): self.service.indexer.enqueue(gid)
        self.assertTrue(self.service.indexer.process_once())
        self.assertEqual(self.service.health()['metadata_index']['indexed'], 3)
        for gid in ('1', '2', '3'): self.assertFalse(self.service.indexer.enqueue(gid))
        self.assertFalse(self.service.indexer.process_once())
        self.library.delete_gallery('1')
        self.assertEqual(self.service.health()['metadata_index']['indexed'], 2)
        self.assertNotIn(1, [gid for gid, _ in self.service.vectors.search([1]*8)])

    def test_retry_and_batch_halving(self):
        for gid in ('1', '2', '3'): self.service.indexer.enqueue(gid)
        original = self.provider.embed_texts
        self.provider.embed_texts = lambda **kw: (_ for _ in ()).throw(ProviderError('throttled', transient=True, status=429, retry_after=30))
        self.service.indexer.process_once()
        self.assertEqual(self.service.health()['metadata_index']['jobs']['retry_wait'], 3)
        with self.service.db.connect() as db: db.execute('UPDATE assistant_jobs SET next_retry_at=0')
        self.provider.embed_texts = lambda **kw: (_ for _ in ()).throw(ProviderError('payload', status=413))
        self.service.indexer.process_once()
        self.assertEqual(self.service.indexer.batch_size, 1)
        self.provider.embed_texts = original
        for _ in range(3): self.service.indexer.process_once()
        self.assertEqual(self.service.health()['metadata_index']['indexed'], 3)

    def test_canonical_no_private_paths_and_determinism(self):
        record = self.library.assistant_gallery('1')
        one = document(record)
        record['path'] = '/secret/path'; record['cookie'] = 'secret'
        self.assertEqual(document(record), one)
        self.assertNotIn('secret', one[0])

    def test_metadata_change_during_rerank_is_rechecked(self):
        original = self.provider.chat
        def chat(**kwargs):
            if kwargs['purpose'] == 'rerank': self.library.delete_gallery('1')
            return original(**kwargs)
        self.provider.chat = chat
        self.provider.replies = [{'semantic_query': 'Calm'}, {'results': [{'id': '1'}]}]
        self.assertEqual(self.service.recommend({'message': 'Calm'})['results'], [])

    def test_resolver_alias_fuzzy_and_ambiguity(self):
        self.library.search_aliases = [{'艾莉絲', 'alice'}]
        self.assertEqual(self.library.assistant_resolve({'kind': 'artist', 'value': '艾莉絲'})['name'], 'alice')
        self.assertEqual(self.library.assistant_resolve({'kind': 'artist', 'value': 'alcie'})['name'], 'alice')
        self.library.search_aliases = [{'alice', 'bob', 'both'}]
        self.assertIsNone(self.library.assistant_resolve({'kind': 'artist', 'value': 'both'}))

    def test_restart_does_not_scan_but_model_change_does(self):
        self.service.close()
        restarted = AssistantService(self.library, {'enabled': True, 'background_enabled': False}, provider=FakeModelProvider())
        self.addCleanup(restarted.close)
        self.assertIsNone(restarted.indexer.scan_thread)
        restarted.close()
        changed = AssistantService(self.library, {'enabled': True, 'background_enabled': False, 'embedding_model': 'new-model'}, provider=FakeModelProvider())
        self.addCleanup(changed.close)
        changed.indexer.scan_thread.join(3)
        self.assertEqual(changed.health()['metadata_index']['jobs']['queued'], 3)
        self.assertEqual(changed.db.state('metadata_scan_pending'), '0')

    def test_interrupted_scan_resumes_cursor(self):
        self.service.db.state('metadata_scan_pending', 1)
        self.service.db.state('metadata_scan_after', 1)
        self.service.close()
        restarted = AssistantService(self.library, {'enabled': True, 'background_enabled': False}, provider=FakeModelProvider())
        self.addCleanup(restarted.close)
        restarted.indexer.scan_thread.join(3)
        self.assertEqual(restarted.health()['metadata_index']['jobs']['queued'], 2)
        self.assertEqual(restarted.db.state('metadata_scan_pending'), '0')

    def test_corrupt_sidecar_does_not_touch_catalog(self):
        self.service.close()
        self.service.db.path.write_bytes(b'not sqlite')
        recovered = AssistantService(self.library, {'enabled': True, 'background_enabled': False}, provider=FakeModelProvider())
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.health()['metadata_index']['total'], 3)
        self.assertEqual(recovered.health()['metadata_index']['indexed'], 0)
        self.assertTrue(list(self.service.db.path.parent.glob('assistant.sqlite3.corrupt-*')))

    def test_fast_path_skips_quality_and_bounds_metadata_reads(self):
        self.service.config['rerank_enabled'] = False
        from unittest.mock import patch
        original = self.library.assistant_records
        reads = []
        def records(ids):
            reads.append(len(ids))
            return original(ids)
        self.provider.replies = [{'semantic_query': 'Calm'}]
        with patch.object(self.library, 'assistant_records', side_effect=records):
            result = self.service.recommend({'message': 'Calm'})
        self.assertTrue(result['results'])
        self.assertFalse(any(c.get('purpose') == 'rerank' for c in self.provider.calls))
        self.assertLessEqual(sum(reads), 405)
        self.assertIn('elapsed_seconds', result)
        self.assertIn('local_search', result['timings'])
