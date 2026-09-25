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
            db.executescript('''
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
            ''')
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
            db.execute("UPDATE assistant_jobs SET status='cancelled', updated_at=? WHERE gallery_id=? AND status IN ('queued','retry_wait','running') AND dedupe_key<>?", (now, gallery_id, dedupe))
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

    def claim(self, model, limit=32, lease_seconds=180):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE assistant_jobs SET status='queued',lease_until=NULL WHERE status='running' AND lease_until<?", (now,))
            rows = db.execute("""SELECT * FROM assistant_jobs WHERE status IN ('queued','retry_wait')
                AND (next_retry_at IS NULL OR next_retry_at<=?) AND json_extract(payload_json,'$.model')=?
                ORDER BY priority,created_at LIMIT ?""", (now, model, limit)).fetchall()
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

    def retry_failed(self, model):
        with self.connect() as db:
            return db.execute("UPDATE assistant_jobs SET status='queued',attempts=0,next_retry_at=NULL WHERE status='failed' AND json_extract(payload_json,'$.model')=?", (model,)).rowcount

    def delete_gallery(self, gallery_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('assistant_documents', 'assistant_embeddings', 'assistant_jobs'):
                db.execute(f'DELETE FROM {table} WHERE gallery_id=?', (int(gallery_id),))
            self.bump(db)

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
            indexed = db.execute('SELECT count(*) FROM assistant_embeddings WHERE model_id=?', (model,)).fetchone()[0]
            jobs = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM assistant_jobs WHERE json_extract(payload_json,'$.model')=? GROUP BY status", (model,))}
            return dict(indexed=indexed, model=model, jobs=jobs)
