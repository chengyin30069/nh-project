"""Optional assistant lifecycle facade used by the existing HTTP server."""
import os
from contextlib import nullcontext
from .requests import RequestQueue
from .schema import validate_request
import sqlite3
import time
from .settings import settings
from .diagnostics import provider_error
from .provider import ProviderError
from .db import AssistantDatabase
from .indexer import AssistantIndexer
from .nim_client import NvidiaNimClient
from .recommender import Recommender
from .vector_index import VectorIndex
from .vector_index import VisualVectorIndex
from .visual_indexer import VisualIndexer
from .visual import namespace as visual_namespace
from .images import source_token


class AssistantService:
    def __init__(self, library, config, env=None, provider=None):
        self.config = settings(config)
        self.library = library
        self.enabled = self.config['enabled']
        self.connection_check = {'state': 'not_checked'}
        self.provider = self.db = self.indexer = self.requests = None
        if not self.enabled:
            return
        path = library.path.parent / 'assistant.sqlite3'
        try:
            self.db = AssistantDatabase(path)
        except sqlite3.DatabaseError as exc:
            if getattr(exc, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise
            # Quarantine only the disposable sidecar, never library.sqlite3.
            for suffix in ('', '-wal', '-shm'):
                source = path.with_name(path.name + suffix)
                if source.exists():
                    source.rename(source.with_name(source.name + f'.corrupt-{time.time_ns()}'))
            self.db = AssistantDatabase(path)
        self.provider = provider or NvidiaNimClient(self.config, os.environ if env is None else env)
        self.vectors = VectorIndex(self.db, self.config['embedding_model'])
        self.visual_vectors = VisualVectorIndex(self.db, self.config['embedding_model'], visual_namespace(self.config['visual_model']))
        self.indexer = AssistantIndexer(library, self.db, self.provider, self.config, self.vectors)
        self.visual_indexer = VisualIndexer(library, self.db, self.provider, self.config)
        self.recommender = Recommender(library, self.provider, self.config, self.vectors, self.visual_vectors)
        self.requests = RequestQueue(self.recommend)
        library.assistant_change_callback = self._gallery_changed
        library.assistant_delete_callback = self.indexer.delete
        if self.db.model('embedding', self.config['embedding_model']):
            self.indexer.scan()
            self.visual_indexer.reembed_existing()
        elif self.db.state('metadata_scan_pending') == '1':
            self.indexer.scan(resume=True)
        for role in ('parser', 'quality'):
            self.db.model(role, self.config[role + '_model'])

    def health(self):
        total = self.library.assistant_count()
        return dict(enabled=self.enabled, available=True, error=getattr(self.provider, "last_error", None),
                    connection_check=self.connection_check, api_key_configured=bool(self.provider and self.provider.configured),
                    provider_state=self.provider.state if self.provider else 'disabled',
                    metadata_index=(self.db.status(self.config['embedding_model']) if self.db else dict(indexed=0, model=self.config['embedding_model'])) | {'total': total},
                    visual_index=self.db.visual_status(self.config['embedding_model'], self.visual_indexer.namespace) | {
                        'total': total, 'enabled': self.config['remote_image_analysis_enabled'],
                        'pilot_approved': self.db.state('visual_pilot_approved') == self.visual_indexer.namespace,
                        'latest_operation_id': self.db.latest_visual_operation_id(self.visual_indexer.namespace)},
                    background_enabled=self.config['background_enabled'],
                    scanning=bool(self.indexer and self.indexer.scan_lock.locked()))

    def check_connection(self):
        if not self.provider or not self.provider.configured:
            self.connection_check = {'state': 'missing_key', 'message': 'API key was not found in the server environment. Set the configured variable and recreate the container.'}
        else:
            try:
                self.provider.embed_texts(model=self.config['embedding_model'], texts=['Connection check.'], input_type='query', purpose='query')
                self.connection_check = {'state': 'verified', 'message': 'NIM accepted the key and the configured embedding model responded.', 'model': self.config['embedding_model']}
            except ProviderError as exc:
                self.connection_check = {'state': 'failed', **provider_error(exc)}
        self.connection_check['checked_at'] = time.time()
        return self.connection_check

    def submit(self, payload):
        return self.requests.submit(validate_request(payload, self.config["result_limit"]))

    def recommend(self, payload, progress=None):
        if not self.enabled:
            raise ValueError('Assistant is disabled in server configuration.')
        budget = getattr(self.provider, 'interactive_budget', nullcontext)
        with budget():
            return self.recommender.recommend(payload, progress=progress)

    def index(self, action, payload=None):
        if not self.enabled:
            raise ValueError('Assistant is disabled in server configuration.')
        payload = {} if payload is None else payload
        if action == 'metadata':
            if payload != {}:
                raise ValueError('Metadata index body must be empty.')
            return {'scanning': self.indexer.scan()}
        if action == 'retry-failed':
            if payload == {} or payload == {'kind': 'metadata'}:
                count = self.db.retry_failed(self.config['embedding_model'])
                self.indexer.wake.set()
            elif payload == {'kind': 'visual', 'namespace': self.visual_indexer.namespace}:
                count = self.db.retry_visual_failed(self.visual_indexer.namespace)
                self.visual_indexer.wake.set()
            else:
                raise ValueError('Invalid retry kind or namespace.')
            return {'queued': count}
        if action.startswith('gallery/'):
            if payload != {}:
                raise ValueError('Gallery index body must be empty.')
            from .schema import gallery_id
            gid = action.removeprefix('gallery/')
            if not gallery_id(gid) or not self.library.assistant_gallery(gid):
                raise ValueError('Downloaded gallery not found.')
            return {'queued': self.indexer.enqueue(gid)}
        raise ValueError('Index action is not available in V1.')

    def visual_index(self, payload):
        return self.visual_indexer.submit(payload)

    def visual_check(self, payload):
        return self.visual_indexer.capability_check(payload)

    def visual_pilot_approve(self, payload):
        return self.visual_indexer.approve_pilot(payload)

    def visual_operation(self, operation_id):
        operation = self.db.operation(operation_id)
        if operation:
            operation['active_namespace'] = self.visual_indexer.namespace
            operation['active_model'] = self.config['visual_model']
            operation['requires_model_refresh'] = operation['namespace'] != self.visual_indexer.namespace
        return operation

    def visual_control(self, operation_id, action):
        if action == 'resume' and not self.config['remote_image_analysis_enabled']:
            raise ValueError('Remote image analysis is disabled in configuration.')
        operation = self.db.operation(operation_id)
        if action == 'resume' and operation and operation['namespace'] != self.visual_indexer.namespace:
            self.db.rebase_visual_operation(operation_id, self.visual_indexer.namespace)
        else:
            self.db.control_operation(operation_id, action)
        self.visual_indexer.wake.set()
        return self.visual_operation(operation_id)

    def _gallery_changed(self, gallery_id):
        result = self.indexer.enqueue(gallery_id)
        try:
            self.db.invalidate_visual(gallery_id, source_token(self.library.storage_dir / f'{gallery_id}.cbz'))
        except OSError:
            self.db.delete_gallery(gallery_id)
        # An opt-in on-new policy creates only this gallery's explicit operation.
        if self.config['visual_index_on_new_gallery'] and self.config['remote_image_analysis_enabled'] and self.provider.configured:
            self.visual_indexer.submit({'scope': 'ids', 'gallery_ids': [str(gallery_id)]})
        return result

    def close(self):
        if self.requests:
            self.requests.close()
        self.library.assistant_change_callback = None
        self.library.assistant_delete_callback = None
        if self.indexer:
            self.indexer.close()
        if getattr(self, 'visual_indexer', None):
            self.visual_indexer.close()
        if self.provider:
            self.provider.close()
