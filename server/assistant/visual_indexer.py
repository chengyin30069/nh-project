"""Durable visual operations; one local decode/dispatch worker."""
import json
import random
import threading
import time

from .images import ImageSampleError, matches_sample, messages_for_sample, safe_evidence, sample, source_token
from .provider import ProviderError
from .visual import PROMPT, namespace, observation


class VisualIndexer:
    def __init__(self, library, db, provider, config):
        self.library, self.db, self.provider, self.config = library, db, provider, config
        self.namespace = namespace(config['visual_model'])
        self.db.pause_outdated_visual_operations(self.namespace)
        if not config['remote_image_analysis_enabled']:
            self.db.pause_visual_namespace(self.namespace)
        self.wake, self.stop = threading.Event(), threading.Event()
        self.summaries_since_embedding = 0
        self.remote_cooldown_until = 0
        self.thread = None
        if config['background_enabled']:
            self.thread = threading.Thread(target=self._loop, name='nh-assistant-visual', daemon=True)
            self.thread.start()

    def submit(self, payload):
        if not self.config['remote_image_analysis_enabled']:
            raise ValueError('Remote image analysis is disabled in configuration.')
        if not self.provider.configured:
            raise ValueError('NIM API key is not loaded.')
        if not isinstance(payload, dict) or set(payload) - {'scope', 'gallery_ids'}:
            raise ValueError('Invalid visual index request.')
        scope = payload.get('scope')
        if scope == 'ids':
            ids = payload.get('gallery_ids')
            from .schema import gallery_id
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not gallery_id(item) for item in ids):
                raise ValueError('Provide 1–100 numeric downloaded gallery IDs.')
            ids = list(dict.fromkeys(int(item) for item in ids))
            if any(not self.library.assistant_gallery(str(gid)) for gid in ids):
                raise ValueError('A selected gallery is not downloaded.')
            if len(ids) > 6 and self.db.state('visual_capability_verified') != self.namespace:
                raise ValueError('Run the opt-in visual model compatibility check before a larger pilot.')
        elif scope == 'all':
            if payload.get('gallery_ids') is not None:
                raise ValueError('Full scan cannot include gallery IDs.')
            if self.db.state('visual_pilot_approved') != self.namespace:
                raise ValueError('Complete and review a 20–50 gallery pilot before a full visual scan.')
            ids = []
        else:
            raise ValueError('Visual scope must be ids or all.')
        operation_id = self.db.create_visual_operation(scope, self.namespace, ids)
        self.wake.set()
        return self.db.operation(operation_id)

    def capability_check(self, payload):
        from .schema import gallery_id
        if not self.config['remote_image_analysis_enabled'] or not self.provider.configured:
            raise ValueError('Remote image analysis and a loaded API key are required.')
        if not isinstance(payload, dict) or set(payload) != {'gallery_id'} or not gallery_id(payload['gallery_id']):
            raise ValueError('Provide one downloaded gallery ID.')
        gid = payload['gallery_id']
        if not self.library.assistant_gallery(gid):
            raise ValueError('Downloaded gallery not found.')
        archive = self.library.storage_dir / f'{gid}.cbz'
        sampled = sample(archive, edge=self.config['max_remote_image_edge'],
                         byte_limit=self.config['max_remote_image_bytes'], image_limit=2,
                         request_limit=self.config['max_remote_request_bytes'])
        if len(sampled['pages']) < 2:
            raise ValueError('Compatibility check requires a gallery with at least two readable pages.')
        expected = [p['page'] for p in sampled['pages']]
        prompt = 'Compatibility check. Read the two attached images in order and return only JSON: {"seen_pages": [the two page numbers from their labels in order], "style": ["one visible style observation"]}. The style value must be an array of strings. No prose.'
        messages = messages_for_sample(sampled, prompt)
        try:
            response = self.provider.visual_chat(model=self.config['visual_model'], messages=messages, max_tokens=200)
            from .schema import json_object
            data = json_object(response.text)
            if not isinstance(data, dict) or set(data) != {'seen_pages', 'style'} or data['seen_pages'] != expected:
                raise ValueError('Configured visual model did not return the required ordered page JSON.')
            style = data['style']
            if isinstance(style, str):
                style = [style]
            observation({'style': style})
            if source_token(archive) != sampled['source_fingerprint']:
                raise ValueError('Archive changed during compatibility check.')
            self.db.state('visual_capability_verified', self.namespace)
            return {'verified': True, 'model': self.config['visual_model'], 'namespace': self.namespace,
                    'image_count': len(expected)}
        finally:
            del messages

    def approve_pilot(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'operation_id'}:
            raise ValueError('Provide the completed pilot operation ID.')
        operation = self.db.operation(payload['operation_id'])
        if not operation or operation['scope'] != 'ids' or operation['namespace'] != self.namespace or operation['status'] != 'completed':
            raise ValueError('A completed current-model selected-ID pilot is required.')
        if not 20 <= operation['discovered'] <= 50 or operation['counts'].get('searchable', 0) < 20:
            raise ValueError('Pilot needs 20–50 selected galleries and at least 20 searchable summaries.')
        self.db.state('visual_pilot_approved', self.namespace)
        return {'approved': True, 'namespace': self.namespace}

    def reembed_existing(self):
        if self.db.visual_status(self.config['embedding_model'], self.namespace)['summary_ready'] == 0:
            return None
        if any(op['scope'] == 'reembed' and op['namespace'] == self.namespace for op in self.db.visual_operations()):
            return None
        operation_id = self.db.create_visual_operation('reembed', self.namespace)
        self.wake.set()
        return operation_id

    def _scan(self, operation):
        if operation['scope'] not in ('all', 'reembed') or operation['cursor'] == -1:
            return False
        if operation['scope'] == 'reembed':
            records = self.db.visual_source_batch(self.namespace, operation['cursor'], 100)
        else:
            records = self.library.assistant_catalog_batch(after_id=operation['cursor'], limit=100)
        if records:
            self.db.append_visual_batch(operation['operation_id'], [r['id'] for r in records], max(int(r['id']) for r in records))
        else:
            self.db.append_visual_batch(operation['operation_id'], [], -1)
        return True

    def _prepare(self, operation, gid):
        self.db.start_visual_operation(operation['operation_id'])
        record = self.library.assistant_gallery(str(gid))
        if not record:
            self.db.visual_item(operation['operation_id'], gid, 'missing', 'source_changed')
            return True
        archive = self.library.storage_dir / f'{gid}.cbz'
        try:
            fingerprint = source_token(archive)
        except OSError:
            self.db.visual_item(operation['operation_id'], gid, 'missing', 'source_changed')
            return True
        document = self.db.visual_document(gid, self.namespace)
        if document and document['source_fingerprint'] == fingerprint and matches_sample(archive, json.loads(document['evidence_json'])):
            with self.db.connect() as db:
                embedded = db.execute("SELECT 1 FROM assistant_embeddings WHERE document_key=? AND model_id=? AND content_hash=?", (document['document_key'], self.config['embedding_model'], document['content_hash'])).fetchone()
            if embedded:
                self.db.visual_item(operation['operation_id'], gid, 'searchable')
            else:
                self.db.enqueue_visual_embedding(operation['operation_id'], gid, document, self.config['embedding_model'])
            return True
        if operation['scope'] == 'reembed':
            self.db.visual_item(operation['operation_id'], gid, 'stale', 'source_changed')
            return True
        self.db.enqueue_visual_job(operation['operation_id'], gid, fingerprint, self.namespace, self.config['visual_model'])
        return True

    def _summary(self):
        if not self.config['remote_image_analysis_enabled'] or not self.provider.configured:
            return False
        jobs = self.db.claim(self.config['visual_model'], 1, lease_seconds=600, job_type='visual_summary')
        if not jobs:
            return False
        job = jobs[0]
        op_id, gid = job['payload']['operation_id'], job['gallery_id']
        archive = self.library.storage_dir / f'{gid}.cbz'
        done = threading.Event()
        renewer = threading.Thread(target=lambda: self._heartbeat(jobs, done), daemon=True)
        renewer.start()
        try:
            if source_token(archive) != job['payload']['source_fingerprint']:
                raise ImageSampleError('source_changed')
            reduced = job['payload'].get('reduced', False)
            sampled = sample(archive, edge=max(256, int(self.config['max_remote_image_edge'] * .7)) if reduced else self.config['max_remote_image_edge'],
                             byte_limit=max(65536, int(self.config['max_remote_image_bytes'] * .7)) if reduced else self.config['max_remote_image_bytes'],
                             image_limit=self.config['max_remote_images_per_request'],
                             request_limit=self.config['max_remote_request_bytes'])
            messages = messages_for_sample(sampled, PROMPT)
            if len(json.dumps({'model': self.config['visual_model'], 'messages': messages, 'max_tokens': 800,
                               'temperature': .1, 'stream': False}, ensure_ascii=False, separators=(',', ':')).encode()) > self.config['max_remote_request_bytes']:
                raise ImageSampleError('payload_too_large')
            try:
                response = self.provider.visual_chat(model=self.config['visual_model'], messages=messages, max_tokens=800)
            finally:
                del messages
            try:
                normalized, text, digest = observation(response.text)
            except (ValueError, TypeError):
                # One bounded repair is a separate remote attempt.
                repair = self.provider.chat(model=self.config['visual_model'],
                                            messages=[{'role': 'user', 'content': 'Repair this observation JSON only: ' + response.text[:3000]}],
                                            max_tokens=800, temperature=0.1, purpose='visual')
                normalized, text, digest = observation(repair.text)
            if source_token(archive) != sampled['source_fingerprint'] or not matches_sample(archive, safe_evidence(sampled)):
                raise ImageSampleError('source_changed')
            evidence = safe_evidence(sampled) | {'observation': normalized}
            self.db.commit_visual_summary(job, text=text, content_hash=digest, evidence=evidence,
                                          source_fingerprint=sampled['source_fingerprint'],
                                          namespace=self.namespace, embedding_model=self.config['embedding_model'])
        except (ImageSampleError, ValueError) as exc:
            code = exc.code if isinstance(exc, ImageSampleError) else 'invalid_analysis_json'
            self.db.fail(jobs, code)
            self.db.visual_item(op_id, gid, 'failed', code)
        except ProviderError as exc:
            if exc.status == 413 and not job['payload'].get('reduced'):
                self.db.reduce_visual_payload(job)
                return True
            if exc.code == 'provider_degraded':
                delay = max(1, exc.retry_after)
                self.db.defer_unattempted(jobs, exc.code, delay=delay)
                self.remote_cooldown_until = max(self.remote_cooldown_until, time.monotonic()+delay)
                return True
            code = 'auth_failed' if exc.status in (401, 403) else 'billing_required' if exc.status == 402 else 'model_unavailable' if exc.status == 404 else 'payload_too_large' if exc.status == 413 else 'provider_rejected' if exc.status in (400, 422) else exc.code
            retry = exc.transient and job['attempts'] <= self.config['max_retries_background']
            delay = max(exc.retry_after, min(300, 2 ** job['attempts']) + random.random()) if retry else None
            self.db.fail(jobs, code, delay=delay)
            if delay is not None:
                self.remote_cooldown_until = max(self.remote_cooldown_until, time.monotonic()+delay)
            if exc.status in (401, 402, 403, 404):
                self.db.pause_visual_namespace(self.namespace)
            if not retry:
                self.db.visual_item(op_id, gid, 'failed', code)
        except OSError:
            self.db.fail(jobs, 'source_changed')
            self.db.visual_item(op_id, gid, 'failed', 'source_changed')
        finally:
            done.set()
        return True

    def _embedding(self):
        jobs = self.db.claim(self.config['embedding_model'], 32, lease_seconds=600, job_type='embed_visual')
        if not jobs:
            return False
        texts, valid = [], []
        for job in jobs:
            document = self.db.visual_document(job['gallery_id'], self.namespace)
            archive = self.library.storage_dir / f"{job['gallery_id']}.cbz"
            fresh = False
            if document and document['content_hash'] == job['payload']['content_hash']:
                try:
                    fresh = source_token(archive) == document['source_fingerprint']
                except OSError:
                    pass
            if fresh:
                texts.append(document['text'])
                valid.append(job)
            else:
                self.db.fail([job], 'source_changed')
                self.db.visual_item(job['payload']['operation_id'], job['gallery_id'], 'failed', 'source_changed')
        if not valid:
            return True
        done = threading.Event()
        threading.Thread(target=lambda: self._heartbeat(valid, done), daemon=True).start()
        try:
            vectors = self.provider.embed_texts(model=self.config['embedding_model'], texts=texts,
                                                input_type='passage', purpose='embed_visual')
            current_jobs, current_vectors = [], []
            for job, vector in zip(valid, vectors):
                document = self.db.visual_document(job['gallery_id'], self.namespace)
                archive = self.library.storage_dir / f"{job['gallery_id']}.cbz"
                try:
                    fresh = document and source_token(archive) == document['source_fingerprint'] and matches_sample(archive, json.loads(document['evidence_json']))
                except OSError:
                    fresh = False
                if fresh:
                    current_jobs.append(job); current_vectors.append(vector)
                else:
                    self.db.fail([job], 'source_changed')
                    self.db.visual_item(job['payload']['operation_id'], job['gallery_id'], 'failed', 'source_changed')
            if current_jobs:
                self.db.finish_visual_embedding(current_jobs, current_vectors)
        except ProviderError as exc:
            if exc.code == 'provider_degraded':
                delay = max(1, exc.retry_after)
                self.db.defer_unattempted(valid, exc.code, delay=delay)
                self.remote_cooldown_until = max(self.remote_cooldown_until, time.monotonic()+delay)
                return True
            if exc.status in (401, 402, 403, 404):
                self.db.pause_visual_namespace(self.namespace)
            if exc.status in (400, 413, 422) and len(valid) > 1:
                self.db.fail(valid, 'batch_reduced', delay=0)
            else:
                for job in valid:
                    retry = exc.transient and job['attempts'] <= self.config['max_retries_background']
                    code = 'billing_required' if exc.status == 402 else exc.code
                    delay = max(exc.retry_after, min(300, 2 ** job['attempts']) + random.random()) if retry else None
                    self.db.fail([job], code, delay=delay)
                    if delay is not None:
                        self.remote_cooldown_until = max(self.remote_cooldown_until, time.monotonic()+delay)
                    if not retry:
                        self.db.visual_item(job['payload']['operation_id'], job['gallery_id'], 'failed', code)
        except ValueError:
            self.db.fail(valid, 'invalid_embedding_response')
            for job in valid:
                self.db.visual_item(job['payload']['operation_id'], job['gallery_id'], 'failed', 'invalid_embedding_response')
        finally:
            done.set()
        return True

    def _heartbeat(self, jobs, done):
        while not done.wait(30):
            self.db.renew(jobs, 600)

    def process_once(self):
        if not self.config['remote_image_analysis_enabled'] or not self.provider.configured:
            return False
        operations = self.db.visual_operations(self.namespace)
        for operation in operations:
            if self._scan(operation):
                return True
            gid = self.db.next_visual_item(operation['operation_id'])
            if gid is not None:
                return self._prepare(operation, gid)
            self.db.finish_visual_operation_if_done(operation['operation_id'], operation['scope'] == 'ids' or operation['cursor'] == -1)
        # Flush a bounded batch so a long full scan becomes searchable while
        # analysis continues, without sending one embedding call per gallery.
        if time.monotonic() < self.remote_cooldown_until:
            return False
        if self.summaries_since_embedding >= 16:
            self.summaries_since_embedding = 0
            if self._embedding():
                return True
        if self._summary():
            self.summaries_since_embedding += 1
            return True
        self.summaries_since_embedding = 0
        return self._embedding()

    def _loop(self):
        while not self.stop.is_set():
            if self.process_once():
                self.stop.wait(.05)
            else:
                self.wake.wait(2)
                self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
