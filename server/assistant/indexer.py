"""Restartable metadata indexing; all waits are interruptible."""
import random
import threading
from .documents import document
from .provider import ProviderError


class AssistantIndexer:
    def __init__(self, library, db, provider, config, vectors):
        self.library, self.db, self.provider, self.config, self.vectors = library, db, provider, config, vectors
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.batch_size = 32
        self.scan_lock = threading.Lock()
        self.mutation_lock = threading.Lock()
        self.scan_thread = None
        self.worker = None
        if config['background_enabled']:
            self.worker = threading.Thread(target=self._loop, name='nh-assistant-index', daemon=True)
            self.worker.start()

    def delete(self, gallery_id):
        with self.mutation_lock:
            # A later catalog upsert can commit before an earlier delete callback.
            # Always reconcile with current authority rather than callback order.
            if self.library.assistant_gallery(gallery_id):
                self._enqueue(gallery_id)
            else:
                self.db.delete_gallery(gallery_id)

    def enqueue(self, gallery_id):
        with self.mutation_lock:
            return self._enqueue(gallery_id)

    def _enqueue(self, gallery_id):
        record = self.library.assistant_backfill_pages(gallery_id)
        if not record:
            self.db.delete_gallery(gallery_id)
            return False
        text, digest = document(record)
        result = self.db.enqueue(int(gallery_id), text, digest, self.config['embedding_model'])
        self.wake.set()
        return result

    def scan(self, *, resume=False):
        if not self.scan_lock.acquire(blocking=False):
            return False
        if not resume:
            self.db.state('metadata_scan_after', 0)
        self.db.state('metadata_scan_pending', 1)
        def run():
            try:
                # Clean sidecar orphans left by downtime/manual archive removal.
                for gallery_id in self.db.gallery_ids():
                    if self.stop.is_set():
                        return
                    with self.mutation_lock:
                        if not self.library.assistant_gallery(gallery_id):
                            self.db.delete_gallery(gallery_id)
                after = int(self.db.state("metadata_scan_after") or 0)
                while not self.stop.is_set():
                    records = self.library.assistant_catalog_batch(after_id=after)
                    if not records:
                        self.db.state("metadata_scan_pending", 0)
                        break
                    for record in records:
                        if self.stop.is_set():
                            return
                        self.enqueue(record['id'])
                    after = max(int(r['id']) for r in records)
                    self.db.state('metadata_scan_after', after)
            finally:
                self.scan_lock.release()
                self.wake.set()
        self.scan_thread = threading.Thread(target=run, name='nh-assistant-scan', daemon=True)
        self.scan_thread.start()
        return True

    def process_once(self):
        if not self.provider.configured:
            return False
        jobs = self.db.claim(self.config['embedding_model'], self.batch_size, lease_seconds=600)
        if not jobs:
            return False
        # Renew leases while waiting behind interactive traffic or provider cooldown.
        finished = threading.Event()
        def heartbeat():
            while not finished.wait(30):
                self.db.renew(jobs, 600)
        renewer = threading.Thread(target=heartbeat, daemon=True)
        renewer.start()
        try:
            vectors = self.provider.embed_texts(model=self.config['embedding_model'], texts=[j['payload']['text'] for j in jobs], input_type='passage', purpose='embed_metadata')
            self.db.finish(jobs, vectors)
            self.vectors.refresh()
        except ProviderError as exc:
            if exc.status in (400, 413, 422) and len(jobs) > 1:
                self.batch_size = max(1, len(jobs) // 2)
                self.db.fail(jobs, 'batch_reduced', delay=0)
            else:
                for job in jobs:
                    retry = exc.transient and job['attempts'] <= self.config['max_retries_background']
                    self.db.fail([job], exc.code, delay=max(exc.retry_after, min(300, 2 ** job['attempts']) + random.random()) if retry else None)
        except ValueError:
            self.db.fail(jobs, 'invalid_embedding')
        finally:
            finished.set()
        return True

    def _loop(self):
        while not self.stop.is_set():
            try:
                worked = self.process_once()
            except Exception:
                worked = False
            if not worked:
                self.wake.wait(5)
                self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.scan_thread:
            self.scan_thread.join(timeout=2)
        if self.worker:
            self.worker.join(timeout=2)
