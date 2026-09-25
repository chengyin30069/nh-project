import tempfile
import unittest
from pathlib import Path
import numpy as np
from server.assistant.db import AssistantDatabase
from server.assistant.vector_index import VectorIndex

class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = AssistantDatabase(Path(self.tmp.name) / 'assistant.sqlite3')

    def test_dedupe_resume_models_cleanup(self):
        self.assertTrue(self.db.enqueue(1, 'one', 'a', 'model'))
        self.assertFalse(self.db.enqueue(1, 'one', 'a', 'model'))
        jobs = self.db.claim('model')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.db.claim('model'), [])
        with self.db.connect() as db: db.execute('UPDATE assistant_jobs SET lease_until=0')
        restarted = AssistantDatabase(self.db.path)
        jobs = restarted.claim('model')
        self.assertEqual(jobs[0]['attempts'], 2)
        restarted.finish(jobs, [[3, 4]])
        self.assertFalse(restarted.enqueue(1, 'one', 'a', 'model'))
        self.assertEqual(restarted.status('model')['indexed'], 1)
        self.assertEqual(restarted.status('other')['indexed'], 0)
        self.assertTrue(restarted.enqueue(1, 'one', 'a', 'other'))
        restarted.delete_gallery('1')
        self.assertEqual(restarted.status('model')['indexed'], 0)
        self.assertEqual(restarted.status('other')['jobs'], {})

    def test_stale_inflight_write_cannot_resurrect(self):
        self.db.enqueue(1, 'old', 'old', 'm')
        old = self.db.claim('m')
        self.db.enqueue(1, 'new', 'new', 'm')
        self.db.finish(old, [[1, 0]])
        self.assertEqual(self.db.status('m')['indexed'], 0)
        current = self.db.claim('m')
        self.db.delete_gallery('1')
        self.db.finish(current, [[1, 0]])
        self.assertEqual(self.db.status('m')['indexed'], 0)

    def test_vector_mask_generation_and_dimension(self):
        for gid, v in [(1, [1, 0]), (2, [0, 1])]:
            self.db.enqueue(gid, str(gid), str(gid), 'm')
            self.db.finish(self.db.claim('m'), [v])
        vectors = VectorIndex(self.db, 'm')
        self.assertEqual(vectors.search([1, 0], allowed={2})[0][0], 2)
        self.assertEqual(vectors.search([1, 0])[0][0], 1)
        self.db.delete_gallery('1')
        self.assertEqual(vectors.search([1, 0])[0][0], 2)
        with self.assertRaises(ValueError): vectors.search([1, 2, 3])
        self.db.enqueue(3, '3', '3', 'm')
        with self.assertRaises(ValueError): self.db.finish(self.db.claim('m'), [[1, 2, 3]])

    def test_15000_vector_cpu_search(self):
        vectors = VectorIndex(self.db, 'm')
        matrix = np.eye(8, dtype=np.float32)[np.arange(15000) % 8]
        vectors.snapshot = (self.db.generation(), np.arange(1, 15001), matrix)
        result = vectors.search([1, 0, 0, 0, 0, 0, 0, 0], limit=80)
        self.assertEqual(len(result), 80)
        self.assertTrue(all(score == 1 for _, score in result))
