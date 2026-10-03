"""Disposable sidecar, transactional durable jobs and embedding namespaces."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

import numpy as np
from .documents import VERSION


class AssistantDatabase:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 2:
                raise ValueError('assistant database schema is newer than this server')
            if version == 0:
                db.executescript('''BEGIN;
            CREATE TABLE IF NOT EXISTS assistant_documents (
              gallery_id INTEGER NOT NULL, kind TEXT NOT NULL, page_start INTEGER NOT NULL DEFAULT 0,
              page_end INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL, content_hash TEXT NOT NULL,
              producer TEXT NOT NULL, producer_version TEXT NOT NULL, coverage TEXT NOT NULL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              PRIMARY KEY(gallery_id,kind,page_start,page_end,producer_version));
            CREATE TABLE IF NOT EXISTS assistant_embeddings (
              gallery_id INTEGER NOT NULL, kind TEXT NOT NULL, document_key TEXT NOT NULL,
              model_id TEXT NOT NULL, dim INTEGER NOT NULL, dtype TEXT NOT NULL DEFAULT 'f32le',
              normalized INTEGER NOT NULL DEFAULT 1, vector BLOB NOT NULL, content_hash TEXT NOT NULL,
              created_at REAL NOT NULL, PRIMARY KEY(document_key,model_id));
            CREATE TABLE IF NOT EXISTS assistant_jobs (
              job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL, gallery_id INTEGER,
              payload_json TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, priority INTEGER NOT NULL,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_retry_at REAL,
              lease_until REAL, error_code TEXT, error_message TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS assistant_model_state (
              role TEXT PRIMARY KEY, model_id TEXT NOT NULL, config_hash TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS assistant_library_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO assistant_library_state VALUES ('embedding_generation','0');
            CREATE INDEX IF NOT EXISTS idx_assistant_docs_gallery_kind ON assistant_documents(gallery_id,kind);
            CREATE INDEX IF NOT EXISTS idx_assistant_jobs_status_priority ON assistant_jobs(status,priority,created_at);
            PRAGMA user_version=1;
            COMMIT;
            ''')
            if version < 2:
                db.executescript('''BEGIN;
                CREATE TABLE IF NOT EXISTS assistant_document_sources (
                  document_key TEXT PRIMARY KEY, gallery_id INTEGER NOT NULL, kind TEXT NOT NULL,
                  source_fingerprint TEXT NOT NULL, producer_namespace TEXT NOT NULL,
                  evidence_json TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_assistant_sources_gallery_kind ON assistant_document_sources(gallery_id,kind);
                CREATE TABLE IF NOT EXISTS assistant_operations (
                  operation_id TEXT PRIMARY KEY, operation_type TEXT NOT NULL, scope TEXT NOT NULL,
                  status TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, discovered INTEGER NOT NULL DEFAULT 0,
                  namespace TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_operation_items (
                  operation_id TEXT NOT NULL, gallery_id INTEGER NOT NULL,
                  status TEXT NOT NULL, error_code TEXT,
                  PRIMARY KEY(operation_id,gallery_id));
                CREATE INDEX IF NOT EXISTS idx_assistant_operation_items_gallery ON assistant_operation_items(gallery_id);
                INSERT OR IGNORE INTO assistant_library_state VALUES ('visual_generation','0');
                INSERT OR IGNORE INTO assistant_library_state VALUES ('visual_pilot_approved','0');
                PRAGMA user_version=2;
                COMMIT;''')
        self.recover()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def bump(db):
        db.execute("UPDATE assistant_library_state SET value=CAST(value AS INTEGER)+1 WHERE key='embedding_generation'")

    def state(self, key, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO assistant_library_state VALUES (?,?)', (key, str(value)))
            row = db.execute('SELECT value FROM assistant_library_state WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def generation(self):
        with self.connect() as db:
            return int(db.execute("SELECT value FROM assistant_library_state WHERE key='embedding_generation'").fetchone()[0])

    def model(self, role, model):
        with self.connect() as db:
            old = db.execute('SELECT model_id FROM assistant_model_state WHERE role=?', (role,)).fetchone()
            db.execute('INSERT OR REPLACE INTO assistant_model_state VALUES (?,?,?,?)', (role, model, hashlib.sha256(model.encode()).hexdigest(), time.time()))
            return old is not None and old[0] != model

    def enqueue(self, gallery_id, text, content_hash, model):
        now = time.time()
        document_key = f'{gallery_id}:metadata:{VERSION}'
        payload = dict(text=text, content_hash=content_hash, model=model, document_key=document_key)
        dedupe = hashlib.sha256(f'embed_metadata:{gallery_id}:{content_hash}:{model}'.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO assistant_documents VALUES (?,'metadata',0,0,?,?,'deterministic',?,'metadata',?,?)
                ON CONFLICT(gallery_id,kind,page_start,page_end,producer_version) DO UPDATE SET
                text=excluded.text,content_hash=excluded.content_hash,updated_at=excluded.updated_at''',
                (gallery_id, text, content_hash, VERSION, now, now))
            # Hide stale vectors immediately, including embeddings from older metadata.
            changed = db.execute('DELETE FROM assistant_embeddings WHERE document_key=? AND content_hash<>?', (document_key, content_hash)).rowcount
            if changed:
                self.bump(db)
            found = db.execute('SELECT 1 FROM assistant_embeddings WHERE document_key=? AND model_id=? AND content_hash=?', (document_key, model, content_hash)).fetchone()
            if found:
                return False
            db.execute("UPDATE assistant_jobs SET status='cancelled', updated_at=? WHERE gallery_id=? AND job_type='embed_metadata' AND status IN ('queued','retry_wait','running') AND dedupe_key<>?", (now, gallery_id, dedupe))
            row = db.execute('SELECT status FROM assistant_jobs WHERE dedupe_key=?', (dedupe,)).fetchone()
            if row and row[0] in ('succeeded', 'cancelled'):
                db.execute("UPDATE assistant_jobs SET status='queued',attempts=0,next_retry_at=NULL,updated_at=? WHERE dedupe_key=?", (now, dedupe))
                return True
            return db.execute('''INSERT OR IGNORE INTO assistant_jobs
                (job_id,job_type,gallery_id,payload_json,dedupe_key,priority,status,created_at,updated_at)
                VALUES (?,'embed_metadata',?,?,?,?, 'queued',?,?)''',
                (uuid.uuid4().hex, gallery_id, json.dumps(payload), dedupe, 10, now, now)).rowcount > 0

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE assistant_jobs SET status='queued',lease_until=NULL WHERE status='running' AND lease_until<?", (time.time(),))

    def claim(self, model, limit=32, lease_seconds=180, job_type='embed_metadata'):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE assistant_jobs SET status='queued',lease_until=NULL WHERE status='running' AND lease_until<?", (now,))
            rows = db.execute("""SELECT * FROM assistant_jobs WHERE status IN ('queued','retry_wait')
                AND job_type=? AND (next_retry_at IS NULL OR next_retry_at<=?) AND json_extract(payload_json,'$.model')=?
                AND (?='embed_metadata' OR EXISTS (SELECT 1 FROM assistant_operations o WHERE o.operation_id=json_extract(assistant_jobs.payload_json,'$.operation_id') AND o.status IN ('queued','running')))
                ORDER BY priority,created_at LIMIT ?""", (job_type, now, model, job_type, limit)).fetchall()
            for row in rows:
                db.execute("UPDATE assistant_jobs SET status='running',attempts=attempts+1,lease_until=?,updated_at=? WHERE job_id=?", (now+lease_seconds, now, row['job_id']))
            return [dict(row) | {'attempts': row['attempts']+1, 'payload': json.loads(row['payload_json'])} for row in rows]

    def renew(self, jobs, seconds):
        with self.connect() as db:
            db.executemany("UPDATE assistant_jobs SET lease_until=? WHERE job_id=? AND status='running'", [(time.time()+seconds, j['job_id']) for j in jobs])

    def finish(self, jobs, vectors):
        if len(jobs) != len(vectors):
            raise ValueError('embedding count mismatch')
        arrays = [np.asarray(v, dtype='<f4') for v in vectors]
        if any(v.ndim != 1 or not v.size or not np.isfinite(v).all() or not np.linalg.norm(v) for v in arrays) or len({v.size for v in arrays}) > 1:
            raise ValueError('invalid embeddings')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for job, vector in zip(jobs, arrays):
                p = job['payload']
                active = db.execute("SELECT 1 FROM assistant_jobs WHERE job_id=? AND status='running' AND attempts=?", (job['job_id'], job['attempts'])).fetchone()
                current = db.execute('SELECT content_hash FROM assistant_documents WHERE gallery_id=? AND kind=? AND producer_version=?', (job['gallery_id'], 'metadata', VERSION)).fetchone()
                if not active or not current or current[0] != p['content_hash']:
                    continue
                dim = db.execute('SELECT dim FROM assistant_embeddings WHERE model_id=? LIMIT 1', (p['model'],)).fetchone()
                if dim and dim[0] != vector.size:
                    raise ValueError('embedding dimension changed within model namespace')
                vector = vector / np.linalg.norm(vector)
                db.execute('INSERT OR REPLACE INTO assistant_embeddings VALUES (?,\'metadata\',?,?,?,\'f32le\',1,?,?,?)',
                    (job['gallery_id'], p['document_key'], p['model'], vector.size, vector.astype('<f4').tobytes(), p['content_hash'], time.time()))
                db.execute("UPDATE assistant_jobs SET status='succeeded',lease_until=NULL,error_code=NULL,error_message=NULL,updated_at=? WHERE job_id=?", (time.time(), job['job_id']))
                self.bump(db)

    def fail(self, jobs, code, *, delay=None):
        with self.connect() as db:
            for job in jobs:
                db.execute('''UPDATE assistant_jobs SET status=?,next_retry_at=?,lease_until=NULL,error_code=?,error_message=?,updated_at=?
                    WHERE job_id=? AND status='running' AND attempts=?''',
                    ('retry_wait' if delay is not None else 'failed', time.time()+delay if delay is not None else None,
                     code, code, time.time(), job['job_id'], job['attempts']))

    def defer_unattempted(self, jobs, code, *, delay):
        """Release claims rejected by the local circuit before any HTTP request."""
        with self.connect() as db:
            for job in jobs:
                db.execute('''UPDATE assistant_jobs SET status='retry_wait',attempts=attempts-1,
                    next_retry_at=?,lease_until=NULL,error_code=?,error_message=?,updated_at=?
                    WHERE job_id=? AND status='running' AND attempts=?''',
                    (time.time()+delay, code, code, time.time(), job['job_id'], job['attempts']))

    def retry_failed(self, model):
        with self.connect() as db:
            return db.execute("UPDATE assistant_jobs SET status='queued',attempts=0,next_retry_at=NULL WHERE job_type='embed_metadata' AND status='failed' AND json_extract(payload_json,'$.model')=?", (model,)).rowcount

    def delete_gallery(self, gallery_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('assistant_documents', 'assistant_embeddings', 'assistant_jobs', 'assistant_document_sources', 'assistant_operation_items'):
                db.execute(f'DELETE FROM {table} WHERE gallery_id=?', (int(gallery_id),))
            self.bump(db)
            db.execute("UPDATE assistant_library_state SET value=CAST(value AS INTEGER)+1 WHERE key='visual_generation'")

    def gallery_ids(self):
        with self.connect() as db:
            return [r[0] for r in db.execute('SELECT DISTINCT gallery_id FROM assistant_documents')]

    def embeddings(self, model):
        with self.connect() as db:
            db.execute('BEGIN')
            generation = int(db.execute("SELECT value FROM assistant_library_state WHERE key='embedding_generation'").fetchone()[0])
            rows = db.execute("SELECT gallery_id,dim,vector FROM assistant_embeddings WHERE model_id=? AND kind='metadata' AND dtype='f32le'", (model,)).fetchall()
            return generation, rows

    def status(self, model):
        with self.connect() as db:
            indexed = db.execute("SELECT count(*) FROM assistant_embeddings WHERE model_id=? AND kind='metadata'", (model,)).fetchone()[0]
            jobs = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM assistant_jobs WHERE job_type='embed_metadata' AND json_extract(payload_json,'$.model')=? GROUP BY status", (model,))}
            return dict(indexed=indexed, model=model, jobs=jobs)

    def visual_generation(self):
        return int(self.state('visual_generation') or 0)

    @staticmethod
    def bump_visual(db):
        db.execute("UPDATE assistant_library_state SET value=CAST(value AS INTEGER)+1 WHERE key='visual_generation'")

    def create_visual_operation(self, scope, namespace, ids=()):
        now, operation_id = time.time(), uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO assistant_operations VALUES (?,?,?,?,?,?,?,?,?)',
                       (operation_id, 'visual_index', scope, 'queued', 0, 0, namespace, now, now))
            for gid in ids:
                db.execute('INSERT INTO assistant_operation_items VALUES (?,? ,?,NULL)', (operation_id, int(gid), 'queued'))
            if ids:
                db.execute('UPDATE assistant_operations SET discovered=? WHERE operation_id=?', (len(ids), operation_id))
        return operation_id

    def visual_operations(self, namespace=None):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM assistant_operations WHERE status IN ('queued','running')
                AND (? IS NULL OR namespace=?) ORDER BY created_at""", (namespace, namespace))]

    def latest_visual_operation_id(self, namespace):
        with self.connect() as db:
            row = db.execute("""SELECT operation_id FROM assistant_operations WHERE namespace=? AND operation_type='visual_index'
                ORDER BY created_at DESC LIMIT 1""", (namespace,)).fetchone()
            return row[0] if row else None

    def operation(self, operation_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM assistant_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['counts'] = {r[0]: r[1] for r in db.execute('SELECT status,count(*) FROM assistant_operation_items WHERE operation_id=? GROUP BY status', (operation_id,))}
            result['errors'] = {r[0]: r[1] for r in db.execute('SELECT error_code,count(*) FROM assistant_operation_items WHERE operation_id=? AND error_code IS NOT NULL GROUP BY error_code', (operation_id,))}
            return result

    def pause_outdated_visual_operations(self, namespace):
        with self.connect() as db:
            db.execute("UPDATE assistant_operations SET status='paused',updated_at=? WHERE namespace<>? AND status IN ('queued','running')",
                       (time.time(), namespace))

    def rebase_visual_operation(self, operation_id, namespace):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT scope,status,namespace FROM assistant_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row or row['scope'] != 'ids' or row['status'] != 'paused' or row['namespace'] == namespace:
                raise ValueError('Only a paused selected-ID operation from a previous visual model can be retried.')
            db.execute("""UPDATE assistant_jobs SET status='cancelled',lease_until=NULL,updated_at=?
                WHERE json_extract(payload_json,'$.operation_id')=? AND job_type IN ('visual_summary','embed_visual')
                AND status IN ('queued','retry_wait','running')""", (time.time(), operation_id))
            db.execute("UPDATE assistant_operations SET namespace=?,status='running',updated_at=? WHERE operation_id=?",
                       (namespace, time.time(), operation_id))
            db.execute("UPDATE assistant_operation_items SET status='queued',error_code=NULL WHERE operation_id=?", (operation_id,))
        return self.operation(operation_id)

    def control_operation(self, operation_id, action):
        target = {'pause': 'paused', 'resume': 'running', 'cancel': 'cancelled'}[action]
        with self.connect() as db:
            row = db.execute('SELECT status FROM assistant_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                raise ValueError('Visual operation not found.')
            if row[0] in ('completed', 'cancelled') or (action == 'resume' and row[0] != 'paused'):
                raise ValueError('Visual operation cannot change from its current state.')
            db.execute('UPDATE assistant_operations SET status=?,updated_at=? WHERE operation_id=?', (target, time.time(), operation_id))
            if action == 'resume':
                db.execute("UPDATE assistant_operation_items SET status='queued' WHERE operation_id=? AND status IN ('summary_ready','summary_pending','embedding_pending')", (operation_id,))
            if action == 'cancel':
                db.execute("UPDATE assistant_jobs SET status='cancelled',lease_until=NULL WHERE status IN ('queued','retry_wait') AND json_extract(payload_json,'$.operation_id')=?", (operation_id,))
        return self.operation(operation_id)

    def pause_visual_namespace(self, namespace):
        with self.connect() as db:
            db.execute("UPDATE assistant_operations SET status='paused',updated_at=? WHERE namespace=? AND scope!='reembed' AND status IN ('queued','running')",
                       (time.time(), namespace))

    def append_visual_batch(self, operation_id, ids, cursor):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM assistant_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row or row[0] not in ('queued', 'running'):
                return False
            for gid in ids:
                db.execute("INSERT OR IGNORE INTO assistant_operation_items VALUES (?,?,'queued',NULL)", (operation_id, int(gid)))
            db.execute("UPDATE assistant_operations SET cursor=?,discovered=(SELECT count(*) FROM assistant_operation_items WHERE operation_id=?),status='running',updated_at=? WHERE operation_id=?",
                       (cursor, operation_id, time.time(), operation_id))
            return True

    def next_visual_item(self, operation_id):
        with self.connect() as db:
            row = db.execute("SELECT gallery_id FROM assistant_operation_items WHERE operation_id=? AND status='queued' ORDER BY gallery_id LIMIT 1", (operation_id,)).fetchone()
            return row[0] if row else None

    def start_visual_operation(self, operation_id):
        with self.connect() as db:
            db.execute("UPDATE assistant_operations SET status='running',updated_at=? WHERE operation_id=? AND status='queued'",
                       (time.time(), operation_id))

    def visual_item(self, operation_id, gallery_id, status, error=None):
        with self.connect() as db:
            db.execute('UPDATE assistant_operation_items SET status=?,error_code=? WHERE operation_id=? AND gallery_id=?',
                       (status, error, operation_id, int(gallery_id)))

    def finish_visual_operation_if_done(self, operation_id, scan_done):
        with self.connect() as db:
            pending = db.execute("SELECT 1 FROM assistant_operation_items WHERE operation_id=? AND status IN ('queued','summary_pending','embedding_pending') LIMIT 1", (operation_id,)).fetchone()
            if scan_done and not pending:
                db.execute("UPDATE assistant_operations SET status='completed',updated_at=? WHERE operation_id=? AND status IN ('queued','running')", (time.time(), operation_id))

    def visual_document(self, gallery_id, namespace):
        with self.connect() as db:
            row = db.execute("""SELECT d.*,s.document_key,s.source_fingerprint,s.evidence_json
                FROM assistant_documents d JOIN assistant_document_sources s
                ON s.gallery_id=d.gallery_id AND s.kind=d.kind AND s.producer_namespace=d.producer_version
                WHERE d.gallery_id=? AND d.kind='visual' AND d.producer_version=? LIMIT 1""", (int(gallery_id), namespace)).fetchone()
            return dict(row) if row else None

    def visual_source_batch(self, namespace, after_id, limit):
        with self.connect() as db:
            return [{'id': r[0]} for r in db.execute("""SELECT DISTINCT gallery_id FROM assistant_document_sources
                WHERE kind='visual' AND producer_namespace=? AND gallery_id>? ORDER BY gallery_id LIMIT ?""",
                (namespace, after_id, limit))]

    def commit_visual_summary(self, job, *, text, content_hash, evidence, source_fingerprint, namespace, embedding_model):
        gid, now = job['gallery_id'], time.time()
        document_key = f'{gid}:visual:{namespace}'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT 1 FROM assistant_jobs WHERE job_id=? AND status='running' AND attempts=?", (job['job_id'], job['attempts'])).fetchone()
            if not active:
                return False
            db.execute('''INSERT INTO assistant_documents VALUES (?,'visual',0,0,?,?,'vlm',?,'sampled',?,?)
                ON CONFLICT(gallery_id,kind,page_start,page_end,producer_version) DO UPDATE SET
                text=excluded.text,content_hash=excluded.content_hash,updated_at=excluded.updated_at''',
                (gid, text, content_hash, namespace, now, now))
            db.execute("INSERT OR REPLACE INTO assistant_document_sources VALUES (? ,?,'visual',?,?,?,?)",
                       (document_key, gid, source_fingerprint, namespace, json.dumps(evidence, ensure_ascii=False), now))
            removed = db.execute('DELETE FROM assistant_embeddings WHERE document_key=? AND content_hash<>?', (document_key, content_hash)).rowcount
            if removed:
                self.bump_visual(db)
            db.execute("UPDATE assistant_jobs SET status='succeeded',lease_until=NULL,updated_at=? WHERE job_id=?", (now, job['job_id']))
            owner = db.execute("""SELECT o.operation_id FROM assistant_operations o JOIN assistant_operation_items i USING(operation_id)
                WHERE i.gallery_id=? AND o.namespace=? AND o.status IN ('queued','running') ORDER BY o.created_at LIMIT 1""", (gid, namespace)).fetchone()
            if owner:
                payload = dict(document_key=document_key, content_hash=content_hash, model=embedding_model,
                               namespace=namespace, operation_id=owner[0])
                dedupe = hashlib.sha256(f'embed_visual:{document_key}:{content_hash}:{embedding_model}'.encode()).hexdigest()
                db.execute('''INSERT OR IGNORE INTO assistant_jobs(job_id,job_type,gallery_id,payload_json,dedupe_key,priority,status,created_at,updated_at)
                    VALUES (?,'embed_visual',?,?,?,?, 'queued',?,?)''', (uuid.uuid4().hex, gid, json.dumps(payload), dedupe, 20, now, now))
                db.execute("UPDATE assistant_jobs SET status='queued',attempts=0,next_retry_at=NULL,payload_json=?,updated_at=? WHERE dedupe_key=? AND status IN ('succeeded','failed','cancelled') AND NOT EXISTS (SELECT 1 FROM assistant_embeddings WHERE document_key=? AND model_id=? AND content_hash=?)",
                           (json.dumps(payload), now, dedupe, document_key, embedding_model, content_hash))
                db.execute("""UPDATE assistant_operation_items SET status='embedding_pending' WHERE gallery_id=? AND status IN ('queued','summary_pending')
                    AND operation_id IN (SELECT operation_id FROM assistant_operations WHERE namespace=? AND status IN ('queued','running'))""", (gid, namespace))
            else:
                db.execute("UPDATE assistant_operation_items SET status='summary_ready' WHERE operation_id=? AND gallery_id=?",
                           (job['payload']['operation_id'], gid))
        return True

    def enqueue_visual_job(self, operation_id, gallery_id, fingerprint, namespace, model):
        payload = dict(operation_id=operation_id, source_fingerprint=fingerprint, namespace=namespace, model=model)
        dedupe = hashlib.sha256(f'visual_summary:{gallery_id}:{fingerprint}:{namespace}'.encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            row = db.execute('SELECT status FROM assistant_jobs WHERE dedupe_key=?', (dedupe,)).fetchone()
            if row and row[0] in ('failed', 'cancelled'):
                db.execute("UPDATE assistant_jobs SET status='queued',attempts=0,next_retry_at=NULL,payload_json=?,updated_at=? WHERE dedupe_key=?", (json.dumps(payload), now, dedupe))
            elif not row:
                db.execute('''INSERT INTO assistant_jobs(job_id,job_type,gallery_id,payload_json,dedupe_key,priority,status,created_at,updated_at)
                    VALUES (?,'visual_summary',?,?,?,?, 'queued',?,?)''', (uuid.uuid4().hex, gallery_id, json.dumps(payload), dedupe, 20, now, now))
            else:
                db.execute("""UPDATE assistant_jobs SET payload_json=?,updated_at=? WHERE dedupe_key=? AND status IN ('queued','retry_wait')
                    AND NOT EXISTS (SELECT 1 FROM assistant_operations WHERE operation_id=json_extract(assistant_jobs.payload_json,'$.operation_id') AND status IN ('queued','running'))""",
                    (json.dumps(payload), now, dedupe))
            db.execute("UPDATE assistant_operation_items SET status='summary_pending' WHERE operation_id=? AND gallery_id=?", (operation_id, gallery_id))

    def enqueue_visual_embedding(self, operation_id, gallery_id, document, model):
        payload = dict(operation_id=operation_id, document_key=document['document_key'],
                       content_hash=document['content_hash'], namespace=document['producer_version'], model=model)
        dedupe = hashlib.sha256(f"embed_visual:{payload['document_key']}:{payload['content_hash']}:{model}".encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            row = db.execute('SELECT status FROM assistant_jobs WHERE dedupe_key=?', (dedupe,)).fetchone()
            if row and row[0] in ('succeeded', 'cancelled', 'failed'):
                db.execute("UPDATE assistant_jobs SET status='queued',attempts=0,next_retry_at=NULL,payload_json=?,updated_at=? WHERE dedupe_key=?", (json.dumps(payload), now, dedupe))
            elif not row:
                db.execute('''INSERT INTO assistant_jobs(job_id,job_type,gallery_id,payload_json,dedupe_key,priority,status,created_at,updated_at)
                    VALUES (?,'embed_visual',?,?,?,?, 'queued',?,?)''', (uuid.uuid4().hex, gallery_id, json.dumps(payload), dedupe, 20, now, now))
            else:
                db.execute("""UPDATE assistant_jobs SET payload_json=?,updated_at=? WHERE dedupe_key=? AND status IN ('queued','retry_wait')
                    AND NOT EXISTS (SELECT 1 FROM assistant_operations WHERE operation_id=json_extract(assistant_jobs.payload_json,'$.operation_id') AND status IN ('queued','running'))""",
                    (json.dumps(payload), now, dedupe))
            db.execute("UPDATE assistant_operation_items SET status='embedding_pending' WHERE operation_id=? AND gallery_id=?", (operation_id, gallery_id))

    def finish_visual_embedding(self, jobs, vectors):
        if len(jobs) != len(vectors):
            raise ValueError('embedding count mismatch')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for job, values in zip(jobs, vectors):
                vector = np.asarray(values, dtype='<f4')
                if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all() or not np.linalg.norm(vector):
                    raise ValueError('invalid embeddings')
                p = job['payload']
                active = db.execute("SELECT 1 FROM assistant_jobs WHERE job_id=? AND status='running' AND attempts=?", (job['job_id'], job['attempts'])).fetchone()
                source = db.execute('SELECT d.content_hash FROM assistant_documents d JOIN assistant_document_sources s ON s.gallery_id=d.gallery_id AND s.kind=d.kind AND s.producer_namespace=d.producer_version WHERE s.document_key=?', (p['document_key'],)).fetchone()
                if not active or not source or source[0] != p['content_hash']:
                    continue
                dimension = db.execute('SELECT dim FROM assistant_embeddings WHERE model_id=? AND kind=? LIMIT 1', (p['model'], 'visual')).fetchone()
                if dimension and dimension[0] != vector.size:
                    raise ValueError('embedding dimension changed')
                vector /= np.linalg.norm(vector)
                db.execute("INSERT OR REPLACE INTO assistant_embeddings VALUES (?,'visual',?,?,?,'f32le',1,?,?,?)",
                           (job['gallery_id'], p['document_key'], p['model'], vector.size, vector.astype('<f4').tobytes(), p['content_hash'], time.time()))
                db.execute("UPDATE assistant_jobs SET status='succeeded',lease_until=NULL,updated_at=? WHERE job_id=?", (time.time(), job['job_id']))
                db.execute("UPDATE assistant_operation_items SET status='searchable' WHERE operation_id=? AND gallery_id=?", (p['operation_id'], job['gallery_id']))
                db.execute("""UPDATE assistant_operation_items SET status='searchable' WHERE gallery_id=? AND status='embedding_pending'
                    AND operation_id IN (SELECT operation_id FROM assistant_operations WHERE namespace=?)""", (job['gallery_id'], p['namespace']))
                self.bump_visual(db)

    def visual_embeddings(self, model, namespace):
        with self.connect() as db:
            rows = db.execute("""SELECT e.gallery_id,e.dim,e.vector,e.document_key,s.source_fingerprint,s.evidence_json
                FROM assistant_embeddings e JOIN assistant_document_sources s ON s.document_key=e.document_key
                JOIN assistant_documents d ON d.gallery_id=e.gallery_id AND d.kind='visual' AND d.producer_version=s.producer_namespace
                WHERE e.kind='visual' AND e.model_id=? AND s.producer_namespace=? AND e.content_hash=d.content_hash AND e.dtype='f32le'""", (model, namespace)).fetchall()
            return self.visual_generation(), [dict(r) for r in rows]

    def visual_status(self, model, namespace):
        with self.connect() as db:
            searchable = db.execute("""SELECT count(*) FROM assistant_embeddings e JOIN assistant_document_sources s ON s.document_key=e.document_key
                WHERE e.kind='visual' AND e.model_id=? AND s.producer_namespace=?""", (model, namespace)).fetchone()[0]
            summary = db.execute("SELECT count(*) FROM assistant_document_sources WHERE kind='visual' AND producer_namespace=?", (namespace,)).fetchone()[0]
            jobs = {r[0]: r[1] for r in db.execute("""SELECT status,count(*) FROM assistant_jobs
                WHERE job_type IN ('visual_summary','embed_visual')
                  AND json_extract(payload_json,'$.namespace')=? GROUP BY status""", (namespace,))}
            return dict(indexed=searchable, summary_ready=summary, namespace=namespace, model=model, jobs=jobs,
                        freshness='awaiting_reconciliation')

    def visual_coverage(self, allowed, model, namespace):
        with self.connect() as db:
            summaries = {r[0] for r in db.execute("SELECT gallery_id FROM assistant_document_sources WHERE kind='visual' AND producer_namespace=?", (namespace,))}
            searchable = {r[0] for r in db.execute("""SELECT e.gallery_id FROM assistant_embeddings e JOIN assistant_document_sources s ON s.document_key=e.document_key
                WHERE e.kind='visual' AND e.model_id=? AND s.producer_namespace=?""", (model, namespace))}
            refused = {r[0] for r in db.execute("SELECT gallery_id FROM assistant_operation_items WHERE error_code='provider_rejected'")}
        return dict(eligible=len(allowed), searchable=len(allowed & searchable),
                    summary_pending_embedding=len(allowed & (summaries - searchable)),
                    refused=len(allowed & refused), stale=None, freshness='awaiting_reconciliation')

    def retry_visual_failed(self, namespace):
        excluded = {'provider_rejected', 'image_decode_failed', 'invalid_analysis_json', 'source_changed',
                    'payload_too_large', 'auth_failed', 'model_unavailable', 'billing_required'}
        now, count = time.time(), 0
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT job_id,job_type,gallery_id,payload_json,error_code FROM assistant_jobs WHERE status='failed' AND job_type IN ('visual_summary','embed_visual')").fetchall()
            for row in rows:
                payload = json.loads(row['payload_json'])
                if payload.get('namespace') != namespace or row['error_code'] in excluded:
                    continue
                op_id = payload['operation_id']
                op = db.execute('SELECT status FROM assistant_operations WHERE operation_id=?', (op_id,)).fetchone()
                if not op or op[0] == 'cancelled':
                    continue
                db.execute("UPDATE assistant_operations SET status='running',updated_at=? WHERE operation_id=?", (now, op_id))
                db.execute("UPDATE assistant_jobs SET status='queued',attempts=0,next_retry_at=NULL,error_code=NULL,error_message=NULL,updated_at=? WHERE job_id=?", (now, row['job_id']))
                db.execute('UPDATE assistant_operation_items SET status=?,error_code=NULL WHERE operation_id=? AND gallery_id=?',
                           ('summary_pending' if row['job_type'] == 'visual_summary' else 'embedding_pending', op_id, row['gallery_id']))
                count += 1
        return count

    def reduce_visual_payload(self, job):
        payload = dict(job['payload'], reduced=True)
        with self.connect() as db:
            return db.execute("""UPDATE assistant_jobs SET status='retry_wait',next_retry_at=?,lease_until=NULL,
                payload_json=?,error_code='payload_too_large',updated_at=?
                WHERE job_id=? AND status='running' AND attempts=?""",
                (time.time(), json.dumps(payload), time.time(), job['job_id'], job['attempts'])).rowcount > 0

    def invalidate_visual(self, gallery_id, fingerprint):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            stale = [r[0] for r in db.execute("SELECT document_key FROM assistant_document_sources WHERE gallery_id=? AND kind='visual' AND source_fingerprint<>?", (int(gallery_id), fingerprint))]
            if not stale:
                return False
            for key in stale:
                db.execute('DELETE FROM assistant_embeddings WHERE document_key=?', (key,))
                db.execute('DELETE FROM assistant_document_sources WHERE document_key=?', (key,))
            db.execute("DELETE FROM assistant_documents WHERE gallery_id=? AND kind='visual'", (int(gallery_id),))
            db.execute("UPDATE assistant_jobs SET status='cancelled' WHERE gallery_id=? AND job_type IN ('visual_summary','embed_visual') AND status IN ('queued','retry_wait','running')", (int(gallery_id),))
            self.bump_visual(db)
            return True
