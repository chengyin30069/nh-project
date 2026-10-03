import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from server.assistant.db import AssistantDatabase
from server.assistant.images import ImageSampleError, _safe_member, positions, sample
from server.assistant.provider import FakeModelProvider
from server.assistant.provider import ProviderError
from server.assistant.service import AssistantService
from server.assistant.settings import settings
from server.assistant.visual import observation
from server.assistant.visual_rollout import advance
from server.library_db import LibraryDatabase


def jpeg():
    output = io.BytesIO()
    Image.new('RGB', (32, 24), 'blue').save(output, 'JPEG')
    return output.getvalue()


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, gid, entries):
        path = self.root / f'{gid}.cbz'
        with zipfile.ZipFile(path, 'w') as cbz:
            for name, content in entries:
                cbz.writestr(name, content)
        return path

    def test_sampling_order_and_bounded_decode(self):
        path = self.archive(1, [('10.jpg', jpeg()), ('2.jpg', jpeg()), ('1.jpg', jpeg())])
        result = sample(path)
        self.assertEqual([p['member'] for p in result['pages']], ['1.jpg', '2.jpg', '10.jpg'])
        self.assertEqual(positions(1), [1])
        self.assertEqual(len(positions(200)), 6)
        self.assertLessEqual(max(map(len, [p['jpeg'] for p in result['pages']])), 524288)
        self.assertNotIn(b'Exif', result['pages'][0]['jpeg'])
        path = self.archive(2, [('1.jpg', b'broken')])
        with self.assertRaises(ImageSampleError):
            sample(path)
        with self.assertRaises(ImageSampleError):
            sample(self.archive(3, [('../1.jpg', jpeg())]))
        encrypted = zipfile.ZipInfo('1.jpg')
        encrypted.flag_bits = 1
        self.assertFalse(_safe_member(encrypted))

    def test_visual_observation_limits(self):
        self.assertEqual(settings({'visual_model': 'z-ai/glm-5-3-flash'})['visual_model'], 'z-ai/glm-5.3-flash')
        data, text, digest = observation({'style': ['thin lines']})
        self.assertIn('thin lines', text)
        self.assertEqual(len(digest), 64)
        for bad in ({'unknown': ['x']}, {'style': ['x'] * 9}, {'style': ['x' * 201]}):
            with self.assertRaises(ValueError): observation(bad)

    def test_compatibility_check_accepts_single_style_string(self):
        library = LibraryDatabase(self.root)
        archive = self.archive(1, [('1.jpg', jpeg()), ('2.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book'},
                                         'num_pages': 2, 'tags': []}, complete=True, source='test')
        provider = FakeModelProvider([{'seen_pages': [1, 2], 'style': 'blue watercolor'}])
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=provider)
        self.addCleanup(service.close)
        result = service.visual_check({'gallery_id': '1'})
        self.assertTrue(result['verified'])
        self.assertEqual(service.db.state('visual_capability_verified'), service.visual_indexer.namespace)

    def test_large_operation_embeds_before_all_summaries_finish(self):
        library = LibraryDatabase(self.root)
        for gid in range(1, 18):
            archive = self.archive(gid, [('1.jpg', jpeg())])
            library.upsert_gallery(archive, {'id': gid, 'title': {'english': f'Book {gid}'},
                                             'num_pages': 1, 'tags': []}, complete=True, source='test')
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=FakeModelProvider())
        self.addCleanup(service.close)
        service.db.state('visual_capability_verified', service.visual_indexer.namespace)
        operation = service.visual_index({'scope': 'ids', 'gallery_ids': [str(gid) for gid in range(1, 18)]})
        for _ in range(100):
            service.visual_indexer.process_once()
            current = service.visual_operation(operation['operation_id'])
            if current['counts'].get('searchable', 0) >= 16:
                break
        self.assertEqual(current['counts'].get('searchable'), 16)
        self.assertEqual(current['counts'].get('summary_pending'), 1)

    def test_pilot_rollout_requires_quality_and_creates_one_full_scan(self):
        library = LibraryDatabase(self.root)
        for gid in range(1, 21):
            archive = self.archive(gid, [('1.jpg', jpeg())])
            library.upsert_gallery(archive, {'id': gid, 'title': {'english': f'Book {gid}'},
                                             'num_pages': 1, 'tags': []}, complete=True, source='test')
        provider = FakeModelProvider([{'style': ['blue watercolor'], 'composition': ['wide panels and close-ups']}] * 20)
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=provider)
        self.addCleanup(service.close)
        namespace = service.visual_indexer.namespace
        service.db.state('visual_capability_verified', namespace)
        pilot_id = service.visual_index({'scope': 'ids', 'gallery_ids': [str(gid) for gid in range(1, 21)]})['operation_id']
        self.assertIsNone(advance(service.db, pilot_id, namespace))
        for _ in range(100):
            service.visual_indexer.process_once()
            if service.db.operation(pilot_id)['status'] == 'completed':
                break
        self.assertEqual(service.db.operation(pilot_id)['counts'].get('searchable'), 20)
        full_id = advance(service.db, pilot_id, namespace)
        self.assertEqual(full_id, advance(service.db, pilot_id, namespace))
        self.assertEqual(service.db.operation(full_id)['scope'], 'all')
        self.assertEqual(service.db.latest_visual_operation_id(namespace), full_id)
        self.assertEqual(service.db.state('visual_pilot_approved'), namespace)

    def test_visual_stages_and_ordinary_search_never_uploads(self):
        library = LibraryDatabase(self.root)
        for gid in (1, 2):
            archive = self.archive(gid, [('1.jpg', jpeg()), ('2.jpg', jpeg())])
            library.upsert_gallery(archive, {'id': gid, 'title': {'english': f'Book {gid}'},
                                             'num_pages': 2, 'tags': [{'id': gid, 'type': 'artist', 'name': f'Artist {gid}'}]}, complete=True, source='test')
        provider = FakeModelProvider()
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=provider)
        self.addCleanup(service.close)
        # A query cannot start image work even with a visual intent.
        provider.replies = [{'visual_query': 'blue pages', 'semantic_query': 'Book'}]
        service.recommend({'message': 'blue pages'})
        self.assertFalse(any(c.get('purpose') == 'visual' for c in provider.calls))
        operation = service.visual_index({'scope': 'ids', 'gallery_ids': ['1', '2']})
        for _ in range(15):
            service.visual_indexer.process_once()
            if service.visual_operation(operation['operation_id'])['status'] == 'completed':
                break
        current = service.visual_operation(operation['operation_id'])
        self.assertEqual(current['counts'].get('searchable'), 2)
        self.assertEqual(service.health()['visual_index']['indexed'], 2)
        self.assertEqual(service.db.visual_generation(), 2)
        self.assertTrue(any(c.get('purpose') == 'visual' for c in provider.calls))
        provider.replies = [{'visual_query': 'blue pages', 'semantic_query': 'Book'}]
        visual_only = service.recommend({'message': 'blue pages'})
        self.assertTrue(any('visual' in r['match_sources'] for r in visual_only['results']))
        for gid in ('1', '2'):
            service.indexer.enqueue(gid)
        service.indexer.process_once()
        before = sum(c.get('purpose') == 'visual' for c in provider.calls)
        provider.replies = [{'visual_query': 'blue pages', 'semantic_query': 'Book',
                             'required': [{'kind': 'artist', 'value': 'Artist 1'}]}]
        result = service.recommend({'message': 'blue pages'})
        self.assertEqual(sum(c.get('purpose') == 'visual' for c in provider.calls), before)
        self.assertEqual(result['coverage']['visual']['searchable'], 1)
        self.assertEqual([r['id'] for r in result['results']], ['1'])
        self.assertTrue(any('visual' in r['match_sources'] for r in result['results']))
        self.assertTrue(any(len(c.get('texts', [])) == 2 for c in provider.calls if c.get('purpose') == 'query'))
        archive = self.archive(1, [('1.jpg', jpeg()), ('2.jpg', jpeg()), ('3.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book 1'}, 'num_pages': 3,
                                         'tags': [{'id': 1, 'type': 'artist', 'name': 'Artist 1'}]}, complete=True, source='test')
        self.assertEqual(service.health()['visual_index']['indexed'], 1)
        with self.assertRaises(ValueError): service.visual_index({'scope': 'all'})

    def test_open_circuit_does_not_exhaust_visual_retries(self):
        library = LibraryDatabase(self.root)
        archive = self.archive(1, [('1.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book'},
                                         'num_pages': 1, 'tags': []}, complete=True, source='test')
        provider = FakeModelProvider([ProviderError('provider_degraded', transient=True, retry_after=60)])
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=provider)
        self.addCleanup(service.close)
        operation = service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})
        service.visual_indexer.process_once()  # enqueue
        service.visual_indexer.process_once()  # rejected locally by circuit
        with service.db.connect() as db:
            job = db.execute("SELECT status,attempts,error_code FROM assistant_jobs WHERE job_type='visual_summary'").fetchone()
        self.assertEqual(tuple(job), ('retry_wait', 0, 'provider_degraded'))
        self.assertEqual(service.visual_operation(operation['operation_id'])['counts'].get('failed', 0), 0)
        self.assertFalse(service.visual_indexer.process_once())
        self.assertEqual(len(provider.calls), 1)

    def test_remote_disabled_stops_visual_embedding_too(self):
        library = LibraryDatabase(self.root)
        archive = self.archive(1, [('1.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book'},
                                         'num_pages': 1, 'tags': []}, complete=True, source='test')
        provider = FakeModelProvider()
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=provider)
        self.addCleanup(service.close)
        service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})
        service.visual_indexer.process_once()
        service.visual_indexer.process_once()
        service.config['remote_image_analysis_enabled'] = False
        self.assertFalse(service.visual_indexer.process_once())
        self.assertEqual(len([call for call in provider.calls if call.get('purpose') == 'embed_visual']), 0)

    def test_disabled_gate_and_source_replacement(self):
        library = LibraryDatabase(self.root)
        archive = self.archive(1, [('1.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book'}, 'num_pages': 1,
                                         'tags': []}, complete=True, source='test')
        service = AssistantService(library, {'enabled': True, 'background_enabled': False}, provider=FakeModelProvider())
        self.addCleanup(service.close)
        with self.assertRaises(ValueError): service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})
        service.config['remote_image_analysis_enabled'] = True
        operation = service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})
        service.visual_indexer.process_once()
        self.archive(1, [('1.jpg', jpeg()), ('2.jpg', jpeg())])
        service.visual_indexer.process_once()
        self.assertEqual(service.visual_operation(operation['operation_id'])['counts'].get('failed'), 1)

    def test_restart_pause_resume_reuses_summary(self):
        library = LibraryDatabase(self.root)
        archive = self.archive(1, [('1.jpg', jpeg()), ('2.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book'}, 'num_pages': 2,
                                         'tags': []}, complete=True, source='test')
        provider = FakeModelProvider()
        config = {'enabled': True, 'background_enabled': False, 'remote_image_analysis_enabled': True}
        service = AssistantService(library, config, provider=provider)
        op = service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})['operation_id']
        service.visual_indexer.process_once()  # enqueue summary
        service.visual_control(op, 'pause')
        self.assertFalse(service.visual_indexer.process_once())
        service.close()
        restarted = AssistantService(library, config, provider=provider)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.visual_operation(op)['status'], 'paused')
        restarted.visual_control(op, 'resume')
        for _ in range(3):
            restarted.visual_indexer.process_once()
            if restarted.db.visual_document(1, restarted.visual_indexer.namespace):
                break
        self.assertIsNotNone(restarted.db.visual_document(1, restarted.visual_indexer.namespace))
        with restarted.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assistant_embeddings WHERE kind='visual'").fetchone()[0], 0)
        restarted.visual_indexer.process_once()  # embed
        restarted.visual_indexer.process_once()  # terminal operation
        self.assertEqual(restarted.visual_operation(op)['counts'].get('searchable'), 1)
        self.assertEqual(sum(c.get('purpose') == 'visual' for c in provider.calls), 1)
        restarted.close()
        changed = AssistantService(library, config | {'embedding_model': 'new-embed'}, provider=provider)
        self.addCleanup(changed.close)
        changed.indexer.scan_thread.join(2)
        for _ in range(8):
            changed.visual_indexer.process_once()
            if changed.health()['visual_index']['indexed'] == 1:
                break
        self.assertEqual(changed.health()['visual_index']['indexed'], 1)
        self.assertEqual(sum(c.get('purpose') == 'visual' for c in provider.calls), 1)

    def test_v1_schema_migrates_without_losing_vectors(self):
        path = self.root / 'assistant.sqlite3'
        import sqlite3
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE assistant_documents (gallery_id INTEGER, kind TEXT, page_start INTEGER, page_end INTEGER, text TEXT, content_hash TEXT, producer TEXT, producer_version TEXT, coverage TEXT, created_at REAL, updated_at REAL, PRIMARY KEY(gallery_id,kind,page_start,page_end,producer_version));
                CREATE TABLE assistant_embeddings (gallery_id INTEGER, kind TEXT, document_key TEXT, model_id TEXT, dim INTEGER, dtype TEXT, normalized INTEGER, vector BLOB, content_hash TEXT, created_at REAL, PRIMARY KEY(document_key,model_id));
                CREATE TABLE assistant_jobs (job_id TEXT PRIMARY KEY, job_type TEXT, gallery_id INTEGER, payload_json TEXT, dedupe_key TEXT UNIQUE, priority INTEGER, status TEXT, attempts INTEGER, next_retry_at REAL, lease_until REAL, error_code TEXT, error_message TEXT, created_at REAL, updated_at REAL);
                CREATE TABLE assistant_model_state (role TEXT PRIMARY KEY, model_id TEXT, config_hash TEXT, updated_at REAL);
                CREATE TABLE assistant_library_state (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO assistant_library_state VALUES ('embedding_generation','1');
                INSERT INTO assistant_embeddings VALUES (1,'metadata','1:metadata:metadata-v1','m',2,'f32le',1,x'0000803f00000000','a',1);
                PRAGMA user_version=1;''')
        db = AssistantDatabase(path)
        self.assertEqual(db.status('m')['indexed'], 1)
        with db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_shared_summary_survives_first_operation_cancel(self):
        library = LibraryDatabase(self.root)
        archive = self.archive(1, [('1.jpg', jpeg()), ('2.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book'}, 'num_pages': 2,
                                         'tags': []}, complete=True, source='test')
        provider = FakeModelProvider()
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=provider)
        self.addCleanup(service.close)
        first = service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})['operation_id']
        second = service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})['operation_id']
        service.visual_indexer.process_once()
        service.visual_indexer.process_once()
        original = provider.visual_chat
        def cancelling_chat(**kwargs):
            service.visual_control(first, 'cancel')
            return original(**kwargs)
        provider.visual_chat = cancelling_chat
        for _ in range(6):
            service.visual_indexer.process_once()
        self.assertEqual(service.visual_operation(first)['status'], 'cancelled')
        self.assertEqual(service.visual_operation(second)['counts'].get('searchable'), 1)
        self.assertEqual(sum(c.get('purpose') == 'visual' for c in provider.calls), 1)

    def test_resume_rebases_obsolete_selected_operation(self):
        library = LibraryDatabase(self.root)
        archive = self.archive(1, [('1.jpg', jpeg())])
        library.upsert_gallery(archive, {'id': 1, 'title': {'english': 'Book'}, 'num_pages': 1,
                                         'tags': []}, complete=True, source='test')
        service = AssistantService(library, {'enabled': True, 'background_enabled': False,
                                             'remote_image_analysis_enabled': True}, provider=FakeModelProvider())
        self.addCleanup(service.close)
        op_id = service.visual_index({'scope': 'ids', 'gallery_ids': ['1']})['operation_id']
        with service.db.connect() as db:
            db.execute("UPDATE assistant_operations SET namespace='obsolete',status='paused' WHERE operation_id=?", (op_id,))
            db.execute("UPDATE assistant_operation_items SET status='failed',error_code='model_unavailable' WHERE operation_id=?", (op_id,))
        self.assertTrue(service.visual_operation(op_id)['requires_model_refresh'])
        self.assertEqual(service.visual_operation(op_id)['errors'], {'model_unavailable': 1})
        resumed = service.visual_control(op_id, 'resume')
        self.assertFalse(resumed['requires_model_refresh'])
        self.assertEqual(resumed['counts'], {'queued': 1})
        self.assertEqual(resumed['errors'], {})

    def test_visual_status_counts_only_active_namespace_jobs(self):
        db = AssistantDatabase(self.root / 'assistant.sqlite3')
        with db.connect() as connection:
            for namespace, status in (('old', 'failed'), ('current', 'succeeded')):
                connection.execute("""INSERT INTO assistant_jobs
                    (job_id,job_type,gallery_id,payload_json,dedupe_key,priority,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (namespace, 'visual_summary', 1, '{"namespace":"' + namespace + '"}',
                     namespace, 20, status, 1, 1))
        self.assertEqual(db.visual_status('embed-model', 'current')['jobs'], {'succeeded': 1})


if __name__ == '__main__':
    unittest.main()
